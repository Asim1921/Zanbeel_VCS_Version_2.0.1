"""Notification fan-out for issue activity, review queues and access grants.

Every notification carries ``payload.target`` -- what it is about, as
``{"kind": "pull_request" | "commit" | "issue" | "repository" | "settings", ...}`` -- so
the bell, the notifications page and the CLI can take the reader to it instead of
showing a dump of ids.
"""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

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

    payload = {"repository_id": repo_id, "issue_number": issue.number, "branch": branch_name,
               "pending_commit_id": pending_commit_id, "target": target_issue(repo_id, issue.number)}
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

    payload = {"repository_id": repo_id, "issue_number": issue.number, "branch": branch_name, "commit_id": commit_id,
               "status": action_norm, "target": target_issue(repo_id, issue.number)}
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
        "target": {"kind": "review_queue", "repository_id": repository.id,
                   "pending_commit_id": pending_commit.id},
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
            payload={"repository_id": issue.repository_id, "issue_number": issue.number,
                     "target": target_issue(issue.repository_id, issue.number)},
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
   $ fox set origin {public_server_url()} --global
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
            "target": target_repository(repository.id),
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


# --- where a notification points ----------------------------------------------------

def target_repository(repository_id: str) -> Dict[str, Any]:
    return {"kind": "repository", "repository_id": repository_id}


def target_pull_request(repository_id: str, pr_id: int) -> Dict[str, Any]:
    return {"kind": "pull_request", "repository_id": repository_id, "pr_id": pr_id}


def target_commit(repository_id: str, commit_id: str) -> Dict[str, Any]:
    return {"kind": "commit", "repository_id": repository_id, "commit_id": commit_id}


def target_issue(repository_id: str, number: Optional[int]) -> Dict[str, Any]:
    return {"kind": "issue", "repository_id": repository_id, "issue_number": number}


def public_server_url() -> str:
    """The address clients should use for this server.

    FOXNEST_PUBLIC_URL when set; otherwise the address the current request came in on;
    otherwise this machine on the configured port. The access notification used to tell
    every new collaborator to connect to a fixed LAN address.
    """
    configured = (os.getenv("FOXNEST_PUBLIC_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    try:
        from app.services.audit import current_request_scope
        scope = current_request_scope()
        if scope:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
            host = headers.get("host")
            if host:
                return f"{scope.get('scheme') or 'http'}://{host}"
    except Exception:
        pass
    return f"http://127.0.0.1:{os.getenv('SERVER_PORT', '33333')}"


# --- what was missing ---------------------------------------------------------------

def _can_read(db: Session, user: User, repository: Repository) -> bool:
    from app.core.permissions import _user_can_read_repository_contents
    return bool(user and user.is_active and _user_can_read_repository_contents(db, user, repository))


def commit_reviewed_for_author(
    db: Session, pending, reviewer: User, decision: str, comment: Optional[str],
    commit_id: Optional[str] = None,
) -> None:
    """Tell the author what happened to their queued commit.

    Most commits here arrive through the review queue, and the author heard nothing
    unless the commit happened to reference an issue.
    """
    author_id = getattr(pending, "author_id", None)
    if not author_id or author_id == reviewer.id:
        return
    repository = getattr(pending, "repository", None)
    approved = decision == "approved"
    subject = ((pending.message or "").splitlines() or [""])[0][:80]
    body = f"@{reviewer.username} {decision} `{(commit_id or pending.id)[:12]}`: {subject}"
    if comment:
        body += f"\n\nReviewer note:\n{comment}"
    target = (target_commit(pending.repository_id, commit_id) if approved and commit_id
              else {"kind": "review_queue", "repository_id": pending.repository_id,
                    "pending_commit_id": pending.id})
    NotificationCRUD.create_notification(
        db,
        user_id=author_id,
        notification_type=f"commit_{decision}",
        title=f"Your commit was {decision}" + (f" in {repository.name}" if repository else ""),
        body=body,
        payload={"repository_id": pending.repository_id,
                 "repository_name": getattr(repository, "name", None),
                 "pending_commit_id": pending.id, "commit_id": commit_id,
                 "reviewer": reviewer.username, "review_comment": comment, "target": target},
        dedupe_key=f"pending_commit:{pending.id}:{decision}:author",
    )


def pull_request_closed(db: Session, repository: Repository, pr, actor: User) -> None:
    """Tell the author their pull request was closed by someone else."""
    if not pr.created_by_id or pr.created_by_id == actor.id:
        return
    NotificationCRUD.create_notification(
        db,
        user_id=pr.created_by_id,
        notification_type="pr_closed",
        title=f"@{actor.username} closed #{pr.id}",
        body=f"{pr.title} ({pr.source_branch} → {pr.target_branch}) in {repository.name}",
        payload={"repository_id": repository.id, "repository_name": repository.name,
                 "pr_id": pr.id, "actor": actor.username,
                 "target": target_pull_request(repository.id, pr.id)},
        dedupe_key=f"pr:{repository.id}:{pr.id}:closed",
    )


def mentions_in_pull_request(
    db: Session, repository: Repository, pr, actor: User, text: Optional[str],
    *, where: str, dedupe: str,
) -> int:
    """Notify people @mentioned in a pull request's description or comment.

    Only people who can read the repository: a mention must not hand someone the text
    of a pull request in a repository they cannot open.
    """
    sent = 0
    for username in parse_mentions(text or ""):
        user = UserCRUD.get_user_by_username(db, username)
        if not user or user.id == actor.id or not _can_read(db, user, repository):
            continue
        NotificationCRUD.create_notification(
            db,
            user_id=user.id,
            notification_type="pr_mention",
            title=f"@{actor.username} mentioned you in #{pr.id}",
            body=f"{where} — {pr.title}",
            payload={"repository_id": repository.id, "repository_name": repository.name,
                     "pr_id": pr.id, "actor": actor.username,
                     "target": target_pull_request(repository.id, pr.id)},
            dedupe_key=f"pr-mention:{dedupe}:{user.id}",
        )
        sent += 1
    return sent


def access_changed(db: Session, user: Optional[User], repository: Repository,
                   level: Optional[str], actor: User) -> None:
    """Tell someone they were given, changed or lost access to a repository."""
    if not user or user.id == actor.id:
        return
    if level:
        title = f"You have {level} access to {repository.name}"
        body = f"@{actor.username} gave you {level} access to {repository.name}."
        kind = "access_granted"
    else:
        title = f"Your access to {repository.name} was removed"
        body = f"@{actor.username} removed your access to {repository.name}."
        kind = "access_revoked"
    NotificationCRUD.create_notification(
        db,
        user_id=user.id,
        notification_type=kind,
        title=title,
        body=body,
        payload={"repository_id": repository.id, "repository_name": repository.name,
                 "permission_level": level, "actor": actor.username,
                 "target": target_repository(repository.id) if level else None},
        dedupe_key=None,
    )


def required_check_failed(db: Session, repository: Repository, status) -> int:
    """Tell pull request authors that a check their merge needs has failed on their head.

    Once per pull request, commit and check: a CI system that reports the same failure
    twice does not notify twice.
    """
    from database.models import PullRequest
    from app.services import pr_source, status_checks

    if status.state not in ("failure", "error"):
        return 0
    candidates = db.query(PullRequest).filter(
        PullRequest.status == "open",
        (PullRequest.repository_id == repository.id)
        | (PullRequest.source_repository_id == repository.id),
    ).all()
    sent = 0
    for pr in candidates:
        if not pr.created_by_id or pr_source.head(db, pr) != status.commit_id:
            continue
        target_repo = repository if pr.repository_id == repository.id else (
            db.query(Repository).filter(Repository.id == pr.repository_id).first())
        if target_repo is None:
            continue
        required = {rule.context for rule in status_checks.required_check_rules(db, target_repo, pr.target_branch)}
        if status.context not in required:
            continue
        NotificationCRUD.create_notification(
            db,
            user_id=pr.created_by_id,
            notification_type="check_failed",
            title=f"{status.context} {status.state} on #{pr.id}",
            body=(status.description or f"A required check failed on {status.commit_id[:12]}.")
                 + f" — {pr.title}",
            payload={"repository_id": target_repo.id, "repository_name": target_repo.name,
                     "pr_id": pr.id, "commit_id": status.commit_id, "context": status.context,
                     "state": status.state, "target_url": status.target_url,
                     "target": target_pull_request(target_repo.id, pr.id)},
            dedupe_key=f"check:{pr.repository_id}:{pr.id}:{status.commit_id}:{status.context}:{status.state}",
        )
        sent += 1
    return sent
