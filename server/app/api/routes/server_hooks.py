"""Server-side hook management.

Same access rule as webhooks: a repository's hooks are the repository owner's or
an admin's to set; global hooks apply to every repository and are admin-only.
"""

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import Repository, ServerHook, User

from app.core.dependencies import get_current_user
from app.services import server_hooks as hook_service


router = APIRouter()


def _assert_may_manage(db: Session, user: User, repository_id: Optional[str]) -> None:
    role = (getattr(user, "role", "developer") or "").lower()
    if role in ("team_lead", "admin"):
        return
    if not repository_id:
        raise HTTPException(
            status_code=403,
            detail="Only admins and team leads may manage server-wide hooks",
        )
    repository = db.query(Repository).filter(Repository.id == repository_id).first()
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if repository.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the repository owner or an admin may manage its hooks",
        )


class ServerHookRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    repository_id: Optional[str] = None
    hook_type: str = hook_service.HOOK_PRE_RECEIVE
    enabled: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)


class ServerHookUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    enabled: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None


@router.get("/api/server-hooks")
async def list_server_hooks(
    repository_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Hooks for a repository, or the global ones when none is given."""
    _assert_may_manage(db, current_user, repository_id)

    query = db.query(ServerHook)
    if repository_id:
        query = query.filter(ServerHook.repository_id == repository_id)
    else:
        query = query.filter(ServerHook.repository_id.is_(None))

    hooks = query.order_by(ServerHook.id.asc()).all()
    return {
        "success": True,
        "hooks": [hook_service.serialize(hook) for hook in hooks],
        "valid_rules": list(hook_service.RULE_KEYS),
        "valid_types": list(hook_service.VALID_HOOK_TYPES),
    }


@router.post("/api/server-hooks")
async def create_server_hook(
    request: ServerHookRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a hook. Rules are validated now so a bad regex cannot land."""
    _assert_may_manage(db, current_user, request.repository_id)

    if request.hook_type not in hook_service.VALID_HOOK_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"hook_type must be one of {', '.join(hook_service.VALID_HOOK_TYPES)}",
        )

    if request.repository_id:
        exists = db.query(Repository).filter(Repository.id == request.repository_id).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Repository not found")

    try:
        config = hook_service.validate_config(request.config or {})
    except hook_service.HookError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    hook = ServerHook(
        repository_id=request.repository_id,
        name=request.name.strip(),
        hook_type=request.hook_type,
        enabled=request.enabled,
        config_json=json.dumps(config, sort_keys=True),
        created_by_id=current_user.id,
    )
    db.add(hook)
    db.commit()
    db.refresh(hook)
    return {"success": True, "hook": hook_service.serialize(hook)}


@router.put("/api/server-hooks/{hook_id}")
async def update_server_hook(
    hook_id: int,
    request: ServerHookUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a hook. Omitted fields are left alone."""
    hook = db.query(ServerHook).filter(ServerHook.id == hook_id).first()
    if not hook:
        raise HTTPException(status_code=404, detail="Hook not found")
    _assert_may_manage(db, current_user, hook.repository_id)

    if request.name is not None:
        hook.name = request.name.strip() or hook.name
    if request.enabled is not None:
        hook.enabled = request.enabled
    if request.config is not None:
        try:
            hook.config_json = json.dumps(
                hook_service.validate_config(request.config), sort_keys=True
            )
        except hook_service.HookError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

    db.commit()
    db.refresh(hook)
    return {"success": True, "hook": hook_service.serialize(hook)}


@router.delete("/api/server-hooks/{hook_id}")
async def delete_server_hook(
    hook_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    hook = db.query(ServerHook).filter(ServerHook.id == hook_id).first()
    if not hook:
        raise HTTPException(status_code=404, detail="Hook not found")
    _assert_may_manage(db, current_user, hook.repository_id)

    db.delete(hook)
    db.commit()
    return {"success": True}


class HookTestRequest(BaseModel):
    message: str = ""
    paths: list = Field(default_factory=list)


@router.post("/api/server-hooks/{hook_id}/test")
async def test_server_hook(
    hook_id: int,
    request: HookTestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Dry-run a hook against a hypothetical commit.

    Lets an admin find out whether a rule does what they meant *before* it
    starts rejecting colleagues' pushes. Content rules cannot be exercised here
    because no file bodies are supplied — the response says so rather than
    reporting a misleading pass.
    """
    hook = db.query(ServerHook).filter(ServerHook.id == hook_id).first()
    if not hook:
        raise HTTPException(status_code=404, detail="Hook not found")
    _assert_may_manage(db, current_user, hook.repository_id)

    try:
        config = json.loads(hook.config_json or "{}")
    except ValueError:
        raise HTTPException(status_code=500, detail="Hook configuration is not valid JSON")

    commit_data = {
        "id": "0" * 40,
        "message": request.message,
        "author": current_user.username,
        # Empty payloads: path rules still apply, content and size rules cannot.
        "files": {path: "" for path in request.paths},
    }
    reasons = hook_service.evaluate(config, commit_data, current_user)

    return {
        "success": True,
        "would_accept": not reasons,
        "reasons": reasons,
        "note": (
            "Content and size rules are not exercised by a dry run, which sends "
            "paths without file bodies."
        ),
    }
