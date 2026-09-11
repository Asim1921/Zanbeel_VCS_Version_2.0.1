"""Forks and cross-repository pull requests.

Pull requests could only run between two branches of one repository, so contributing meant
being granted write access to the original -- which is exactly what you do not want to
hand an outside contributor. A fork is a full copy that keeps a pointer home, letting
someone work in their own repository and still propose the change back.

Forking copies *refs and metadata*, not content. Blobs are content-addressed and shared
across the whole store, so a fork of a 3 GB repository costs a few hundred rows rather
than 3 GB -- and the garbage collector already treats liveness as global, so the shared
blobs stay safe as long as either side references them.

Commits are shared too, and that is forced rather than chosen: ``Commit.id`` is the whole
primary key, so a commit row belongs to exactly one repository and cannot be duplicated
under another. A fork's branches therefore point at the upstream's commit rows. Reading
content, diffing and checking out all work through the commit id and are unaffected; what
this does mean is that listing "commits in this repository" by ``repository_id`` shows
only the fork's *own* new commits, so ``commits_for_repository`` below walks the branches
instead.

The fork's branch policy deliberately starts from defaults rather than being inherited:
the upstream's protected branches and required approvals are the upstream's decisions,
and silently applying them to somebody else's copy would be surprising.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database.crud import BranchCRUD, RepositoryCRUD
from database.models import (
    Branch, Commit, CommitFile, CommitParent, PullRequest, Repository, Tag, User,
)


class ForkError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def fork_repository(
    db: Session,
    source: Repository,
    actor: User,
    name: Optional[str] = None,
) -> Repository:
    """Create the actor's own copy of a repository, remembering where it came from."""
    if source.owner_id == actor.id:
        raise ForkError("You already own this repository.", 400)

    repo_name = (name or source.name).strip()
    if not repo_name:
        raise ForkError("A fork needs a name")

    fork_id = RepositoryCRUD.generate_repo_id(actor.username, repo_name)
    if RepositoryCRUD.get_repository(db, fork_id):
        raise ForkError(
            f"You already have a repository called '{repo_name}'. "
            f"Pass a different name to fork under.", 409,
        )

    fork = Repository(
        id=fork_id,
        name=repo_name,
        description=source.description,
        owner_id=actor.id,
        is_public=source.is_public,
        language=source.language,
        size_bytes=source.size_bytes,
        head_commit_id=source.head_commit_id,
        forked_from_id=source.id,
        # Policy is deliberately not inherited; see the module docstring.
        branch_policy_json=None,
    )
    db.add(fork)
    db.flush()

    for branch in db.query(Branch).filter(Branch.repository_id == source.id).all():
        db.add(Branch(
            repository_id=fork.id,
            name=branch.name,
            head_commit_id=branch.head_commit_id,
            is_default=branch.is_default,
        ))

    for tag in db.query(Tag).filter(Tag.repository_id == source.id).all():
        db.add(Tag(
            repository_id=fork.id,
            name=tag.name,
            commit_id=tag.commit_id,
            message=tag.message,
            created_by_id=tag.created_by_id,
        ))

    db.commit()
    db.refresh(fork)
    return fork


def open_cross_repo_pull_request(
    db: Session,
    fork: Repository,
    upstream: Repository,
    actor: User,
    source_branch: str,
    target_branch: str,
    title: str,
    description: Optional[str] = None,
) -> PullRequest:
    """Propose a fork's branch back to the repository it came from.

    The author needs no write access upstream -- that is the entire point of a fork. The
    upstream's own merge rules still apply when someone there tries to accept it.
    """
    if fork.forked_from_id != upstream.id:
        raise ForkError(
            f"'{fork.name}' is not a fork of '{upstream.name}'.", 400
        )

    source = BranchCRUD.get_branch(db, fork.id, source_branch)
    if not source:
        raise ForkError(f"Branch '{source_branch}' not found in the fork", 404)
    target = BranchCRUD.get_branch(db, upstream.id, target_branch)
    if not target:
        raise ForkError(f"Branch '{target_branch}' not found upstream", 404)

    if not (title or "").strip():
        raise ForkError("A pull request needs a title")

    duplicate = (
        db.query(PullRequest)
        .filter(
            PullRequest.repository_id == upstream.id,
            PullRequest.source_repository_id == fork.id,
            PullRequest.source_branch == source_branch,
            PullRequest.target_branch == target_branch,
            PullRequest.status == "open",
        )
        .first()
    )
    if duplicate:
        raise ForkError(
            f"An open pull request for this branch already exists (#{duplicate.id}).", 409
        )

    pr = PullRequest(
        repository_id=upstream.id,
        source_repository_id=fork.id,
        title=title.strip(),
        description=(description or "").strip() or None,
        source_branch=source_branch,
        target_branch=target_branch,
        status="open",
        created_by_id=actor.id,
    )
    db.add(pr)
    db.commit()
    db.refresh(pr)
    return pr


def list_forks(db: Session, repository: Repository) -> List[Dict[str, Any]]:
    rows = (
        db.query(Repository)
        .filter(Repository.forked_from_id == repository.id)
        .order_by(Repository.created_at)
        .all()
    )
    return [
        {
            "repo_id": row.id,
            "name": row.name,
            "owner": row.owner.username if row.owner else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def describe(db: Session, repository: Repository) -> Dict[str, Any]:
    """Fork relationships in both directions."""
    parent = repository.forked_from
    return {
        "repo_id": repository.id,
        "is_fork": repository.forked_from_id is not None,
        "forked_from": (
            {
                "repo_id": parent.id,
                "name": parent.name,
                "owner": parent.owner.username if parent and parent.owner else None,
            }
            if parent else None
        ),
        "forks": list_forks(db, repository),
    }


def commits_for_repository(db: Session, repository: Repository) -> List[str]:
    """Commit ids belonging to a repository, including inherited ones in a fork.

    A plain repository owns its commit rows. A fork's branches point at the upstream's,
    so filtering by ``repository_id`` alone would report a fork as having no history at
    all; walking the branch heads gives the answer a user expects.
    """
    from app.services.commit_graph import _collect_reachable_commits

    own = {row.id for row in
           db.query(Commit.id).filter(Commit.repository_id == repository.id).all()}
    if repository.forked_from_id is None:
        return sorted(own)

    reachable: set = set(own)
    for branch in db.query(Branch).filter(Branch.repository_id == repository.id).all():
        if branch.head_commit_id:
            reachable.update(_collect_reachable_commits(db, branch.head_commit_id))
    return sorted(reachable)
