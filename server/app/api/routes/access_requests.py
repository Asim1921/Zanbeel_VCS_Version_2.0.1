"""Review queue for issue-scoped repository access requests.

Declaration order matters: /api/access-requests/pending and
/api/access-requests/my-requests must stay ahead of /{request_id}/..."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import desc
from sqlalchemy.orm import Session

from database.crud import IssueAccessRequestCRUD, NotificationCRUD, RepositoryCRUD
from database.database import get_db
from database.models import IssueAccessRequest, Repository, User

from app.core.dependencies import get_current_user
from app.core.pagination import page_params, paginate_query, set_page_headers
from app.services.notifications import _notify_user_access_approved


router = APIRouter()


@router.get("/api/access-requests/pending")
async def list_pending_access_requests(
    request: Request,
    response: Response,
    repo_id: Optional[str] = None,
    status: Optional[str] = "pending",
    page=Depends(page_params()),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List access requests for repository owner, newest first, paged.

    status can be: pending (default), approved, denied, or all (no filter). This used to
    stop silently at 100 requests per repository and 1000 server-wide; a reviewer now
    sees the true total and pages through it.
    """
    try:
        status_filter = None if (status or "").lower() == "all" else status
        role = (getattr(current_user, "role", "") or "").lower()
        query = db.query(IssueAccessRequest)
        if status_filter:
            query = query.filter(IssueAccessRequest.status == status_filter)
        if repo_id:
            repository = RepositoryCRUD.get_repository(db, repo_id)
            if not repository:
                raise HTTPException(status_code=404, detail="Repository not found")
            if repository.owner_id != current_user.id and role not in {"admin", "team_lead"}:
                raise HTTPException(status_code=403, detail="Only repository owner can view access requests")
            query = query.filter(IssueAccessRequest.repository_id == repo_id)
        elif role == "admin":
            pass  # Admin can view platform-wide.
        elif role == "team_lead":
            # Team leads see requests created by their developers (requested_by.team_lead_id = me).
            query = (query.join(User, User.id == IssueAccessRequest.requested_by_id)
                     .filter(User.team_lead_id == current_user.id))
        else:
            # Non-privileged users: only across repositories they own.
            owned = db.query(Repository.id).filter(Repository.owner_id == current_user.id)
            query = query.filter(IssueAccessRequest.repository_id.in_(owned))

        requests, meta = paginate_query(
            query.order_by(desc(IssueAccessRequest.created_at), desc(IssueAccessRequest.id)), *page)
        set_page_headers(request, response, meta)
        return {
            "success": True,
            "pagination": meta,
            "access_requests": [
                {
                    "id": req.id,
                    "issue_number": req.issue.number if req.issue else None,
                    "issue_title": req.issue.title if req.issue else None,
                    "issue_description": ((req.issue.description or "").strip() or None) if req.issue else None,
                    "repository_id": req.repository_id,
                    "repository_name": req.repository.name if req.repository else None,
                    "requested_user": req.requested_user.username if req.requested_user else None,
                    "requested_by": req.requested_by.username if req.requested_by else None,
                    "status": req.status,
                    # keep both keys for frontend/backward compatibility
                    "request_reason": req.request_reason,
                    "reason": req.request_reason,
                    "created_at": req.created_at.isoformat() if req.created_at else None,
                }
                for req in requests
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/access-requests/my-requests")
async def list_my_access_requests(
    request: Request,
    response: Response,
    status: Optional[str] = None,
    page=Depends(page_params()),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List access requests for the current user, newest first, paged."""
    try:
        query = db.query(IssueAccessRequest).filter(IssueAccessRequest.requested_user_id == current_user.id)
        if status and status.lower() != "all":
            query = query.filter(IssueAccessRequest.status == status)
        requests, meta = paginate_query(
            query.order_by(desc(IssueAccessRequest.created_at), desc(IssueAccessRequest.id)), *page)
        set_page_headers(request, response, meta)

        return {
            "success": True,
            "pagination": meta,
            "access_requests": [
                {
                    "id": req.id,
                    "issue_number": req.issue.number if req.issue else None,
                    "issue_title": req.issue.title if req.issue else None,
                    "repository_id": req.repository_id,
                    "repository_name": req.repository.name if req.repository else None,
                    "status": req.status,
                    "created_at": req.created_at.isoformat() if req.created_at else None,
                    "reviewed_at": req.reviewed_at.isoformat() if req.reviewed_at else None,
                    "admin_comment": req.review_comment,
                }
                for req in requests
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/access-requests/{request_id}/approve")
async def approve_access_request(
    request_id: int,
    approval_request: Optional[dict] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Approve an access request and grant write permission"""
    try:
        access_req = IssueAccessRequestCRUD.get_request(db, request_id)
        if not access_req:
            raise HTTPException(status_code=404, detail="Access request not found")
        
        repository = access_req.repository
        role = (getattr(current_user, "role", "") or "").lower()
        if repository.owner_id != current_user.id and role not in {"admin", "team_lead"}:
            raise HTTPException(status_code=403, detail="Only repository owner/admin/team lead can approve access requests")
        
        if access_req.status != "pending":
            raise HTTPException(status_code=400, detail=f"Request already {access_req.status}")
        
        comment = approval_request.get("comment", "Approved") if approval_request else "Approved"
        
        perm = IssueAccessRequestCRUD.approve_request(db, request_id, current_user.id, comment)
        
        # Notify the user about approval with CLI setup guide
        _notify_user_access_approved(db, access_req, repository)
        
        return {
            "success": True,
            "message": f"Access granted to @{access_req.requested_user.username}",
            "permission": {
                "user": access_req.requested_user.username,
                "repository": repository.name,
                "permission_level": perm.permission_level,
                "granted_at": perm.granted_at.isoformat() if perm.granted_at else None,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/access-requests/{request_id}/deny")
async def deny_access_request(
    request_id: int,
    denial_request: Optional[dict] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Deny an access request"""
    try:
        access_req = IssueAccessRequestCRUD.get_request(db, request_id)
        if not access_req:
            raise HTTPException(status_code=404, detail="Access request not found")
        
        repository = access_req.repository
        role = (getattr(current_user, "role", "") or "").lower()
        if repository.owner_id != current_user.id and role not in {"admin", "team_lead"}:
            raise HTTPException(status_code=403, detail="Only repository owner/admin/team lead can deny access requests")
        
        if access_req.status != "pending":
            raise HTTPException(status_code=400, detail=f"Request already {access_req.status}")
        
        comment = denial_request.get("comment", "Request denied") if denial_request else "Request denied"
        
        req = IssueAccessRequestCRUD.deny_request(db, request_id, current_user.id, comment)
        
        # Notify the user about denial
        NotificationCRUD.create_notification(
            db,
            user_id=access_req.requested_user_id,
            notification_type="issue_access_request_denied",
            title="Access Request Denied",
            body=f"Your access request for '{repository.name}' was denied. Reason: {comment}",
            payload={
                "issue_number": access_req.issue.number if access_req.issue else None,
                "repository_id": repository.id,
                "repository_name": repository.name,
                "reason": comment,
            },
            dedupe_key=f"access_req_denied_{request_id}"
        )
        
        return {
            "success": True,
            "message": f"Access request denied",
            "request_status": {
                "user": access_req.requested_user.username,
                "repository": repository.name,
                "status": req.status,
                "reason": comment,
                "denied_at": req.reviewed_at.isoformat() if req.reviewed_at else None,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
