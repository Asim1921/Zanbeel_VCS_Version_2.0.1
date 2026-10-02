"""Notifications for the review workflow.

The review features were built without any of this, so the gate worked and nobody was
told: an author had no way to learn their pull request had been approved, or that
someone had asked for changes, short of opening it again and looking.

Every notifier here is best-effort. A failure to notify must never fail the operation
that triggered it — losing a merge because a notification row could not be written
would be a far worse outcome than a missed notification.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database.crud import NotificationCRUD
from database.models import PullRequest, PullRequestComment, Repository, User

TYPE_REVIEW_SUBMITTED = "pr_review"
TYPE_PR_MERGED = "pr_merged"
TYPE_PR_COMMENT = "pr_comment"
TYPE_REVIEW_REQUESTED = "pr_review_requested"

_STATE_WORDS = {
    "approved": "approved",
    "changes_requested": "requested changes on",
    "commented": "commented on",
}


def _send(db: Session, user_id: Optional[int], **kwargs) -> None:
    if not user_id:
        return
    try:
        NotificationCRUD.create_notification(db, user_id=user_id, **kwargs)
    except Exception:
        # Best effort by design; see the module docstring.
        db.rollback()


def _pr_payload(repository: Repository, pr: PullRequest) -> Dict[str, Any]:
    return {
        "repository_id": repository.id,
        "repository_name": repository.name,
        "pull_request_id": pr.id,
        "title": pr.title,
        "source_branch": pr.source_branch,
        "target_branch": pr.target_branch,
    }


def review_submitted(
    db: Session, repository: Repository, pr: PullRequest, reviewer: User, state: str
) -> None:
    """Tell the author that someone reviewed their pull request."""
    if pr.created_by_id == reviewer.id:
        return  # No point telling someone what they just did.

    verb = _STATE_WORDS.get(state, "reviewed")
    _send(
        db,
        pr.created_by_id,
        notification_type=TYPE_REVIEW_SUBMITTED,
        title=f"@{reviewer.username} {verb} #{pr.id}",
        body=f"{pr.title} — {repository.name}",
        payload={**_pr_payload(repository, pr), "state": state,
                 "reviewer": reviewer.username},
        # One per reviewer per state per pull request: re-approving after a change
        # should not stack up identical notifications.
        dedupe_key=f"pr-review:{pr.id}:{reviewer.id}:{state}",
    )


def pull_request_merged(
    db: Session, repository: Repository, pr: PullRequest, actor: User,
    merge_commit_id: Optional[str] = None,
) -> None:
    """Tell the author their pull request landed."""
    if pr.created_by_id == actor.id:
        return
    _send(
        db,
        pr.created_by_id,
        notification_type=TYPE_PR_MERGED,
        title=f"@{actor.username} merged #{pr.id}",
        body=f"{pr.title} — into {pr.target_branch}",
        payload={**_pr_payload(repository, pr), "merged_by": actor.username,
                 "merge_commit_id": merge_commit_id},
        dedupe_key=f"pr-merged:{pr.id}",
    )


def comment_added(
    db: Session, repository: Repository, pr: PullRequest, author: User,
    comment: PullRequestComment,
) -> None:
    """Tell the author, and anyone already in the thread, about a new comment.

    Thread participants matter as much as the author: a reply to a reviewer's question
    is for that reviewer, and only notifying the author would leave them waiting.
    """
    recipients = set()
    if pr.created_by_id:
        recipients.add(pr.created_by_id)

    if comment.in_reply_to_id:
        thread = (
            db.query(PullRequestComment)
            .filter(
                (PullRequestComment.id == comment.in_reply_to_id)
                | (PullRequestComment.in_reply_to_id == comment.in_reply_to_id)
            )
            .all()
        )
        recipients.update(row.author_id for row in thread if row.author_id)

    recipients.discard(author.id)

    line = f"{comment.file_path}:{comment.line}" if comment.file_path else "the discussion"
    for user_id in recipients:
        _send(
            db,
            user_id,
            notification_type=TYPE_PR_COMMENT,
            title=f"@{author.username} commented on #{pr.id}",
            body=f"{line} — {pr.title}",
            payload={**_pr_payload(repository, pr), "comment_id": comment.id,
                     "file_path": comment.file_path, "line": comment.line,
                     "author": author.username},
            dedupe_key=f"pr-comment:{comment.id}:{user_id}",
        )


def review_requested_from_owners(
    db: Session, repository: Repository, pr: PullRequest, owners: List[str]
) -> None:
    """Tell code owners that a pull request needs them.

    Without this, CODEOWNERS blocks a merge on people who were never told they were
    required — the gate is satisfiable in principle and stuck in practice.
    """
    from database.crud import UserCRUD

    for username in owners:
        owner = UserCRUD.get_user_by_username(db, username)
        if not owner or owner.id == pr.created_by_id:
            continue
        _send(
            db,
            owner.id,
            notification_type=TYPE_REVIEW_REQUESTED,
            title=f"Your review is required on #{pr.id}",
            body=f"{pr.title} — you own files this pull request changes",
            payload={**_pr_payload(repository, pr), "reason": "code_owner"},
            dedupe_key=f"pr-owner:{pr.id}:{owner.id}",
        )
