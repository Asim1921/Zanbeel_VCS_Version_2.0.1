"""Login rate limiting and account lockout.

The gap this closes: ``/api/auth/login`` had no attempt counter, no delay and no
lockout. PBKDF2 at 200,000 iterations makes each guess expensive, but "expensive
per guess" is not a defence when the number of guesses is unbounded — it only
sets the rate.

Two limits, because they stop different attacks:

* **Per account.** Repeated failures against one username lock that username for
  a cooling-off period. This is the targeted-guessing case.
* **Per source address.** Many failures from one address throttle that address
  regardless of which usernames it tried. This is the spraying case — one guess
  each against a hundred accounts never trips a per-account limit.

**Attempts are recorded against the submitted username whether or not it
exists.** If only real accounts could be locked, the lockout response would
itself confirm which usernames are real, turning a defence into an enumeration
oracle. A nonsense username hammered enough locks exactly like a real one.

The response for a lockout is deliberately the same shape for every case, and
carries ``Retry-After`` so an honest client can wait rather than hammer.
"""

import os
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from database.models import AccountLock, LoginAttempt


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# Tuned so an honest person fat-fingering a password is never locked out, while
# an automated attack is throttled to a few guesses a minute.
MAX_ACCOUNT_FAILURES = _int_env("FOXNEST_LOGIN_MAX_FAILURES", 5)
ACCOUNT_WINDOW_MINUTES = _int_env("FOXNEST_LOGIN_WINDOW_MINUTES", 15)
LOCKOUT_MINUTES = _int_env("FOXNEST_LOGIN_LOCKOUT_MINUTES", 15)

MAX_IP_FAILURES = _int_env("FOXNEST_LOGIN_MAX_IP_FAILURES", 20)
IP_WINDOW_MINUTES = _int_env("FOXNEST_LOGIN_IP_WINDOW_MINUTES", 15)

# Attempts older than this are pruned. Kept longer than the window so an
# administrator can still see the shape of a recent attack.
RETENTION_HOURS = _int_env("FOXNEST_LOGIN_RETENTION_HOURS", 24)


class RateLimited(Exception):
    """Raised when a caller must back off. Carries the seconds to wait."""

    def __init__(self, retry_after_seconds: int, message: str):
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        self.message = message


def client_ip(request) -> Optional[str]:
    """Best-effort source address.

    ``X-Forwarded-For`` is honoured only when the deployment says it is behind a
    proxy. Trusting it unconditionally would let any caller spoof the header and
    walk straight through the per-address limit.
    """
    if not request:
        return None
    if os.getenv("FOXNEST_TRUST_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    client = getattr(request, "client", None)
    return (getattr(client, "host", None) or None)


def _normalise(username: Optional[str]) -> str:
    """Lower-cased so 'Alice' and 'alice' share one counter."""
    return (username or "").strip().lower()[:150]


def prune(db: Session) -> int:
    """Drop attempts outside the retention window and expired locks."""
    removed = 0
    cutoff = datetime.utcnow() - timedelta(hours=RETENTION_HOURS)
    try:
        removed = (
            db.query(LoginAttempt).filter(LoginAttempt.created_at < cutoff).delete()
        )
        db.query(AccountLock).filter(AccountLock.locked_until < datetime.utcnow()).delete()
        db.commit()
    except Exception:
        db.rollback()
    return removed or 0


def active_lock(db: Session, username: str) -> Optional[AccountLock]:
    """The live lock for a username, or None. Expired locks are cleared."""
    key = _normalise(username)
    if not key:
        return None
    lock = db.query(AccountLock).filter(AccountLock.username == key).first()
    if not lock:
        return None
    if lock.locked_until <= datetime.utcnow():
        try:
            db.delete(lock)
            db.commit()
        except Exception:
            db.rollback()
        return None
    return lock


def _recent_failures(db: Session, *, username: Optional[str] = None,
                     ip: Optional[str] = None, minutes: int = 15) -> int:
    since = datetime.utcnow() - timedelta(minutes=minutes)
    query = db.query(func.count(LoginAttempt.id)).filter(
        LoginAttempt.success.is_(False), LoginAttempt.created_at >= since
    )
    if username is not None:
        query = query.filter(LoginAttempt.username == _normalise(username))
    if ip is not None:
        query = query.filter(LoginAttempt.ip_address == ip)
    return int(query.scalar() or 0)


def check(db: Session, username: str, ip: Optional[str]) -> None:
    """Refuse the attempt if the account or the source address is over limit.

    Called *before* the password is verified, so a locked account costs no
    PBKDF2 work — which matters, since that work is what an attacker is trying
    to make the server do.
    """
    lock = active_lock(db, username)
    if lock:
        wait = (lock.locked_until - datetime.utcnow()).total_seconds()
        raise RateLimited(
            wait,
            "Too many failed sign-in attempts. Try again later.",
        )

    if ip:
        ip_failures = _recent_failures(db, ip=ip, minutes=IP_WINDOW_MINUTES)
        if ip_failures >= MAX_IP_FAILURES:
            raise RateLimited(
                IP_WINDOW_MINUTES * 60,
                "Too many failed sign-in attempts from this address. Try again later.",
            )


def record_failure(db: Session, username: str, ip: Optional[str],
                   user_agent: Optional[str] = None) -> Optional[AccountLock]:
    """Log a failed attempt and lock the account if it has crossed the limit."""
    key = _normalise(username)
    try:
        db.add(LoginAttempt(username=key, ip_address=ip, success=False,
                            user_agent=(user_agent or "")[:300] or None))
        db.commit()
    except Exception:
        db.rollback()
        return None

    failures = _recent_failures(db, username=key, minutes=ACCOUNT_WINDOW_MINUTES)
    if failures < MAX_ACCOUNT_FAILURES:
        return None

    until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
    try:
        lock = db.query(AccountLock).filter(AccountLock.username == key).first()
        if lock:
            lock.locked_until = until
            lock.failed_count = failures
            lock.last_ip = ip
        else:
            lock = AccountLock(
                username=key,
                locked_until=until,
                failed_count=failures,
                reason=f"{failures} failed attempts in {ACCOUNT_WINDOW_MINUTES} minutes",
                last_ip=ip,
            )
            db.add(lock)
        db.commit()
        db.refresh(lock)
        return lock
    except Exception:
        db.rollback()
        return None


def record_success(db: Session, username: str, ip: Optional[str],
                   user_agent: Optional[str] = None) -> None:
    """Log a success and clear the account's failure history.

    Clearing on success is what keeps a legitimate user who mistypes twice a
    week from slowly accumulating their way into a lockout.
    """
    key = _normalise(username)
    try:
        db.add(LoginAttempt(username=key, ip_address=ip, success=True,
                            user_agent=(user_agent or "")[:300] or None))
        db.query(LoginAttempt).filter(
            LoginAttempt.username == key, LoginAttempt.success.is_(False)
        ).delete()
        db.query(AccountLock).filter(AccountLock.username == key).delete()
        db.commit()
    except Exception:
        db.rollback()


def unlock(db: Session, username: str) -> bool:
    """Administratively clear a lock. Returns whether one was present."""
    key = _normalise(username)
    try:
        removed = db.query(AccountLock).filter(AccountLock.username == key).delete()
        db.query(LoginAttempt).filter(
            LoginAttempt.username == key, LoginAttempt.success.is_(False)
        ).delete()
        db.commit()
        return bool(removed)
    except Exception:
        db.rollback()
        return False


def status(db: Session, limit: int = 50) -> dict:
    """What administrators need to see: who is locked, and what is being tried."""
    now = datetime.utcnow()
    locks = (
        db.query(AccountLock)
        .filter(AccountLock.locked_until > now)
        .order_by(AccountLock.locked_until.desc())
        .limit(limit)
        .all()
    )

    since = now - timedelta(minutes=ACCOUNT_WINDOW_MINUTES)
    top_usernames = (
        db.query(LoginAttempt.username, func.count(LoginAttempt.id).label("n"))
        .filter(LoginAttempt.success.is_(False), LoginAttempt.created_at >= since)
        .group_by(LoginAttempt.username)
        .order_by(func.count(LoginAttempt.id).desc())
        .limit(limit)
        .all()
    )
    top_ips = (
        db.query(LoginAttempt.ip_address, func.count(LoginAttempt.id).label("n"))
        .filter(
            LoginAttempt.success.is_(False),
            LoginAttempt.created_at >= since,
            LoginAttempt.ip_address.isnot(None),
        )
        .group_by(LoginAttempt.ip_address)
        .order_by(func.count(LoginAttempt.id).desc())
        .limit(limit)
        .all()
    )

    return {
        "policy": {
            "max_account_failures": MAX_ACCOUNT_FAILURES,
            "account_window_minutes": ACCOUNT_WINDOW_MINUTES,
            "lockout_minutes": LOCKOUT_MINUTES,
            "max_ip_failures": MAX_IP_FAILURES,
            "ip_window_minutes": IP_WINDOW_MINUTES,
        },
        "locked_accounts": [
            {
                "username": lock.username,
                "locked_until": lock.locked_until.isoformat(),
                "seconds_remaining": int((lock.locked_until - now).total_seconds()),
                "failed_count": lock.failed_count,
                "reason": lock.reason,
                "last_ip": lock.last_ip,
            }
            for lock in locks
        ],
        "recent_failures_by_username": [
            {"username": name, "failures": count} for name, count in top_usernames
        ],
        "recent_failures_by_ip": [
            {"ip_address": ip, "failures": count} for ip, count in top_ips
        ],
        "window_minutes": ACCOUNT_WINDOW_MINUTES,
    }
