"""Release listing and creation."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.crud import ReleaseCRUD, RepositoryCRUD, TagCRUD
from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user
from app.core.permissions import _require_repository_read_access, require_repository_access
from app.schemas import ReleaseCreateRequest
from app.services.semver import _validate_semver
from app.services import webhooks as hook_service


router = APIRouter()


@router.get("/api/repository/{repo_id}/releases")
async def list_releases(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view releases")

    releases = ReleaseCRUD.list_releases(db, repo_id)
    return {
        "success": True,
        "releases": [
            {
                "version": r.version,
                "title": r.title,
                "notes": r.notes,
                "tag": r.tag.name if r.tag else None,
                "commit_id": r.tag.commit_id if r.tag else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "created_by": r.created_by.username if r.created_by else None
            }
            for r in releases
        ]
    }

@router.post("/api/repository/{repo_id}/releases")
async def create_release(
    repo_id: str,
    request: ReleaseCreateRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "create release in", required_scope="write")

    version = _validate_semver(request.version)
    if ReleaseCRUD.get_release(db, repo_id, version):
        raise HTTPException(status_code=400, detail="Release version already exists")

    tag_name = request.tag or (version if version.startswith("v") else f"v{version}")
    tag = TagCRUD.get_tag(db, repo_id, tag_name)
    if not tag:
        commit_id = repository.head_commit_id
        if not commit_id:
            raise HTTPException(status_code=400, detail="No commit available for release")
        tag = TagCRUD.create_tag(db, repo_id, tag_name, commit_id, f"Release {version}", current_user.id)

    release = ReleaseCRUD.create_release(db, repo_id, tag.id, version, request.title, request.notes, current_user.id)

    hook_service.dispatch(db, hook_service.EVENT_RELEASE_PUBLISHED, repo_id, {
        "repository": {"id": repo_id, "name": repository.name},
        "release": {
            "version": release.version,
            "title": release.title,
            "notes": release.notes,
            "tag": tag.name,
            "commit_id": tag.commit_id,
        },
        "published_by": current_user.username,
    })

    return {
        "success": True,
        "release": {
            "version": release.version,
            "title": release.title,
            "notes": release.notes,
            "tag": tag.name,
            "commit_id": tag.commit_id
        }
    }
