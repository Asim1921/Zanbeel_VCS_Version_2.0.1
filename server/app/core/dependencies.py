"""FastAPI dependencies for resolving and authorising the current user.

Two credential schemes share the ``Authorization: Bearer`` header:

* **session tokens** issued by ``/api/auth/login``, which carry the full powers
  of the account and expire on their own schedule; and
* **personal access tokens** (``fxp_…``), which are named, individually
  revocable, and limited to the scopes they were created with.

They are distinguished by prefix rather than by trying one and falling back to
the other, so a malformed session token can never be silently reinterpreted as
a token lookup.

An SSH sign-in yields a personal access token too (bound to the key that
signed), so everything below that guards tokens guards key-based sign-in as well.

Two rules keep a scoped credential from becoming more than its scopes:

* **Credentials and identity need a password sign-in** (require_password_session).
  A token that could mint tokens, register an SSH key or change the account's
  email could turn itself into an unscoped, unexpiring credential -- or, via a
  password reset to the new address, into the account itself.
* **Role-derived administrative power needs the admin scope** (has_admin_powers).
  An admin's repo:write token is a repository credential; it must not configure
  server-wide hooks or edit other people's accounts just because of who made it.
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
# is to the caller's own session rather than to repository data. Revoking the
# token in use is the clearest case: `fox logout` must work with any token.
_SCOPE_EXEMPT_PATHS = frozenset({
    "/api/auth/me",
    "/api/auth/login",
    "/api/auth/ssh/challenge",
    "/api/auth/ssh/verify",
    "/api/tokens/current",
})

AUTH_SESSION = "session"
AUTH_ACCESS_TOKEN = "access_token"
AUTH_SSH_KEY = "ssh_key"

# Sent with every refusal from require_password_session, so a client can tell
# "sign in with your password for this" from any other 403 without parsing prose.
CREDENTIAL_REQUIRED_HEADER = "X-Zanbeel-Credential-Required"
PASSWORD_SESSION_MESSAGE = (
    "{action} needs a password sign-in. Access tokens and SSH keys are limited "
    "to what they were created for and cannot create or change credentials. "
    "Use the web interface, or run the command again and enter your password."
)


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
        via_key = getattr(token, "ssh_key_id", None) is not None

        if request is not None:
            path = request.url.path
            method = request.method.upper()
            if method in _WRITE_METHODS and path not in _SCOPE_EXEMPT_PATHS:
                if token_service.SCOPE_REPO_WRITE not in granted:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "This SSH key is read-only. Add the key with the 'repo:write' "
                            "scope to make changes."
                            if via_key else
                            "This access token is read-only. "
                            "Create a token with the 'repo:write' scope to make changes."
                        ),
                    )
            # Stash for require_admin_user, which needs the scope as well as the
            # role: an admin's read-only token should not administer anything.
            request.state.token_scopes = granted
            request.state.auth_scheme = AUTH_SSH_KEY if via_key else AUTH_ACCESS_TOKEN
            request.state.access_token = token

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
        request.state.auth_scheme = AUTH_SESSION
        request.state.access_token = None
    return user


def auth_scheme(request: Request) -> Optional[str]:
    """How the current request authenticated: session, access_token or ssh_key."""
    return getattr(request.state, "auth_scheme", None)


def current_access_token(request: Request):
    """The AccessToken row behind this request, or None for a password session."""
    return getattr(request.state, "access_token", None)


def has_admin_powers(request: Request, user: User) -> bool:
    """True when ``user`` may exercise their administrative role on this request.

    The role is necessary; for a token it is not sufficient -- the token must also
    carry the admin scope. A password session is unscoped and passes on role alone.
    """
    if not token_service.is_privileged(user):
        return False
    scopes = getattr(request.state, "token_scopes", None)
    return scopes is None or token_service.SCOPE_ADMIN in scopes


def assert_password_session(request: Request, action: str) -> None:
    """Refuse unless this request was authenticated by a password sign-in."""
    if auth_scheme(request) == AUTH_SESSION:
        return
    raise HTTPException(
        status_code=403,
        detail=PASSWORD_SESSION_MESSAGE.format(action=action),
        headers={CREDENTIAL_REQUIRED_HEADER: "password"},
    )


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


def require_password_session(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> User:
    """For endpoints that create or change credentials. See the module docstring."""
    assert_password_session(request, "Managing credentials")
    return current_user


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
