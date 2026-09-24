"""Push, pull and commit listing."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.crud import (
    ActivityCRUD, BranchCRUD, CommitCRUD, IssueCRUD, IssueLinkCRUD, PendingCommitCRUD,
    RepositoryCRUD, UserCRUD, UserPermissionCRUD, parse_issue_references,
)
from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user
from app.core.errors import _raise_head_mismatch
from app.core.pagination import paginate_list
from app.core.permissions import (
    _require_repository_checkout_access, _require_repository_read_access,
)
from app.schemas import PushCommitRequest
from app.services import branch_ops
from app.services.branches import _get_default_branch, _normalize_branch_name, _resolve_branch
from app.services.commit_graph import _collect_reachable_commits
from app.services.issues import _sync_issues_for_new_commit
from app.services.notifications import (
    _notify_issue_commit_submitted, _notify_pending_commit_reviewer,
)
from app.services.push_policy import screen_commit_files
from app.services.serializers import commit_to_dict
from app.services.signing import sign_commit, verify_commit, verify_repository
from app.services import webhooks as hook_service
from app.services import server_hooks


def _branch_foxignore_text(db: Session, head_commit_id: Optional[str]) -> Optional[str]:
    """The .foxignore already committed on this branch, if any."""
    if not head_commit_id:
        return None
    try:
        from app.services.commit_graph import _get_commit_tree
        from app.services.ignore_rules import IGNORE_FILENAME

        blob = _get_commit_tree(db, head_commit_id).get(IGNORE_FILENAME)
        if blob is None:
            return None
        return blob.decode("utf-8", errors="replace")
    except Exception:
        # Never fail a push because the ignore file could not be read; fall back to defaults.
        return None


router = APIRouter()


@router.post("/api/repository/{repo_id}/push")
async def push_commit(
    repo_id: str,
    request: PushCommitRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Push a commit to repository.

    The pusher is the authenticated caller. It used to be read from the request body,
    which meant anyone able to reach this endpoint could push as any user; the client
    already sends its bearer token, so taking the identity from the token instead costs
    nothing and closes that hole. The commit's *author* may still differ from the pusher,
    but only privileged roles may push someone else's work (checked below).
    """
    if not request.commit:
        raise HTTPException(status_code=400, detail="Commit data required")
    
    try:
        # Ensure repository exists
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")
        
        # Get the author username from commit data
        author_username = request.commit.get("author")
        if not author_username:
            raise HTTPException(status_code=400, detail="Commit author required")

        # The pusher is whoever the token says it is. A `pusher` in the body is accepted
        # only when it agrees with that, so a stale client sending it still works while a
        # forged one is refused rather than silently honoured.
        pusher_username = current_user.username
        if request.pusher and request.pusher != pusher_username:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Authenticated as '{pusher_username}' but the request claims to be "
                    f"pushed by '{request.pusher}'."
                ),
            )
        
        # Check if repository is archived and user is trying regular push
        if repository.is_archived and not request.archive:
            raise HTTPException(
                status_code=400, 
                detail="Repository is archived. Use 'fox push --archive' to push to archived repository."
            )
        
        # Get the pusher user to check role and permissions
        user = UserCRUD.get_user_by_username(db, pusher_username)
        if not user:
            raise HTTPException(
                status_code=403,
                detail=f"User '{pusher_username}' does not exist. Please contact an administrator to create your account."
            )

        user_role = getattr(user, 'role', 'developer')
        
        # Resolve branch name (default if not specified)
        if request.branch:
            branch_name = _normalize_branch_name(request.branch)
        else:
            branch_name = _get_default_branch(db, repo_id).name

        current_branch = BranchCRUD.get_branch(db, repo_id, branch_name)
        actual_head = current_branch.head_commit_id if current_branch else None
        if request.expected_head_commit_id is not None and request.expected_head_commit_id != actual_head:
            _raise_head_mismatch(branch_name, request.expected_head_commit_id, actual_head)

        is_rollback_push = branch_name.startswith("rollback_")

        # Prevent non-privileged users from pushing someone else's regular commits
        if (
            pusher_username != author_username
            and not is_rollback_push
            and user_role not in ['team_lead', 'admin']
        ):
            raise HTTPException(
                status_code=403,
                detail="Only team leads/admins can push commits authored by another user on non-rollback branches."
            )

        # Branch protection: a repository can require more than plain write access to
        # push, and more still to push a protected or default branch. Rollback branches
        # are exempt -- they are machine-generated and never protected.
        if not is_rollback_push:
            try:
                branch_ops.enforce(db, user, repository, "push", branch_name)
            except branch_ops.BranchOpError as exc:
                raise HTTPException(status_code=exc.status_code, detail=exc.message)

        # Check basic permissions (owner or has permission) using pusher identity
        is_owner = repository.owner.username == pusher_username
        has_write_permission = UserPermissionCRUD.has_permission(db, pusher_username, repo_id, 'write')
        
        if not is_owner and not has_write_permission:
            raise HTTPException(
                status_code=403, 
                detail=f"User '{pusher_username}' does not have permission to push to this repository. Please contact the repository owner or an administrator."
            )

        # Add repository_id to commit data
        commit_data = request.commit.copy()
        commit_data["repository_id"] = repo_id
        commit_data["branch"] = branch_name

        # Admission control: drop ignored paths and refuse oversized payloads before any
        # of this reaches the object store. Raises 413 when a size limit is exceeded.
        screening = screen_commit_files(
            commit_data,
            fallback_foxignore_text=_branch_foxignore_text(db, actual_head),
        )
        if screening["dropped"]:
            print(
                f"  - Ignored {len(screening['dropped'])} path(s) via .foxignore: "
                + ", ".join(f"{path} ({pattern})" for path, pattern in screening["dropped"][:5])
            )

        # Pre-receive policy. Runs after ignore filtering so a rule never fires on
        # a path that was going to be dropped anyway, and before anything is
        # written so a rejected push leaves no trace.
        try:
            server_hooks.run_pre_receive(db, repository, commit_data, current_user, branch_name)
        except server_hooks.HookRejection as rejection:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "PRE_RECEIVE_REJECTED",
                    "hook": rejection.hook_name,
                    "reasons": rejection.reasons,
                    "message": f"Push rejected by '{rejection.hook_name}': "
                               + "; ".join(rejection.reasons),
                },
            )

        # Check if user role is 'developer' - developers always need approval regardless of ownership or permissions
        # Team leads can push directly
        user_is_developer = user_role == 'developer'
        
        # Debug logging
        print(f"DEBUG - Push request:")
        print(f"  - Branch: {branch_name}")
        print(f"  - Commit author: {author_username}")
        print(f"  - Pusher: {pusher_username}")
        print(f"  - User role: {user_role}")
        print(f"  - Is developer: {user_is_developer}")
        print(f"  - Is owner: {is_owner}")
        
        # If user is a developer, they ALWAYS need approval, regardless of being owner
        if user_is_developer:
            # Developer - create pending commit for approval
            pending_commit = PendingCommitCRUD.create_pending_commit(db, commit_data)
            
            # Get team lead info
            team_lead_name = None
            if user.team_lead:
                team_lead_name = user.team_lead.username
            
            # Create activity
            ActivityCRUD.create_activity(
                db, user.id, "pending_commit", 
                f"Created pending commit: {pending_commit.message[:50]}...", repo_id
            )

            hook_service.dispatch(db, hook_service.EVENT_COMMIT_PENDING, repo_id, {
                "repository": {"id": repo_id, "name": repository.name},
                "branch": branch_name,
                "author": pusher_username,
                "team_lead": team_lead_name,
                "pending_commit": {
                    "id": pending_commit.id,
                    "message": pending_commit.message,
                },
            })

            # Notify reviewer(s) so admins/team leads see it immediately in the bell + counts.
            try:
                _notify_pending_commit_reviewer(
                    db,
                    repository=repository,
                    pending_commit=pending_commit,
                    actor=user,
                    team_lead_name=team_lead_name,
                    branch_name=branch_name,
                )
            except Exception:
                pass

            # Link issue/branch + notify issue creator/assignee/watchers immediately on submission.
            try:
                for ref in parse_issue_references(pending_commit.message or ""):
                    issue = IssueCRUD.get_issue_by_number(db, repo_id, ref["number"])
                    if not issue:
                        continue
                    IssueLinkCRUD.link_branch(db, repo_id, issue.id, branch_name)
                    _notify_issue_commit_submitted(
                        db,
                        repo_id=repo_id,
                        issue=issue,
                        actor=user,
                        pending_commit_id=pending_commit.id,
                        branch_name=branch_name,
                        message=pending_commit.message or "",
                    )
            except Exception:
                # Best-effort notifications; never fail the push because of this.
                pass
            
            return {
                "success": True,
                "commit_id": pending_commit.id,
                "status": "pending_approval",
                "team_lead": team_lead_name,
                "reviewer_hint": team_lead_name or (repository.owner.username if repository and repository.owner else None) or "team lead/admin",
                "ignored_files": [
                    {"path": path, "pattern": pattern} for path, pattern in screening["dropped"]
                ],
                "message": (
                    f"Your {'rollback ' if is_rollback_push else ''}commit has been submitted for review by "
                    f"{team_lead_name or 'the team lead/admin'}."
                )
            }
        else:
            # Team lead or other role - check permissions for direct push
            is_team_lead = UserPermissionCRUD.has_permission(db, pusher_username, repo_id, 'team_lead')
            
            print(f"  - Has team_lead permission: {is_team_lead}")
            
            if not is_team_lead and not is_owner:
                raise HTTPException(
                    status_code=403,
                    detail=f"User '{pusher_username}' does not have team lead permission to push directly."
                )
            
            # Team lead or owner with non-developer role can push directly
            commit = CommitCRUD.create_commit(db, commit_data)

            # Attest what was accepted, and from whom, so later tampering is detectable.
            sign_commit(db, commit, pusher_username, current_user.id)

            # Update branch head (create if new branch is pushed)
            branch = current_branch or BranchCRUD.get_branch(db, repo_id, branch_name)
            if not branch:
                BranchCRUD.create_branch(db, repo_id, branch_name, head_commit_id=commit.id)
            else:
                BranchCRUD.update_branch_head(db, repo_id, branch_name, commit.id)
            
            # Create activity
            ActivityCRUD.create_activity(
                db, user.id, "push_commit", 
                f"Pushed commit to '{branch_name}' from {actual_head or 'None'} to {commit.id}: {commit.message[:50]}...", repo_id
            )

            # Tell any integrations. Dispatch is fire-and-forget on a worker
            # thread, so a slow or dead receiver cannot make this push hang.
            hook_service.dispatch(db, hook_service.EVENT_PUSH, repo_id, {
                "repository": {"id": repo_id, "name": repository.name},
                "branch": branch_name,
                "before": actual_head,
                "after": commit.id,
                "pusher": pusher_username,
                "commit": {
                    "id": commit.id,
                    "message": commit.message,
                    "author": pusher_username,
                    "file_count": screening["kept"],
                },
            })

            _sync_issues_for_new_commit(db, repo_id, commit, branch_name, user.id)
            
            # Handle archiving based on flag
            if request.archive:
                RepositoryCRUD.archive_repository(db, repo_id, reason="Archived via push --archive command")
            
            return {
                "success": True,
                "commit_id": commit.id,
                "status": "merged",
                "ignored_files": [
                    {"path": path, "pattern": pattern} for path, pattern in screening["dropped"]
                ],
            }
    
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        # Log and raise other exceptions
        import traceback
        print(f"Error in push_commit: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/repository/{repo_id}/pull")
async def pull_commits(
    repo_id: str,
    since_commit: Optional[str] = None,
    branch: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pull commits from repository"""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    _require_repository_checkout_access(db, current_user, repository, branch, "pull commits")
    
    try:
        commits = CommitCRUD.get_commits_by_repository(db, repo_id)

        if branch:
            resolved_branch = _resolve_branch(db, repo_id, branch)
            reachable = set(_collect_reachable_commits(db, resolved_branch.head_commit_id))
            commits = [commit for commit in commits if commit.id in reachable]
        
        # Filter commits if since_commit is provided
        if since_commit:
            filtered_commits = []
            for commit in commits:
                if commit.id == since_commit:
                    break
                filtered_commits.append(commit)
            commits = filtered_commits
        
        return {
            "success": True, 
            "commits": [commit_to_dict(commit, include_files=True) for commit in commits],
            "head": repository.head_commit_id if not branch else _resolve_branch(db, repo_id, branch).head_commit_id
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/repository/{repo_id}/commits")
async def get_commits(
    repo_id: str,
    full: bool = False,
    branch: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get commit history"""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_checkout_access(db, current_user, repository, branch, "view commits")
    
    try:
        # No SQL limit: the branch filter and pagination below both need the full
        # history to report an honest total.
        commits = CommitCRUD.get_commits_by_repository(db, repo_id, limit=None)
        if branch:
            resolved_branch = _resolve_branch(db, repo_id, branch)
            reachable = set(_collect_reachable_commits(db, resolved_branch.head_commit_id))
            commits = [commit for commit in commits if commit.id in reachable]
        # Paged after the reachability filter so `total` is the number of
        # commits actually on this branch, not in the whole repository.
        page, meta = paginate_list(commits, limit, offset)
        return {
            "success": True,
            "pagination": meta,
            "commits": [commit_to_dict(commit, include_files=full) for commit in page]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.get("/api/repository/{repo_id}/commits/{commit_id}/verify")
async def verify_commit_signature(
    repo_id: str,
    commit_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check a commit against the attestation the server recorded when it was accepted.

    `valid` means the commit, its message, its author and its complete file list are
    exactly as accepted. `unsigned` means no attestation exists (it predates this feature)
    and is not evidence of tampering.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "verify commits")

    result = verify_commit(db, commit_id)
    if result["status"] == "unknown":
        raise HTTPException(status_code=404, detail=result["detail"])
    return {"success": True, **result}


@router.get("/api/repository/{repo_id}/verify")
async def verify_repository_signatures(
    repo_id: str,
    limit: int = 500,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify every commit in the repository and summarise which, if any, no longer match."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "verify commits")

    return {"success": True, **verify_repository(db, repo_id, limit=max(1, min(limit, 5000)))}
