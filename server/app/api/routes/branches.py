"""Branch listing and mutation.

Routes that name a branch in the path use the ``:path`` converter. Branch names contain
slashes (``feature/login``), and a plain ``{branch_name}`` stops at the first one, so a
rename, delete, default change or move of any such branch answered 404 -- and the web
client, which only knows branches by name, could not manage them at all.
"""

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database.crud import ActivityCRUD, BranchCRUD, RepositoryCRUD
from database.database import get_db
from database.models import BranchProtectionPolicy, MergeConflictSession, PullRequest, User

from app.core.dependencies import get_current_user
from app.core.pagination import page_params, paginate_list, set_page_headers
from app.core.permissions import (
    _require_repository_checkout_access, _require_repository_read_access,
    can_manage_repository, can_write_repository, require_repository_access,
    user_sees_all_repository_branches,
)
from app.schemas import (
    BranchCreateRequest, BranchHeadUpdateRequest, BranchMergeRequest,
    BranchPolicyRequest, BranchPublishRequest, BranchRenameRequest, CopyFilesRequest,
    ResolveConflictsRequest, SaveConflictResolutionRequest,
)
from app.services import branch_ops, merge_conflicts, refs, repo_refs, server_hooks
from app.services.branches import _get_default_branch, _normalize_branch_name


router = APIRouter()


def _permissions(db: Session, user: User, repository) -> dict:
    """What the caller may do here, so a client offers only what will be accepted."""
    return {
        "can_write": can_write_repository(db, user, repository),
        "can_manage": can_manage_repository(db, user, repository),
    }


@router.get("/api/repository/{repo_id}/branches")
def list_branches(
    repo_id: str,
    request: Request,
    response: Response,
    details: bool = False,
    page=Depends(page_params()),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Every branch, default first, paged.

    ``details=true`` adds what a branch list needs to be useful: the last commit, how far
    each branch is ahead of and behind the default branch, its protection, and the open
    pull requests that use it. It walks history, so plain listings leave it off -- and it
    is computed for the page only, not for every branch in the repository.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    _require_repository_checkout_access(db, current_user, repository, None, "view branches")

    branches = BranchCRUD.get_branches_by_repository(db, repo_id)
    if not user_sees_all_repository_branches(db, current_user, repository):
        branches = [b for b in branches if b.is_default] or branches[:1]
    # Default branch first, then alphabetical; the exact name and id break ties so
    # pages never overlap.
    branches = sorted(
        branches,
        key=lambda b: (not bool(b.is_default), (b.name or "").lower(), b.name or "", b.id or 0),
    )
    default_branch = next((b.name for b in branches if b.is_default), None)
    rows, meta = paginate_list(branches, *page)
    set_page_headers(request, response, meta)
    if details:
        listed = repo_refs.branch_overview(db, repository, rows)
    else:
        listed = [
            {
                "name": b.name,
                "head_commit_id": b.head_commit_id,
                "is_default": b.is_default,
                "created_at": b.created_at.isoformat() if b.created_at else None,
                "updated_at": b.updated_at.isoformat() if b.updated_at else None
            }
            for b in rows
        ]
    return {
        "success": True,
        "branches": listed,
        "default_branch": default_branch,
        "permissions": _permissions(db, current_user, repository),
        "pagination": meta,
    }


@router.post("/api/repository/{repo_id}/branches")
async def create_branch(
    repo_id: str,
    request: BranchCreateRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "create branch in", required_scope="write")

    branch_name = _normalize_branch_name(request.name)

    # The repository's branch policy may demand more than plain write access.
    try:
        branch_ops.enforce(db, current_user, repository, "create_branch", branch_name)
    except branch_ops.BranchOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    if BranchCRUD.get_branch(db, repo_id, branch_name):
        raise HTTPException(status_code=400, detail="Branch already exists")

    # Where the new branch starts: a commit, a tag, another branch, or the default
    # branch. A commit must belong to this repository -- any id on the server used to
    # do, which let a branch here expose another repository's files.
    if request.from_commit:
        head_commit_id = repo_refs.require_commit(db, repository, request.from_commit).id
    elif getattr(request, "from_tag", None):
        tag = repo_refs.resolve(db, repository, request.from_tag)
        if tag["kind"] != "tag":
            raise HTTPException(status_code=404, detail=f"Tag '{request.from_tag}' not found")
        head_commit_id = tag["commit_id"]
    elif request.from_branch:
        source = BranchCRUD.get_branch(db, repo_id, _normalize_branch_name(request.from_branch))
        if not source:
            raise HTTPException(
                status_code=404, detail=f"Branch '{request.from_branch}' not found"
            )
        head_commit_id = source.head_commit_id
    else:
        # The default branch's tip, not repository.head_commit_id: the latter is moved by
        # writes that never touch a branch and is not what anyone means by "current".
        default = repo_refs.default_branch(db, repo_id)
        head_commit_id = (default.head_commit_id if default else None) or repository.head_commit_id

    server_hooks.check_new_branch(db, repository, branch_name, current_user)

    # Through the reference service like every other branch change, so the creation is
    # checked, recorded in the audit trail and announced to webhooks.
    branch = refs.update_reference(
        db, repository=repository, actor=current_user, branch_name=branch_name,
        new_commit_id=head_commit_id, create_if_missing=True,
    )
    return {
        "success": True,
        "branch": {
            "name": branch.name,
            "head_commit_id": branch.head_commit_id,
            "is_default": branch.is_default
        }
    }

@router.put("/api/repository/{repo_id}/branches/{branch_name:path}/rename")
async def rename_branch(
    repo_id: str,
    branch_name: str,
    request: BranchRenameRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "rename branch in", required_scope="manage")

    old_name = _normalize_branch_name(branch_name)
    new_name = _normalize_branch_name(request.new_name)
    if old_name == new_name:
        raise HTTPException(status_code=400, detail="The new name is the same as the old one")
    if not BranchCRUD.get_branch(db, repo_id, old_name):
        raise HTTPException(status_code=404, detail=f"Branch '{old_name}' not found")
    if BranchCRUD.get_branch(db, repo_id, new_name):
        raise HTTPException(status_code=400, detail="Target branch name already exists")

    branch = refs.rename_reference(
        db, repository=repository, actor=current_user,
        old_name=old_name, new_name=new_name,
    )

    # What referred to the old name follows it, as on GitHub. An open pull request kept
    # pointing at a branch that no longer existed and could be neither merged nor shown,
    # and a rule written for exactly this branch silently stopped applying to it. (A
    # pattern rule is about names, not this branch, so it stays as written.)
    retargeted = []
    for pr in db.query(PullRequest).filter(PullRequest.status == "open").all():
        source_repo = pr.source_repository_id or pr.repository_id
        changed = False
        if source_repo == repo_id and pr.source_branch == old_name:
            pr.source_branch = new_name
            changed = True
        if pr.repository_id == repo_id and pr.target_branch == old_name:
            pr.target_branch = new_name
            changed = True
        if changed:
            retargeted.append(pr.id)
    moved_rules = (
        db.query(BranchProtectionPolicy)
        .filter(BranchProtectionPolicy.repository_id == repo_id,
                BranchProtectionPolicy.branch_pattern == old_name)
        .all()
    )
    for rule in moved_rules:
        rule.branch_pattern = new_name
    db.commit()

    return {
        "success": True,
        "branch": {"name": branch.name, "head_commit_id": branch.head_commit_id},
        "retargeted_pull_requests": retargeted,
        "moved_protection_rules": len(moved_rules),
    }

@router.put("/api/repository/{repo_id}/branches/{branch_name:path}/head")
async def update_branch_head(
    repo_id: str,
    branch_name: str,
    request: BranchHeadUpdateRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "update branch head in", required_scope="manage")

    commit = repo_refs.require_commit(db, repository, request.head_commit_id)

    # Straight through the reference service: this endpoint could previously move any
    # branch to any commit on a bare "manage" check, protection notwithstanding.
    branch = refs.update_reference(
        db, repository=repository, actor=current_user,
        branch_name=_normalize_branch_name(branch_name),
        new_commit_id=commit.id,
        expected_commit_id=request.expected_head_commit_id,
    )
    return {
        "success": True,
        "branch": {
            "name": branch.name,
            "head_commit_id": branch.head_commit_id,
            "generation": branch.generation,
        },
    }

@router.put("/api/repository/{repo_id}/branches/{branch_name:path}/default")
async def set_default_branch(
    repo_id: str,
    branch_name: str,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "set default branch in", required_scope="manage")

    name = _normalize_branch_name(branch_name)
    target = BranchCRUD.get_branch(db, repo_id, name)
    if not target:
        raise HTTPException(status_code=404, detail=f"Branch '{name}' not found")
    if not target.head_commit_id:
        raise HTTPException(status_code=400, detail=f"Branch '{name}' has no commits yet")
    previous = getattr(_get_default_branch(db, repo_id), "name", None)
    branch = BranchCRUD.set_default_branch(db, repo_id, name)
    if branch.head_commit_id:
        repository.head_commit_id = branch.head_commit_id
        db.commit()
    ActivityCRUD.create_activity(
        db, current_user.id, "set_default_branch",
        f"Made '{branch.name}' the default branch" + (f" (was '{previous}')" if previous else ""),
        repo_id, target_type="branch", target_id=branch.name,
    )
    # Code search indexes the default branch, so it has to follow.
    try:
        from app.services import search_index
        search_index.schedule(repo_id)
    except Exception:
        pass

    return {"success": True, "branch": {"name": branch.name, "is_default": branch.is_default}}

@router.delete("/api/repository/{repo_id}/branches/{branch_name:path}")
async def delete_branch(
    repo_id: str,
    branch_name: str,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a branch. The response carries its last commit, so it can be restored by
    creating a branch of the same name from that commit."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "delete branch in", required_scope="manage")

    branch = BranchCRUD.get_branch(db, repo_id, _normalize_branch_name(branch_name))
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    if branch.is_default:
        raise HTTPException(status_code=400, detail="Cannot delete the default branch")

    # An open pull request whose branch disappears can be neither merged nor shown. Say
    # which ones, so they can be merged or closed first.
    in_use = repo_refs.open_pull_requests_for(db, repository).get(branch.name, [])
    if in_use:
        numbers = ", ".join(f"#{pr['id']}" for pr in in_use)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "BRANCH_IN_USE",
                "message": f"'{branch.name}' is used by open pull request {numbers}. "
                           "Merge or close it first.",
                "pull_requests": in_use,
            },
        )

    head = branch.head_commit_id
    name = branch.name
    refs.delete_reference(
        db, repository=repository, actor=current_user, branch_name=name
    )
    return {"success": True, "message": f"Branch '{name}' deleted",
            "branch": {"name": name, "head_commit_id": head}}


def _branch_op(fn, db, repo_id, current_user, *args, **kwargs):
    """Shared plumbing: resolve the repository, run the operation, translate errors."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    try:
        return repository, fn(db, repository, current_user, *args, **kwargs)
    except branch_ops.BranchOpError as exc:
        if exc.payload:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"message": exc.message, **exc.payload},
            )
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("/api/repository/{repo_id}/branch-policy")
async def get_branch_policy(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The repository's branch protection policy, plus the caller's own scope.

    Returns the built-in defaults when the repository has never set one.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view branch policy")

    return {
        "success": True,
        "policy": branch_ops.get_policy(repository),
        "your_scope": branch_ops.actor_scope(db, current_user, repository),
        "default_branch": _get_default_branch(db, repo_id).name,
    }


@router.put("/api/repository/{repo_id}/branch-policy")
async def update_branch_policy(
    repo_id: str,
    request: BranchPolicyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replace the branch protection policy. Requires manage access."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not can_manage_repository(db, current_user, repository):
        raise HTTPException(
            status_code=403,
            detail="Changing the branch policy requires manage access on this repository.",
        )

    supplied = {k: v for k, v in request.model_dump().items() if v is not None}
    try:
        policy = branch_ops.set_policy(db, repository, supplied)
    except branch_ops.BranchOpError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    ActivityCRUD.create_activity(
        db, current_user.id, "update_branch_policy",
        f"Updated branch policy ({len(policy.get('protected_branches', []))} protected branch(es))",
        repo_id,
    )
    return {"success": True, "policy": policy}


@router.post("/api/repository/{repo_id}/branches/merge")
async def merge_branches(
    repo_id: str,
    request: BranchMergeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Merge one branch into another without going through a pull request."""
    _repo, result = _branch_op(
        branch_ops.merge_branches, db, repo_id, current_user,
        request.source_branch, request.target_branch,
        expected_head_commit_id=request.expected_head_commit_id,
        dry_run=request.dry_run,
    )
    return {"success": True, **result}


@router.post("/api/repository/{repo_id}/branches/publish")
async def publish_branch(
    repo_id: str,
    request: BranchPublishRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fast-forward one branch to another.

    Refuses when the target has diverged rather than quietly creating a merge commit --
    the caller asked for the safe operation.
    """
    _repo, result = _branch_op(
        branch_ops.publish_branch, db, repo_id, current_user,
        request.source_branch, request.target_branch,
        expected_head_commit_id=request.expected_head_commit_id,
    )
    return {"success": True, **result}


@router.post("/api/repository/{repo_id}/branches/copy-files")
async def copy_files_between_branches(
    repo_id: str,
    request: CopyFilesRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Take specific paths from one branch onto another, leaving the rest untouched.

    This is the endpoint `fox` has been calling since it was written; the server never
    implemented it.
    """
    _repo, result = _branch_op(
        branch_ops.copy_files, db, repo_id, current_user,
        request.source_branch, request.target_branch, request.paths,
        expected_head_commit_id=request.expected_head_commit_id,
    )
    return {"success": True, **result}


# --- conflict resolution for a direct branch merge --------------------------------
# A conflicted `fox merge` used to report the conflict and stop: resolution sessions
# belonged to pull requests, so there was nowhere to park the three sides. These are
# the same session endpoints, addressed by branch pair rather than by pull request.

def _conflict_error(exc: "merge_conflicts.ConflictError") -> JSONResponse:
    """The same refusal shape as the pull request conflict endpoints."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "detail": exc.message, "message": exc.message,
                 **exc.payload},
    )


def _load_branch_session(db: Session, repo_id: str, session_id: int):
    session = (
        db.query(MergeConflictSession)
        .filter(
            MergeConflictSession.id == session_id,
            MergeConflictSession.repository_id == repo_id,
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Merge session not found")
    if session.pull_request_id is not None:
        # Addressed here it would skip the pull request's own review gate.
        raise HTTPException(
            status_code=400,
            detail={
                "code": "WRONG_SESSION_KIND",
                "message": f"Session {session_id} belongs to pull request "
                           f"#{session.pull_request_id}; resolve it there.",
                "pull_request_id": session.pull_request_id,
            },
        )
    return session


@router.post("/api/repository/{repo_id}/branches/conflicts")
async def start_branch_merge_conflict_session(
    repo_id: str,
    request: BranchMergeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Park a conflicted branch merge for manual resolution.

    Reuses an open session for the same pair when neither branch has moved, so
    reopening the resolver does not discard work already done.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository,
                              "resolve merge conflicts in", required_scope="manage")

    # Refuse on a branch that would never accept the result, rather than inviting
    # someone to work through a resolution that cannot land.
    refs.check_reference_operation(
        db, repository=repository, actor=current_user,
        branch_name=request.target_branch, operation=refs.OP_MERGE,
    )

    try:
        session = merge_conflicts.open_branch_session(
            db, repo_id, current_user,
            source_branch=request.source_branch,
            target_branch=request.target_branch,
        )
    except merge_conflicts.ConflictError as exc:
        return _conflict_error(exc)

    return {"success": True, **merge_conflicts.get_bundle(db, session)}


@router.get("/api/repository/{repo_id}/branches/conflicts/{session_id}")
async def get_branch_merge_conflicts(
    repo_id: str,
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """All three sides of every conflicted file in a branch merge session."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view merge conflicts")

    session = _load_branch_session(db, repo_id, session_id)
    return {"success": True, **merge_conflicts.get_bundle(db, session)}


@router.put("/api/repository/{repo_id}/branches/conflicts/{session_id}/files")
async def save_branch_merge_resolution(
    repo_id: str,
    session_id: int,
    request: SaveConflictResolutionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save the decision for one file -- new content, delete, or clear -- without merging."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository,
                              "resolve merge conflicts in", required_scope="manage")

    session = _load_branch_session(db, repo_id, session_id)
    try:
        result = merge_conflicts.save_resolution(
            db, session, request.path, current_user,
            content_b64=request.content_b64, delete=request.delete, clear=request.clear,
        )
    except merge_conflicts.ConflictError as exc:
        return _conflict_error(exc)
    return {"success": True, **result}


@router.post("/api/repository/{repo_id}/branches/conflicts/{session_id}/resolve")
async def resolve_branch_merge_conflicts(
    repo_id: str,
    session_id: int,
    request: Optional[ResolveConflictsRequest] = Body(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply the resolutions and complete the branch merge.

    Every conflicted path must be decided, here or saved earlier; a partial resolution is
    refused rather than merged with the remainder guessed. The target branch's review
    rules still apply -- resolving conflicts is not a way to land unreviewed content.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository,
                              "resolve merge conflicts in", required_scope="manage")

    session = _load_branch_session(db, repo_id, session_id)
    request = request or ResolveConflictsRequest()
    try:
        result = merge_conflicts.resolve(
            db, session, request.resolutions, current_user,
            expected_head_commit_id=request.expected_head_commit_id,
            allow_markers=request.allow_markers,
        )
    except merge_conflicts.ConflictError as exc:
        return _conflict_error(exc)
    return {"success": True, **result}


@router.post("/api/repository/{repo_id}/branches/conflicts/{session_id}/abort")
async def abort_branch_merge_conflicts(
    repo_id: str,
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Discard a branch merge session without merging anything."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository,
                              "resolve merge conflicts in", required_scope="manage")

    session = _load_branch_session(db, repo_id, session_id)
    try:
        result = merge_conflicts.abort(db, session, current_user)
    except merge_conflicts.ConflictError as exc:
        return _conflict_error(exc)
    return {"success": True, **result}


@router.get("/api/repository/{repo_id}/branches/conflicts")
async def list_branch_merge_sessions(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Open branch merge sessions, so an interrupted resolution can be found again."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view merge conflicts")
    return {"success": True,
            "sessions": merge_conflicts.list_open(db, repo_id, branch_only=True)}
