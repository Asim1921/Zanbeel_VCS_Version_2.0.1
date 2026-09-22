"""Password reset by emailed one-time code.

A six-digit code is only 10^6 possibilities, so the code on its own is not the security
boundary. What makes this safe is the combination of a short lifetime, a hard cap on
guesses, single use, and the fact that only a hash of the code is ever stored -- reading
`password_reset_otps` does not let anyone complete a reset.

Requesting a new code invalidates any earlier one for that account, so a code that was
emailed and then superseded cannot still be used.
"""

import hashlib
import hmac
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import (
    AUTH_SECRET,
    OTP_MAX_ATTEMPTS,
    OTP_TTL_SECONDS,
    SMTP_FROM,
    SMTP_FROM_NAME,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_USE_TLS,
)
from database.models import PasswordResetOTP, User


class PasswordResetError(Exception):
    """Raised with a message and status suitable for returning to the caller."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# Self-service resets are unauthenticated, so a request costs an attacker nothing.
# The cooldown stops the endpoint being used to flood somebody's inbox.
RESEND_COOLDOWN_SECONDS = 60


def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def find_user_by_email(db: Session, email: str) -> Optional[User]:
    """The single active account for this address, or None.

    Email is not unique in the schema. If two accounts share an address there is no
    safe way to choose between them, so nothing is sent rather than risk resetting
    the wrong account.
    """
    address = (email or "").strip().lower()
    if not address or "@" not in address:
        return None
    matches = (
        db.query(User)
        .filter(func.lower(User.email) == address, User.is_active.is_(True))
        .all()
    )
    return matches[0] if len(matches) == 1 else None


def seconds_until_resend_allowed(db: Session, user: User) -> int:
    """How long the caller must wait before another code may be sent to this account."""
    latest = (
        db.query(PasswordResetOTP)
        .filter(PasswordResetOTP.user_id == user.id)
        .order_by(PasswordResetOTP.created_at.desc(), PasswordResetOTP.id.desc())
        .first()
    )
    if not latest or not latest.created_at:
        return 0
    elapsed = (datetime.utcnow() - latest.created_at).total_seconds()
    return max(0, int(RESEND_COOLDOWN_SECONDS - elapsed))


def _hash_code(code: str, username: str) -> str:
    """Keyed hash, so codes cannot be recovered from the table with a rainbow table.

    The username is mixed in so an identical code issued for two accounts does not
    produce the same stored value.
    """
    return hmac.new(
        AUTH_SECRET.encode("utf-8"),
        f"{username}:{code}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _generate_code() -> str:
    """A six-digit code from a cryptographic source, leading zeros preserved."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _send_email(to_address: str, username: str, code: str, minutes: int) -> None:
    message = EmailMessage()
    message["Subject"] = "Your Zanbeel password reset code"
    message["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM}>"
    message["To"] = to_address
    message.set_content(
        f"Hello {username},\n\n"
        f"Your Zanbeel password reset code is:\n\n"
        f"    {code}\n\n"
        f"It expires in {minutes} minutes and can be used once.\n\n"
        f"If you did not request this, you can ignore this email — your password has "
        f"not been changed.\n\n"
        f"— Zanbeel Version Control\n"
    )

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT,
                                  context=ssl.create_default_context(), timeout=30) as server:
                if SMTP_USERNAME:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(message)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                if SMTP_USE_TLS:
                    server.starttls(context=ssl.create_default_context())
                if SMTP_USERNAME:
                    server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise PasswordResetError(
            "The mail server rejected the configured credentials. Check SMTP_USERNAME "
            "and SMTP_PASSWORD on the server.", 502
        ) from exc
    except Exception as exc:
        raise PasswordResetError(f"Could not send the reset email: {exc}", 502) from exc


def request_code(
    db: Session,
    user: User,
    requested_by: Optional[User] = None,
) -> Tuple[PasswordResetOTP, str]:
    """Issue a code for `user`, email it, and return the record and masked address."""
    if not smtp_configured():
        raise PasswordResetError(
            "Email delivery is not configured on this server, so a reset code cannot be "
            "sent. Set SMTP_HOST and SMTP_FROM in the server environment.", 503
        )

    address = (user.email or "").strip()
    if not address:
        raise PasswordResetError(
            f"'{user.username}' has no email address on file, so there is nowhere to "
            f"send a code.", 400
        )

    # Supersede any outstanding code, so only the newest one can be used.
    (
        db.query(PasswordResetOTP)
        .filter(PasswordResetOTP.user_id == user.id, PasswordResetOTP.consumed_at.is_(None))
        .update({PasswordResetOTP.consumed_at: datetime.utcnow()}, synchronize_session=False)
    )

    code = _generate_code()
    record = PasswordResetOTP(
        user_id=user.id,
        username=user.username,
        email=address,
        code_hash=_hash_code(code, user.username),
        expires_at=datetime.utcnow() + timedelta(seconds=OTP_TTL_SECONDS),
        attempts=0,
        requested_by_id=requested_by.id if requested_by else None,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    # Send only after the row is committed: an email the server has no record of would
    # leave the recipient with a code that can never be redeemed.
    try:
        _send_email(address, user.username, code, max(1, OTP_TTL_SECONDS // 60))
    except PasswordResetError:
        record.consumed_at = datetime.utcnow()
        db.commit()
        raise

    return record, mask_email(address)


def mask_email(address: str) -> str:
    """b****@example.com -- enough to confirm the right inbox without exposing it."""
    local, _, domain = address.partition("@")
    if not domain:
        return "****"
    visible = local[:1] if local else ""
    return f"{visible}{'*' * max(3, len(local) - 1)}@{domain}"


def verify_and_consume(db: Session, user: User, code: str) -> PasswordResetOTP:
    """Check a submitted code and mark it used. Raises PasswordResetError otherwise."""
    submitted = (code or "").strip()
    if not submitted:
        raise PasswordResetError("Enter the 6-digit code that was emailed.", 400)

    record = (
        db.query(PasswordResetOTP)
        .filter(
            PasswordResetOTP.user_id == user.id,
            PasswordResetOTP.consumed_at.is_(None),
        )
        .order_by(PasswordResetOTP.created_at.desc(), PasswordResetOTP.id.desc())
        .first()
    )
    if not record:
        raise PasswordResetError(
            "No reset code is outstanding for this user. Request a new one.", 400
        )

    if record.expires_at and record.expires_at < datetime.utcnow():
        record.consumed_at = datetime.utcnow()
        db.commit()
        raise PasswordResetError("That code has expired. Request a new one.", 400)

    if (record.attempts or 0) >= OTP_MAX_ATTEMPTS:
        record.consumed_at = datetime.utcnow()
        db.commit()
        raise PasswordResetError(
            "Too many incorrect attempts. This code has been cancelled — request a new one.",
            429,
        )

    # compare_digest so a wrong code cannot be narrowed down by timing.
    if not hmac.compare_digest(record.code_hash, _hash_code(submitted, user.username)):
        record.attempts = (record.attempts or 0) + 1
        db.commit()
        remaining = max(0, OTP_MAX_ATTEMPTS - record.attempts)
        raise PasswordResetError(
            f"That code is not correct. {remaining} attempt(s) left.", 400
        )

    record.consumed_at = datetime.utcnow()
    db.commit()
    return record
