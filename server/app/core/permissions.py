"""Repository- and issue-level authorisation rules."""

from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from database.crud import RepositoryCRUD, UserPermissionCRUD
from database.models import Commit, Repository, User, UserPermission

from app.core.dependencies import get_actor_user
from app.services.branches import _get_default_branch, _normalize_branch_name


def can_manage_repository(db: Session, actor: User, repository: Repository) -> bool:
    """Return True when actor can perform management actions (archive/delete/admin-level)."""
    if not actor:
        return False

    if actor.id == repository.owner_id:
        return True

    actor_role = getattr(actor, 'role', 'developer')
    if actor_role in ['team_lead', 'admin']:
        return True

    if UserPermissionCRUD.has_permission(db, actor.username, repository.id, 'team_lead'):
        return True
    if UserPermissionCRUD.has_permission(db, actor.username, repository.id, 'admin'):
        return True

    return False


def user_sees_all_repository_branches(db: Session, actor: User, repository: Repository) -> bool:
    """Admins/owners, commit authors, and non-issue-scoped collaborators see every branch.

    Users with only issue-scoped permission rows (issue_id set) and no commits on this repo
    only see the default branch.
    """
    if not actor:
        return False
    if can_manage_repository(db, actor, repository):
        return True
    if actor.id == repository.owner_id:
        return True
    if db.query(Commit).filter(
        Commit.repository_id == repository.id,
        Commit.author_id == actor.id,
    ).first():
        return True
    if db.query(UserPermission).filter(
        UserPermission.repository_id == repository.id,
        UserPermission.user_id == actor.id,
        UserPermission.issue_id.is_(None),
    ).first():
        return True
    return False


def can_write_repository(db: Session, actor: User, repository: Repository) -> bool:
    """Return True when actor can modify repository content/metadata (write+)."""
    if not actor:
        return False

    if actor.id == repository.owner_id:
        return True

    actor_role = getattr(actor, 'role', 'developer')
    if actor_role in ['team_lead', 'admin']:
        return True

    # If the user has already authored commits in this repository,
    # allow continued write access to their contributed code.
    authored_commit = db.query(Commit).filter(
        Commit.repository_id == repository.id,
        Commit.author_id == actor.id
    ).first()
    if authored_commit:
        return True

    return UserPermissionCRUD.has_permission(db, actor.username, repository.id, 'write')

def require_repository_access(db: Session, actor_username: Optional[str], repository: Repository, action: str, required_scope: str = "manage") -> User:
    """Validate actor has required access to repository; raise 403 otherwise."""
    actor = get_actor_user(db, actor_username)
    if required_scope == "write":
        allowed = can_write_repository(db, actor, repository)
    else:
        allowed = can_manage_repository(db, actor, repository)

    if not allowed:
        raise HTTPException(
            status_code=403,
            detail=f"User '{actor.username}' is not allowed to {action} repository '{repository.name}'"
        )
    return actor


def _is_privileged_role(role: Optional[str]) -> bool:
    return (role or "developer") in ["team_lead", "admin"]


def _user_can_view_issues(db: Session, actor: User, repository: Repository) -> bool:
    if _is_privileged_role(getattr(actor, "role", "developer")):
        return True
    accessible_ids = {r.id for r in RepositoryCRUD.get_accessible_repositories_for_user(db, actor)}
    return repository.id in accessible_ids


def _actor_has_issue_only_access(db: Session, actor: Optional[User], repository: Repository) -> bool:
    """True when actor only has issue-scoped access (no commits / no non-issue permission / not manager).

    Used to prevent issue-only users from browsing repo contents (branches/files/docs), even when
    the repository is public in the UI.
    """
    if not actor:
        return False
    if can_manage_repository(db, actor, repository):
        return False
    if db.query(Commit).filter(Commit.repository_id == repository.id, Commit.author_id == actor.id).first():
        return False
    # Non-issue permission present → not issue-only
    if db.query(UserPermission).filter(
        UserPermission.repository_id == repository.id,
        UserPermission.user_id == actor.id,
        UserPermission.issue_id.is_(None),
    ).first():
        return False
    # Any issue-scoped permission → issue-only
    return db.query(UserPermission).filter(
        UserPermission.repository_id == repository.id,
        UserPermission.user_id == actor.id,
        UserPermission.issue_id.isnot(None),
    ).first() is not None


def _user_can_read_repository_contents(db: Session, actor: Optional[User], repository: Repository) -> bool:
    """Permission to read repository *contents* (branches/files/commits/docs)."""
    if not actor:
        return False
    if can_manage_repository(db, actor, repository):
        return True
    if db.query(Commit).filter(Commit.repository_id == repository.id, Commit.author_id == actor.id).first():
        return True
    # Only non-issue-scoped collaborators can read contents.
    return db.query(UserPermission).filter(
        UserPermission.repository_id == repository.id,
        UserPermission.user_id == actor.id,
        UserPermission.issue_id.is_(None),
    ).first() is not None


def _require_repository_read_access(db: Session, actor: User, repository: Repository, action: str) -> None:
    """Enforce read-only access to repository contents/metadata."""
    # Issue-only users are not allowed to browse repo contents.
    if _actor_has_issue_only_access(db, actor, repository):
        raise HTTPException(status_code=403, detail=f"Not allowed to {action} for this repository")
    # Public visibility does not imply authenticated users can browse the repo in the web UI.
    # Only real collaborators (owner/admin, commit authors, non-issue permissions) can read contents.
    if _user_can_read_repository_contents(db, actor, repository):
        return
    raise HTTPException(status_code=403, detail=f"Not allowed to {action} for this repository")


def _require_repository_checkout_access(
    db: Session,
    actor: User,
    repository: Repository,
    branch: Optional[str],
    action: str,
) -> None:
    """Allow fetching code for working on issues, without allowing full repo browsing.

    - Full collaborators (owner/admin, commit authors, non-issue permissions) can fetch any branch.
    - Issue-only users may fetch the default branch only (so they can work on assigned issues),
      but still cannot browse the repo (files/docs/branches) beyond that.
    """
    if _user_can_read_repository_contents(db, actor, repository):
        return

    if _actor_has_issue_only_access(db, actor, repository):
        default_branch = _get_default_branch(db, repository.id).name
        requested = _normalize_branch_name(branch) if branch else default_branch
        if requested == default_branch:
            return
        raise HTTPException(status_code=403, detail=f"Not allowed to {action} for this branch")

    raise HTTPException(status_code=403, detail=f"Not allowed to {action} for this repository")
