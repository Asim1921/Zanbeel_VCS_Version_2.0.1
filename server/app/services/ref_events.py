"""What happens after a branch moves, triggered from the one place branches move.

Three things follow every change: an activity-log row (``audit.record_ref_move``), the
webhook events below, and an update of the code-search index when the default branch
moved (``search_index.branch_moved``).

``push`` used to be sent by two call sites: a push from a team lead, and a fast-forward
publish. Every other way a branch moves sent nothing -- approving a developer's queued
commit (the usual way commits land here), a pull-request merge, cherry-pick, revert,
rebase, rollback, a conflict resolution. A CI system listening for ``push`` therefore
never built most of what landed on ``main``.

``refs`` is the only component allowed to change a branch, so it calls ``emit`` after
every change it makes, and the events cannot be forgotten by a new code path. Like all
dispatch, this never raises: an integration must not be able to fail a branch update.

Events:

* ``push`` -- any branch created, moved or deleted, with ``before``/``after``,
  ``created``/``deleted``/``forced`` and the operation that moved it.
* ``branch.created`` / ``branch.deleted``.
* ``pull_request.updated`` -- the source branch of an open pull request moved, so CI
  should build the new head and report its status there. Sent to the pull request's
  own repository, which for a fork's pull request is the upstream.
"""

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from database.models import Commit, PullRequest, Repository, User

logger = logging.getLogger(__name__)


def _repository_info(repository: Repository) -> Dict[str, Any]:
    owner = getattr(repository, "owner", None)
    return {
        "id": repository.id,
        "name": repository.name,
        "owner": getattr(owner, "username", None),
    }


def _commit_info(db: Session, commit_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not commit_id:
        return None
    commit = db.query(Commit).filter(Commit.id == commit_id).first()
    if not commit:
        return {"id": commit_id}
    return {
        "id": commit.id,
        "message": commit.message,
        "author": getattr(commit.author, "username", None),
        "timestamp": commit.created_at.isoformat() if commit.created_at else None,
    }


def _open_pull_requests_from(db: Session, repository: Repository, branch: str):
    """Open pull requests whose source is ``branch`` of ``repository``, in any repository."""
    same_repo = db.query(PullRequest).filter(
        PullRequest.repository_id == repository.id,
        PullRequest.source_branch == branch,
        PullRequest.status == "open",
        (PullRequest.source_repository_id.is_(None))
        | (PullRequest.source_repository_id == repository.id),
    ).all()
    from_fork = db.query(PullRequest).filter(
        PullRequest.source_repository_id == repository.id,
        PullRequest.repository_id != repository.id,
        PullRequest.source_branch == branch,
        PullRequest.status == "open",
    ).all()
    return same_repo + from_fork


def emit(
    db: Session,
    *,
    repository: Repository,
    actor: Optional[User],
    branch: str,
    before: Optional[str],
    after: Optional[str],
    operation: str,
    created: bool = False,
    deleted: bool = False,
) -> None:
    """Record and announce one completed branch change. Never raises."""
    from app.services import audit
    audit.record_ref_move(
        db, repository=repository, actor=actor, branch=branch, before=before, after=after,
        operation=operation, created=created, deleted=deleted,
    )
    _announce(db, repository=repository, actor=actor, branch=branch, before=before,
              after=after, operation=operation, created=created, deleted=deleted)
    try:
        from app.services import search_index
        search_index.branch_moved(db, repository, branch)
    except Exception:
        logger.exception("Could not schedule the search index update for %s", getattr(repository, "id", "?"))


def _announce(db, *, repository, actor, branch, before, after, operation, created, deleted) -> None:
    try:
        from app.services import webhooks as hook_service
        from app.services.branch_protection import OP_FORCE_UPDATE

        repo = _repository_info(repository)
        pusher = getattr(actor, "username", None)
        hook_service.dispatch(db, hook_service.EVENT_PUSH, repository.id, {
            "repository": repo,
            "ref": f"refs/heads/{branch}",
            "branch": branch,
            "before": before,
            "after": after,
            "created": created,
            "deleted": deleted,
            "forced": operation == OP_FORCE_UPDATE,
            "operation": operation,
            "pusher": pusher,
            "head_commit": _commit_info(db, after),
        })

        if created:
            hook_service.dispatch(db, hook_service.EVENT_BRANCH_CREATED, repository.id, {
                "repository": repo, "branch": branch, "commit": after, "created_by": pusher,
            })
        if deleted:
            hook_service.dispatch(db, hook_service.EVENT_BRANCH_DELETED, repository.id, {
                "repository": repo, "branch": branch, "last_commit": before, "deleted_by": pusher,
            })

        if after and not deleted:
            for pr in _open_pull_requests_from(db, repository, branch):
                target = db.query(Repository).filter(Repository.id == pr.repository_id).first()
                hook_service.dispatch(db, hook_service.EVENT_PR_UPDATED, pr.repository_id, {
                    "repository": _repository_info(target) if target else {"id": pr.repository_id},
                    "pull_request": {
                        "id": pr.id,
                        "title": pr.title,
                        "source_repository_id": pr.source_repository_id or pr.repository_id,
                        "source_branch": pr.source_branch,
                        "target_branch": pr.target_branch,
                        "before": before,
                        "head": after,
                    },
                    "pusher": pusher,
                })
    except Exception:
        logger.exception("Could not send webhook events for %s:%s", getattr(repository, "id", "?"), branch)
