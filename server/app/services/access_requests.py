"""Automatic issue-scoped repository access requests raised from issue text."""

from typing import List, Optional

from sqlalchemy.orm import Session

from database.crud import IssueAccessRequestCRUD, NotificationCRUD, UserCRUD, parse_mentions
from database.models import Commit, Issue, Repository, User, UserPermission

from app.core.permissions import _is_privileged_role


def _access_request_reason_text(
    issue: Issue,
    override_description: Optional[str],
    *,
    fallback: str,
) -> str:
    """Use the issue creator's description when present; otherwise a short fallback line."""
    raw = override_description if override_description is not None else (issue.description or "")
    text = (raw or "").strip()
    return text if text else fallback


def _handle_issue_access_requests(db: Session, issue: Issue, creator: User, repository: Repository, description: str) -> None:
    """Create access requests for mentioned users who don't own the repository"""
    if not description:
        return

    mentioned_usernames = parse_mentions(description)
    for username in mentioned_usernames:
        mentioned_user = UserCRUD.get_user_by_username(db, username)
        if not mentioned_user or not mentioned_user.is_active:
            continue
        reason = _access_request_reason_text(
            issue,
            description,
            fallback=f"Tagged in issue #{issue.number}: {issue.title}",
        )
        _maybe_create_issue_access_request_for_user(
            db,
            issue,
            creator,
            repository,
            mentioned_user,
            reason,
        )


def _maybe_create_issue_access_request_for_user(
    db: Session,
    issue: Issue,
    creator: User,
    repository: Repository,
    target_user: User,
    request_reason: str,
) -> None:
    if not target_user or not target_user.is_active:
        return
    if target_user.id == repository.owner_id:
        return
    # IMPORTANT: Issue-scoped permission for other issues must NOT suppress a new access request.
    # Only global (non-issue) collaborators or contributors (commit authors) should bypass requests.
    if db.query(Commit).filter(Commit.repository_id == repository.id, Commit.author_id == target_user.id).first():
        return
    if db.query(UserPermission).filter(
        UserPermission.repository_id == repository.id,
        UserPermission.user_id == target_user.id,
        UserPermission.issue_id.is_(None),
    ).first():
        return

    try:
        access_req = IssueAccessRequestCRUD.create_request(
            db,
            issue_id=issue.id,
            requested_by_id=creator.id,
            requested_user_id=target_user.id,
            repo_id=repository.id,
            request_reason=request_reason,
        )
        if not access_req:
            return

        repo_owner = repository.owner
        if repo_owner:
            NotificationCRUD.create_notification(
                db,
                user_id=repo_owner.id,
                notification_type="issue_access_request_created",
                title="Repository Access Request",
                body=f"@{target_user.username} needs access to '{repository.name}' (issue #{issue.number})",
                payload={
                    "issue_number": issue.number,
                    "issue_title": issue.title,
                    "repository_id": repository.id,
                    "repository_name": repository.name,
                    "requested_user": target_user.username,
                    "access_request_id": access_req.id,
                    "action_required": True,
                    "actions": [
                        {"type": "APPROVE", "url": f"/api/access-requests/{access_req.id}/approve"},
                        {"type": "DENY", "url": f"/api/access-requests/{access_req.id}/deny"},
                        {"type": "REVIEW", "url": f"/repository/{repository.id}/issues/{issue.number}"},
                    ],
                },
                dedupe_key=f"access_req_{access_req.id}_{repo_owner.id}",
            )

        # Notify ONLY the issue creator's reviewer chain (team lead/admin), not all admins.
        try:
            reviewers: List[User] = []
            # Primary: creator's team lead (if set and active)
            if creator and getattr(creator, "team_lead_id", None):
                tl = db.query(User).filter(User.id == creator.team_lead_id).first()
                if tl and tl.is_active:
                    reviewers.append(tl)

            # Secondary: repository owner if they are privileged (common for owner-led review workflows)
            repo_owner = repository.owner
            if not reviewers and repo_owner and repo_owner.is_active and _is_privileged_role(getattr(repo_owner, "role", "developer")):
                reviewers.append(repo_owner)

            # If creator is already privileged, notify them (they can approve)
            if creator and _is_privileged_role(getattr(creator, "role", "developer")):
                if creator.is_active and all(r.id != creator.id for r in reviewers):
                    reviewers.append(creator)

            # Fallback: one admin (lowest id) so requests never get stuck
            if not reviewers:
                admin = db.query(User).filter(User.is_active == True, User.role == "admin").order_by(User.id.asc()).first()  # noqa: E712
                if admin:
                    reviewers.append(admin)

            for reviewer in reviewers:
                if not reviewer or not reviewer.id:
                    continue
                # Don't double-notify the owner notification (owner gets a separate, owner-scoped request)
                if repo_owner and reviewer.id == repo_owner.id:
                    continue
                NotificationCRUD.create_notification(
                    db,
                    user_id=reviewer.id,
                    notification_type="issue_access_request_created",
                    title="Repository Access Request",
                    body=f"@{target_user.username} needs access to '{repository.name}' (issue #{issue.number})",
                    payload={
                        "issue_number": issue.number,
                        "issue_title": issue.title,
                        "repository_id": repository.id,
                        "repository_name": repository.name,
                        "requested_user": target_user.username,
                        "requested_by": creator.username if creator else None,
                        "access_request_id": access_req.id,
                        "action_required": True,
                        "actions": [
                            {"type": "APPROVE", "url": f"/api/access-requests/{access_req.id}/approve"},
                            {"type": "DENY", "url": f"/api/access-requests/{access_req.id}/deny"},
                            {"type": "REVIEW", "url": f"/repository/{repository.id}/issues/{issue.number}"},
                        ],
                    },
                    dedupe_key=f"access_req_{access_req.id}_{reviewer.id}",
                )
        except Exception:
            pass

        NotificationCRUD.create_notification(
            db,
            user_id=target_user.id,
            notification_type="issue_access_request_pending",
            title="Access Request Pending",
            body=f"You were mentioned or assigned to issue #{issue.number} for '{repository.name}'. Awaiting approval from repository owner.",
            payload={
                "issue_number": issue.number,
                "repository_id": repository.id,
                "repository_name": repository.name,
                "access_request_id": access_req.id,
                "status": "pending",
            },
            dedupe_key=f"access_req_user_{access_req.id}_{target_user.id}",
        )
    except Exception as e:
        print(f"Error creating access request for {target_user.username}: {str(e)}")
        return
