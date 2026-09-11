"""SSH public key authentication over HTTP.

The gap: bearer tokens were the only credential, so automation had to store a
long-lived secret that grants access if it leaks, and developers had no
key-based option at all.

**Why challenge-response over HTTP rather than a real SSH daemon.** The whole
protocol here is HTTP — pushes, pulls, the web UI. Running sshd alongside it
would mean a second transport, a second authorisation path and a second set of
bugs. Instead the server proves the client holds the private key the way SSH
itself does: it issues a random nonce, the client signs it, and the server
verifies with the registered public key. What crosses the wire is a signature
over a one-time value, so capturing it gains an attacker nothing.

Three properties make that safe:

* **The nonce is single-use.** It is deleted the moment it is consumed, so a
  captured challenge/response pair cannot be replayed.
* **The nonce is bound to a username.** A signature for one account cannot be
  presented as another's.
* **Signature verification uses `cryptography`**, not hand-rolled maths. Getting
  RSA padding checks subtly wrong is the classic way to build an authentication
  bypass that looks like it works.

Verification succeeds only with the private key. The server stores nothing that
would let it forge a signature, which is the advantage over a shared token.
"""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.serialization import load_ssh_public_key
from sqlalchemy.orm import Session

from database.models import SSHChallenge, SSHKey, User

# Long enough to paste a key and run a signing command, short enough that an
# abandoned nonce is not sitting around.
CHALLENGE_TTL_SECONDS = 120

SUPPORTED_KEY_TYPES = (
    "ssh-rsa",
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
)


class SSHKeyError(Exception):
    """A caller mistake; carries the HTTP status to answer with."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def parse_public_key(text: str) -> Tuple[str, str, str]:
    """Validate an OpenSSH public key line.

    Returns (key_type, normalised_line, fingerprint). The fingerprint is the
    standard OpenSSH ``SHA256:...`` form, so it matches what ``ssh-keygen -lf``
    prints and a user can compare the two by eye.
    """
    raw = (text or "").strip()
    if not raw:
        raise SSHKeyError("A public key is required")

    # Reject anything with newlines: a private key pasted by mistake is
    # multi-line, and this catches that before it reaches the database.
    if "\n" in raw or "\r" in raw:
        raise SSHKeyError(
            "Paste the single-line public key (the .pub file), not a private key"
        )

    if "PRIVATE KEY" in raw.upper():
        raise SSHKeyError("That looks like a private key. Paste the public key instead.")

    parts = raw.split()
    if len(parts) < 2:
        raise SSHKeyError("Malformed key. Expected: '<type> <base64> [comment]'")

    key_type = parts[0]
    if key_type not in SUPPORTED_KEY_TYPES:
        raise SSHKeyError(
            f"Unsupported key type '{key_type}'. Supported: {', '.join(SUPPORTED_KEY_TYPES)}"
        )

    try:
        blob = base64.b64decode(parts[1], validate=True)
    except Exception:
        raise SSHKeyError("The key body is not valid base64")

    try:
        load_ssh_public_key(f"{key_type} {parts[1]}".encode())
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise SSHKeyError(f"Could not parse the key: {exc}")

    digest = hashlib.sha256(blob).digest()
    fingerprint = "SHA256:" + base64.b64encode(digest).decode().rstrip("=")

    comment = " ".join(parts[2:]).strip()
    normalised = f"{key_type} {parts[1]}" + (f" {comment}" if comment else "")
    return key_type, normalised, fingerprint


def add_key(db: Session, user: User, title: str, public_key: str) -> SSHKey:
    """Register a key for a user."""
    label = (title or "").strip()
    if not label:
        raise SSHKeyError("A title is required so you can tell your keys apart")

    key_type, normalised, fingerprint = parse_public_key(public_key)

    existing = db.query(SSHKey).filter(SSHKey.fingerprint == fingerprint).first()
    if existing:
        # Server-wide uniqueness: the same key on two accounts would make the
        # challenge-response ambiguous about who is signing in.
        if existing.user_id == user.id:
            raise SSHKeyError("You have already added this key", status_code=409)
        raise SSHKeyError(
            "That key is already registered to another account", status_code=409
        )

    key = SSHKey(
        user_id=user.id,
        title=label[:100],
        key_type=key_type,
        public_key=normalised,
        fingerprint=fingerprint,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


def list_keys(db: Session, user_id: int) -> List[SSHKey]:
    return (
        db.query(SSHKey)
        .filter(SSHKey.user_id == user_id)
        .order_by(SSHKey.created_at.desc())
        .all()
    )


def delete_key(db: Session, user: User, key_id: int) -> bool:
    key = (
        db.query(SSHKey)
        .filter(SSHKey.id == key_id, SSHKey.user_id == user.id)
        .first()
    )
    if not key:
        return False
    db.delete(key)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Challenge / response
# ---------------------------------------------------------------------------


def _purge_expired(db: Session) -> None:
    """Drop stale nonces. Cheap, and keeps the table from growing forever."""
    try:
        db.query(SSHChallenge).filter(SSHChallenge.expires_at < datetime.utcnow()).delete()
        db.commit()
    except Exception:
        db.rollback()


def create_challenge(db: Session, username: str) -> SSHChallenge:
    """Issue a one-time nonce for ``username``.

    Deliberately issued even for an unknown user, and with no hint about whether
    keys exist. Refusing here would turn this endpoint into a way to enumerate
    which accounts exist and which have keys.
    """
    _purge_expired(db)

    challenge = SSHChallenge(
        nonce=secrets.token_urlsafe(32),
        username=(username or "").strip(),
        expires_at=datetime.utcnow() + timedelta(seconds=CHALLENGE_TTL_SECONDS),
    )
    db.add(challenge)
    db.commit()
    db.refresh(challenge)
    return challenge


def _verify_signature(key: SSHKey, message: bytes, signature: bytes) -> bool:
    """Check one signature against one registered public key."""
    try:
        public_key = load_ssh_public_key(key.public_key.encode())
    except Exception:
        return False

    try:
        if isinstance(public_key, ed25519.Ed25519PublicKey):
            public_key.verify(signature, message)
        elif isinstance(public_key, rsa.RSAPublicKey):
            # rsa-sha2-256: what modern OpenSSH emits for an ssh-rsa key.
            public_key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        else:
            return False
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify_challenge(db: Session, nonce: str, signature_b64: str) -> Optional[User]:
    """Consume a nonce and return the user whose key signed it, or None.

    The challenge row is deleted whether or not verification succeeds, so a
    failed attempt cannot be retried against the same nonce with a different
    signature.
    """
    challenge = db.query(SSHChallenge).filter(SSHChallenge.nonce == nonce).first()
    if not challenge:
        return None

    username = challenge.username
    expired = challenge.expires_at < datetime.utcnow()

    # Burn it first, unconditionally.
    try:
        db.delete(challenge)
        db.commit()
    except Exception:
        db.rollback()
        return None

    if expired:
        return None

    try:
        signature = base64.b64decode(signature_b64 or "", validate=True)
    except Exception:
        return None
    if not signature:
        return None

    user = db.query(User).filter(User.username == username).first()
    if not user or not user.is_active:
        return None

    message = nonce.encode("utf-8")
    for key in list_keys(db, user.id):
        if _verify_signature(key, message, signature):
            try:
                key.last_used_at = datetime.utcnow()
                db.commit()
            except Exception:
                db.rollback()
            return user

    return None


def serialize(key: SSHKey) -> dict:
    """Public representation. The key itself is public, so it is safe to return."""
    return {
        "id": key.id,
        "title": key.title,
        "key_type": key.key_type,
        "fingerprint": key.fingerprint,
        "public_key": key.public_key,
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "created_at": key.created_at.isoformat() if key.created_at else None,
    }
