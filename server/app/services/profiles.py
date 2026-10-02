"""User profiles: who someone is, and what they have actually done here.

The counts come from the same tables the rest of the system reads, so a profile cannot
drift from the repository list or the commit history it claims to summarise.

Visibility follows the repository rules already in place. A profile viewed by someone
else lists only the repositories that viewer may see, because a profile page would
otherwise be a way to enumerate private repositories by owner.
"""

import re
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.services import blob_store
from database.models import Commit, PullRequest, Repository, User

#: Small enough to load inline on a page, large enough for a real photograph.
MAX_AVATAR_BYTES = 2 * 1024 * 1024

#: Checked against the file's own leading bytes rather than the declared content type,
#: because the declaration comes from the uploader and the bytes do not.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

_WEBSITE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


class ProfileError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def detect_image_type(content: bytes) -> Optional[str]:
    """The image type the bytes actually are, or None if they are not an image.

    WEBP needs its own check: the magic is split across the RIFF container header.
    """
    for magic, mime in _MAGIC:
        if content.startswith(magic):
            return mime
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def store_avatar(db: Session, user: User, content: bytes) -> str:
    """Validate and store an avatar, returning its blob hash."""
    if not content:
        raise ProfileError("The uploaded file is empty.")
    if len(content) > MAX_AVATAR_BYTES:
        raise ProfileError(
            f"An avatar must be {MAX_AVATAR_BYTES // (1024 * 1024)} MB or smaller; "
            f"this one is {len(content) // 1024} KB.",
            413,
        )
    mime = detect_image_type(content)
    if not mime:
        raise ProfileError("That file is not a PNG, JPEG, GIF or WEBP image.")

    blob_hash = blob_store.store.put(content)
    user.avatar_blob_hash = blob_hash
    user.avatar_content_type = mime
    db.commit()
    return blob_hash


def read_avatar(user: User) -> Optional[bytes]:
    if not user.avatar_blob_hash:
        return None
    return blob_store.store.get(user.avatar_blob_hash)


def clear_avatar(db: Session, user: User) -> None:
    # The blob itself is left alone: it is content-addressed and may be shared with
    # another user who uploaded the same image, so deleting it here could blank theirs.
    user.avatar_blob_hash = None
    user.avatar_content_type = None
    db.commit()


def update_profile(db: Session, user: User, fields: Dict[str, Any]) -> User:
    """Apply the fields a person may change about themselves.

    Deliberately excludes role, team lead and is_active: those are decisions other
    people make about an account, and letting the account holder set them would make
    the permission system self-serve.
    """
    if "full_name" in fields and fields["full_name"] is not None:
        user.full_name = (fields["full_name"] or "").strip()[:100] or None

    if "bio" in fields and fields["bio"] is not None:
        bio = (fields["bio"] or "").strip()
        if len(bio) > 1000:
            raise ProfileError("A bio must be 1000 characters or fewer.")
        user.bio = bio or None

    if "location" in fields and fields["location"] is not None:
        user.location = (fields["location"] or "").strip()[:100] or None

    if "website" in fields and fields["website"] is not None:
        website = (fields["website"] or "").strip()
        if website and not _WEBSITE.match(website):
            raise ProfileError("A website must start with http:// or https://")
        user.website = website[:200] or None

    if "email" in fields and fields["email"] is not None:
        email = (fields["email"] or "").strip()
        if email and "@" not in email:
            raise ProfileError("That does not look like an email address.")
        user.email = email or None

    db.commit()
    db.refresh(user)
    return user


def identity(user: User, viewer: Optional[User] = None) -> Dict[str, Any]:
    """The public shape of a user. Email is shown to the account holder and admins
    only, so a profile page does not hand out everyone's address."""
    is_self = bool(viewer and viewer.id == user.id)
    is_admin = bool(viewer and (viewer.role or "").lower() == "admin")
    return {
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "bio": user.bio,
        "location": user.location,
        "website": user.website,
        "has_avatar": bool(user.avatar_blob_hash),
        "avatar_url": f"/api/users/{user.username}/avatar" if user.avatar_blob_hash else None,
        "joined_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "is_active": bool(user.is_active),
        "email": user.email if (is_self or is_admin) else None,
        "is_self": is_self,
    }


def _visible_repositories(db: Session, user: User, viewer: Optional[User]) -> List[Repository]:
    """Repositories owned by `user` that `viewer` is allowed to know about."""
    from app.core.permissions import _user_can_read_repository_contents

    owned = (
        db.query(Repository)
        .filter(Repository.owner_id == user.id)
        .order_by(Repository.updated_at.desc().nullslast())
        .all()
    )
    if viewer is None:
        return []
    if viewer.id == user.id or (viewer.role or "").lower() == "admin":
        return owned

    visible = []
    for repo in owned:
        try:
            if _user_can_read_repository_contents(db, viewer, repo):
                visible.append(repo)
        except Exception:
            # A permission helper that cannot answer is treated as "no", so a failure
            # hides a repository rather than exposing one.
            continue
    return visible


def build(db: Session, user: User, viewer: Optional[User] = None,
          repo_limit: int = 30, commit_limit: int = 20) -> Dict[str, Any]:
    """A profile: who they are, what they own, and what they have been doing."""
    repositories = _visible_repositories(db, user, viewer)
    visible_ids = [r.id for r in repositories]

    commit_total = db.query(func.count(Commit.id)).filter(
        Commit.author_id == user.id
    ).scalar() or 0

    recent_commits = []
    if visible_ids:
        recent_commits = (
            db.query(Commit)
            .filter(Commit.author_id == user.id, Commit.repository_id.in_(visible_ids))
            .order_by(Commit.created_at.desc())
            .limit(commit_limit)
            .all()
        )

    pull_requests = db.query(func.count(PullRequest.id)).filter(
        PullRequest.created_by_id == user.id
    ).scalar() or 0

    return {
        "user": identity(user, viewer),
        "stats": {
            "repositories": len(repositories),
            # The total is the honest number of commits they authored; the list below
            # is only what this viewer may see, and saying so avoids a profile that
            # looks empty to a colleague without explanation.
            "commits": commit_total,
            "commits_visible": len(recent_commits),
            "pull_requests": pull_requests,
        },
        "repositories": [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "is_archived": bool(r.is_archived),
                "size_bytes": r.size_bytes,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                "head_commit_id": r.head_commit_id,
            }
            for r in repositories[:repo_limit]
        ],
        "recent_commits": [
            {
                "id": c.id,
                "short_id": c.id[:8],
                "repository_id": c.repository_id,
                "repository_name": next(
                    (r.name for r in repositories if r.id == c.repository_id), None),
                "message": (c.message or "").split("\n")[0],
                "timestamp": c.created_at.isoformat() if c.created_at else None,
            }
            for c in recent_commits
        ],
    }
