"""Notification fan-out for issue activity, review queues and access grants."""

from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from database.crud import IssueCRUD, IssueEventCRUD, NotificationCRUD, UserCRUD, parse_mentions
from database.models import Issue, Repository, User


def _issue_notification_recipients(issue: Issue, exclude_user_id: Optional[int] = None) -> List[int]:
    """Return user ids to notify for an issue (creator, assignee, watchers)."""
    user_ids: List[int] = []
    for uid in [issue.created_by_id, issue.assigned_to_id]:
        if uid and uid not in user_ids:
            user_ids.append(uid)
    for w in (issue.watchers or []):
        if w and getattr(w, "user_id", None) and w.user_id not in user_ids:
            user_ids.append(w.user_id)
    if exclude_user_id and exclude_user_id in user_ids:
        user_ids = [u for u in user_ids if u != exclude_user_id]
    return user_ids


def _notify_issue_commit_submitted(
    db: Session,
    repo_id: str,
    issue: Issue,
    actor: User,
    pending_commit_id: str,
    branch_name: str,
    message: str,
) -> None:
    # Human-visible audit trail in issue comments + events
    IssueCRUD.add_comment(
        db,
        issue,
        author_id=actor.id,
        body=(
            f"Submitted commit `{pending_commit_id[:12]}` for review on branch `{branch_name}`.\n\n"
            f"Message:\n{message}"
        ),
    )
    IssueEventCRUD.add_event(
        db,
        issue.id,
        "issue_commit_submitted",
        actor_id=actor.id,
        payload={
            "repository_id": repo_id,
            "issue_number": issue.number,
            "pending_commit_id": pending_commit_id,
            "branch": branch_name,
            "message": message,
        },
    )

    payload = {"repository_id": repo_id, "issue_number": issue.number, "branch": branch_name, "pending_commit_id": pending_commit_id}
    for uid in _issue_notification_recipients(issue, exclude_user_id=actor.id):
        NotificationCRUD.create_notification(
            db,
            user_id=uid,
            notification_type="issue_commit_submitted",
            title=f"Issue #{issue.number}: commit submitted for review",
            body=f"@{actor.username} submitted `{pending_commit_id[:12]}` on `{branch_name}`",
            payload={**payload, "actor": actor.username},
            dedupe_key=f"issue:{issue.id}:pending:{pending_commit_id}:submitted",
        )


def _notify_issue_commit_reviewed(
    db: Session,
    repo_id: str,
    issue: Issue,
    reviewer: User,
    commit_id: str,
    branch_name: Optional[str],
    action: str,
    comment: Optional[str] = None,
) -> None:
    action_norm = (action or "").strip().lower()
    if action_norm not in {"approved", "rejected"}:
        action_norm = "reviewed"
    suffix = f"\n\nReviewer note:\n{comment}" if comment else ""

    IssueCRUD.add_comment(
        db,
        issue,
        author_id=reviewer.id,
        body=(
            f"{action_norm.title()} commit `{commit_id[:12]}`"
            + (f" on branch `{branch_name}`" if branch_name else "")
            + f".{suffix}"
        ),
    )
    IssueEventCRUD.add_event(
        db,
        issue.id,
        f"issue_commit_{action_norm}",
        actor_id=reviewer.id,
        payload={
            "repository_id": repo_id,
            "issue_number": issue.number,
            "commit_id": commit_id,
            "branch": branch_name,
            "review_comment": comment,
        },
    )

    payload = {"repository_id": repo_id, "issue_number": issue.number, "branch": branch_name, "commit_id": commit_id, "status": action_norm}
    for uid in _issue_notification_recipients(issue, exclude_user_id=reviewer.id):
        NotificationCRUD.create_notification(
            db,
            user_id=uid,
            notification_type=f"issue_commit_{action_norm}",
            title=f"Issue #{issue.number}: commit {action_norm}",
            body=f"@{reviewer.username} {action_norm} `{commit_id[:12]}`" + (f" on `{branch_name}`" if branch_name else ""),
            payload={**payload, "actor": reviewer.username, "review_comment": comment},
            dedupe_key=f"issue:{issue.id}:commit:{commit_id}:{action_norm}",
        )


def _notify_pending_commit_reviewer(
    db: Session,
    repository: Repository,
    pending_commit: "PendingCommit",
    actor: User,
    team_lead_name: Optional[str],
    branch_name: str,
) -> None:
    """Notify the appropriate reviewer(s) that a pending commit needs review."""
    recipients: List[int] = []

    # Prefer the author's assigned team lead if present.
    if actor.team_lead_id:
        recipients.append(actor.team_lead_id)

    # Fallback to repository owner (common when dev isn't assigned to a team lead).
    if repository.owner_id and repository.owner_id not in recipients:
        recipients.append(repository.owner_id)

    # Always notify admins as a safety net.
    try:
        admin_ids = [
            u.id
            for u in db.query(User).filter(User.role == "admin", User.is_active == True).all()  # noqa: E712
            if u and u.id
        ]
        for uid in admin_ids:
            if uid not in recipients:
                recipients.append(uid)
    except Exception:
        pass

    payload = {
        "repository_id": repository.id,
        "repository_name": repository.name,
        "pending_commit_id": pending_commit.id,
        "branch": branch_name,
        "author": actor.username,
        "team_lead": team_lead_name,
        "actions": [
            {"type": "OPEN_ISSUE", "payload": {"repository_id": repository.id, "issue_number": None}},
        ],
    }

    for uid in recipients:
        if uid == actor.id:
            continue
        NotificationCRUD.create_notification(
            db,
            user_id=uid,
            notification_type="pending_commit_review",
            title=f"Pending commit review: {repository.name}",
            body=f"@{actor.username} submitted `{pending_commit.id[:12]}` on `{branch_name}`",
            payload=payload,
            dedupe_key=f"pending_commit:{pending_commit.id}:review_request:{uid}",
        )


def _maybe_notify_issue_mentions(db: Session, body: str, actor: User, issue: Issue, context: str) -> None:
    if not body:
        return
    for username in parse_mentions(body):
        target = UserCRUD.get_user_by_username(db, username)
        if not target or not target.is_active:
            continue
        NotificationCRUD.create_notification(
            db,
            user_id=target.id,
            notification_type="issue_mention",
            title=f"You were mentioned in {context}",
            body=f"@{actor.username} mentioned you on issue #{issue.number}",
            payload={"repository_id": issue.repository_id, "issue_number": issue.number},
            dedupe_key=None,
        )


def _notify_user_access_approved(db: Session, access_req, repository: Repository) -> None:
    """Send access summary in-app; full CLI quick start is download-only (.txt)."""
    repo_owner = repository.owner
    granted_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    issue_num = access_req.issue.number if access_req.issue else None
    issue_branch = str(issue_num) if issue_num is not None else "issue"

    summary_body = f"""✅ REPOSITORY ACCESS GRANTED

Repository: {repository.name}
Owner: @{repo_owner.username}
Permission Level: Developer (write access)
Granted At: {granted_at}
Repository ID: {repository.id}"""
    if issue_num is not None:
        summary_body += f"\nIssue: #{issue_num}"

    workdir_hint = repository.name.replace('"', '\\"')
    cli_quickstart = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK START - FoxNest CLI

0️⃣  LOGIN (REQUIRED)
   $ fox login

1️⃣  SET SERVER (REMOTE ORIGIN)
   $ fox set origin 192.168.15.207:33333 --global
   (Use the same host/port as the web app backend)

2️⃣  CREATE A WORKING FOLDER AND INIT
   PowerShell: mkdir "{workdir_hint}"; cd "{workdir_hint}"
   Bash:       mkdir -p "{workdir_hint}" && cd "{workdir_hint}"
   $ fox init --username "{access_req.requested_user.username}" --repo-name "{repository.name}"

3️⃣  LINK THIS LOCAL REPO TO THE REMOTE REPO ID
   $ fox set repo-id {repository.id}

4️⃣  PULL LATEST CHANGES
   $ fox pull

5️⃣  CREATE FEATURE BRANCH FOR ISSUE #{issue_num if issue_num is not None else 'N/A'}
   $ fox branch create feature/issue-{issue_branch} --checkout
   (Alternative: $ fox checkout -b feature/issue-{issue_branch})

6️⃣  MAKE CHANGES & COMMIT
   $ fox add src/yourfile.py
   $ fox commit -m "Fix: description (Fixes #{issue_branch})"

7️⃣  PUSH YOUR CHANGES
   $ fox push

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HELPFUL COMMANDS

Check status:        $ fox status
View commits:        $ fox log
Pull latest:         $ fox pull
Create branch:       $ fox branch create <branch-name> --checkout
Switch branch:       $ fox checkout <branch-name>
View file history:   $ fox log <filename>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    """.strip()

    cli_txt_download = f"{summary_body}\n\n{cli_quickstart}"

    NotificationCRUD.create_notification(
        db,
        user_id=access_req.requested_user_id,
        notification_type="issue_access_request_approved",
        title="✅ Repository Access Granted",
        body=summary_body,
        payload={
            "issue_number": issue_num,
            "repository_id": repository.id,
            "repository_name": repository.name,
            "repository_owner": repo_owner.username,
            "cli_setup_guide": cli_txt_download,
            "access_granted": True,
            "actions": [
                {
                    "type": "DOWNLOAD_TXT",
                    "payload": {
                        "filename": f"{repository.name}-foxnest-cli-quickstart.txt",
                        "text": cli_txt_download,
                    },
                }
            ],
        },
        dedupe_key=f"access_approved_{access_req.id}"
    )
