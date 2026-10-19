"""Release listing, publishing, editing and withdrawal, and drafted release notes.

There is deliberately no ``GET /releases/{version}``: the branch-protection router owns
``GET /releases/immutable`` and is registered after this one, so a catch-all here would
swallow it.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from database.crud import RepositoryCRUD
from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user
from app.core.pagination import page_params, set_page_headers
from app.core.permissions import (
    _require_repository_read_access, can_manage_repository, can_write_repository,
    require_repository_access,
)
from app.schemas import ReleaseCreateRequest, ReleaseUpdateRequest
from app.services import release_ops
from app.services import webhooks as hook_service


from app.services import audit

router = APIRouter()


def _repository(db: Session, repo_id: str):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repository


def _release_payload(release) -> dict:
    return {
        "version": release.version,
        "title": release.title,
        "notes": release.notes,
        "tag": release.tag.name if release.tag else None,
        "commit_id": release.tag.commit_id if release.tag else None,
    }


@router.get("/api/repository/{repo_id}/releases")
def list_releases(
    repo_id: str,
    request: Request,
    response: Response,
    page=Depends(page_params()),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Newest first, paged. ``latest`` marks the highest version that is not a
    pre-release, across every release rather than within the page."""
    repository = _repository(db, repo_id)
    _require_repository_read_access(db, current_user, repository, "view releases")
    releases, meta = release_ops.list_releases(db, repo_id, *page)
    set_page_headers(request, response, meta)
    return {
        "success": True,
        "releases": releases,
        "pagination": meta,
        "permissions": {
            "can_write": can_write_repository(db, current_user, repository),
            "can_manage": can_manage_repository(db, current_user, repository),
        },
    }


@router.get("/api/repository/{repo_id}/releases/notes")
def draft_release_notes(
    repo_id: str,
    target: Optional[str] = None,
    previous_tag: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Notes drafted from the pull requests and commits since the previous release."""
    repository = _repository(db, repo_id)
    _require_repository_read_access(db, current_user, repository, "view releases")
    return {"success": True, **release_ops.draft_notes(db, repository, target=target,
                                                      previous_tag=previous_tag)}


@router.post("/api/repository/{repo_id}/releases")
async def create_release(
    repo_id: str,
    request: ReleaseCreateRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = _repository(db, repo_id)

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "create release in", required_scope="write")

    release = release_ops.create_release(
        db, repository, current_user, version=request.version, tag=request.tag,
        target=request.target, title=request.title, notes=request.notes,
    )
    tag = release.tag
    audit.record(db, current_user.id, "release_created", repository_id=repo_id,
                 description=f"Published release {release.version} ({tag.name})",
                 target_type="release", target_id=release.version, commit_id=tag.commit_id)

    hook_service.dispatch(db, hook_service.EVENT_RELEASE_PUBLISHED, repo_id, {
        "repository": {"id": repo_id, "name": repository.name},
        "release": _release_payload(release),
        "published_by": current_user.username,
    })

    return {"success": True, "release": _release_payload(release)}


@router.patch("/api/repository/{repo_id}/releases/{version}")
async def update_release(
    repo_id: str,
    version: str,
    request: ReleaseUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Correct a release's title or notes. The version and tag do not change."""
    repository = _repository(db, repo_id)
    require_repository_access(db, current_user.username, repository, "edit releases in",
                              required_scope="write")
    release = release_ops.get_release(db, repo_id, version)
    changed = release_ops.update_release(db, release, title=request.title, notes=request.notes)
    if changed:
        audit.record(db, current_user.id, "release_updated", repository_id=repo_id,
                     description=f"Edited release {release.version}", target_type="release",
                     target_id=release.version, payload={"changed": sorted(changed)})
        hook_service.dispatch(db, hook_service.EVENT_RELEASE_EDITED, repo_id, {
            "repository": {"id": repo_id, "name": repository.name},
            "release": _release_payload(release),
            "changed": sorted(changed),
            "edited_by": current_user.username,
        })
    return {"success": True, "release": _release_payload(release), "changed": sorted(changed)}


@router.delete("/api/repository/{repo_id}/releases/{version}")
async def delete_release(
    repo_id: str,
    version: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Withdraw a release. Its tag stays; delete that separately if it should go too."""
    repository = _repository(db, repo_id)
    require_repository_access(db, current_user.username, repository, "delete releases in",
                              required_scope="manage")
    release = release_ops.get_release(db, repo_id, version)
    removed = release_ops.delete_release(db, release)
    audit.record(db, current_user.id, "release_deleted", repository_id=repo_id,
                 description=f"Deleted release {removed['version']} (tag {removed['tag']} kept)",
                 target_type="release", target_id=removed["version"],
                 commit_id=removed["commit_id"])
    hook_service.dispatch(db, hook_service.EVENT_RELEASE_DELETED, repo_id, {
        "repository": {"id": repo_id, "name": repository.name},
        "release": removed,
        "deleted_by": current_user.username,
    })
    return {"success": True, "release": removed}
