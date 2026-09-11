"""FastAPI dependencies for resolving and authorising the current user.

Two credential schemes share the ``Authorization: Bearer`` header:

* **session tokens** issued by ``/api/auth/login``, which carry the full powers
  of the account and expire on their own schedule; and
* **personal access tokens** (``fxp_…``), which are named, individually
  revocable, and limited to the scopes they were created with.

They are distinguished by prefix rather than by trying one and falling back to
the other, so a malformed session token can never be silently reinterpreted as
a token lookup.
"""

from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from database.crud import UserCRUD
from database.database import get_db
from database.models import User

from app.core.security import decode_access_token
from app.services import access_tokens as token_service


http_bearer = HTTPBearer(auto_error=False)

# Methods that change state. A read-only token must not be able to reach these,
# and checking the method centrally covers every route at once — far safer than
# hoping each of ~200 handlers remembers to ask.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Endpoints a read-only token must still be able to POST to, because the write
# is to the caller's own session rather than to repository data.
_SCOPE_EXEMPT_PATHS = frozenset({
    "/api/auth/me",
    "/api/auth/login",
    "/api/auth/ssh/challenge",
    "/api/auth/ssh/verify",
})


def _resolve_credential(db: Session, credential: str, request: Optional[Request]) -> User:
    """Resolve either credential scheme to a user, enforcing token scopes."""
    if token_service.looks_like_access_token(credential):
        token = token_service.resolve_token(db, credential)
        if not token:
            raise HTTPException(status_code=401, detail="Invalid or revoked access token")

        user = db.query(User).filter(User.id == token.user_id).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="User is inactive or not found")

        granted = token_service.expand_scopes(token_service.scopes_of(token))

        if request is not None:
            path = request.url.path
            method = request.method.upper()
            if method in _WRITE_METHODS and path not in _SCOPE_EXEMPT_PATHS:
                if token_service.SCOPE_REPO_WRITE not in granted:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "This access token is read-only. "
                            "Create a token with the 'repo:write' scope to make changes."
                        ),
                    )
            # Stash for require_admin_user, which needs the scope as well as the
            # role: an admin's read-only token should not administer anything.
            request.state.token_scopes = granted
            request.state.auth_scheme = "access_token"

        token_service.touch(db, token)
        return user

    payload = decode_access_token(credential)
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token subject")

    user = UserCRUD.get_user_by_username(db, username)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User is inactive or not found")

    if request is not None:
        request.state.token_scopes = None  # a session token is unscoped
        request.state.auth_scheme = "session"
    return user


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(http_bearer),
    db: Session = Depends(get_db),
) -> User:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Authentication required")
    return _resolve_credential(db, credentials.credentials, request)


def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(http_bearer),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Same as get_current_user when Authorization is sent; otherwise None (no 401)."""
    if not credentials or not credentials.credentials:
        return None
    try:
        return _resolve_credential(db, credentials.credentials, request)
    except HTTPException:
        return None


def require_admin_user(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> User:
    role = getattr(current_user, "role", "developer")
    if role not in ["team_lead", "admin"]:
        raise HTTPException(status_code=403, detail="Admin/team lead role required")

    # A token only administers if it was explicitly granted the admin scope.
    # Role alone is not enough: the point of a scoped token is that it can be
    # weaker than the account holding it.
    scopes = getattr(request.state, "token_scopes", None)
    if scopes is not None and token_service.SCOPE_ADMIN not in scopes:
        raise HTTPException(
            status_code=403,
            detail="This access token lacks the 'admin' scope required for this endpoint",
        )
    return current_user


def require_reviewer_user(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> User:
    """Alias for reviewer-gated endpoints (team_lead/admin)."""
    return require_admin_user(request, current_user)


def get_actor_user(db: Session, actor_username: Optional[str]) -> User:
    """Resolve and validate the requesting user for permission-sensitive actions."""
    if not actor_username:
        raise HTTPException(status_code=403, detail="actor_username is required")

    user = UserCRUD.get_user_by_username(db, actor_username)
    if not user:
        raise HTTPException(status_code=403, detail=f"User '{actor_username}' does not exist")
    if not user.is_active:
        raise HTTPException(status_code=403, detail=f"User '{actor_username}' is inactive")

    return user
