"""Tag listing, creation and deletion.

A tag name may contain slashes (``release/1.2``), so the delete route takes the rest of
the path as the name.
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
from app.schemas import TagCreateRequest
from app.services import release_ops
from app.services import webhooks as hook_service


from app.services import audit

router = APIRouter()


def _repository(db: Session, repo_id: str):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repository


@router.get("/api/repository/{repo_id}/tags")
def list_tags(
    repo_id: str,
    request: Request,
    response: Response,
    page=Depends(page_params()),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Newest first, paged."""
    repository = _repository(db, repo_id)
    _require_repository_read_access(db, current_user, repository, "view tags")
    tags, meta = release_ops.list_tags(db, repo_id, *page)
    set_page_headers(request, response, meta)
    return {
        "success": True,
        "tags": tags,
        "pagination": meta,
        "permissions": {
            "can_write": can_write_repository(db, current_user, repository),
            "can_manage": can_manage_repository(db, current_user, repository),
        },
    }

@router.post("/api/repository/{repo_id}/tags")
async def create_tag(
    repo_id: str,
    request: TagCreateRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Name a commit. ``commit_id`` or ``target`` (a branch, tag or commit) says which;
    neither means the tip of the default branch."""
    repository = _repository(db, repo_id)

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "create tag in", required_scope="write")

    tag = release_ops.create_tag(db, repository, current_user, name=request.name,
                                 commit_id=request.commit_id, target=request.target,
                                 message=request.message)
    audit.record(db, current_user.id, "tag_created", repository_id=repo_id,
                 description=f"Tagged {tag.commit_id[:8]} as '{tag.name}'", target_type="tag",
                 target_id=tag.name, commit_id=tag.commit_id)

    hook_service.dispatch(db, hook_service.EVENT_TAG_CREATED, repo_id, {
        "repository": {"id": repo_id, "name": repository.name},
        "tag": {"name": tag.name, "commit_id": tag.commit_id, "message": tag.message},
        "created_by": current_user.username,
    })

    return {
        "success": True,
        "tag": {
            "name": tag.name,
            "commit_id": tag.commit_id,
            "message": tag.message
        }
    }

@router.delete("/api/repository/{repo_id}/tags/{tag_name:path}")
async def delete_tag(
    repo_id: str,
    tag_name: str,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = _repository(db, repo_id)

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "delete tag in", required_scope="manage")

    removed = release_ops.delete_tag(db, repository, tag_name)
    audit.record(db, current_user.id, "tag_deleted", repository_id=repo_id,
                 description=f"Deleted tag '{tag_name}'", target_type="tag", target_id=tag_name,
                 commit_id=removed["commit_id"])
    hook_service.dispatch(db, hook_service.EVENT_TAG_DELETED, repo_id, {
        "repository": {"id": repo_id, "name": repository.name},
        "tag": removed,
        "deleted_by": current_user.username,
    })
    return {"success": True, "message": f"Tag '{tag_name}' deleted", "tag": removed}
