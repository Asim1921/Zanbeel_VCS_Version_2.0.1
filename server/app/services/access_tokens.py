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
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from database.models import AccessToken, SSHKey, User

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


def is_privileged(user: User) -> bool:
    return (getattr(user, "role", None) or "developer") in ("team_lead", "admin")


def check_scopes_allowed(user: User, scopes: Sequence[str]) -> None:
    """A credential must never be able to do more than the person who created it.

    Without this, a developer could mint an admin-scoped token (or register an
    admin-scoped key) and escalate. Shared by tokens and SSH keys so the two
    cannot drift apart.
    """
    if SCOPE_ADMIN in scopes and not is_privileged(user):
        raise ValueError("Only team leads and admins may grant the 'admin' scope")


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
    check_scopes_allowed(user, requested)

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


def list_tokens(
    db: Session,
    user_id: int,
    include_revoked: bool = False,
    include_ssh_sessions: bool = False,
) -> List[AccessToken]:
    """Every token belonging to a user, newest first.

    Tokens issued by SSH sign-ins are left out unless asked for: they come and go
    with every sign-in, and they are managed through the key that issued them.
    """
    query = db.query(AccessToken).filter(AccessToken.user_id == user_id)
    if not include_revoked:
        query = query.filter(AccessToken.revoked_at.is_(None))
    if not include_ssh_sessions:
        query = query.filter(AccessToken.ssh_key_id.is_(None))
    return query.order_by(AccessToken.created_at.desc(), AccessToken.id.desc()).all()


def is_live(token: AccessToken, now: Optional[datetime] = None) -> bool:
    now = now or datetime.utcnow()
    return token.revoked_at is None and not (token.expires_at and token.expires_at < now)


# ---------------------------------------------------------------------------
# Tokens issued by SSH sign-in
# ---------------------------------------------------------------------------


def issue_ssh_session(db: Session, user: User, key: SSHKey, hours: int) -> Tuple[AccessToken, str]:
    """Mint the token an SSH sign-in hands back.

    It is an ordinary access token in every way that matters for enforcement --
    same prefix, same scope checks -- so key-based sign-in cannot open a door the
    token rules do not already guard. What differs is its lifetime (short: the
    client can always sign again) and its tie to the key (removing the key ends it).

    The key's scopes are re-checked against the user's role at every sign-in, so a
    team lead demoted to developer stops getting admin-scoped sessions from an old key.
    """
    scopes = [s for s in scopes_of_key(key) if s in VALID_SCOPES] or [SCOPE_REPO_READ]
    if not is_privileged(user):
        scopes = [s for s in scopes if s != SCOPE_ADMIN] or [SCOPE_REPO_READ]

    # Spent sessions of this key are no use to anyone; keep the table from growing
    # by one row per sign-in forever.
    now = datetime.utcnow()
    for stale in (
        db.query(AccessToken)
        .filter(AccessToken.ssh_key_id == key.id)
        .all()
    ):
        if not is_live(stale, now):
            db.delete(stale)

    plaintext, prefix, token_hash = generate_token()
    token = AccessToken(
        user_id=user.id,
        name=f"SSH sign-in: {key.title}"[:100],
        prefix=prefix,
        token_hash=token_hash,
        scopes=",".join(scopes),
        expires_at=now + timedelta(hours=hours),
        ssh_key_id=key.id,
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token, plaintext


def scopes_of_key(key: SSHKey) -> List[str]:
    raw = (getattr(key, "scopes", None) or SCOPE_REPO_WRITE).split(",")
    return [scope.strip() for scope in raw if scope.strip()]


def live_session_count(db: Session, key_id: int) -> int:
    now = datetime.utcnow()
    return sum(
        1 for token in db.query(AccessToken).filter(AccessToken.ssh_key_id == key_id).all()
        if is_live(token, now)
    )


def delete_ssh_sessions(db: Session, key_id: int) -> int:
    """Remove every token a key issued. The caller commits."""
    return (
        db.query(AccessToken)
        .filter(AccessToken.ssh_key_id == key_id)
        .delete(synchronize_session=False)
    )


def revoke_all_for_user(db: Session, user_id: int) -> int:
    """Revoke every live token a user holds, personal and SSH-issued alike."""
    now = datetime.utcnow()
    count = 0
    for token in db.query(AccessToken).filter(
        AccessToken.user_id == user_id, AccessToken.revoked_at.is_(None)
    ).all():
        token.revoked_at = now
        count += 1
    db.commit()
    return count


def serialize(token: AccessToken) -> dict:
    """Public representation. Never includes the secret or its hash."""
    now = datetime.utcnow()
    expired = bool(token.expires_at and token.expires_at < now)
    ssh_key_id = getattr(token, "ssh_key_id", None)
    return {
        "id": token.id,
        "name": token.name,
        "prefix": token.prefix,
        "scopes": scopes_of(token),
        "kind": "ssh" if ssh_key_id else "personal",
        "ssh_key_id": ssh_key_id,
        "created_at": token.created_at.isoformat() if token.created_at else None,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
        "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
        "revoked": token.revoked_at is not None,
        "expired": expired,
        "active": token.revoked_at is None and not expired,
    }
