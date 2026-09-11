"""Branch-level operations governed by a repository's branch policy.

``branch_policies.py`` has held the rules -- protected branches, per-operation minimum
scopes -- since it was written, but nothing imported it and the column it reads was never
created, so every repository silently ran on defaults with no enforcement anywhere. This
module is the missing half: it applies those rules and provides the three operations the
policy names but that had no implementation.

    merge       three-way merge of one branch into another, without a pull request
    publish     fast-forward only; refuses when the target has diverged
    copy-files  take specific paths from one branch onto another

Publish is deliberately fast-forward only. A "publish" that quietly created a merge commit
when the target had moved would be a merge wearing the wrong name, and the caller asked
for the safe operation.
"""

import hashlib
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import branch_policies
from database.crud import ActivityCRUD, BranchCRUD, CommitCRUD, FileObjectCRUD
from app.services import webhooks as hook_service
from database.models import Repository, User

from app.services.branches import _get_default_branch, _normalize_branch_name
from app.services.commit_graph import _find_merge_base, _get_commit_tree, _is_ancestor
from app.services.merge import _merge_trees


class BranchOpError(Exception):
    def __init__(self, message: str, status_code: int = 400, payload: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


# --- policy ---------------------------------------------------------------------

def get_policy(repository: Repository) -> Dict[str, Any]:
    return branch_policies.get_branch_policy(repository)


def set_policy(db: Session, repository: Repository, policy: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a policy, keeping only recognised keys and validated scopes."""
    defaults = branch_policies.default_branch_policy()
    cleaned: Dict[str, Any] = {}

    for key, default_value in defaults.items():
        if key in ("protected_branches", "required_status_checks"):
            continue
        if key == "required_approvals":
            try:
                cleaned[key] = max(0, int(policy.get(key, default_value)))
            except (TypeError, ValueError):
                raise BranchOpError("required_approvals must be a non-negative integer")
            continue
        value = policy.get(key, default_value)
        if value not in branch_policies.SCOPE_ORDER:
            raise BranchOpError(
                f"'{key}' must be one of {', '.join(branch_policies.SCOPE_ORDER)}, got {value!r}"
            )
        cleaned[key] = value

    protected = policy.get("protected_branches", [])
    if not isinstance(protected, list):
        raise BranchOpError("protected_branches must be a list of branch names")
    cleaned["protected_branches"] = sorted(
        {_normalize_branch_name(str(name)) for name in protected if str(name).strip()}
    )

    # Status check contexts are free-form names reported by CI, not branch names,
    # so they are kept verbatim apart from trimming and de-duplication. Order is
    # preserved because it is how the UI lists them.
    required_checks = policy.get("required_status_checks", defaults["required_status_checks"])
    if not isinstance(required_checks, list):
        raise BranchOpError("required_status_checks must be a list of check names")
    seen, checks = set(), []
    for name in required_checks:
        text = str(name).strip()
        if text and text not in seen:
            seen.add(text)
            checks.append(text)
    cleaned["required_status_checks"] = checks

    repository.branch_policy_json = json.dumps(cleaned, sort_keys=True)
    db.commit()
    return cleaned


def actor_scope(db: Session, actor: User, repository: Repository) -> str:
    return branch_policies.actor_branch_scope(db, actor, repository)


def enforce(db: Session, actor: User, repository: Repository, operation: str,
            branch_name: str) -> None:
    """Enforce the policy's minimum scope for one operation on one branch."""
    policy = get_policy(repository)
    default_branch = _get_default_branch(db, repository.id).name
    key = f"{operation}_min_scope"
    minimum = policy.get(key, "write")

    if not branch_policies.actor_meets_scope(db, actor, repository, minimum):
        raise BranchOpError(
            f"'{operation.replace('_', ' ')}' requires '{minimum}' access on this "
            f"repository; you have '{actor_scope(db, actor, repository)}'.", 403,
        )

    if branch_policies.is_protected_branch(policy, branch_name, default_branch):
        required = policy.get("default_branch_push_min_scope", "manage")
        if not branch_policies.actor_meets_scope(db, actor, repository, required):
            raise BranchOpError(
                f"'{branch_name}' is protected; this requires '{required}' access.", 403,
                {"code": "PROTECTED_BRANCH", "branch": branch_name},
            )


def _resolve_pair(db: Session, repo_id: str, source_name: str, target_name: str):
    source = BranchCRUD.get_branch(db, repo_id, _normalize_branch_name(source_name))
    target = BranchCRUD.get_branch(db, repo_id, _normalize_branch_name(target_name))
    if not source:
        raise BranchOpError(f"Branch '{source_name}' not found", 404)
    if not target:
        raise BranchOpError(f"Branch '{target_name}' not found", 404)
    if source.name == target.name:
        raise BranchOpError("Source and target are the same branch")
    return source, target


def _check_expected_head(target, expected_head_commit_id: Optional[str]) -> None:
    if expected_head_commit_id is not None and expected_head_commit_id != target.head_commit_id:
        raise BranchOpError(
            f"'{target.name}' is at {(target.head_commit_id or 'none')[:12]}, not "
            f"{expected_head_commit_id[:12]}.", 409, {"code": "HEAD_MISMATCH"},
        )


def _write(db: Session, repo_id: str, target, tree: Dict[str, bytes], author: str,
           message: str, parents: List[str], seed: str):
    commit_id = hashlib.sha256(
        f"{seed}:{repo_id}:{datetime.utcnow().isoformat()}".encode()
    ).hexdigest()[:40]
    entries = []
    for path, content in tree.items():
        stored = FileObjectCRUD.store_file_object(db, content)
        entries.append(
            SimpleNamespace(file_path=path, file_hash=stored.hash, file_size=len(content))
        )
    commit = CommitCRUD.create_commit_from_file_hashes(
        db,
        {
            "id": commit_id,
            "repository_id": repo_id,
            "author": author,
            "message": message,
            "parents": [p for p in parents if p],
            "timestamp": datetime.utcnow().isoformat(),
        },
        entries,
    )
    BranchCRUD.update_branch_head(db, repo_id, target.name, commit.id)
    return commit


# --- operations -----------------------------------------------------------------

def merge_branches(db: Session, repository: Repository, actor: User, source_name: str,
                   target_name: str, expected_head_commit_id: Optional[str] = None,
                   dry_run: bool = False) -> Dict[str, Any]:
    """Three-way merge source into target, creating a merge commit."""
    source, target = _resolve_pair(db, repository.id, source_name, target_name)
    enforce(db, actor, repository, "merge", target.name)
    _check_expected_head(target, expected_head_commit_id)

    if _is_ancestor(db, source.head_commit_id, target.head_commit_id):
        return {"status": "already_up_to_date", "source": source.name, "target": target.name}

    base_id = _find_merge_base(db, target.head_commit_id, source.head_commit_id)
    merged, conflicts = _merge_trees(
        _get_commit_tree(db, base_id),
        _get_commit_tree(db, target.head_commit_id),
        _get_commit_tree(db, source.head_commit_id),
    )
    if conflicts:
        raise BranchOpError(
            f"Merging '{source.name}' into '{target.name}' has conflicts.", 409,
            {"code": "MERGE_CONFLICT", "conflicts": sorted(conflicts),
             "source_branch": source.name, "target_branch": target.name},
        )
    if dry_run:
        return {"status": "clean", "source": source.name, "target": target.name,
                "files": len(merged)}

    commit = _write(
        db, repository.id, target, merged, actor.username,
        f"Merge branch '{source.name}' into '{target.name}'",
        [target.head_commit_id, source.head_commit_id], f"branch-merge:{source.name}",
    )
    ActivityCRUD.create_activity(
        db, actor.id, "merge_branch",
        f"Merged '{source.name}' into '{target.name}' as {commit.id[:8]}", repository.id,
    )

    hook_service.dispatch(db, hook_service.EVENT_BRANCH_MERGED, repository.id, {
        "repository": {"id": repository.id, "name": repository.name},
        "source": source.name,
        "target": target.name,
        "merge_commit": commit.id,
        "merged_by": actor.username,
    })
    return {"status": "merged", "merge_commit_id": commit.id,
            "source": source.name, "target": target.name}


def publish_branch(db: Session, repository: Repository, actor: User, source_name: str,
                   target_name: str, expected_head_commit_id: Optional[str] = None) -> Dict[str, Any]:
    """Fast-forward target to source. Refuses if the target has diverged."""
    source, target = _resolve_pair(db, repository.id, source_name, target_name)
    enforce(db, actor, repository, "publish", target.name)
    _check_expected_head(target, expected_head_commit_id)

    if target.head_commit_id == source.head_commit_id:
        return {"status": "already_up_to_date", "source": source.name, "target": target.name}

    if target.head_commit_id and not _is_ancestor(db, target.head_commit_id, source.head_commit_id):
        raise BranchOpError(
            f"'{target.name}' has commits that '{source.name}' does not, so this cannot "
            f"fast-forward. Merge instead if you want to combine them.", 409,
            {"code": "NOT_FAST_FORWARD", "source": source.name, "target": target.name},
        )

    previous = target.head_commit_id
    BranchCRUD.update_branch_head(db, repository.id, target.name, source.head_commit_id)
    ActivityCRUD.create_activity(
        db, actor.id, "publish_branch",
        f"Fast-forwarded '{target.name}' to '{source.name}' "
        f"({(previous or 'none')[:8]} -> {source.head_commit_id[:8]})", repository.id,
    )

    hook_service.dispatch(db, hook_service.EVENT_PUSH, repository.id, {
        "repository": {"id": repository.id, "name": repository.name},
        "branch": target.name,
        "before": previous,
        "after": source.head_commit_id,
        "pusher": actor.username,
        "reason": "fast-forward publish",
    })
    return {"status": "published", "source": source.name, "target": target.name,
            "previous_head": previous, "new_head": source.head_commit_id}


def copy_files(db: Session, repository: Repository, actor: User, source_name: str,
               target_name: str, paths: List[str],
               expected_head_commit_id: Optional[str] = None) -> Dict[str, Any]:
    """Take specific paths from source onto target, leaving everything else alone."""
    source, target = _resolve_pair(db, repository.id, source_name, target_name)
    enforce(db, actor, repository, "copy_files", target.name)
    _check_expected_head(target, expected_head_commit_id)

    wanted = [p for p in (paths or []) if p and p.strip()]
    if not wanted:
        raise BranchOpError("No paths given to copy")

    source_tree = _get_commit_tree(db, source.head_commit_id)
    target_tree = dict(_get_commit_tree(db, target.head_commit_id))

    missing = [p for p in wanted if p not in source_tree]
    if missing:
        raise BranchOpError(
            f"Not present on '{source.name}': {', '.join(missing[:5])}", 404,
            {"missing": missing},
        )

    changed = [p for p in wanted if target_tree.get(p) != source_tree[p]]
    if not changed:
        return {"status": "already_up_to_date", "copied": 0,
                "source": source.name, "target": target.name}

    for path in changed:
        target_tree[path] = source_tree[path]

    commit = _write(
        db, repository.id, target, target_tree, actor.username,
        f"Copy {len(changed)} path(s) from '{source.name}' into '{target.name}'\n\n"
        + "\n".join(f"  {p}" for p in changed[:20]),
        # Single parent: this takes content from the source without inheriting its
        # history, which is what makes it a copy rather than a merge.
        [target.head_commit_id], f"copy-files:{source.name}",
    )
    ActivityCRUD.create_activity(
        db, actor.id, "copy_files",
        f"Copied {len(changed)} path(s) from '{source.name}' to '{target.name}' "
        f"as {commit.id[:8]}", repository.id,
    )
    return {"status": "copied", "commit_id": commit.id, "copied": len(changed),
            "paths": changed, "source": source.name, "target": target.name}
