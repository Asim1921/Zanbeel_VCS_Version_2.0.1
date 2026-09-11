"""Personal access tokens: named, scoped, individually revocable credentials.

Why these exist: the CLI used to send the user's actual password to
``/api/auth/login`` on every machine it ran on, so a compromised laptop meant a
password rotation and there was no way to cut off one machine without cutting
off all of them. A token can be revoked on its own, carries only the scopes it
needs, and never reveals the password that would let an attacker change the
account itself.

Storage follows the usual rule for shared secrets: only the SHA-256 of the
token is kept, so a database disclosure does not hand over working credentials.
The plaintext is returned exactly once, at creation.

SHA-256 rather than a slow KDF is deliberate here and is *not* the same
trade-off as password hashing: these secrets are 256 bits of CSPRNG output, so
there is no dictionary to run and no entropy to recover by brute force. A slow
KDF would only add latency to every authenticated request.
"""

import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from database.models import AccessToken, User

# Chosen so a token is visually unmistakable in a log or a paste, and so the
# auth dependency can tell a token from a session credential without a lookup.
TOKEN_PREFIX = "fxp_"
_SECRET_BYTES = 32

SCOPE_REPO_READ = "repo:read"
SCOPE_REPO_WRITE = "repo:write"
SCOPE_ADMIN = "admin"

VALID_SCOPES = (SCOPE_REPO_READ, SCOPE_REPO_WRITE, SCOPE_ADMIN)

# Writing implies reading, and admin implies both. Expanding here means callers
# compare against one flat set rather than re-deriving the hierarchy.
_SCOPE_IMPLIES = {
    SCOPE_REPO_READ: {SCOPE_REPO_READ},
    SCOPE_REPO_WRITE: {SCOPE_REPO_WRITE, SCOPE_REPO_READ},
    SCOPE_ADMIN: {SCOPE_ADMIN, SCOPE_REPO_WRITE, SCOPE_REPO_READ},
}


def normalise_scopes(scopes: Optional[Iterable[str]]) -> List[str]:
    """Validate and de-duplicate a requested scope list, preserving rank order.

    Raises ValueError naming the offending scope rather than silently dropping
    it: a token quietly created with fewer powers than asked for fails later,
    somewhere far from the mistake.
    """
    if not scopes:
        return [SCOPE_REPO_READ]

    cleaned = []
    for raw in scopes:
        scope = (raw or "").strip().lower()
        if not scope:
            continue
        if scope not in VALID_SCOPES:
            raise ValueError(
                f"Unknown scope '{scope}'. Valid scopes: {', '.join(VALID_SCOPES)}"
            )
        if scope not in cleaned:
            cleaned.append(scope)

    if not cleaned:
        return [SCOPE_REPO_READ]

    return [scope for scope in VALID_SCOPES if scope in cleaned]


def expand_scopes(scopes: Sequence[str]) -> set:
    """Return every scope implied by the given ones."""
    granted = set()
    for scope in scopes:
        granted |= _SCOPE_IMPLIES.get(scope, {scope})
    return granted


def scopes_of(token: AccessToken) -> List[str]:
    """The scope list stored on a token row."""
    raw = (token.scopes or "").split(",")
    return [scope.strip() for scope in raw if scope.strip()]


def hash_token(plaintext: str) -> str:
    """The stored form of a token secret."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_token() -> Tuple[str, str, str]:
    """Mint a new secret.

    Returns (plaintext, prefix, token_hash). The prefix is stored so the UI can
    show which token is which; it is a fragment of the *public* label, not of
    the secret material used for authentication.
    """
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    plaintext = f"{TOKEN_PREFIX}{secret}"
    prefix = plaintext[: len(TOKEN_PREFIX) + 6]
    return plaintext, prefix, hash_token(plaintext)


def looks_like_access_token(credential: str) -> bool:
    """True if the credential is shaped like a personal access token.

    Session tokens are ``payload.signature`` and never carry this prefix, so the
    two schemes can share one Authorization header without ambiguity.
    """
    return bool(credential) and credential.startswith(TOKEN_PREFIX)


def create_token(
    db: Session,
    user: User,
    name: str,
    scopes: Optional[Iterable[str]] = None,
    expires_at: Optional[datetime] = None,
) -> Tuple[AccessToken, str]:
    """Create a token for ``user``. Returns the row and the one-time plaintext."""
    label = (name or "").strip()
    if not label:
        raise ValueError("Token name is required")

    requested = normalise_scopes(scopes)

    # A token must never be able to do more than the person who created it.
    # Without this, a developer could mint an admin-scoped token and escalate.
    if SCOPE_ADMIN in requested and getattr(user, "role", "developer") not in ("team_lead", "admin"):
        raise ValueError("Only team leads and admins may create admin-scoped tokens")

    plaintext, prefix, token_hash = generate_token()

    token = AccessToken(
        user_id=user.id,
        name=label[:100],
        prefix=prefix,
        token_hash=token_hash,
        scopes=",".join(requested),
        expires_at=expires_at,
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token, plaintext


def resolve_token(db: Session, credential: str) -> Optional[AccessToken]:
    """Look up a live token by its plaintext, or None.

    Returns None for revoked and expired tokens as well as unknown ones, so a
    caller cannot distinguish 'never existed' from 'no longer valid' — that
    difference is useful to an attacker probing for old credentials and to
    nobody else.
    """
    if not looks_like_access_token(credential):
        return None

    digest = hash_token(credential)

    # Indexed equality on the hash. hmac.compare_digest below guards the
    # comparison itself; the lookup is by an unguessable 256-bit value.
    token = db.query(AccessToken).filter(AccessToken.token_hash == digest).first()
    if not token:
        return None
    if not hmac.compare_digest(token.token_hash, digest):
        return None
    if token.revoked_at is not None:
        return None
    if token.expires_at is not None and token.expires_at < datetime.utcnow():
        return None

    return token


def touch(db: Session, token: AccessToken) -> None:
    """Record that a token was just used.

    Best-effort: failing to update a usage timestamp must never turn a valid
    request into a failed one, so the error is swallowed and the transaction
    rolled back to keep the session usable for the actual request.
    """
    try:
        token.last_used_at = datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()


def revoke_token(db: Session, token: AccessToken) -> AccessToken:
    """Mark a token revoked. Idempotent — re-revoking keeps the original time."""
    if token.revoked_at is None:
        token.revoked_at = datetime.utcnow()
        db.commit()
        db.refresh(token)
    return token


def list_tokens(db: Session, user_id: int, include_revoked: bool = False) -> List[AccessToken]:
    """Every token belonging to a user, newest first."""
    query = db.query(AccessToken).filter(AccessToken.user_id == user_id)
    if not include_revoked:
        query = query.filter(AccessToken.revoked_at.is_(None))
    return query.order_by(AccessToken.created_at.desc()).all()


def serialize(token: AccessToken) -> dict:
    """Public representation. Never includes the secret or its hash."""
    now = datetime.utcnow()
    expired = bool(token.expires_at and token.expires_at < now)
    return {
        "id": token.id,
        "name": token.name,
        "prefix": token.prefix,
        "scopes": scopes_of(token),
        "created_at": token.created_at.isoformat() if token.created_at else None,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
        "revoked": token.revoked_at is not None,
        "expired": expired,
        "active": token.revoked_at is None and not expired,
    }
