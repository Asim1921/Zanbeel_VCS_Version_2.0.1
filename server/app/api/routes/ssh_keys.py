"""SSH public key management and key-based sign-in.

The two challenge endpoints are unauthenticated by necessity — they are how a
client authenticates in the first place. They are safe to expose because a
challenge is a random nonce that is useless without the private key, and
because neither endpoint reveals whether an account exists.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user
from app.core.security import create_access_token
from app.services import ssh_keys as key_service


router = APIRouter()


class AddKeyRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=100)
    public_key: str = Field(..., min_length=1)


class ChallengeRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)


class VerifyRequest(BaseModel):
    nonce: str = Field(..., min_length=1)
    signature: str = Field(..., min_length=1)


@router.get("/api/ssh-keys")
async def list_ssh_keys(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The caller's own keys."""
    keys = key_service.list_keys(db, current_user.id)
    return {
        "success": True,
        "keys": [key_service.serialize(key) for key in keys],
        "supported_types": list(key_service.SUPPORTED_KEY_TYPES),
    }


@router.post("/api/ssh-keys")
async def add_ssh_key(
    request: AddKeyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Register a public key on the caller's own account."""
    try:
        key = key_service.add_key(db, current_user, request.title, request.public_key)
    except key_service.SSHKeyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return {"success": True, "key": key_service.serialize(key)}


@router.delete("/api/ssh-keys/{key_id}")
async def delete_ssh_key(
    key_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove one of the caller's keys.

    Scoped to the caller's own rows, so guessing another user's key id returns
    404 rather than removing their credential.
    """
    if not key_service.delete_key(db, current_user, key_id):
        raise HTTPException(status_code=404, detail="Key not found")
    return {"success": True}


@router.post("/api/auth/ssh/challenge")
async def ssh_challenge(request: ChallengeRequest, db: Session = Depends(get_db)):
    """Issue a one-time nonce to sign.

    Answers identically for known and unknown usernames, and says nothing about
    whether any keys are registered — otherwise this becomes an account
    enumeration endpoint that needs no credentials at all.
    """
    challenge = key_service.create_challenge(db, request.username)
    return {
        "success": True,
        "nonce": challenge.nonce,
        "expires_in": key_service.CHALLENGE_TTL_SECONDS,
    }


@router.post("/api/auth/ssh/verify")
async def ssh_verify(request: VerifyRequest, db: Session = Depends(get_db)):
    """Exchange a signed nonce for a session token."""
    user = key_service.verify_challenge(db, request.nonce, request.signature)
    if not user:
        # One message for every failure mode — expired, replayed, wrong key,
        # unknown user. Distinguishing them tells an attacker which part to fix.
        raise HTTPException(status_code=401, detail="SSH key authentication failed")

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
            "is_active": user.is_active,
        },
    }
