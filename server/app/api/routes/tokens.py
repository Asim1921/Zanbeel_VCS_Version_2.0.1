"""Personal access token management.

Every endpoint here operates on the caller's *own* tokens, with two
administrator exceptions at the bottom: seeing which credentials exist, and
revoking all of one person's at once (a stolen laptop should not mean disabling
the account). There is still no way for an admin to *mint* a token for someone
else: an admin who could do that could act as that user indefinitely and
invisibly, which is exactly the accountability gap tokens are meant to close.

Creating a token needs a password sign-in (require_password_session). A token
that could mint tokens could outlive its own expiry and revocation by making a
replacement, and widen its own scopes on the way.
"""

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.crud import UserCRUD
from database.database import get_db
from database.models import AccessToken, SSHKey, User

from app.core.dependencies import (
    assert_password_session, current_access_token, get_current_user,
    require_admin_user, require_password_session,
)
from app.core.pagination import page_params, paginate_list, paginate_query, set_page_headers
from app.services import access_tokens as token_service
from app.services import credential_notifications
from app.services import ssh_keys as key_service


from app.services import audit

router = APIRouter()


class CreateTokenRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    scopes: Optional[List[str]] = None
    expires_in_days: Optional[int] = Field(None, ge=1, le=3650)


@router.get("/api/tokens")
async def list_access_tokens(
    request: Request,
    response: Response,
    include_revoked: bool = False,
    page=Depends(page_params()),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the caller's tokens, paged (revoked ones accumulate). Secrets are never returned."""
    tokens = token_service.list_tokens(db, current_user.id, include_revoked=include_revoked)
    rows, meta = paginate_list(tokens, *page)
    set_page_headers(request, response, meta)
    return {
        "success": True,
        "tokens": [token_service.serialize(token) for token in rows],
        "valid_scopes": list(token_service.VALID_SCOPES),
        "pagination": meta,
    }


@router.post("/api/tokens")
async def create_access_token_route(
    request: CreateTokenRequest,
    current_user: User = Depends(require_password_session),
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

    credential_notifications.token_created(db, current_user, token)
    audit.record(db, current_user.id, "token_created",
                 description=f"Created access token '{token.name}' ({', '.join(token_service.scopes_of(token))})",
                 target_type="access_token", target_id=token.id)
    return {
        "success": True,
        "token": plaintext,
        "warning": "Copy this token now. It cannot be shown again.",
        "access_token": token_service.serialize(token),
    }


@router.delete("/api/tokens/current")
async def revoke_current_token(
    http_request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke the token this request was made with -- what ``fox logout`` calls.

    Allowed for any token, read-only ones included: giving up your own access is
    never an escalation, and a logout that leaves the token live is not a logout.
    """
    token = current_access_token(http_request)
    if token is None:
        raise HTTPException(
            status_code=400,
            detail="This request was signed in with a password session, not an access token. "
                   "Password sessions expire on their own.",
        )
    token_service.revoke_token(db, token)
    audit.record(db, current_user.id, "token_revoked", description=f"Revoked access token '{token.name}' (signed out)",
                 target_type="access_token", target_id=token.id)
    return {"success": True, "access_token": token_service.serialize(token)}


@router.delete("/api/tokens/{token_id}")
async def revoke_access_token(
    token_id: int,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke one of the caller's tokens.

    Scoped to the caller's own rows, so guessing another user's token id
    returns 404 rather than revoking their credential. A token may revoke itself
    here if it can write (a read-only one uses /api/tokens/current, which the
    write-scope check exempts); revoking any *other* token needs a password
    sign-in, so a stolen token cannot lock its owner's other machines out.
    """
    token = (
        db.query(AccessToken)
        .filter(AccessToken.id == token_id, AccessToken.user_id == current_user.id)
        .first()
    )
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")

    in_use = current_access_token(http_request)
    if not (in_use is not None and in_use.id == token.id):
        assert_password_session(http_request, "Revoking another credential")

    token_service.revoke_token(db, token)
    audit.record(db, current_user.id, "token_revoked", description=f"Revoked access token '{token.name}'",
                 target_type="access_token", target_id=token.id)
    return {"success": True, "access_token": token_service.serialize(token)}


# ---------------------------------------------------------------------------
# Administrator oversight
# ---------------------------------------------------------------------------


def _credentials_of(db: Session, user: User) -> dict:
    tokens = [
        token for token in token_service.list_tokens(db, user.id, include_ssh_sessions=True)
        if token_service.is_live(token)
    ]
    keys = key_service.list_keys(db, user.id)
    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role,
            "is_active": user.is_active,
        },
        "tokens": [token_service.serialize(t) for t in tokens if t.ssh_key_id is None],
        "ssh_keys": [
            key_service.serialize(key, active_sessions=token_service.live_session_count(db, key.id))
            for key in keys
        ],
    }


@router.get("/api/admin/credentials")
async def list_all_credentials(
    request: Request,
    response: Response,
    username: Optional[str] = None,
    page=Depends(page_params(default=50)),
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Every live token and registered key, by user, paged by user. Metadata only,
    never secrets.

    Answers "which machines can act as whom" -- the first question after a laptop
    goes missing. Users with no credentials are left out.
    """
    if username:
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        users, meta = [user], {"total": 1, "limit": 1, "offset": 0, "returned": 1,
                               "has_more": False, "next_offset": None}
    else:
        holders = (
            db.query(AccessToken.user_id).filter(AccessToken.revoked_at.is_(None))
            .union(db.query(SSHKey.user_id))
        )
        users, meta = paginate_query(
            db.query(User).filter(User.id.in_(holders)).order_by(User.username, User.id), *page)
    set_page_headers(request, response, meta)

    entries = [_credentials_of(db, user) for user in users]
    if not username:
        entries = [e for e in entries if e["tokens"] or e["ssh_keys"]]
    return {"success": True, "users": entries, "pagination": meta}


@router.post("/api/admin/users/{username}/credentials/revoke")
async def revoke_user_credentials(
    username: str,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Revoke every token and remove every SSH key one user has.

    The account itself stays usable: the owner signs in with their password and
    creates fresh credentials. Disabling the account remains the heavier option.
    """
    user = UserCRUD.get_user_by_username(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    keys = key_service.delete_all_for_user(db, user.id)
    tokens = token_service.revoke_all_for_user(db, user.id)
    credential_notifications.credentials_revoked(db, user, current_user, tokens, keys)
    audit.record(db, current_user.id, "credentials_revoked",
                 description=f"Revoked every credential of {user.username}: {tokens} token(s), {keys} key(s)",
                 target_type="user", target_id=user.username)
    return {"success": True, "revoked_tokens": tokens, "removed_ssh_keys": keys}
