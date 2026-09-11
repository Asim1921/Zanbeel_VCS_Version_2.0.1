"""Approval queues for pending commits and pending repositories."""

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.crud import (
    IssueCRUD, PendingCommitCRUD, PendingRepositoryCRUD, UserCRUD, parse_issue_references,
)
from database.database import get_db
from database.models import User

from app.core.dependencies import require_reviewer_user
from app.schemas import ReviewCommitRequest
from app.services.branches import _get_default_branch
from app.services.issues import _sync_issues_for_new_commit
from app.services.notifications import _notify_issue_commit_reviewed
from app.services.signing import sign_commit


router = APIRouter()


@router.get("/api/pending-commits")
async def get_all_pending_commits(
    status: str = 'pending', 
    team_lead_username: str = None,
    current_user: User = Depends(require_reviewer_user),
    db: Session = Depends(get_db)
):
    """Get all pending commits across all repositories, optionally filtered by team lead"""
    try:
        pending_commits = PendingCommitCRUD.get_all_pending_commits(db, status)
        
        # Determine which team lead to filter by
        filter_team_lead_username = team_lead_username
        
        # If no specific team lead is requested and current user is a team lead,
        # filter to commits they are responsible for:
        # - commits from developers assigned to them, OR
        # - commits targeting repositories they own (common in issue-tracking access flow)
        current_user_role = getattr(current_user, 'role', '').lower()
        if not filter_team_lead_username and current_user_role == 'team_lead':
            filter_team_lead_username = current_user.username
        
        # Filter by team lead if specified or auto-determined
        if filter_team_lead_username:
            team_lead = UserCRUD.get_user_by_username(db, filter_team_lead_username)
            if team_lead:
                # Team lead can review:
                # - commits from their assigned developers
                # - commits into repositories they own (even if developer has no team_lead_id)
                pending_commits = [
                    pc for pc in pending_commits 
                    if (
                        (pc.author and pc.author.team_lead_id == team_lead.id)
                        or (pc.repository and pc.repository.owner_id == team_lead.id)
                    )
                ]
        
        return {
            "success": True,
            "pending_commits": [
                {
                    "id": pc.id,
                    "repository_id": pc.repository.id,
                    "repository_name": pc.repository.name,
                    "author": pc.author.username,
                    "author_full_name": pc.author.full_name,
                    "team_lead_name": pc.author.team_lead.username if pc.author.team_lead else None,
                    "message": pc.message,
                    "created_at": pc.created_at.isoformat() if pc.created_at else None,
                    "status": pc.status,
                    "reviewed_by": pc.reviewer.username if pc.reviewer else None,
                    "reviewed_at": pc.reviewed_at.isoformat() if pc.reviewed_at else None,
                    "review_comment": pc.review_comment
                }
                for pc in pending_commits
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/repository/{repo_id}/pending-commits")
async def get_repository_pending_commits(repo_id: str, status: str = None, current_user: User = Depends(require_reviewer_user), db: Session = Depends(get_db)):
    """Get pending commits for a specific repository"""
    try:
        pending_commits = PendingCommitCRUD.get_pending_commits_by_repository(db, repo_id, status)
        return {
            "success": True,
            "pending_commits": [
                {
                    "id": pc.id,
                    "author": pc.author.username,
                    "message": pc.message,
                    "created_at": pc.created_at.isoformat() if pc.created_at else None,
                    "status": pc.status,
                    "reviewed_by": pc.reviewer.username if pc.reviewer else None,
                    "reviewed_at": pc.reviewed_at.isoformat() if pc.reviewed_at else None,
                    "review_comment": pc.review_comment
                }
                for pc in pending_commits
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

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
    status: str = 'pending', 
    team_lead_username: str = None,
    current_user: User = Depends(require_reviewer_user),
    db: Session = Depends(get_db)
):
    """Get all pending repository requests, optionally filtered by team lead"""
    try:
        # Determine which team lead to filter by
        filter_team_lead_username = team_lead_username
        
        # If no specific team lead is requested and current user is NOT admin,
        # automatically filter to show only their developers' requests
        current_user_role = getattr(current_user, 'role', '').lower()
        if not filter_team_lead_username and current_user_role == 'team_lead':
            filter_team_lead_username = current_user.username
        
        if filter_team_lead_username:
            pending_repos = PendingRepositoryCRUD.get_pending_repositories_by_team_lead(db, filter_team_lead_username, status)
        else:
            pending_repos = PendingRepositoryCRUD.get_all_pending_repositories(db, status)
        
        return {
            "success": True,
            "pending_repositories": [
                {
                    "id": pr.id,
                    "repo_name": pr.repo_name,
                    "description": pr.description,
                    "requested_by": pr.requested_by.username,
                    "requested_by_full_name": pr.requested_by.full_name,
                    "owner": pr.owner.username,
                    "owner_full_name": pr.owner.full_name,
                    "created_at": pr.created_at.isoformat() if pr.created_at else None,
                    "status": pr.status,
                    "reviewed_by": pr.reviewer.username if pr.reviewer else None,
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
