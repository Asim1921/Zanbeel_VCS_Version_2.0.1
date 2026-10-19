"""Sign-in rate limiting and account lockout.

The gap this closes: ``/api/auth/login`` had no attempt counter, no delay and no
lockout. PBKDF2 at 200,000 iterations makes each guess expensive, but "expensive per
guess" is not a defence when the number of guesses is unbounded -- it only sets the
rate. A first version (2.0.1) added counters, but checked them *before* recording the
attempt, so a burst of concurrent guesses all passed the check together.

Four limits, because they stop different attacks:

* **Per account, per address** (``FOXNEST_LOGIN_MAX_FAILURES``, default 5 in 15
  minutes). Targeted guessing from one machine is refused for that account, without
  locking the real owner out from theirs.
* **Per account, from anywhere** (``FOXNEST_LOGIN_MAX_ACCOUNT_FAILURES``, default 20).
  Guessing spread across many machines locks the account itself. Each lock is twice
  as long as the one before (15 minutes, 30, an hour ... up to a day), so a patient
  attacker gets a few dozen guesses a day rather than thousands.
* **Per address, any account** (``FOXNEST_LOGIN_MAX_IP_FAILURES``, default 20). One
  guess each against a hundred accounts never trips a per-account limit; this does.
* **Password-reset codes** count per address too, and per account across codes, so
  requesting a new code every minute does not reopen the six-digit keyspace.

**An attempt is recorded before the password is checked**, then the limits are counted
again with it included. Concurrent attempts that all passed the first check are each
counted by the second, and any over the limit is withdrawn and refused -- so at most
the limit of them ever reach the password check.

**Attempts are recorded against the submitted username whether or not it exists.**
If only real accounts could be locked, the lockout response would itself confirm which
usernames are real. A nonsense username hammered enough locks exactly like a real one,
and the login route spends the same PBKDF2 work on both.

Every refusal carries ``Retry-After`` with the actual wait, so an honest client can
wait rather than hammer.
"""

import os
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from database.models import AccountLock, LoginAttempt


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


KIND_PASSWORD = "password"
KIND_OTP = "otp"
KIND_RESET_REQUEST = "reset_request"

# Tuned so an honest person fat-fingering a password is never locked out, while an
# automated attack is throttled to a few guesses a minute and then locked.
MAX_PAIR_FAILURES = _int_env("FOXNEST_LOGIN_MAX_FAILURES", 5)
WINDOW_MINUTES = _int_env("FOXNEST_LOGIN_WINDOW_MINUTES", 15)
MAX_ACCOUNT_FAILURES = _int_env("FOXNEST_LOGIN_MAX_ACCOUNT_FAILURES", 20)
LOCKOUT_MINUTES = _int_env("FOXNEST_LOGIN_LOCKOUT_MINUTES", 15)
MAX_LOCKOUT_MINUTES = _int_env("FOXNEST_LOGIN_MAX_LOCKOUT_MINUTES", 24 * 60)

MAX_IP_FAILURES = _int_env("FOXNEST_LOGIN_MAX_IP_FAILURES", 20)
IP_WINDOW_MINUTES = _int_env("FOXNEST_LOGIN_IP_WINDOW_MINUTES", 15)

# Wrong reset codes per account, across however many codes were issued, per hour.
MAX_OTP_FAILURES = _int_env("FOXNEST_OTP_MAX_ACCOUNT_FAILURES", 10)
OTP_WINDOW_MINUTES = 60
# Reset codes requested from one address in the address window.
MAX_RESET_REQUESTS = _int_env("FOXNEST_RESET_MAX_REQUESTS_PER_IP", 10)

# Attempts older than this are pruned. Longer than every window, so an administrator
# can still see the shape of a recent attack.
RETENTION_HOURS = _int_env("FOXNEST_LOGIN_RETENTION_HOURS", 24)
# A lock this long after the previous one expired starts again at LOCKOUT_MINUTES.
ESCALATION_RESET_HOURS = 24

_PRUNE_EVERY_SECONDS = 600
_last_prune = 0.0


class RateLimited(Exception):
    """Raised when a caller must back off. Carries the seconds to wait."""

    def __init__(self, retry_after_seconds: float, message: str, scope: str = "account"):
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds + 0.999))
        self.scope = scope
        self.message = f"{message} Try again in {_human_wait(self.retry_after_seconds)}."


def _human_wait(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = (seconds + 59) // 60
    if minutes < 120:
        return f"{minutes} minutes"
    hours = (minutes + 59) // 60
    return f"{hours} hours"


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


def _now() -> datetime:
    return datetime.utcnow()


# --- housekeeping ---------------------------------------------------------------------

def prune(db: Session) -> int:
    """Drop attempts outside the retention window, and locks too old to escalate from."""
    global _last_prune
    _last_prune = time.monotonic()
    removed = 0
    now = _now()
    try:
        removed = (
            db.query(LoginAttempt)
            .filter(LoginAttempt.created_at < now - timedelta(hours=RETENTION_HOURS))
            .delete(synchronize_session=False)
        )
        db.query(AccountLock).filter(
            AccountLock.locked_until < now - timedelta(hours=ESCALATION_RESET_HOURS)
        ).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
    return removed or 0


def _maybe_prune(db: Session) -> None:
    """Prune from the sign-in path itself, at most every ten minutes.

    It used to run only when an administrator opened the lockout view, so on a server
    nobody watched the table grew with every sign-in forever.
    """
    if time.monotonic() - _last_prune >= _PRUNE_EVERY_SECONDS:
        prune(db)


# --- counting -------------------------------------------------------------------------

def _failures(db: Session, *, kind: Optional[str], minutes: int,
              username: Optional[str] = None, ip: Optional[str] = None):
    since = _now() - timedelta(minutes=minutes)
    query = db.query(LoginAttempt).filter(
        LoginAttempt.success.is_(False), LoginAttempt.created_at >= since
    )
    if kind is not None:
        query = query.filter(LoginAttempt.kind == kind)
    if username is not None:
        query = query.filter(LoginAttempt.username == _normalise(username))
    if ip is not None:
        query = query.filter(LoginAttempt.ip_address == ip)
    return query


def _count(query) -> int:
    return int(query.with_entities(func.count(LoginAttempt.id)).scalar() or 0)


def _wait_until_under(query, limit: int, minutes: int) -> float:
    """Seconds until enough of the counted attempts age out to be under ``limit``."""
    count = _count(query)
    excess = count - limit + 1
    if excess <= 0:
        return 0.0
    oldest = (query.order_by(LoginAttempt.created_at.asc(), LoginAttempt.id.asc())
              .offset(excess - 1).limit(1).first())
    if not oldest or not oldest.created_at:
        return minutes * 60.0
    expires = oldest.created_at + timedelta(minutes=minutes)
    return max(1.0, (expires - _now()).total_seconds())


def active_lock(db: Session, username: str) -> Optional[AccountLock]:
    """The live lock for a username, or None. Expired rows are kept for escalation."""
    key = _normalise(username)
    if not key:
        return None
    lock = db.query(AccountLock).filter(AccountLock.username == key).first()
    if not lock or lock.locked_until <= _now():
        return None
    return lock


def _limits(db: Session, key: str, ip: Optional[str], kind: str, include_equal: bool) -> None:
    """Raise RateLimited if any limit for this attempt is reached.

    ``include_equal``: before recording, reaching the limit refuses; after recording
    (the attempt itself counted) only exceeding it does.
    """
    over = (lambda n, limit: n >= limit) if include_equal else (lambda n, limit: n > limit)

    if kind == KIND_PASSWORD:
        pair = _failures(db, kind=KIND_PASSWORD, minutes=WINDOW_MINUTES, username=key, ip=ip)
        if ip and over(_count(pair), MAX_PAIR_FAILURES):
            raise RateLimited(
                _wait_until_under(pair, MAX_PAIR_FAILURES if include_equal else MAX_PAIR_FAILURES + 1,
                                  WINDOW_MINUTES),
                "Too many failed sign-in attempts for this account from your address.",
                scope="pair",
            )
        account = _failures(db, kind=KIND_PASSWORD, minutes=WINDOW_MINUTES, username=key)
        if over(_count(account), MAX_ACCOUNT_FAILURES):
            raise RateLimited(
                _wait_until_under(account, MAX_ACCOUNT_FAILURES if include_equal
                                  else MAX_ACCOUNT_FAILURES + 1, WINDOW_MINUTES),
                "Too many failed sign-in attempts for this account.",
                scope="account",
            )
    elif kind == KIND_OTP:
        account = _failures(db, kind=KIND_OTP, minutes=OTP_WINDOW_MINUTES, username=key)
        if over(_count(account), MAX_OTP_FAILURES):
            raise RateLimited(
                _wait_until_under(account, MAX_OTP_FAILURES if include_equal else MAX_OTP_FAILURES + 1,
                                  OTP_WINDOW_MINUTES),
                "Too many wrong reset codes for this account.",
                scope="otp",
            )
    elif kind == KIND_RESET_REQUEST and ip:
        requests = _failures(db, kind=KIND_RESET_REQUEST, minutes=IP_WINDOW_MINUTES, ip=ip)
        if over(_count(requests), MAX_RESET_REQUESTS):
            raise RateLimited(
                _wait_until_under(requests, MAX_RESET_REQUESTS if include_equal
                                  else MAX_RESET_REQUESTS + 1, IP_WINDOW_MINUTES),
                "Too many password-reset requests from your address.",
                scope="reset_request",
            )

    if ip:
        address = _failures(db, kind=None, minutes=IP_WINDOW_MINUTES, ip=ip)
        if over(_count(address), MAX_IP_FAILURES):
            raise RateLimited(
                _wait_until_under(address, MAX_IP_FAILURES if include_equal else MAX_IP_FAILURES + 1,
                                  IP_WINDOW_MINUTES),
                "Too many failed attempts from your address.",
                scope="address",
            )


def check(db: Session, username: str, ip: Optional[str], kind: str = KIND_PASSWORD) -> None:
    """Refuse if the account is locked or any limit is already reached. Records nothing."""
    key = _normalise(username)
    if kind == KIND_PASSWORD:
        lock = active_lock(db, key)
        if lock:
            raise RateLimited(
                (lock.locked_until - _now()).total_seconds(),
                "This account is temporarily locked after repeated failed sign-in attempts.",
                scope="locked",
            )
    _limits(db, key, ip, kind, include_equal=True)


# --- the attempt lifecycle ------------------------------------------------------------

def begin_attempt(db: Session, username: str, ip: Optional[str],
                  user_agent: Optional[str] = None, kind: str = KIND_PASSWORD) -> int:
    """Admit one attempt, or raise RateLimited. Returns the recorded attempt's id.

    Called *before* the credential is verified, so a refused attempt costs no PBKDF2
    work -- that work is exactly what an attacker wants to spend. The attempt is written
    as a failure; ``succeed`` turns it into a success if the credential verifies.
    """
    _maybe_prune(db)
    check(db, username, ip, kind)

    attempt = LoginAttempt(username=_normalise(username), ip_address=ip, success=False,
                           kind=kind, user_agent=(user_agent or "")[:300] or None)
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    # Count again with this attempt included. Concurrent attempts that all passed
    # check() above are each counted here; any over the limit withdraws itself.
    try:
        _limits(db, _normalise(username), ip, kind, include_equal=False)
    except RateLimited:
        try:
            db.delete(attempt)
            db.commit()
        except Exception:
            db.rollback()
        raise
    return attempt.id


def succeed(db: Session, attempt_id: int, username: str) -> None:
    """The credential verified: mark the attempt, clear the account's failure history.

    Clearing is what keeps a legitimate user who mistypes twice a week from slowly
    accumulating their way into a lockout. A success also ends lock escalation.
    """
    key = _normalise(username)
    try:
        kind = (db.query(LoginAttempt.kind).filter(LoginAttempt.id == attempt_id).scalar()
                or KIND_PASSWORD)
        # Both as SQL statements, in this order: with autoflush off (as the app's sessions
        # are), setting the attempt's flag on the object would still be pending when the
        # delete ran, the delete would take the attempt too, and the commit would fail.
        db.query(LoginAttempt).filter(LoginAttempt.id == attempt_id).update(
            {LoginAttempt.success: True}, synchronize_session=False)
        db.query(LoginAttempt).filter(
            LoginAttempt.username == key,
            LoginAttempt.kind == kind,
            LoginAttempt.success.is_(False),
            LoginAttempt.id != attempt_id,
        ).delete(synchronize_session=False)
        if kind == KIND_PASSWORD:
            db.query(AccountLock).filter(AccountLock.username == key).delete(
                synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()


def fail(db: Session, attempt_id: int, username: str, ip: Optional[str],
         user=None) -> Optional[AccountLock]:
    """The credential did not verify. The attempt is already a failure on record; lock
    the account if it has now crossed the per-account limit."""
    key = _normalise(username)
    attempt = db.query(LoginAttempt).filter(LoginAttempt.id == attempt_id).first()
    if not attempt or attempt.kind != KIND_PASSWORD:
        return None

    failures = _count(_failures(db, kind=KIND_PASSWORD, minutes=WINDOW_MINUTES, username=key))
    if failures < MAX_ACCOUNT_FAILURES:
        return None
    return _lock(db, key, failures, ip, user)


def _lock(db: Session, key: str, failures: int, ip: Optional[str], user) -> Optional[AccountLock]:
    now = _now()
    try:
        lock = db.query(AccountLock).filter(AccountLock.username == key).first()
        if lock and lock.locked_until > now:
            return lock  # Already locked; a concurrent failure got here first.
        recent = lock is not None and lock.locked_until > now - timedelta(hours=ESCALATION_RESET_HOURS)
        count = ((lock.lock_count or 1) + 1) if recent else 1
        minutes = min(LOCKOUT_MINUTES * (2 ** (count - 1)), MAX_LOCKOUT_MINUTES)
        reason = (f"{failures} failed sign-in attempts in {WINDOW_MINUTES} minutes"
                  + (f" (lock {count} in a row)" if count > 1 else ""))
        if lock is None:
            lock = AccountLock(username=key)
            db.add(lock)
        lock.locked_until = now + timedelta(minutes=minutes)
        lock.failed_count = failures
        lock.lock_count = count
        lock.reason = reason
        lock.last_ip = ip
        db.commit()
        db.refresh(lock)
    except Exception:
        db.rollback()
        return None

    _announce_lock(db, key, lock, minutes, user)
    return lock


def _announce_lock(db: Session, key: str, lock: AccountLock, minutes: int, user) -> None:
    """Tell the account holder and the activity log -- when the account exists.

    A lock on a name with no account is visible in the lockout view; the activity log
    records actions against accounts, so there is nothing to attribute it to. Best
    effort: failing to notify must not turn a refusal into a server error.
    """
    if user is None or not getattr(user, "id", None):
        return
    from app.services import audit

    audit.record(
        db, user.id, "account_locked",
        description=f"Sign-in locked for {minutes} minutes: {lock.reason}",
        target_type="user", target_id=key,
        payload={"username": key, "minutes": minutes, "lock_count": lock.lock_count,
                 "failures": lock.failed_count, "last_ip": lock.last_ip,
                 "locked_until": lock.locked_until.isoformat()},
    )
    try:
        from database.crud import NotificationCRUD

        NotificationCRUD.create_notification(
            db, user_id=user.id,
            notification_type="account_locked",
            title="Sign-in to your account was locked",
            body=(
                f"After {lock.failed_count} failed sign-in attempts"
                + (f" (the last from {lock.last_ip})" if lock.last_ip else "")
                + f", password sign-in to your account is locked for {minutes} minutes. "
                "If this was not you, someone may be guessing your password: once the lock "
                "lifts, change it. A password reset by email unlocks the account at once."
            ),
            payload={"minutes": minutes, "locked_until": lock.locked_until.isoformat(),
                     "target": {"kind": "profile"}},
        )
    except Exception:
        db.rollback()


# --- administration -------------------------------------------------------------------

def unlock(db: Session, username: Optional[str] = None, ip: Optional[str] = None) -> dict:
    """Administratively clear a lock and the failures behind it (by account, address or both).

    Also clears the failures, so the account does not re-lock on the next mistake, and
    the escalation, so the next lock (if any) starts short again.
    """
    key = _normalise(username) if username else None
    result = {"was_locked": False, "cleared_failures": 0}
    try:
        if key:
            lock = db.query(AccountLock).filter(AccountLock.username == key).first()
            result["was_locked"] = bool(lock and lock.locked_until > _now())
            db.query(AccountLock).filter(AccountLock.username == key).delete(
                synchronize_session=False)
            result["cleared_failures"] += (
                db.query(LoginAttempt)
                .filter(LoginAttempt.username == key, LoginAttempt.success.is_(False))
                .delete(synchronize_session=False)
            )
        if ip:
            result["cleared_failures"] += (
                db.query(LoginAttempt)
                .filter(LoginAttempt.ip_address == ip, LoginAttempt.success.is_(False))
                .delete(synchronize_session=False)
            )
        db.commit()
    except Exception:
        db.rollback()
    return result


def status(db: Session, limit: int = 50) -> dict:
    """What administrators need to see: who is locked, what is throttled, what is tried."""
    now = _now()
    locks = (
        db.query(AccountLock)
        .filter(AccountLock.locked_until > now)
        .order_by(AccountLock.locked_until.desc())
        .limit(limit)
        .all()
    )

    since = now - timedelta(minutes=WINDOW_MINUTES)
    by_username = (
        db.query(LoginAttempt.username, func.count(LoginAttempt.id).label("n"))
        .filter(LoginAttempt.success.is_(False), LoginAttempt.kind == KIND_PASSWORD,
                LoginAttempt.created_at >= since)
        .group_by(LoginAttempt.username)
        .order_by(func.count(LoginAttempt.id).desc())
        .limit(limit)
        .all()
    )
    ip_since = now - timedelta(minutes=IP_WINDOW_MINUTES)
    by_ip = (
        db.query(LoginAttempt.ip_address, func.count(LoginAttempt.id).label("n"))
        .filter(
            LoginAttempt.success.is_(False),
            LoginAttempt.created_at >= ip_since,
            LoginAttempt.ip_address.isnot(None),
        )
        .group_by(LoginAttempt.ip_address)
        .order_by(func.count(LoginAttempt.id).desc())
        .limit(limit)
        .all()
    )
    pairs = (
        db.query(LoginAttempt.username, LoginAttempt.ip_address,
                 func.count(LoginAttempt.id).label("n"))
        .filter(LoginAttempt.success.is_(False), LoginAttempt.kind == KIND_PASSWORD,
                LoginAttempt.created_at >= since, LoginAttempt.ip_address.isnot(None))
        .group_by(LoginAttempt.username, LoginAttempt.ip_address)
        .having(func.count(LoginAttempt.id) >= MAX_PAIR_FAILURES)
        .order_by(func.count(LoginAttempt.id).desc())
        .limit(limit)
        .all()
    )

    return {
        "policy": {
            "max_failures_per_address": MAX_PAIR_FAILURES,
            "max_account_failures": MAX_ACCOUNT_FAILURES,
            "account_window_minutes": WINDOW_MINUTES,
            "lockout_minutes": LOCKOUT_MINUTES,
            "max_lockout_minutes": MAX_LOCKOUT_MINUTES,
            "max_ip_failures": MAX_IP_FAILURES,
            "ip_window_minutes": IP_WINDOW_MINUTES,
            "max_reset_code_failures": MAX_OTP_FAILURES,
        },
        "locked_accounts": [
            {
                "username": lock.username,
                "locked_until": lock.locked_until.isoformat(),
                "seconds_remaining": int((lock.locked_until - now).total_seconds()),
                "failed_count": lock.failed_count,
                "lock_count": lock.lock_count or 1,
                "reason": lock.reason,
                "last_ip": lock.last_ip,
            }
            for lock in locks
        ],
        "throttled_pairs": [
            {"username": name, "ip_address": ip, "failures": count}
            for name, ip, count in pairs
        ],
        "throttled_addresses": [
            {"ip_address": ip, "failures": count} for ip, count in by_ip if count >= MAX_IP_FAILURES
        ],
        "recent_failures_by_username": [
            {"username": name, "failures": count} for name, count in by_username
        ],
        "recent_failures_by_ip": [
            {"ip_address": ip, "failures": count} for ip, count in by_ip
        ],
        "window_minutes": WINDOW_MINUTES,
    }
