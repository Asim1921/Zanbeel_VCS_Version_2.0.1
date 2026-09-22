"""Pull request lifecycle including three-way merge."""

import hashlib

from datetime import datetime
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database.crud import (
    ActivityCRUD, BranchCRUD, CommitCRUD, FileObjectCRUD, PullRequestCRUD, RepositoryCRUD,
)
from database.database import get_db

from database.models import MergeConflictSession, User

from app.core.dependencies import get_actor_user, get_current_user
from app.core.errors import _raise_head_mismatch
from app.core.permissions import (
    _require_repository_read_access, can_manage_repository, require_repository_access,
)
from app.schemas import (
    AddPullRequestCommentRequest, CrossRepoPullRequestRequest, MergePullRequestRequest,
    PullRequestCreateRequest, ResolveCommentRequest, ResolveConflictsRequest,
    SubmitReviewRequest,
)
from app.services.branches import _normalize_branch_name
from app.services import webhooks as hook_service
from app.services.commit_graph import _find_merge_base, _get_commit_tree
from app.services.issues import _sync_issues_for_pr_created, _sync_issues_for_pr_merged
from app.services.merge import _merge_trees
from app.services.signing import sign_commit
from app.services import codeowners, forks, merge_conflicts, pr_comments, pr_reviews


router = APIRouter()


@router.get("/api/repository/{repo_id}/pull-requests")
async def list_pull_requests(
    repo_id: str,
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view pull requests")

    prs = PullRequestCRUD.list_pull_requests(db, repo_id, status)
    return {
        "success": True,
        "pull_requests": [
            {
                "id": pr.id,
                "title": pr.title,
                "description": pr.description,
                "source_branch": pr.source_branch,
                "target_branch": pr.target_branch,
                "status": pr.status,
                "created_by": pr.created_by.username if pr.created_by else None,
                "created_at": pr.created_at.isoformat() if pr.created_at else None,
                "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
                "merge_commit_id": pr.merge_commit_id
            }
            for pr in prs
        ]
    }

@router.post("/api/repository/{repo_id}/pull-requests")
async def create_pull_request(
    repo_id: str,
    request: PullRequestCreateRequest,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "create pull request in", required_scope="write")

    source_branch = _normalize_branch_name(request.source_branch)
    target_branch = _normalize_branch_name(request.target_branch)
    if source_branch == target_branch:
        raise HTTPException(status_code=400, detail="Source and target branches must differ")

    if not BranchCRUD.get_branch(db, repo_id, source_branch):
        raise HTTPException(status_code=404, detail="Source branch not found")
    if not BranchCRUD.get_branch(db, repo_id, target_branch):
        raise HTTPException(status_code=404, detail="Target branch not found")

    pr = PullRequestCRUD.create_pull_request(
        db,
        repo_id,
        request.title,
        request.description,
        source_branch,
        target_branch,
        current_user.id
    )
    _sync_issues_for_pr_created(db, repo_id, pr)

    hook_service.dispatch(db, hook_service.EVENT_PR_OPENED, repo_id, {
        "repository": {"id": repo_id, "name": repository.name},
        "pull_request": {
            "id": pr.id,
            "title": pr.title,
            "status": pr.status,
            "source_branch": pr.source_branch,
            "target_branch": pr.target_branch,
            "author": current_user.username,
        },
    })

    return {
        "success": True,
        "pull_request": {
            "id": pr.id,
            "title": pr.title,
            "status": pr.status,
            "source_branch": pr.source_branch,
            "target_branch": pr.target_branch
        }
    }

@router.get("/api/repository/{repo_id}/pull-requests/{pr_id}")
async def get_pull_request(
    repo_id: str,
    pr_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view pull request")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    return {
        "success": True,
        "pull_request": {
            "id": pr.id,
            "title": pr.title,
            "description": pr.description,
            "source_branch": pr.source_branch,
            "target_branch": pr.target_branch,
            "status": pr.status,
            "merge_commit_id": pr.merge_commit_id
        }
    }

@router.post("/api/repository/{repo_id}/pull-requests/{pr_id}/close")
async def close_pull_request(
    repo_id: str,
    pr_id: int,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    resolved_actor = current_user.username if current_user else actor_username
    actor = get_actor_user(db, resolved_actor)
    if actor.id != pr.created_by_id and not can_manage_repository(db, actor, repository):
        raise HTTPException(status_code=403, detail="Not allowed to close this pull request")

    pr = PullRequestCRUD.close_pull_request(db, pr, reviewer_id=actor.id)
    return {"success": True, "status": pr.status}

@router.post("/api/repository/{repo_id}/pull-requests/{pr_id}/merge")
async def merge_pull_request(
    repo_id: str,
    pr_id: int,
    request: Optional[MergePullRequestRequest] = Body(default=None),
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    resolved_actor = current_user.username if current_user else actor_username
    require_repository_access(db, resolved_actor, repository, "merge pull request in", required_scope="manage")

    if pr.status != "open":
        raise HTTPException(status_code=400, detail=f"Pull request is {pr.status}")

    # Review gate. Checked before any merge work so an unapproved pull request is
    # refused rather than merged and reported afterwards.
    try:
        pr_reviews.enforce_merge_gate(db, repository, pr)
    except pr_reviews.ReviewError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "detail": exc.message, **exc.payload},
        )

    source_branch = BranchCRUD.get_branch(db, repo_id, pr.source_branch)
    target_branch = BranchCRUD.get_branch(db, repo_id, pr.target_branch)
    if not source_branch or not target_branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    pre_merge_target_head = target_branch.head_commit_id

    expected_head_commit_id = request.expected_head_commit_id if request else None
    if expected_head_commit_id is not None and expected_head_commit_id != target_branch.head_commit_id:
        _raise_head_mismatch(target_branch.name, expected_head_commit_id, target_branch.head_commit_id)

    base_commit_id = _find_merge_base(db, target_branch.head_commit_id, source_branch.head_commit_id)
    base_tree = _get_commit_tree(db, base_commit_id)
    target_tree = _get_commit_tree(db, target_branch.head_commit_id)
    source_tree = _get_commit_tree(db, source_branch.head_commit_id)
    merged_tree, conflicts = _merge_trees(base_tree, target_tree, source_tree)

    if conflicts:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "code": "MERGE_CONFLICT",
                "status": "conflicts",
                "source_branch": source_branch.name,
                "target_branch": target_branch.name,
                "message": "Merge has conflicts that require manual resolution.",
                "conflicts": sorted(conflicts),
                # Where to go next. Previously this was a dead end: the conflicts were
                # reported and nothing could act on them.
                "resolve_url": (
                    f"/api/repository/{repo_id}/pull-requests/{pr.id}/conflicts"
                ),
            }
        )

    commit_id = hashlib.sha256(
        f"merge:{repo_id}:{pr.id}:{datetime.utcnow().isoformat()}".encode()
    ).hexdigest()[:40]

    commit_data = {
        "id": commit_id,
        "repository_id": repo_id,
        "author": current_user.username,
        "message": f"Merge branch '{source_branch.name}' into '{target_branch.name}'",
        "parents": [target_branch.head_commit_id, source_branch.head_commit_id],
        "timestamp": datetime.utcnow().isoformat()
    }

    file_entries = []
    for file_path, content in merged_tree.items():
        file_obj = FileObjectCRUD.store_file_object(db, content)
        file_entries.append(SimpleNamespace(
            file_path=file_path,
            file_hash=file_obj.hash,
            file_size=len(content)
        ))

    commit = CommitCRUD.create_commit_from_file_hashes(db, commit_data, file_entries)
    BranchCRUD.update_branch_head(db, repo_id, target_branch.name, commit.id)
    sign_commit(db, commit, current_user.username, current_user.id)
    PullRequestCRUD.mark_merged(db, pr, merge_commit_id=commit.id, reviewer_id=current_user.id)

    hook_service.dispatch(db, hook_service.EVENT_PR_MERGED, repo_id, {
        "repository": {"id": repo_id, "name": repository.name},
        "pull_request": {
            "id": pr.id,
            "title": pr.title,
            "source_branch": pr.source_branch,
            "target_branch": pr.target_branch,
        },
        "merge_commit": commit.id,
        "merged_by": current_user.username,
    })

    ActivityCRUD.create_activity(
        db,
        user_id=current_user.id,
        activity_type="merge_pull_request",
        description=(
            f"Merged PR #{pr.id} {source_branch.name}->{target_branch.name} "
            f"from {pre_merge_target_head[:8] if pre_merge_target_head else 'None'} to {commit.id[:8]}"
        ),
        repository_id=repo_id
    )

    _sync_issues_for_pr_merged(db, repo_id, pr, commit, current_user.id)

    return {
        "success": True,
        "status": "merged",
        "merge_commit_id": commit.id
    }


def _load_session(db, repo_id: str, pr_id: int, session_id: int):
    session = (
        db.query(MergeConflictSession)
        .filter(
            MergeConflictSession.id == session_id,
            MergeConflictSession.repository_id == repo_id,
            MergeConflictSession.pull_request_id == pr_id,
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Merge conflict session not found")
    return session


@router.post("/api/repository/{repo_id}/pull-requests/{pr_id}/conflicts")
async def start_merge_conflict_session(
    repo_id: str,
    pr_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Open (or reuse) a session for resolving this pull request's conflicts by hand.

    An existing open session is returned unchanged so reopening the resolver does not
    discard work in progress; if a branch has moved, the stale session is retired and a
    fresh one computed.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository,
                              "resolve merge conflicts in", required_scope="manage")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")
    if pr.status != "open":
        raise HTTPException(status_code=400, detail=f"Pull request is {pr.status}")

    try:
        session = merge_conflicts.open_session(db, repo_id, pr, current_user)
    except merge_conflicts.ConflictError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    return {"success": True, **merge_conflicts.get_bundle(db, session)}


@router.get("/api/repository/{repo_id}/pull-requests/{pr_id}/conflicts/{session_id}")
async def get_merge_conflicts(
    repo_id: str,
    pr_id: int,
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """All three sides of every conflicted file, plus a conflict-marked suggested merge."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view merge conflicts")

    session = _load_session(db, repo_id, pr_id, session_id)
    return {"success": True, **merge_conflicts.get_bundle(db, session)}


@router.post("/api/repository/{repo_id}/pull-requests/{pr_id}/conflicts/{session_id}/resolve")
async def resolve_merge_conflicts(
    repo_id: str,
    pr_id: int,
    session_id: int,
    request: ResolveConflictsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply the supplied resolutions and complete the merge.

    Every conflicted path must be resolved; a partial submission is refused rather than
    merged with the remainder guessed. Refuses outright if either branch moved while the
    conflicts were being worked through.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository,
                              "resolve merge conflicts in", required_scope="manage")

    session = _load_session(db, repo_id, pr_id, session_id)
    try:
        result = merge_conflicts.resolve(
            db, session, request.resolutions or {}, current_user,
            expected_head_commit_id=request.expected_head_commit_id,
        )
    except merge_conflicts.ConflictError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "detail": exc.message, **exc.payload},
        )
    return {"success": True, **result}


@router.post("/api/repository/{repo_id}/pull-requests/{pr_id}/conflicts/{session_id}/abort")
async def abort_merge_conflicts(
    repo_id: str,
    pr_id: int,
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Discard a resolution session. Neither branch is touched."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository,
                              "resolve merge conflicts in", required_scope="manage")

    session = _load_session(db, repo_id, pr_id, session_id)
    try:
        return {"success": True, **merge_conflicts.abort(db, session, current_user)}
    except merge_conflicts.ConflictError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.post("/api/repository/{repo_id}/pull-requests/{pr_id}/reviews")
async def submit_pull_request_review(
    repo_id: str,
    pr_id: int,
    request: SubmitReviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Approve, request changes, or leave a comment on a pull request.

    Reviews append rather than replace, so the history is preserved; only a reviewer's
    most recent approve/request-changes counts toward the merge gate.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "review pull requests")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    try:
        review = pr_reviews.submit(db, repository, pr, current_user,
                                   request.state, request.body)
    except pr_reviews.ReviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    ActivityCRUD.create_activity(
        db, current_user.id, "review_pull_request",
        f"{request.state.replace('_', ' ')} on PR #{pr.id} "
        f"({pr.source_branch} -> {pr.target_branch})", repo_id,
    )

    hook_service.dispatch(db, hook_service.EVENT_PR_REVIEWED, repo_id, {
        "repository": {"id": repo_id, "name": repository.name},
        "pull_request": {"id": pr.id, "title": pr.title},
        "review": {"state": request.state, "reviewer": current_user.username},
    })
    return {
        "success": True,
        "review": {
            "id": review.id, "state": review.state, "body": review.body,
            "commit_id": review.commit_id,
        },
        **pr_reviews.summarize(db, repository, pr),
    }


@router.get("/api/repository/{repo_id}/pull-requests/{pr_id}/reviews")
async def list_pull_request_reviews(
    repo_id: str,
    pr_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Every review on a pull request, plus whether it currently satisfies the merge gate."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view pull request reviews")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    return {
        "success": True,
        "reviews": pr_reviews.history(db, pr.id),
        **pr_reviews.summarize(db, repository, pr),
    }


@router.post("/api/repository/{repo_id}/pull-requests/{pr_id}/comments")
async def add_pull_request_comment(
    repo_id: str,
    pr_id: int,
    request: AddPullRequestCommentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Comment on one line of one file in a pull request.

    Pass `in_reply_to_id` to answer an existing thread; the reply inherits that thread's
    file and line, so a discussion cannot drift onto a different line.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "comment on pull requests")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    if request.in_reply_to_id is None and (not request.file_path or request.line is None):
        raise HTTPException(
            status_code=400,
            detail="file_path and line are required unless replying to an existing thread",
        )

    try:
        comment = pr_comments.add(
            db, repository, pr, current_user,
            file_path=request.file_path or "", line=request.line or 1,
            body=request.body, side=request.side, in_reply_to_id=request.in_reply_to_id,
        )
    except pr_comments.CommentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    return {"success": True, "comment_id": comment.id,
            **pr_comments.threads(db, repository, pr)}


@router.get("/api/repository/{repo_id}/pull-requests/{pr_id}/comments")
async def list_pull_request_comments(
    repo_id: str,
    pr_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Every inline comment, grouped into threads and flagged when outdated."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view pull request comments")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    return {"success": True, **pr_comments.threads(db, repository, pr)}


@router.post("/api/repository/{repo_id}/pull-requests/{pr_id}/comments/{comment_id}/resolve")
async def resolve_pull_request_comment(
    repo_id: str,
    pr_id: int,
    comment_id: int,
    request: ResolveCommentRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a comment thread settled, or reopen it. The thread stays visible either way."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "resolve pull request comments")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    try:
        return {"success": True,
                **pr_comments.set_resolved(db, pr, comment_id, current_user, request.resolved)}
    except pr_comments.CommentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.delete("/api/repository/{repo_id}/pull-requests/{pr_id}/comments/{comment_id}")
async def delete_pull_request_comment(
    repo_id: str,
    pr_id: int,
    comment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a comment. Only its author may; deleting a thread root removes its replies."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "delete pull request comments")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    try:
        return {"success": True, **pr_comments.delete(db, pr, comment_id, current_user)}
    except pr_comments.CommentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("/api/repository/{repo_id}/pull-requests/{pr_id}/owners")
async def get_pull_request_code_owners(
    repo_id: str,
    pr_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Which code owners must approve this pull request, and which paths asked for them."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view code owners")

    pr = PullRequestCRUD.get_pull_request(db, repo_id, pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="Pull request not found")

    return {"success": True, **codeowners.required_owners(db, repository, pr)}


@router.post("/api/repository/{repo_id}/pull-requests/from-fork")
async def create_cross_repo_pull_request(
    repo_id: str,
    request: CrossRepoPullRequestRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Propose a fork's branch back to the repository it was forked from.

    The author needs read access here, not write -- being able to contribute without
    write access is the whole reason forks exist. The upstream's own review and branch
    rules still apply when someone there accepts it.
    """
    upstream = RepositoryCRUD.get_repository(db, repo_id)
    if not upstream:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, upstream, "open a pull request against")

    fork = RepositoryCRUD.get_repository(db, request.source_repo_id)
    if not fork:
        raise HTTPException(status_code=404, detail="Source repository not found")
    if fork.owner_id != current_user.id and \
            getattr(current_user, "role", "developer") not in ("team_lead", "admin"):
        raise HTTPException(
            status_code=403,
            detail="You can only propose changes from a fork you own.",
        )

    try:
        pr = forks.open_cross_repo_pull_request(
            db, fork, upstream, current_user,
            request.source_branch, request.target_branch,
            request.title, request.description,
        )
    except forks.ForkError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    ActivityCRUD.create_activity(
        db, current_user.id, "create_pull_request",
        f"Opened PR #{pr.id} from fork '{fork.name}' "
        f"({pr.source_branch} -> {pr.target_branch})", upstream.id,
    )
    return {
        "success": True,
        "pull_request": {
            "id": pr.id,
            "title": pr.title,
            "source_repository_id": pr.source_repository_id,
            "source_branch": pr.source_branch,
            "target_branch": pr.target_branch,
            "status": pr.status,
        },
    }
