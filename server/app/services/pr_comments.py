"""Inline comments on a pull request diff.

Issues already support discussion, but a pull request had nowhere to put a remark about
one particular line, so review feedback had to be written as prose describing where to
look. These comments anchor to a file and line on a specific side of the diff.

Two behaviours are worth stating because the alternatives are worse:

* **Outdated comments are kept, not deleted.** A comment records the source-branch tip it
  was written against; once that file changes, the comment is flagged ``outdated`` because
  the line it points at may no longer say what the commenter read. Deleting it would
  destroy review discussion precisely when someone acted on it.

* **Resolving is explicit and reversible.** A thread stays visible once resolved rather
  than vanishing, so a reviewer can see what was settled and reopen it.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database.crud import BranchCRUD
from database.models import (
    CommitFile, PullRequest, PullRequestComment, Repository, User,
)


class CommentError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


VALID_SIDES = ("new", "old")
MAX_BODY = 20_000


def _source_head(db: Session, repository: Repository, pr: PullRequest) -> Optional[str]:
    branch = BranchCRUD.get_branch(db, repository.id, pr.source_branch)
    return branch.head_commit_id if branch else None


def _file_hash_at(db: Session, commit_id: Optional[str], path: str) -> Optional[str]:
    if not commit_id:
        return None
    row = (
        db.query(CommitFile.file_hash)
        .filter(CommitFile.commit_id == commit_id, CommitFile.file_path == path)
        .first()
    )
    return row[0] if row else None


def add(
    db: Session,
    repository: Repository,
    pr: PullRequest,
    author: User,
    file_path: str,
    line: int,
    body: str,
    side: str = "new",
    in_reply_to_id: Optional[int] = None,
) -> PullRequestComment:
    """Anchor a comment to one line of one file."""
    if pr.status != "open":
        raise CommentError(f"Pull request is {pr.status}; it can no longer be commented on.", 409)

    text = (body or "").strip()
    if not text:
        raise CommentError("Comment body cannot be empty")
    if len(text) > MAX_BODY:
        raise CommentError(f"Comment is too long (limit {MAX_BODY:,} characters)")
    if side not in VALID_SIDES:
        raise CommentError(f"side must be one of {', '.join(VALID_SIDES)}")
    if not file_path or not file_path.strip():
        raise CommentError("file_path is required")
    try:
        line_number = int(line)
    except (TypeError, ValueError):
        raise CommentError("line must be a number")
    if line_number < 1:
        raise CommentError("line must be 1 or greater")

    parent = None
    if in_reply_to_id is not None:
        parent = (
            db.query(PullRequestComment)
            .filter(
                PullRequestComment.id == in_reply_to_id,
                PullRequestComment.pull_request_id == pr.id,
            )
            .first()
        )
        if not parent:
            raise CommentError("The comment being replied to does not exist here", 404)
        # A reply belongs to its root's location; threads do not straddle lines.
        file_path, line_number, side = parent.file_path, parent.line, parent.side
        if parent.in_reply_to_id is not None:
            # Keep threads one level deep so a reply to a reply stays in the same thread.
            in_reply_to_id = parent.in_reply_to_id

    comment = PullRequestComment(
        pull_request_id=pr.id,
        author_id=author.id,
        file_path=file_path.strip(),
        line=line_number,
        side=side,
        body=text,
        commit_id=_source_head(db, repository, pr),
        in_reply_to_id=in_reply_to_id,
        resolved=False,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return comment


def _serialize(db: Session, comment: PullRequestComment, head: Optional[str]) -> Dict[str, Any]:
    # Outdated when the file's content has changed since the comment was written.
    outdated = False
    if head and comment.commit_id and comment.commit_id != head:
        outdated = (
            _file_hash_at(db, comment.commit_id, comment.file_path)
            != _file_hash_at(db, head, comment.file_path)
        )
    return {
        "id": comment.id,
        "author": comment.author.username if comment.author else None,
        "file_path": comment.file_path,
        "line": comment.line,
        "side": comment.side,
        "body": comment.body,
        "commit_id": comment.commit_id,
        "in_reply_to_id": comment.in_reply_to_id,
        "resolved": bool(comment.resolved),
        "resolved_by": comment.resolved_by.username if comment.resolved_by else None,
        "outdated": outdated,
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
    }


def threads(db: Session, repository: Repository, pr: PullRequest) -> Dict[str, Any]:
    """Comments grouped into threads, ordered by file then line."""
    head = _source_head(db, repository, pr)
    rows = (
        db.query(PullRequestComment)
        .filter(PullRequestComment.pull_request_id == pr.id)
        .order_by(PullRequestComment.id)
        .all()
    )

    roots: List[Dict[str, Any]] = []
    by_id: Dict[int, Dict[str, Any]] = {}
    for comment in rows:
        payload = _serialize(db, comment, head)
        by_id[comment.id] = payload
        if comment.in_reply_to_id is None:
            payload["replies"] = []
            roots.append(payload)

    for comment in rows:
        if comment.in_reply_to_id is not None:
            parent = by_id.get(comment.in_reply_to_id)
            if parent is not None:
                parent.setdefault("replies", []).append(by_id[comment.id])

    roots.sort(key=lambda t: (t["file_path"], t["line"], t["id"]))
    return {
        "source_head_commit_id": head,
        "threads": roots,
        "total": len(rows),
        "unresolved": sum(1 for t in roots if not t["resolved"]),
        "outdated": sum(1 for t in roots if t["outdated"]),
    }


def set_resolved(
    db: Session,
    pr: PullRequest,
    comment_id: int,
    actor: User,
    resolved: bool,
) -> Dict[str, Any]:
    """Mark a thread settled, or reopen it."""
    comment = (
        db.query(PullRequestComment)
        .filter(
            PullRequestComment.id == comment_id,
            PullRequestComment.pull_request_id == pr.id,
        )
        .first()
    )
    if not comment:
        raise CommentError("Comment not found", 404)
    if comment.in_reply_to_id is not None:
        raise CommentError(
            "Resolve the thread's first comment rather than a reply.", 400
        )

    comment.resolved = resolved
    comment.resolved_by_id = actor.id if resolved else None
    comment.resolved_at = datetime.utcnow() if resolved else None
    db.commit()
    return {"id": comment.id, "resolved": bool(comment.resolved)}


def delete(db: Session, pr: PullRequest, comment_id: int, actor: User) -> Dict[str, Any]:
    """Remove a comment. Only its author may do so, and replies go with it."""
    comment = (
        db.query(PullRequestComment)
        .filter(
            PullRequestComment.id == comment_id,
            PullRequestComment.pull_request_id == pr.id,
        )
        .first()
    )
    if not comment:
        raise CommentError("Comment not found", 404)
    if comment.author_id != actor.id:
        raise CommentError("Only the author can delete a comment", 403)

    removed = 1
    if comment.in_reply_to_id is None:
        replies = (
            db.query(PullRequestComment)
            .filter(PullRequestComment.in_reply_to_id == comment.id)
            .all()
        )
        for reply in replies:
            db.delete(reply)
            removed += 1
    db.delete(comment)
    db.commit()
    return {"deleted": removed}
