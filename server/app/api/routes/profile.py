"""Profile: reading a user, and changing your own.

Split from users.py, which is administration — creating accounts, setting roles,
deactivating people. What someone may change about themselves is a different question
from what an administrator may change about them, and keeping them apart is what stops
a self-service endpoint quietly growing a role field.
"""

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_user
from app.services import profiles
from database.crud import UserCRUD
from database.database import get_db
from database.models import User

router = APIRouter()


class ProfileUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    bio: Optional[str] = None
    location: Optional[str] = None
    website: Optional[str] = None
    email: Optional[str] = None


def _user_or_404(db: Session, username: str) -> User:
    user = UserCRUD.get_user_by_username(db, username)
    if not user:
        raise HTTPException(status_code=404, detail=f"No user named '{username}'")
    return user


@router.get("/api/profile")
async def get_own_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Your own profile, including the fields only you and an administrator see."""
    return {"success": True, **profiles.build(db, current_user, viewer=current_user)}


@router.get("/api/users/{username}/profile")
async def get_user_profile(
    username: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Another person's profile.

    The repository list is filtered to what the viewer may already see, so a profile
    cannot be used to enumerate private repositories by owner.
    """
    user = _user_or_404(db, username)
    return {"success": True, **profiles.build(db, user, viewer=current_user)}


@router.put("/api/profile")
async def update_own_profile(
    request: ProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change your own details. Role and account status are deliberately not here."""
    try:
        user = profiles.update_profile(db, current_user, request.model_dump(exclude_unset=True))
    except profiles.ProfileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return {"success": True, "user": profiles.identity(user, viewer=user)}


@router.post("/api/profile/avatar")
async def upload_own_avatar(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set your avatar.

    The image type is taken from the file's own leading bytes, not the declared
    content type: the declaration comes from whoever is uploading, the bytes do not.
    """
    content = await file.read()
    try:
        profiles.store_avatar(db, current_user, content)
    except profiles.ProfileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return {
        "success": True,
        "avatar_url": f"/api/users/{current_user.username}/avatar",
        "content_type": current_user.avatar_content_type,
    }


@router.delete("/api/profile/avatar")
async def delete_own_avatar(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove your avatar and fall back to initials."""
    profiles.clear_avatar(db, current_user)
    return {"success": True}


@router.get("/api/users/{username}/avatar")
async def get_user_avatar(
    username: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """An avatar's bytes.

    Served through the API rather than a static directory so it stays behind the same
    authentication as everything else, and so the blob store remains the only place
    file content lives.
    """
    user = _user_or_404(db, username)
    content = profiles.read_avatar(user)
    if content is None:
        raise HTTPException(status_code=404, detail="This user has no avatar")
    return Response(
        content=content,
        media_type=user.avatar_content_type or "application/octet-stream",
        headers={
            # The bytes are content-addressed, so a changed avatar is a different
            # blob and a long cache cannot serve a stale one.
            "Cache-Control": "private, max-age=86400",
            "ETag": f'"{user.avatar_blob_hash}"',
        },
    )
