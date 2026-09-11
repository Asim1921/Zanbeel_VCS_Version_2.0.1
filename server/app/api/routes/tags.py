"""Tag listing, creation and deletion."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.crud import CommitCRUD, RepositoryCRUD, TagCRUD
from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user
from app.core.permissions import _require_repository_read_access, require_repository_access
from app.schemas import TagCreateRequest
from app.services.branches import _normalize_branch_name
from app.services import webhooks as hook_service


router = APIRouter()


@router.get("/api/repository/{repo_id}/tags")
async def list_tags(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view tags")

    tags = TagCRUD.list_tags(db, repo_id)
    return {
        "success": True,
        "tags": [
            {
                "name": t.name,
                "commit_id": t.commit_id,
                "message": t.message,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "created_by": t.created_by.username if t.created_by else None
            }
            for t in tags
        ]
    }

@router.post("/api/repository/{repo_id}/tags")
async def create_tag(
    repo_id: str,
    request: TagCreateRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "create tag in", required_scope="write")

    tag_name = _normalize_branch_name(request.name)
    commit_id = request.commit_id or repository.head_commit_id
    if not commit_id:
        raise HTTPException(status_code=400, detail="No commit available for tagging")
    if not CommitCRUD.get_commit(db, commit_id):
        raise HTTPException(status_code=400, detail="Commit not found")

    tag = TagCRUD.create_tag(db, repo_id, tag_name, commit_id, request.message, current_user.id)

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

@router.delete("/api/repository/{repo_id}/tags/{tag_name}")
async def delete_tag(
    repo_id: str,
    tag_name: str,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "delete tag in", required_scope="manage")

    TagCRUD.delete_tag(db, repo_id, tag_name)
    return {"success": True, "message": f"Tag '{tag_name}' deleted"}
