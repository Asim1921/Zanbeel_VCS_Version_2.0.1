"""Issue tracking: issues, comments, watchers, labels and milestones."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.crud import (
    IssueCRUD, IssueLabelCRUD, MilestoneCRUD, NotificationCRUD, RepositoryCRUD, UserCRUD,
)
from database.database import get_db
from database.models import IssueWatcher, User

from app.core.dependencies import get_current_user
from app.core.permissions import (
    _user_can_view_issues, can_manage_repository, require_repository_access,
)
from app.schemas import (
    IssueCommentCreateRequest, IssueCreateRequest, IssueLabelUpsertRequest, IssueUpdateRequest,
    IssueWatchRequest, MilestoneCreateRequest,
)
from app.services.access_requests import (
    _access_request_reason_text, _handle_issue_access_requests,
    _maybe_create_issue_access_request_for_user,
)
from app.services.issues import (
    _issue_permissions_for_actor, _issue_to_detail_dict, _issue_to_list_dict,
)
from app.services.notifications import _maybe_notify_issue_mentions


router = APIRouter()


@router.get("/api/repository/{repo_id}/issues")
async def list_issues(
    repo_id: str,
    status: Optional[str] = None,
    search: Optional[str] = None,
    label: Optional[str] = None,
    milestone_id: Optional[int] = None,
    assignee: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not _user_can_view_issues(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Not allowed to view issues for this repository")

    items, total = IssueCRUD.list_issues(
        db,
        repo_id,
        status=status or None,
        search=search or None,
        milestone_id=milestone_id,
        label=label or None,
        assignee_username=assignee or None,
        limit=min(max(limit, 1), 200),
        offset=max(offset, 0),
        include_total=True,
    )
    return {
        "success": True,
        "issues": [_issue_to_list_dict(db, i) for i in items],
        "total": total,
    }


@router.post("/api/repository/{repo_id}/issues")
async def create_issue(
    repo_id: str,
    request: IssueCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository, "create issues in", required_scope="write")

    assigned_to_id = None
    assigned_user = None
    if request.assigned_to and request.assigned_to.strip():
        assignee_username = request.assigned_to.strip().lstrip('@')  # Remove @ prefix if present
        assignee_user = UserCRUD.get_user_by_username(db, assignee_username)
        if not assignee_user:
            raise HTTPException(status_code=400, detail=f"Assignee user '{assignee_username}' not found")
        assigned_to_id = assignee_user.id
        assigned_user = assignee_user

    milestone_id = request.milestone_id
    if milestone_id is not None:
        ms = MilestoneCRUD.get_milestone(db, repo_id, milestone_id)
        if not ms:
            raise HTTPException(status_code=400, detail="Milestone not found for this repository")

    issue = IssueCRUD.create_issue(
        db,
        repo_id,
        request.title,
        request.description or "",
        request.issue_type,
        request.priority,
        current_user.id,
        assigned_to_id=assigned_to_id,
        milestone_id=milestone_id,
        labels=request.labels or [],
    )
    if request.description:
        _maybe_notify_issue_mentions(db, request.description, current_user, issue, "issue description")
        
        # Handle access requests for mentioned users
        _handle_issue_access_requests(db, issue, current_user, repository, request.description)

    # Handle access requests for assigned users without repo access
    if assigned_user:
        assign_reason = _access_request_reason_text(
            issue,
            None,
            fallback=f"Assigned to issue #{issue.number}: {issue.title}",
        )
        _maybe_create_issue_access_request_for_user(
            db,
            issue,
            current_user,
            repository,
            assigned_user,
            assign_reason,
        )

    # Notify assignee
    if assigned_to_id:
        NotificationCRUD.create_notification(
            db,
            user_id=assigned_to_id,
            notification_type="issue_assigned",
            title=f"Assigned to issue #{issue.number}",
            body=f"@{current_user.username} assigned you to: {issue.title}",
            payload={"repository_id": repo_id, "issue_number": issue.number},
            dedupe_key=f"issue_assigned_{issue.id}_{assigned_to_id}",
        )

    return {"success": True, "issue": _issue_to_list_dict(db, issue)}


@router.get("/api/repository/{repo_id}/issues/{issue_number:int}")
async def get_issue_detail(
    repo_id: str,
    issue_number: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not _user_can_view_issues(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Not allowed to view issues for this repository")

    issue = IssueCRUD.get_issue_by_number(db, repo_id, issue_number)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")
    payload = _issue_to_detail_dict(db, repo_id, issue)
    payload["permissions"] = _issue_permissions_for_actor(db, current_user, repository, issue)
    return {"success": True, "issue": payload}


@router.put("/api/repository/{repo_id}/issues/{issue_number:int}")
async def update_issue(
    repo_id: str,
    issue_number: int,
    request: IssueUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository, "update issues in", required_scope="write")

    issue = IssueCRUD.get_issue_by_number(db, repo_id, issue_number)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    if request.status is not None:
        # Restrict "close/reopen" to repository owner/admins (manage-level).
        # Assignees/creators can move between open/in_progress/resolved only.
        ns = (request.status or "").strip().lower()
        if ns == "open" and not can_manage_repository(db, current_user, repository):
            raise HTTPException(status_code=403, detail="Only repository owner/admin can reopen issues")
        if ns == "closed":
            is_creator = bool(issue.created_by_id and current_user and current_user.id == issue.created_by_id)
            if not (can_manage_repository(db, current_user, repository) or is_creator):
                raise HTTPException(status_code=403, detail="Only repository owner/admin or issue creator can close issues")
        try:
            IssueCRUD.transition_status(db, issue, request.status, actor_id=current_user.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.refresh(issue)

    if request.title is not None:
        issue.title = request.title.strip() or issue.title
    if request.description is not None:
        issue.description = request.description
    if request.priority is not None:
        issue.priority = request.priority.strip().lower()
    if request.issue_type is not None:
        issue.issue_type = request.issue_type.strip().lower()

    assignee_user = None
    if request.assigned_to is not None:
        if request.assigned_to.strip() == "":
            IssueCRUD.set_assignee(db, issue, None, actor_id=current_user.id)
        else:
            assignee_user = UserCRUD.get_user_by_username(db, request.assigned_to.strip())
            if not assignee_user:
                raise HTTPException(status_code=400, detail=f"Assignee user '{request.assigned_to.strip()}' not found")
            IssueCRUD.set_assignee(db, issue, assignee_user.id, actor_id=current_user.id)

    if request.milestone_id is not None:
        if request.milestone_id == -1 or request.milestone_id == 0:
            issue.milestone_id = None
        else:
            ms = MilestoneCRUD.get_milestone(db, repo_id, request.milestone_id)
            if not ms:
                raise HTTPException(status_code=400, detail="Milestone not found for this repository")
            issue.milestone_id = ms.id

    db.commit()
    db.refresh(issue)

    if request.description is not None:
        _maybe_notify_issue_mentions(db, request.description, current_user, issue, "issue description")
        _handle_issue_access_requests(db, issue, current_user, repository, request.description)

    if assignee_user:
        assign_reason = _access_request_reason_text(
            issue,
            None,
            fallback=f"Assigned to issue #{issue.number}: {issue.title}",
        )
        _maybe_create_issue_access_request_for_user(
            db,
            issue,
            current_user,
            repository,
            assignee_user,
            assign_reason,
        )

    out = _issue_to_detail_dict(db, repo_id, issue)
    out["permissions"] = _issue_permissions_for_actor(db, current_user, repository, issue)
    return {"success": True, "issue": out}


@router.post("/api/repository/{repo_id}/issues/{issue_number:int}/comments")
async def add_issue_comment(
    repo_id: str,
    issue_number: int,
    request: IssueCommentCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository, "comment on issues in", required_scope="write")

    issue = IssueCRUD.get_issue_by_number(db, repo_id, issue_number)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    body = (request.body or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="Comment body is required")

    IssueCRUD.add_comment(db, issue, current_user.id, body)
    _maybe_notify_issue_mentions(db, body, current_user, issue, "issue comment")
    
    # Notify issue creator if not the commenter
    if issue.created_by_id != current_user.id:
        NotificationCRUD.create_notification(
            db,
            user_id=issue.created_by_id,
            notification_type="issue_comment",
            title=f"New comment on issue #{issue.number}",
            body=f"@{current_user.username} commented: {body[:100]}...",
            payload={"repository_id": repo_id, "issue_number": issue.number},
            dedupe_key=None,
        )
    
    # Notify assignee if not the commenter
    if issue.assigned_to_id and issue.assigned_to_id != current_user.id:
        NotificationCRUD.create_notification(
            db,
            user_id=issue.assigned_to_id,
            notification_type="issue_comment",
            title=f"New comment on issue #{issue.number}",
            body=f"@{current_user.username} commented: {body[:100]}...",
            payload={"repository_id": repo_id, "issue_number": issue.number},
            dedupe_key=None,
        )
    
    db.refresh(issue)
    out = _issue_to_detail_dict(db, repo_id, issue)
    out["permissions"] = _issue_permissions_for_actor(db, current_user, repository, issue)
    return {"success": True, "issue": out}


@router.post("/api/repository/{repo_id}/issues/{issue_number:int}/watch")
async def watch_issue(
    repo_id: str,
    issue_number: int,
    request: IssueWatchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not _user_can_view_issues(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Not allowed to watch issues for this repository")

    issue = IssueCRUD.get_issue_by_number(db, repo_id, issue_number)
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    existing = db.query(IssueWatcher).filter(
        IssueWatcher.issue_id == issue.id,
        IssueWatcher.user_id == current_user.id,
    ).first()
    if request.watch:
        if not existing:
            db.add(IssueWatcher(issue_id=issue.id, user_id=current_user.id))
            db.commit()
    else:
        if existing:
            db.delete(existing)
            db.commit()

    return {"success": True}


@router.get("/api/repository/{repo_id}/issue-labels")
async def list_issue_labels(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not _user_can_view_issues(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Not allowed to view labels for this repository")

    labels = IssueLabelCRUD.list_labels(db, repo_id)
    return {
        "success": True,
        "labels": [{"id": lbl.id, "name": lbl.name, "color": lbl.color} for lbl in labels],
    }


@router.post("/api/repository/{repo_id}/issue-labels")
async def upsert_issue_label(
    repo_id: str,
    request: IssueLabelUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository, "manage issue labels in", required_scope="write")

    lbl = IssueLabelCRUD.upsert_label(db, repo_id, request.name.strip(), request.color)
    return {"success": True, "label": {"id": lbl.id, "name": lbl.name, "color": lbl.color}}


@router.get("/api/repository/{repo_id}/milestones")
async def list_milestones(
    repo_id: str,
    include_closed: bool = True,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not _user_can_view_issues(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Not allowed to view milestones for this repository")

    milestones = MilestoneCRUD.list_milestones(db, repo_id, include_closed=include_closed)
    return {
        "success": True,
        "milestones": [
            {
                "id": m.id,
                "title": m.title,
                "description": m.description,
                "due_date": m.due_date.isoformat() if m.due_date else None,
                "is_closed": m.is_closed,
            }
            for m in milestones
        ],
    }


@router.post("/api/repository/{repo_id}/milestones")
async def create_milestone(
    repo_id: str,
    request: MilestoneCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    require_repository_access(db, current_user.username, repository, "create milestones in", required_scope="write")

    due = None
    if request.due_date:
        try:
            due = datetime.fromisoformat(request.due_date.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid due_date")

    m = MilestoneCRUD.create_milestone(
        db,
        repo_id,
        request.title.strip(),
        description=request.description,
        due_date=due,
        created_by_id=current_user.id,
    )
    return {
        "success": True,
        "milestone": {
            "id": m.id,
            "title": m.title,
            "description": m.description,
            "due_date": m.due_date.isoformat() if m.due_date else None,
            "is_closed": m.is_closed,
        },
    }
