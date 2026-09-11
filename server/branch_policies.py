"""
Branch policy, merge, publish (fast-forward), and copy-files helpers for FoxNest server.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from database.crud import BranchCRUD, CommitCRUD, UserPermissionCRUD
from database.models import Repository, User

# scope levels (lowest to highest privilege for branch operations)
SCOPE_ORDER = ("read", "write", "manage", "team_lead")


def default_branch_policy() -> Dict[str, Any]:
    return {
        "create_branch_min_scope": "write",
        "push_min_scope": "write",
        "pull_min_scope": "read",
        "merge_min_scope": "manage",
        "publish_min_scope": "manage",
        "copy_files_min_scope": "write",
        "default_branch_push_min_scope": "manage",
        "protected_branches": [],
        # Current approvals a pull request needs before it can merge. 0 keeps the gate
        # off, so upgrading does not silently start blocking existing workflows.
        "required_approvals": 0,
        # Status check contexts that must be green before a merge, e.g.
        # ["ci/unit-tests"]. Empty for the same reason as above.
        "required_status_checks": [],
    }


def get_branch_policy(repository: Repository) -> Dict[str, Any]:
    policy = default_branch_policy()
    raw = getattr(repository, "branch_policy_json", None)
    if not raw:
        return policy
    try:
        loaded = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(loaded, dict):
            policy.update({k: v for k, v in loaded.items() if k in policy or k == "protected_branches"})
    except Exception:
        pass
    if not isinstance(policy.get("protected_branches"), list):
        policy["protected_branches"] = []
    return policy


def _scope_rank(scope: str) -> int:
    try:
        return SCOPE_ORDER.index(scope)
    except ValueError:
        return SCOPE_ORDER.index("write")


def actor_branch_scope(db: Session, actor: User, repository: Repository) -> str:
    """Highest branch-related scope for actor on this repository."""
    if not actor:
        return "read"
    if actor.id == repository.owner_id:
        return "team_lead"
    role = getattr(actor, "role", "developer") or "developer"
    if role in ("admin", "team_lead"):
        return "team_lead"
    if UserPermissionCRUD.has_permission(db, actor.username, repository.id, "team_lead"):
        return "team_lead"
    if UserPermissionCRUD.has_permission(db, actor.username, repository.id, "admin"):
        return "team_lead"
    if UserPermissionCRUD.has_permission(db, actor.username, repository.id, "manage"):
        return "manage"
    if UserPermissionCRUD.has_permission(db, actor.username, repository.id, "write"):
        return "write"
    # Prior commit authorship grants write (matches can_write_repository)
    from database.models import Commit

    if db.query(Commit).filter(
        Commit.repository_id == repository.id,
        Commit.author_id == actor.id,
    ).first():
        return "write"
    return "read"


def actor_meets_scope(db: Session, actor: User, repository: Repository, min_scope: str) -> bool:
    return _scope_rank(actor_branch_scope(db, actor, repository)) >= _scope_rank(min_scope or "write")


def is_protected_branch(policy: Dict[str, Any], branch_name: str, default_branch_name: str) -> bool:
    protected = set(policy.get("protected_branches") or [])
    if branch_name == default_branch_name:
        return True
    return branch_name in protected


def enforce_branch_create(
    db: Session,
    actor: User,
    repository: Repository,
    branch_name: str,
    default_branch_name: str,
) -> None:
    policy = get_branch_policy(repository)
    min_scope = policy.get("create_branch_min_scope", "write")
    if not actor_meets_scope(db, actor, repository, min_scope):
        raise HTTPException(
            status_code=403,
            detail=f"Creating branches requires '{min_scope}' access on this repository.",
        )


def enforce_branch_push(
    db: Session,
    actor: User,
    repository: Repository,
    branch_name: str,
    default_branch_name: str,
) -> None:
    policy = get_branch_policy(repository)
    min_scope = policy.get("push_min_scope", "write")
    if not actor_meets_scope(db, actor, repository, min_scope):
        raise HTTPException(
            status_code=403,
            detail=f"Pushing to branches requires '{min_scope}' access on this repository.",
        )
    if is_protected_branch(policy, branch_name, default_branch_name):
        req = policy.get("default_branch_push_min_scope", "manage")
        if not actor_meets_scope(db, actor, repository, req):
            raise HTTPException(
                status_code=403,
                detail=f"Branch '{branch_name}' is protected; push requires '{req}' access.",
            )


def enforce_branch_merge(
    db: Session,
    actor: User,
    repository: Repository,
    target_branch_name: str,
    default_branch_name: str,
) -> None:
    policy = get_branch_policy(repository)
    min_scope = policy.get("merge_min_scope", "manage")
    if is_protected_branch(policy, target_branch_name, default_branch_name):
        if not actor_meets_scope(db, actor, repository, min_scope):
            raise HTTPException(
                status_code=403,
                detail=f"Merging into '{target_branch_name}' requires '{min_scope}' access.",
            )
    elif not actor_meets_scope(db, actor, repository, min_scope):
        raise HTTPException(
            status_code=403,
            detail=f"Branch merge requires '{min_scope}' access on this repository.",
        )


def _commit_parent_ids(commit) -> List[str]:
    parents: List[str] = []
    if not commit:
        return parents
    if getattr(commit, "parent_commit_id", None):
        parents.append(commit.parent_commit_id)
    for link in getattr(commit, "parent_links", None) or []:
        pid = getattr(link, "parent_commit_id", None)
        if pid and pid not in parents:
            parents.append(pid)
    return parents


def collect_reachable_commit_ids(db: Session, head_commit_id: Optional[str]) -> Set[str]:
    if not head_commit_id:
        return set()
    seen: Set[str] = set()
    stack = [head_commit_id]
    while stack:
        cid = stack.pop()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        commit = CommitCRUD.get_commit(db, cid)
        if not commit:
            continue
        for parent in _commit_parent_ids(commit):
            if parent:
                stack.append(parent)
    return seen


def is_ancestor(db: Session, ancestor_id: str, descendant_id: str) -> bool:
    if not ancestor_id or not descendant_id:
        return False
    if ancestor_id == descendant_id:
        return True
    reachable = collect_reachable_commit_ids(db, descendant_id)
    return ancestor_id in reachable
