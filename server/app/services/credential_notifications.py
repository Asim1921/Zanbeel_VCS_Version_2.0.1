"""Tell people when a credential appears on, or is taken off, their account.

A new token or SSH key is the first thing an intruder adds to keep a way in, so
the owner hears about every one -- the reason GitHub emails on both. These go to
the owner's own notifications, never the shared activity feed: which machines
can act as someone is not everyone's business.

Best effort, like review notifications: failing to notify must never turn a
successful credential change into an error.
"""

from typing import Optional

from sqlalchemy.orm import Session

from database.crud import NotificationCRUD
from database.models import AccessToken, SSHKey, User


def _send(db: Session, user_id: Optional[int], **kwargs) -> None:
    if not user_id:
        return
    try:
        NotificationCRUD.create_notification(db, user_id=user_id, **kwargs)
    except Exception:
        db.rollback()


def token_created(db: Session, user: User, token: AccessToken) -> None:
    expiry = token.expires_at.strftime("%Y-%m-%d") if token.expires_at else "never"
    _send(
        db, user.id,
        notification_type="credential_token_created",
        title=f"New access token: {token.name}",
        body=(
            f"An access token named '{token.name}' ({token.prefix}…) with scopes "
            f"{token.scopes.replace(',', ', ')} was added to your account. It expires: {expiry}. "
            "If this was not you, revoke it under Settings → Access tokens and change your password."
        ),
        payload={"token_id": token.id, "name": token.name, "scopes": token.scopes,
                 "target": {"kind": "settings", "tab": "tokens"}},
    )


def ssh_key_added(db: Session, user: User, key: SSHKey) -> None:
    _send(
        db, user.id,
        notification_type="credential_ssh_key_added",
        title=f"New SSH key: {key.title}",
        body=(
            f"An SSH key '{key.title}' ({key.fingerprint}) was added to your account with scopes "
            f"{(key.scopes or '').replace(',', ', ')}. If this was not you, remove it under "
            "Settings → SSH keys and change your password."
        ),
        payload={"key_id": key.id, "fingerprint": key.fingerprint,
                 "target": {"kind": "settings", "tab": "ssh"}},
    )


def credentials_revoked(db: Session, user: User, actor: User, tokens: int, keys: int) -> None:
    _send(
        db, user.id,
        notification_type="credentials_revoked",
        title="Your access tokens and SSH keys were revoked",
        body=(
            f"{actor.username} revoked {tokens} access token(s) and removed {keys} SSH key(s) "
            "from your account. Sign in with your password to create new ones."
        ),
        payload={"tokens": tokens, "keys": keys, "by": actor.username,
                 "target": {"kind": "settings", "tab": "tokens"}},
    )
