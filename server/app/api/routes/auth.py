"""Authentication, password management and self-service registration."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database.crud import PendingUserRegistrationCRUD, UserCRUD
from database.database import get_db
from database.models import User

from app.config import PASSWORD_SETUP_KEY
from app.core.dependencies import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.services import password_reset, rate_limit
from app.schemas import (
    BootstrapPasswordRequest, ChangePasswordRequest, ForgotPasswordRequest, LoginRequest,
    RegistrationRequest, ResetPasswordWithOtpRequest,
)


logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/auth/login")
async def login(request: LoginRequest, http_request: Request, db: Session = Depends(get_db)):
    ip = rate_limit.client_ip(http_request)
    user_agent = http_request.headers.get("user-agent") if http_request else None

    # Checked before the password is verified, so a locked account costs no
    # PBKDF2 work -- that work is exactly what an attacker wants to spend.
    try:
        rate_limit.check(db, request.username, ip)
    except rate_limit.RateLimited as limited:
        raise HTTPException(
            status_code=429,
            detail=limited.message,
            headers={"Retry-After": str(limited.retry_after_seconds)},
        )

    user = UserCRUD.get_user_by_username(db, request.username)

    # Every rejection below records a failure against the *submitted* username,
    # real or not. Counting only real accounts would make the lockout itself an
    # account-existence oracle.
    if not user:
        rate_limit.record_failure(db, request.username, ip, user_agent)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User is inactive")
    if not user.password_hash:
        raise HTTPException(status_code=403, detail="Password not set. Use bootstrap password setup.")
    if not verify_password(request.password, user.password_hash):
        rate_limit.record_failure(db, request.username, ip, user_agent)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    rate_limit.record_success(db, request.username, ip, user_agent)

    # Track last login for admin engagement monitoring
    try:
        user.last_login_at = datetime.utcnow()
        db.commit()
        db.refresh(user)
    except Exception:
        db.rollback()

    token = create_access_token(user.username, getattr(user, "role", "developer"))
    return {
        "success": True,
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "team_lead_id": user.team_lead_id,
            "is_active": user.is_active,
            "last_login_at": user.last_login_at.isoformat() if getattr(user, "last_login_at", None) else None,
        }
    }

@router.get("/api/auth/me")
async def auth_me(current_user: User = Depends(get_current_user)):
    return {
        "success": True,
        "user": {
            "id": current_user.id,
            "username": current_user.username,
            "email": current_user.email,
            "full_name": current_user.full_name,
            "role": current_user.role,
            "team_lead_id": current_user.team_lead_id,
            "is_active": current_user.is_active
        }
    }

@router.post("/api/auth/change-password")
async def change_password(request: ChangePasswordRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not request.new_password or len(request.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    if current_user.password_hash and not verify_password(request.current_password or "", current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    current_user.password_hash = hash_password(request.new_password)
    db.commit()
    return {"success": True, "message": "Password updated successfully"}

@router.post("/api/auth/bootstrap-password")
async def bootstrap_password(request: BootstrapPasswordRequest, db: Session = Depends(get_db)):
    if not PASSWORD_SETUP_KEY:
        raise HTTPException(status_code=403, detail="Password bootstrap is disabled")
    if request.setup_key != PASSWORD_SETUP_KEY:
        raise HTTPException(status_code=403, detail="Invalid setup key")
    if not request.new_password or len(request.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    user = UserCRUD.get_user_by_username(db, request.username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.password_hash:
        raise HTTPException(status_code=400, detail="Password already set for this user")

    user.password_hash = hash_password(request.new_password)
    db.commit()
    return {"success": True, "message": f"Password initialized for {user.username}"}

# Every outcome of a forgot-password request returns this, so the endpoint cannot be
# used to discover which email addresses have accounts.
_FORGOT_PASSWORD_REPLY = (
    "If that email address has an account, a 6-digit code has been sent to it. "
    "The code expires in 10 minutes."
)


@router.post("/api/auth/forgot-password")
async def forgot_password(request: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Email a reset code to the address supplied, if it belongs to an account.

    Unauthenticated by necessity -- someone who has forgotten their password cannot log
    in first. The reply is identical whether or not the address is registered; anything
    else turns this into a way to enumerate accounts.
    """
    user = password_reset.find_user_by_email(db, request.email)

    if user and password_reset.smtp_configured():
        if password_reset.seconds_until_resend_allowed(db, user) <= 0:
            try:
                password_reset.request_code(db, user, requested_by=None)
            except password_reset.PasswordResetError as exc:
                # Logged, not returned: the caller must not learn why nothing arrived.
                logger.warning("forgot-password delivery failed for user_id=%s: %s",
                               user.id, exc.message)

    return {"success": True, "message": _FORGOT_PASSWORD_REPLY}


@router.post("/api/auth/reset-password")
async def reset_password_with_otp(
    request: ResetPasswordWithOtpRequest,
    db: Session = Depends(get_db),
):
    """Complete a self-service reset with the emailed code.

    Failures are deliberately indistinguishable from one another: an unknown address,
    a missing code and a wrong code all produce the same message, so this cannot be
    used to probe for accounts either.
    """
    if not request.new_password or len(request.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    generic = "That code is not correct, or it has expired. Request a new one."

    user = password_reset.find_user_by_email(db, request.email)
    if not user:
        raise HTTPException(status_code=400, detail=generic)

    try:
        password_reset.verify_and_consume(db, user, request.otp)
    except password_reset.PasswordResetError as exc:
        # The attempt cap is worth surfacing verbatim: it is the one case where
        # retrying is futile, and it reveals nothing that guessing had not already.
        detail = exc.message if exc.status_code == 429 else generic
        raise HTTPException(status_code=exc.status_code, detail=detail)

    user.password_hash = hash_password(request.new_password)
    user.updated_at = datetime.now()
    db.commit()

    # A user who was locked out by failed logins has just proved control of the
    # mailbox; leaving the lock in place would block the sign-in they came here for.
    rate_limit.unlock(db, user.username)

    return {
        "success": True,
        "message": "Password updated. You can now sign in with your new password.",
    }


@router.post("/api/auth/register-request")
async def register_request(request: RegistrationRequest, db: Session = Depends(get_db)):
    """Create a self-registration request that requires admin approval."""
    username = request.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    if not request.password or len(request.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    requested_role = (request.requested_role or 'developer').strip().lower()
    if requested_role not in ['developer', 'team_lead']:
        raise HTTPException(status_code=400, detail="requested_role must be developer or team_lead")

    if UserCRUD.get_user_by_username(db, username):
        raise HTTPException(status_code=400, detail="Username already exists")

    existing_pending = PendingUserRegistrationCRUD.get_by_username(db, username, status='pending')
    if existing_pending:
        raise HTTPException(status_code=400, detail="A pending registration already exists for this username")

    password_hash = hash_password(request.password)
    pending = PendingUserRegistrationCRUD.create_request(
        db,
        username=username,
        password_hash=password_hash,
        email=request.email,
        full_name=request.full_name,
        requested_role=requested_role,
        requested_team_lead_username=request.requested_team_lead_username
    )

    return {
        "success": True,
        "message": "Registration request submitted. Awaiting admin approval.",
        "request_id": pending.id,
        "status": pending.status
    }
