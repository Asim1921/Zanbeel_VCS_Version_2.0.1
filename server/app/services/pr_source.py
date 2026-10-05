"""Where a pull request's source branch actually lives.

A pull request from a fork names a branch in the *fork*, but most of the code that read
``pr.source_branch`` looked it up in the repository the pull request was opened against.
When the upstream happened to have no branch of that name the merge failed; when it did --
``main`` into ``main`` is the usual fork contribution -- the merge, the review pinning and
the conflict session all quietly used the upstream's own branch, so the pull request was
marked merged and the fork's work never landed.

Every reader of the source side goes through here so there is one answer.
"""

from typing import Optional

from sqlalchemy.orm import Session

from database.crud import BranchCRUD, RepositoryCRUD
from database.models import Branch, PullRequest, Repository


def repository_id(pr: PullRequest) -> str:
    """The repository holding the source branch: the fork for a cross-repo PR."""
    return pr.source_repository_id or pr.repository_id


def repository(db: Session, pr: PullRequest) -> Optional[Repository]:
    return RepositoryCRUD.get_repository(db, repository_id(pr))


def branch(db: Session, pr: PullRequest) -> Optional[Branch]:
    return BranchCRUD.get_branch(db, repository_id(pr), pr.source_branch)


def head(db: Session, pr: PullRequest) -> Optional[str]:
    row = branch(db, pr)
    return row.head_commit_id if row else None


def is_cross_repository(pr: PullRequest) -> bool:
    return bool(pr.source_repository_id) and pr.source_repository_id != pr.repository_id
