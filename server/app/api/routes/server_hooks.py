"""Server-side hook management.

Same access rule as webhooks: a repository's hooks are the repository owner's or
an admin's to set; global hooks apply to every repository and are admin-only.
"""

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import Repository, ServerHook, User

from app.core.dependencies import get_current_user, has_admin_powers
from app.services import server_hooks as hook_service


from app.services import audit

router = APIRouter()


def _assert_may_manage(
    http_request: Request, db: Session, user: User, repository_id: Optional[str]
) -> None:
    # The role alone is not enough when a token is in use: an admin's repo:write
    # token is a repository credential, not a server-administration one.
    if has_admin_powers(http_request, user):
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


def _check_hook_type(hook_type: str) -> None:
    if hook_type == hook_service.HOOK_POST_RECEIVE:
        raise HTTPException(
            status_code=400,
            detail="post-receive hooks never ran on this server. To be told about pushes, "
                   "create a webhook subscribed to the 'push' event instead.",
        )
    if hook_type not in hook_service.VALID_HOOK_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"hook_type must be one of {', '.join(hook_service.VALID_HOOK_TYPES)}",
        )


class ServerHookUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    enabled: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None


@router.get("/api/server-hooks")
async def list_server_hooks(
    http_request: Request,
    repository_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Hooks for a repository, or the global ones when none is given."""
    _assert_may_manage(http_request, db, current_user, repository_id)

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
def create_server_hook(
    http_request: Request,
    request: ServerHookRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a hook. Rules are validated now so a bad regex cannot land."""
    _assert_may_manage(http_request, db, current_user, request.repository_id)

    _check_hook_type(request.hook_type)

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
    audit.record(db, current_user.id, "push_rule_created", repository_id=hook.repository_id,
                 description=f"Added push rule '{hook.name}' ({', '.join(sorted(config)) or 'no rules'})",
                 target_type="push_rule", target_id=hook.id)
    return {"success": True, "hook": hook_service.serialize(hook)}


@router.put("/api/server-hooks/{hook_id}")
def update_server_hook(
    http_request: Request,
    hook_id: int,
    request: ServerHookUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a hook. Omitted fields are left alone."""
    hook = db.query(ServerHook).filter(ServerHook.id == hook_id).first()
    if not hook:
        raise HTTPException(status_code=404, detail="Hook not found")
    _assert_may_manage(http_request, db, current_user, hook.repository_id)

    if request.name is not None:
        hook.name = request.name.strip() or hook.name
    if request.enabled is not None:
        hook.enabled = request.enabled
    if request.config is not None:
        incoming = dict(request.config)
        # The policy-service secret is never sent back to the browser, so a form that
        # re-saves the rules cannot include it. Keep it unless a new one is given or
        # the service it signs for has changed.
        try:
            previous = json.loads(hook.config_json or "{}")
        except ValueError:
            previous = {}
        if (not incoming.get("external_secret") and previous.get("external_secret")
                and incoming.get("external_url") == previous.get("external_url")):
            incoming["external_secret"] = previous["external_secret"]
        try:
            hook.config_json = json.dumps(
                hook_service.validate_config(incoming), sort_keys=True
            )
        except hook_service.HookError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

    db.commit()
    db.refresh(hook)
    changed = [name for name in ("name", "enabled", "config") if getattr(request, name) is not None]
    audit.record(db, current_user.id, "push_rule_updated", repository_id=hook.repository_id,
                 description=f"Changed push rule '{hook.name}': {', '.join(changed) or 'nothing'}"
                             + ("" if hook.enabled else " (disabled)"),
                 target_type="push_rule", target_id=hook.id)
    return {"success": True, "hook": hook_service.serialize(hook)}


@router.delete("/api/server-hooks/{hook_id}")
async def delete_server_hook(
    http_request: Request,
    hook_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    hook = db.query(ServerHook).filter(ServerHook.id == hook_id).first()
    if not hook:
        raise HTTPException(status_code=404, detail="Hook not found")
    _assert_may_manage(http_request, db, current_user, hook.repository_id)

    name, repository_id, hook_id = hook.name, hook.repository_id, hook.id
    db.delete(hook)
    db.commit()
    audit.record(db, current_user.id, "push_rule_deleted", repository_id=repository_id,
                 description=f"Removed push rule '{name}'", target_type="push_rule", target_id=hook_id)
    return {"success": True}


class HookTestRequest(BaseModel):
    message: str = ""
    paths: list = Field(default_factory=list)
    # Text content to test content rules with: {path: text}. Paths listed only in
    # ``paths`` are tested with no content.
    files: Dict[str, str] = Field(default_factory=dict)
    deleted: list = Field(default_factory=list)
    branch: Optional[str] = None
    new_branch: bool = False


# Sync on purpose: a rule with an external policy service makes a blocking HTTP call,
# which in an ``async def`` would stall every other request until it timed out.
@router.post("/api/server-hooks/{hook_id}/test")
def test_server_hook(
    http_request: Request,
    hook_id: int,
    request: HookTestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Dry-run a hook against a hypothetical change.

    Lets an admin find out whether a rule does what they meant *before* it starts
    rejecting colleagues' pushes. Content rules are exercised for the files given
    with content; paths given without content are checked by path rules only.
    """
    hook = db.query(ServerHook).filter(ServerHook.id == hook_id).first()
    if not hook:
        raise HTTPException(status_code=404, detail="Hook not found")
    _assert_may_manage(http_request, db, current_user, hook.repository_id)

    try:
        config = json.loads(hook.config_json or "{}")
    except ValueError:
        raise HTTPException(status_code=500, detail="Hook configuration is not valid JSON")

    files = {path: b"" for path in request.paths}
    files.update({path: (text or "").encode("utf-8") for path, text in request.files.items()})
    commit_data = {
        "id": "0" * 40,
        "message": request.message,
        "author": current_user.username,
        "files": files,
    }
    reasons = hook_service.evaluate(
        config, commit_data, current_user,
        branch=request.branch, deleted=[str(p) for p in request.deleted],
        new_branch=request.new_branch,
    )

    untested = [path for path in request.paths if path not in request.files]
    return {
        "success": True,
        "would_accept": not reasons,
        "reasons": reasons,
        "note": (
            "Paths given without content were checked by path rules only: "
            + ", ".join(untested[:10]) if untested else None
        ),
    }
