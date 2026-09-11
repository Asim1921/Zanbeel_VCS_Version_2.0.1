"""Personal access token management.

Every endpoint here operates on the caller's *own* tokens. There is deliberately
no route for an admin to list or mint tokens for another user: an admin who
could do that could act as that user indefinitely and invisibly, which is
exactly the accountability gap tokens are meant to close. Disabling the account
remains the way to cut off someone else's access, and it revokes their tokens
with it.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import AccessToken, User

from app.core.dependencies import get_current_user
from app.services import access_tokens as token_service


router = APIRouter()


class CreateTokenRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    scopes: Optional[List[str]] = None
    expires_in_days: Optional[int] = Field(None, ge=1, le=3650)


@router.get("/api/tokens")
async def list_access_tokens(
    include_revoked: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the caller's tokens. Secrets are never returned."""
    tokens = token_service.list_tokens(db, current_user.id, include_revoked=include_revoked)
    return {
        "success": True,
        "tokens": [token_service.serialize(token) for token in tokens],
        "valid_scopes": list(token_service.VALID_SCOPES),
    }


@router.post("/api/tokens")
async def create_access_token_route(
    request: CreateTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a token and return its plaintext once.

    The response carries ``token`` exactly this once. It is not recoverable
    afterwards — only its hash is stored — so the client must save it now.
    """
    expires_at = None
    if request.expires_in_days:
        expires_at = datetime.utcnow() + timedelta(days=request.expires_in_days)

    try:
        token, plaintext = token_service.create_token(
            db,
            current_user,
            name=request.name,
            scopes=request.scopes,
            expires_at=expires_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "success": True,
        "token": plaintext,
        "warning": "Copy this token now. It cannot be shown again.",
        "access_token": token_service.serialize(token),
    }


@router.delete("/api/tokens/{token_id}")
async def revoke_access_token(
    token_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke one of the caller's tokens.

    Scoped to the caller's own rows, so guessing another user's token id
    returns 404 rather than revoking their credential.
    """
    token = (
        db.query(AccessToken)
        .filter(AccessToken.id == token_id, AccessToken.user_id == current_user.id)
        .first()
    )
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    token_service.revoke_token(db, token)
    return {"success": True, "access_token": token_service.serialize(token)}
