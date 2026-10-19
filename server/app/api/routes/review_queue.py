"""Approval queues for pending commits and pending repositories."""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import or_
from sqlalchemy.orm import Session, aliased

from database.crud import (
    IssueCRUD, PendingCommitCRUD, PendingRepositoryCRUD, RepositoryCRUD, UserCRUD,
    parse_issue_references,
)
from database.database import get_db
from database.models import PendingCommit, PendingRepository, Repository, User

from app.core.dependencies import require_reviewer_user
from app.core.pagination import page_params, paginate_query, set_page_headers
from app.schemas import ReviewCommitRequest
from app.services.branches import _get_default_branch
from app.services.issues import _sync_issues_for_new_commit
from app.services.notifications import _notify_issue_commit_reviewed
from app.services.signing import sign_commit
from app.services import webhooks as hook_service


from app.services import audit
from app.services import notifications as notify

router = APIRouter()


def _status_filter(query, column, status: Optional[str]):
    """``status`` narrows the queue; empty or ``all`` is the whole history."""
    if status and status.lower() != "all":
        query = query.filter(column == status)
    return query


def _responsible_for(query, team_lead: User):
    """Commits a team lead reviews: from developers assigned to them, or into
    repositories they own (common in the issue-tracking access flow, where the
    developer has no team lead of their own)."""
    author = aliased(User)
    return (query.outerjoin(author, author.id == PendingCommit.author_id)
            .outerjoin(Repository, Repository.id == PendingCommit.repository_id)
            .filter(or_(author.team_lead_id == team_lead.id, Repository.owner_id == team_lead.id)))


def _username(user: Optional[User]) -> Optional[str]:
    return user.username if user is not None else None


@router.get("/api/pending-commits")
async def get_all_pending_commits(
    request: Request,
    response: Response,
    status: str = 'pending',
    team_lead_username: str = None,
    page=Depends(page_params()),
    current_user: User = Depends(require_reviewer_user),
    db: Session = Depends(get_db)
):
    """Pending commits across all repositories, newest first, paged.

    A team lead sees the commits they are responsible for; an administrator sees all,
    or one team lead's with ``team_lead_username``. ``status=all`` includes the reviewed
    history, which is what grows without bound -- hence the paging.
    """
    try:
        query = _status_filter(db.query(PendingCommit), PendingCommit.status, status)

        filter_team_lead_username = team_lead_username
        current_user_role = getattr(current_user, 'role', '').lower()
        if not filter_team_lead_username and current_user_role == 'team_lead':
            filter_team_lead_username = current_user.username
        if filter_team_lead_username:
            team_lead = UserCRUD.get_user_by_username(db, filter_team_lead_username)
            if team_lead:
                query = _responsible_for(query, team_lead)

        pending_commits, meta = paginate_query(
            query.order_by(PendingCommit.created_at.desc(), PendingCommit.id.desc()), *page)
        set_page_headers(request, response, meta)
        return {
            "success": True,
            "pagination": meta,
            "pending_commits": [
                {
                    "id": pc.id,
                    "repository_id": pc.repository_id,
                    "repository_name": pc.repository.name if pc.repository else None,
                    "author": _username(pc.author),
                    "author_full_name": pc.author.full_name if pc.author else None,
                    "team_lead_name": _username(pc.author.team_lead) if pc.author else None,
                    "message": pc.message,
                    "created_at": pc.created_at.isoformat() if pc.created_at else None,
                    "status": pc.status,
                    "reviewed_by": _username(pc.reviewer),
                    "reviewed_at": pc.reviewed_at.isoformat() if pc.reviewed_at else None,
                    "review_comment": pc.review_comment
                }
                for pc in pending_commits
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/repository/{repo_id}/pending-commits")
async def get_repository_pending_commits(
    repo_id: str,
    request: Request,
    response: Response,
    status: str = None,
    page=Depends(page_params()),
    current_user: User = Depends(require_reviewer_user),
    db: Session = Depends(get_db),
):
    """Pending commits for one repository, newest first, paged.

    Scoped like the cross-repository queue: any team lead used to be able to list any
    repository's queue -- authors, messages, review comments -- just by naming it. A
    team lead now sees a repository's whole queue only if they own it, and otherwise
    only the commits of developers assigned to them.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    try:
        query = _status_filter(
            db.query(PendingCommit).filter(PendingCommit.repository_id == repo_id),
            PendingCommit.status, status)
        if (getattr(current_user, "role", "") or "").lower() != "admin" \
                and repository.owner_id != current_user.id:
            author = aliased(User)
            query = (query.join(author, author.id == PendingCommit.author_id)
                     .filter(author.team_lead_id == current_user.id))
        pending_commits, meta = paginate_query(
            query.order_by(PendingCommit.created_at.desc(), PendingCommit.id.desc()), *page)
        set_page_headers(request, response, meta)
        return {
            "success": True,
            "pagination": meta,
            "pending_commits": [
                {
                    "id": pc.id,
                    "author": _username(pc.author),
                    "message": pc.message,
                    "created_at": pc.created_at.isoformat() if pc.created_at else None,
                    "status": pc.status,
                    "reviewed_by": _username(pc.reviewer),
                    "reviewed_at": pc.reviewed_at.isoformat() if pc.reviewed_at else None,
                    "review_comment": pc.review_comment
                }
                for pc in pending_commits
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

def _announce_review(db: Session, pending, reviewer: User, decision: str,
                     comment: Optional[str], commit_id: Optional[str] = None) -> None:
    """Send commit.reviewed. On approval the branch move also sends push, via refs."""
    branch = None
    try:
        raw = json.loads(pending.files_data) if getattr(pending, "files_data", None) else {}
        if isinstance(raw, dict):
            branch = (raw.get("meta") or {}).get("branch")
    except Exception:
        branch = None
    repository = getattr(pending, "repository", None)
    hook_service.dispatch(db, hook_service.EVENT_COMMIT_REVIEWED, pending.repository_id, {
        "repository": {"id": pending.repository_id,
                       "name": getattr(repository, "name", None)},
        "branch": branch,
        "decision": decision,
        "reviewer": reviewer.username,
        "comment": comment,
        "author": getattr(getattr(pending, "author", None), "username", None),
        "commit": {"id": commit_id or pending.id, "message": pending.message},
    })
    subject = ((pending.message or "").splitlines() or [""])[0][:60]
    author = getattr(getattr(pending, "author", None), "username", None) or "someone"
    audit.record(
        db, reviewer.id, "approve_commit" if decision == "approved" else "reject_commit",
        repository_id=pending.repository_id,
        description=f"{decision.capitalize()} {author}'s commit {(commit_id or pending.id)[:8]}"
                    + (f" on '{branch}'" if branch else "") + f": {subject}",
        target_type="commit", target_id=commit_id or pending.id, commit_id=commit_id,
        payload={"pending_commit_id": pending.id, "author": author, "comment": comment},
    )
    notify.commit_reviewed_for_author(db, pending, reviewer, decision, comment, commit_id=commit_id)


@router.post("/api/pending-commits/{commit_id}/review")
async def review_pending_commit(commit_id: str, request: ReviewCommitRequest, current_user: User = Depends(require_reviewer_user), db: Session = Depends(get_db)):
    """Approve or reject a pending commit"""
    try:
        if request.action == "approve":
            pending, commit = PendingCommitCRUD.approve_pending_commit(
                db, commit_id, current_user.username, request.comment
            )
            # A commit only becomes real on approval, so this is where it gets attested.
            # The reviewer is recorded as the pusher: they are the authenticated identity
            # that admitted it to the repository.
            if commit is not None:
                sign_commit(db, commit, current_user.username, current_user.id)
                _announce_review(db, pending, current_user, "approved", request.comment,
                                 commit_id=commit.id)
            try:
                branch_name = None
                if pending and getattr(pending, "files_data", None):
                    try:
                        raw = json.loads(pending.files_data)
                        if isinstance(raw, dict) and "meta" in raw:
                            branch_name = raw.get("meta", {}).get("branch")
                    except Exception:
                        branch_name = None

                _sync_issues_for_new_commit(db, commit.repository_id, commit, branch_name or _get_default_branch(db, commit.repository_id).name, current_user.id)

                # Notify on approval (commit now exists and will show in linked commits)
                for ref in parse_issue_references(commit.message or ""):
                    issue = IssueCRUD.get_issue_by_number(db, commit.repository_id, ref["number"])
                    if not issue:
                        continue
                    _notify_issue_commit_reviewed(
                        db,
                        repo_id=commit.repository_id,
                        issue=issue,
                        reviewer=current_user,
                        commit_id=commit.id,
                        branch_name=branch_name,
                        action="approved",
                        comment=request.comment,
                    )
            except Exception:
                pass
            return {
                "success": True,
                "message": "Commit approved and merged",
                "commit_id": commit.id,
                "status": "approved"
            }
        elif request.action == "reject":
            pending = PendingCommitCRUD.reject_pending_commit(
                db, commit_id, current_user.username, request.comment
            )
            _announce_review(db, pending, current_user, "rejected", request.comment)
            try:
                branch_name = None
                if pending and getattr(pending, "files_data", None):
                    try:
                        raw = json.loads(pending.files_data)
                        if isinstance(raw, dict) and "meta" in raw:
                            branch_name = raw.get("meta", {}).get("branch")
                    except Exception:
                        branch_name = None
                # Notify issue participants that the submitted commit was rejected.
                for ref in parse_issue_references(getattr(pending, "message", "") or ""):
                    issue = IssueCRUD.get_issue_by_number(db, pending.repository_id, ref["number"])
                    if not issue:
                        continue
                    _notify_issue_commit_reviewed(
                        db,
                        repo_id=pending.repository_id,
                        issue=issue,
                        reviewer=current_user,
                        commit_id=pending.id,
                        branch_name=branch_name,
                        action="rejected",
                        comment=request.comment,
                    )
            except Exception:
                pass
            return {
                "success": True,
                "message": "Commit rejected",
                "status": "rejected"
            }
        else:
            raise HTTPException(status_code=400, detail="Invalid action. Use 'approve' or 'reject'")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/pending-repositories")
async def get_all_pending_repositories(
    request: Request,
    response: Response,
    status: str = 'pending',
    team_lead_username: str = None,
    page=Depends(page_params()),
    current_user: User = Depends(require_reviewer_user),
    db: Session = Depends(get_db)
):
    """Pending repository requests, newest first, paged; a team lead sees the ones
    that would be theirs."""
    try:
        filter_team_lead_username = team_lead_username
        current_user_role = getattr(current_user, 'role', '').lower()
        if not filter_team_lead_username and current_user_role == 'team_lead':
            filter_team_lead_username = current_user.username

        query = _status_filter(db.query(PendingRepository), PendingRepository.status, status)
        if filter_team_lead_username:
            team_lead = UserCRUD.get_user_by_username(db, filter_team_lead_username)
            # An unknown team lead owns nothing (as before: an empty list, not everything).
            query = query.filter(PendingRepository.owner_id == (team_lead.id if team_lead else -1))

        pending_repos, meta = paginate_query(
            query.order_by(PendingRepository.created_at.desc(), PendingRepository.id.desc()), *page)
        set_page_headers(request, response, meta)
        return {
            "success": True,
            "pagination": meta,
            "pending_repositories": [
                {
                    "id": pr.id,
                    "repo_name": pr.repo_name,
                    "description": pr.description,
                    "requested_by": _username(pr.requested_by),
                    "requested_by_full_name": pr.requested_by.full_name if pr.requested_by else None,
                    "owner": _username(pr.owner),
                    "owner_full_name": pr.owner.full_name if pr.owner else None,
                    "created_at": pr.created_at.isoformat() if pr.created_at else None,
                    "status": pr.status,
                    "reviewed_by": _username(pr.reviewer),
                    "reviewed_at": pr.reviewed_at.isoformat() if pr.reviewed_at else None,
                    "review_comment": pr.review_comment
                }
                for pr in pending_repos
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.post("/api/pending-repositories/{pending_id}/review")
async def review_pending_repository(pending_id: int, request: ReviewCommitRequest, current_user: User = Depends(require_reviewer_user), db: Session = Depends(get_db)):
    """Approve or reject a pending repository"""
    try:
        if request.action == "approve":
            pending, repository = PendingRepositoryCRUD.approve_pending_repository(
                db, pending_id, current_user.username, request.comment
            )
            return {
                "success": True,
                "message": "Repository approved and created",
                "repo_id": repository.id,
                "status": "approved"
            }
        elif request.action == "reject":
            pending = PendingRepositoryCRUD.reject_pending_repository(
                db, pending_id, current_user.username, request.comment
            )
            return {
                "success": True,
                "message": "Repository request rejected",
                "status": "rejected"
            }
        else:
            raise HTTPException(status_code=400, detail="Invalid action. Use 'approve' or 'reject'")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
