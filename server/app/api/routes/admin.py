"""Administrative dashboards, registration review and sample data seeding."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.crud import (
    ActivityCRUD, IssueAccessRequestCRUD, PendingCommitCRUD, PendingRepositoryCRUD,
    PendingUserRegistrationCRUD, RepositoryCRUD, UserCRUD,
)
from database.database import get_db
from database.models import Repository, User

from app.core.dependencies import require_admin_user, require_reviewer_user
from app.core.security import hash_password
from app.schemas import ReviewRegistrationRequest
from app.services import password_reset


router = APIRouter()


@router.post("/api/admin/users/{username}/reset-password/request-otp")
async def request_password_reset_otp(
    username: str,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Email a six-digit reset code to the account's own address.

    The code goes to the user, never back in the response, so an admin cannot reset an
    account without the account holder seeing it happen.
    """
    user = UserCRUD.get_user_by_username(db, username)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    try:
        record, masked = password_reset.request_code(db, user, requested_by=current_user)
    except password_reset.PasswordResetError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    ActivityCRUD.create_activity(
        db,
        user_id=current_user.id,
        activity_type="password_reset_otp_sent",
        description=f"Sent a password reset code to {username}",
    )

    return {
        "success": True,
        "message": f"A 6-digit code was emailed to {masked}. It expires in "
                   f"{max(1, password_reset.OTP_TTL_SECONDS // 60)} minutes.",
        "username": username,
        "sent_to": masked,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
    }


@router.post("/api/admin/users/{username}/reset-password")
async def reset_password_with_otp(
    username: str,
    request: dict,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Set a new password once the emailed code has been supplied."""
    new_password = (request or {}).get("new_password") or ""
    code = (request or {}).get("otp") or (request or {}).get("code") or ""

    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    user = UserCRUD.get_user_by_username(db, username)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")

    try:
        password_reset.verify_and_consume(db, user, code)
    except password_reset.PasswordResetError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    user.password_hash = hash_password(new_password)
    user.updated_at = datetime.now()
    db.commit()

    ActivityCRUD.create_activity(
        db,
        user_id=current_user.id,
        activity_type="password_reset",
        description=f"Reset password for user: {username} (verified by emailed code)",
    )

    return {
        "success": True,
        "message": f"Password reset successfully for '{username}'. "
                   f"Any existing sessions remain valid until they expire.",
        "username": username,
    }


@router.get("/api/admin/pending-counts")
async def get_pending_counts(
    current_user: User = Depends(require_reviewer_user),
    db: Session = Depends(get_db),
):
    """Counts for sidebar/dashboard badges (pending commits/repos/users + access requests)."""
    role = (getattr(current_user, "role", "") or "").lower()

    # Pending commits
    pending_commits = PendingCommitCRUD.get_all_pending_commits(db, status="pending")
    if role == "team_lead":
        # Same policy as /api/pending-commits: team leads see their developers + repos they own.
        pending_commits = [
            pc
            for pc in pending_commits
            if (
                (pc.author and pc.author.team_lead_id == current_user.id)
                or (pc.repository and pc.repository.owner_id == current_user.id)
            )
        ]
    pending_commits_count = len(pending_commits)

    # Access requests
    try:
        if role == "admin":
            access_requests_count = len(IssueAccessRequestCRUD.list_pending_requests(db, repo_id=None, status="pending", limit=1000))
        elif role == "team_lead":
            from database.models import IssueAccessRequest
            access_requests_count = int(
                db.query(IssueAccessRequest)
                .join(User, User.id == IssueAccessRequest.requested_by_id)
                .filter(IssueAccessRequest.status == "pending", User.team_lead_id == current_user.id)
                .count()
            )
        else:
            user_repos = db.query(Repository).filter(Repository.owner_id == current_user.id).all()
            repo_ids = [r.id for r in user_repos]
            access_requests_count = 0
            for rid in repo_ids:
                access_requests_count += len(IssueAccessRequestCRUD.list_pending_requests(db, repo_id=rid, status="pending"))
    except Exception:
        access_requests_count = 0

    # Pending repositories (admin sees all; team_lead sees theirs)
    try:
        pending_repos = PendingRepositoryCRUD.get_all_pending_repositories(db, status="pending")
        if role == "team_lead":
            pending_repos = [pr for pr in pending_repos if pr.team_lead and pr.team_lead.username == current_user.username]
        pending_repos_count = len(pending_repos)
    except Exception:
        pending_repos_count = 0

    # Pending user registrations (admin only)
    pending_users_count = 0
    if role == "admin":
        try:
            pending_users_count = len(PendingUserRegistrationCRUD.list_requests(db, status="pending"))
        except Exception:
            pending_users_count = 0

    return {
        "success": True,
        "counts": {
            "pending_commits": pending_commits_count,
            "access_requests": access_requests_count,
            "pending_repositories": pending_repos_count,
            "pending_users": pending_users_count,
        },
    }

@router.get("/api/admin/pending-user-registrations")
async def list_pending_user_registrations(
    status: str = 'pending',
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db)
):
    requests = PendingUserRegistrationCRUD.list_requests(db, status)
    return {
        "success": True,
        "pending_registrations": [
            {
                "id": req.id,
                "username": req.username,
                "email": req.email,
                "full_name": req.full_name,
                "requested_role": req.requested_role,
                "requested_team_lead_username": req.requested_team_lead_username,
                "status": req.status,
                "review_comment": req.review_comment,
                "reviewed_by": req.reviewer.username if req.reviewer else None,
                "reviewed_at": req.reviewed_at.isoformat() if req.reviewed_at else None,
                "created_at": req.created_at.isoformat() if req.created_at else None,
            }
            for req in requests
        ]
    }

@router.post("/api/admin/pending-user-registrations/{request_id}/review")
async def review_pending_user_registration(
    request_id: int,
    request: ReviewRegistrationRequest,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db)
):
    pending = PendingUserRegistrationCRUD.get_by_id(db, request_id)
    if not pending:
        raise HTTPException(status_code=404, detail="Pending registration not found")

    if pending.status != 'pending':
        raise HTTPException(status_code=400, detail=f"Request is already {pending.status}")

    action = (request.action or '').strip().lower()
    if action not in ['approve', 'reject']:
        raise HTTPException(status_code=400, detail="action must be approve or reject")

    if action == 'reject':
        pending.status = 'rejected'
        pending.review_comment = request.comment
        pending.reviewed_by_id = current_user.id
        pending.reviewed_at = datetime.utcnow()
        db.commit()
        return {"success": True, "status": "rejected", "message": "Registration request rejected"}

    if UserCRUD.get_user_by_username(db, pending.username):
        raise HTTPException(status_code=400, detail="Username already exists")

    final_role = (request.role or pending.requested_role or 'developer').strip().lower()
    if final_role not in ['developer', 'team_lead']:
        raise HTTPException(status_code=400, detail="Role must be developer or team_lead")

    team_lead_id = request.team_lead_id
    if final_role == 'developer' and team_lead_id is None and pending.requested_team_lead_username:
        team_lead = UserCRUD.get_user_by_username(db, pending.requested_team_lead_username)
        if team_lead and team_lead.role == 'team_lead':
            team_lead_id = team_lead.id

    if team_lead_id is not None:
        team_lead = UserCRUD.get_user_by_id(db, team_lead_id)
        if not team_lead or team_lead.role != 'team_lead':
            raise HTTPException(status_code=400, detail="Selected team lead is invalid")

    user = UserCRUD.create_user(
        db,
        username=pending.username,
        email=pending.email,
        full_name=pending.full_name,
        role=final_role,
        team_lead_id=team_lead_id,
        password_hash=pending.password_hash
    )

    pending.status = 'approved'
    pending.review_comment = request.comment
    pending.reviewed_by_id = current_user.id
    pending.reviewed_at = datetime.utcnow()
    db.commit()

    return {
        "success": True,
        "status": "approved",
        "message": "Registration request approved and user created",
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "team_lead_id": user.team_lead_id,
        }
    }


@router.post("/api/admin/create-sample-data")
async def create_sample_data(current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Create sample repositories for testing (development only)"""
    try:
        sample_repos = [
            {"username": "john_doe", "repo_name": "legacy-system", "description": "Legacy system components and utilities"},
            {"username": "jane_smith", "repo_name": "old-mobile-prototype", "description": "Initial mobile app prototype"},
            {"username": "mike_wilson", "repo_name": "experimental-ui", "description": "Experimental UI components library"},
            {"username": "sarah_connor", "repo_name": "temp-data-migration", "description": "Temporary scripts for data migration"},
        ]
        
        created_repos = []
        for repo_data in sample_repos:
            try:
                repository = RepositoryCRUD.create_repository(
                    db, repo_data["username"], repo_data["repo_name"], repo_data["description"]
                )
                created_repos.append(repository.id)
            except ValueError:
                # Repository already exists, skip
                repo_id = RepositoryCRUD.generate_repo_id(repo_data["username"], repo_data["repo_name"])
                created_repos.append(repo_id)
        
        return {"success": True, "created_repositories": created_repos}
    
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/api/admin/gc")
async def inspect_garbage(
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Report what a garbage collection sweep would reclaim. Changes nothing."""
    from app.services import gc as gc_service

    return {"success": True, **gc_service.analyze(db)}


@router.post("/api/admin/gc")
async def run_garbage_collection(
    dry_run: bool = True,
    limit: Optional[int] = None,
    prune_dangling_commits: bool = False,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Delete unreferenced blobs.

    Defaults to a dry run: pass ``dry_run=false`` to actually delete. Dangling commits
    are left alone unless ``prune_dangling_commits=true``, because removing them rewrites
    history rather than just reclaiming space.
    """
    from app.services import gc as gc_service

    report = gc_service.collect(
        db,
        dry_run=dry_run,
        limit=limit,
        prune_dangling_commits=prune_dangling_commits,
    )

    if not dry_run and (report["deleted_objects"] or report["deleted_commits"]):
        ActivityCRUD.create_activity(
            db,
            current_user.id,
            "gc",
            (
                f"Garbage collection removed {report['deleted_objects']} object(s), "
                f"{report['deleted_bytes']} bytes"
                + (f" and {report['deleted_commits']} dangling commit(s)" if report["deleted_commits"] else "")
            ),
            None,
        )

    return {"success": True, **report}
