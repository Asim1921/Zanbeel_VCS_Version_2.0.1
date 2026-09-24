"""The only component permitted to change a branch reference.

Before this module there were sixteen places that moved, created, renamed or deleted a
branch, and two of them checked a policy. A protected branch could still be moved by a
rollback, a rebase, a pull-request merge or ``PUT /branches/{name}/head``. Protection that
only covers the paths somebody remembered is not protection, so every one of those call
sites now funnels through here.

Three things happen together, in one transaction, or not at all:

* **compare-and-swap** -- the caller states the commit and generation it believes the
  branch is at. If either has moved, the update is refused rather than applied on top of
  somebody else's work.
* **policy evaluation** -- the branch's mode decides whether it accepts this kind of
  operation from anyone, which is separate from whether this actor holds the scope for it.
* **audit** -- the decision is recorded either way. A refused force-push is the event
  worth having.

A force update is detected here rather than trusted from the caller: if the proposed
commit is not a descendant of the current tip, the operation is reclassified as
``force_update`` whatever the caller called it. That is what stops a force being
relabelled as an ordinary push to slip past the rules.
"""

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from database.crud import BranchCRUD
from database.models import Branch, BranchUnlock, RefAuditEvent, Repository, User

from app.services import branch_protection as protection
from app.services.branch_protection import (
    OP_CREATE, OP_DELETE, OP_FORCE_UPDATE, OP_MERGE, OP_RENAME, OP_UPDATE,
)

import branch_policies as legacy_policies


class ReferenceError(HTTPException):
    """A refused reference change, carrying the machine-readable reason.

    Subclasses HTTPException deliberately. Routes all over this codebase end in
    ``except HTTPException: raise`` followed by a catch-all that reports 500, so a plain
    exception would have been reported as a server fault and the caller would lose the
    error code telling them *why* they were refused. A dedicated handler in app/main.py
    still renders the richer body.
    """

    def __init__(self, message: str, code: str, status_code: int = 403,
                 payload: Optional[Dict[str, Any]] = None):
        super().__init__(status_code=status_code, detail=message)
        self.message = message
        self.code = code
        self.payload = payload or {}


# ---------------------------------------------------------------- audit

def _event_hash(payload: Dict[str, Any], previous: Optional[str]) -> str:
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{previous or ''}:{material}".encode("utf-8")).hexdigest()


def record_event(
    db: Session,
    *,
    repository_id: str,
    reference: str,
    event_type: str,
    decision: str,
    actor: Optional[User] = None,
    operation: Optional[str] = None,
    old_commit_id: Optional[str] = None,
    new_commit_id: Optional[str] = None,
    old_generation: Optional[int] = None,
    new_generation: Optional[int] = None,
    effective: Optional[protection.EffectivePolicy] = None,
    error_code: Optional[str] = None,
    reason: Optional[str] = None,
) -> RefAuditEvent:
    """Append one hash-chained audit event.

    Chained per repository: each event carries the hash of the one before it, so removing
    or editing an event in the middle breaks every hash after it.
    """
    # The actor may have been loaded in another session by the time we audit; reading
    # a detached instance would raise and lose the event, which is the one thing an
    # audit log must not do.
    try:
        actor_id = actor.id if actor else None
        actor_name = actor.username if actor else None
    except Exception:
        actor_id, actor_name = None, None

    previous = (
        db.query(RefAuditEvent)
        .filter(RefAuditEvent.repository_id == repository_id)
        .order_by(RefAuditEvent.id.desc())
        .first()
    )
    payload = {
        "repository_id": repository_id,
        "reference": reference,
        "event_type": event_type,
        "operation": operation,
        "actor": actor_name,
        "old_commit_id": old_commit_id,
        "new_commit_id": new_commit_id,
        "old_generation": old_generation,
        "new_generation": new_generation,
        "decision": decision,
        "error_code": error_code,
        "occurred_at": datetime.utcnow().isoformat(),
    }
    event = RefAuditEvent(
        repository_id=repository_id,
        event_type=event_type,
        reference=reference,
        operation=operation,
        actor_id=actor_id,
        actor_username=actor_name,
        old_commit_id=old_commit_id,
        new_commit_id=new_commit_id,
        old_generation=old_generation,
        new_generation=new_generation,
        policy_id=effective.policy_id if effective else None,
        policy_version=effective.policy_version if effective else None,
        mode=effective.mode if effective else None,
        decision=decision,
        error_code=error_code,
        reason=reason,
        previous_event_hash=previous.event_hash if previous else None,
        event_hash=_event_hash(payload, previous.event_hash if previous else None),
    )
    db.add(event)
    return event


# ---------------------------------------------------------------- unlock grants

def active_unlock(
    db: Session, repository_id: str, branch_name: str, operation: str
) -> Optional[BranchUnlock]:
    """An unexpired, unused grant covering this branch and operation."""
    now = datetime.utcnow()
    candidates = (
        db.query(BranchUnlock)
        .filter(
            BranchUnlock.repository_id == repository_id,
            BranchUnlock.branch_name == branch_name,
            BranchUnlock.consumed_at.is_(None),
        )
        .order_by(BranchUnlock.id.desc())
        .all()
    )
    for grant in candidates:
        if grant.expires_at and grant.expires_at < now:
            continue
        allowed = {op.strip() for op in (grant.operations or "").split(",") if op.strip()}
        if operation in allowed or "*" in allowed:
            return grant
    return None


# ---------------------------------------------------------------- the choke point

def _classify(db: Session, current_head: Optional[str], new_commit_id: Optional[str],
              operation: str) -> str:
    """Reclassify an update as a force when it discards history.

    Trusting the caller's label would make the force-update rule trivially avoidable.
    """
    if operation not in (OP_UPDATE, OP_MERGE):
        return operation
    if not current_head or not new_commit_id or current_head == new_commit_id:
        return operation
    if legacy_policies.is_ancestor(db, current_head, new_commit_id):
        return operation          # fast-forward: the tip is still reachable
    return OP_FORCE_UPDATE


def check_reference_operation(
    db: Session,
    *,
    repository: Repository,
    actor: Optional[User],
    branch_name: str,
    operation: str,
    new_commit_id: Optional[str] = None,
    current_head: Optional[str] = None,
    record: bool = True,
) -> protection.EffectivePolicy:
    """Decide whether `operation` may touch `branch_name`. Raises ReferenceError if not.

    Exposed separately so a caller can validate before doing expensive work (building a
    merge tree, say) and again inside the transaction that applies the change.
    """
    effective = protection.resolve_effective_policy(db, repository, branch_name)
    resolved = _classify(db, current_head, new_commit_id, operation)

    if effective.permits(resolved):
        return effective

    grant = active_unlock(db, repository.id, branch_name, resolved)
    if grant:
        return effective

    code = effective.denial_code(resolved)
    message = (
        f"'{branch_name}' is {effective.mode}; "
        f"{resolved.replace('_', ' ')} is not permitted on this branch."
    )
    if record:
        record_event(
            db,
            repository_id=repository.id,
            reference=branch_name,
            event_type=f"branch.{resolved}.denied",
            decision="denied",
            actor=actor,
            operation=resolved,
            old_commit_id=current_head,
            new_commit_id=new_commit_id,
            effective=effective,
            error_code=code,
        )
        db.commit()
    raise ReferenceError(
        message, code, 403,
        {
            "branch": branch_name,
            "mode": effective.mode,
            "policy_id": effective.policy_id,
            "policy_version": effective.policy_version,
            "current_commit": current_head,
        },
    )


def update_reference(
    db: Session,
    *,
    repository: Repository,
    actor: Optional[User],
    branch_name: str,
    new_commit_id: str,
    operation: str = OP_UPDATE,
    expected_commit_id: Optional[str] = None,
    expected_generation: Optional[int] = None,
    create_if_missing: bool = False,
) -> Branch:
    """Move a branch, or create it, having satisfied policy and compare-and-swap."""
    branch = BranchCRUD.get_branch(db, repository.id, branch_name)

    if branch is None:
        if not create_if_missing:
            raise ReferenceError(
                f"Branch '{branch_name}' not found", "STALE_REFERENCE", 404,
                {"branch": branch_name},
            )
        effective = check_reference_operation(
            db, repository=repository, actor=actor, branch_name=branch_name,
            operation=OP_CREATE, new_commit_id=new_commit_id, current_head=None,
        )
        branch = BranchCRUD.create_branch(
            db, repository.id, branch_name, head_commit_id=new_commit_id
        )
        record_event(
            db, repository_id=repository.id, reference=branch_name,
            event_type="branch.create.completed", decision="allowed", actor=actor,
            operation=OP_CREATE, new_commit_id=new_commit_id,
            old_generation=None, new_generation=getattr(branch, "generation", 0) or 0,
            effective=effective,
        )
        db.commit()
        return branch

    current_head = branch.head_commit_id
    current_generation = branch.generation or 0

    # Compare-and-swap: refuse rather than overwrite somebody else's update.
    if expected_commit_id is not None and expected_commit_id != current_head:
        raise ReferenceError(
            f"'{branch_name}' is at {(current_head or 'none')[:12]}, not "
            f"{expected_commit_id[:12]}.",
            "STALE_REFERENCE", 409,
            {"branch": branch_name, "current_commit": current_head,
             "current_generation": current_generation},
        )
    if expected_generation is not None and expected_generation != current_generation:
        raise ReferenceError(
            f"'{branch_name}' has moved since you read it "
            f"(generation {current_generation}, you expected {expected_generation}).",
            "STALE_REFERENCE", 409,
            {"branch": branch_name, "current_commit": current_head,
             "current_generation": current_generation},
        )

    effective = check_reference_operation(
        db, repository=repository, actor=actor, branch_name=branch_name,
        operation=operation, new_commit_id=new_commit_id, current_head=current_head,
    )
    resolved = _classify(db, current_head, new_commit_id, operation)
    grant = active_unlock(db, repository.id, branch_name, resolved)

    branch = BranchCRUD.update_branch_head(db, repository.id, branch_name, new_commit_id)
    branch.generation = current_generation + 1
    if grant:
        # Single use: spend the grant now so it cannot authorise a second change.
        grant.consumed_at = datetime.utcnow()
        grant.consumed_by_id = actor.id if actor else None

    record_event(
        db, repository_id=repository.id, reference=branch_name,
        event_type=f"branch.{resolved}.completed", decision="allowed", actor=actor,
        operation=resolved, old_commit_id=current_head, new_commit_id=new_commit_id,
        old_generation=current_generation, new_generation=branch.generation,
        effective=effective,
        reason=f"unlock grant {grant.id}" if grant else None,
    )
    db.commit()
    return branch


def delete_reference(
    db: Session, *, repository: Repository, actor: Optional[User], branch_name: str
) -> None:
    branch = BranchCRUD.get_branch(db, repository.id, branch_name)
    if not branch:
        raise ReferenceError(f"Branch '{branch_name}' not found", "STALE_REFERENCE", 404)

    effective = check_reference_operation(
        db, repository=repository, actor=actor, branch_name=branch_name,
        operation=OP_DELETE, current_head=branch.head_commit_id,
    )
    grant = active_unlock(db, repository.id, branch_name, OP_DELETE)
    head, generation = branch.head_commit_id, branch.generation or 0

    BranchCRUD.delete_branch(db, repository.id, branch_name)
    if grant:
        grant.consumed_at = datetime.utcnow()
        grant.consumed_by_id = actor.id if actor else None

    record_event(
        db, repository_id=repository.id, reference=branch_name,
        event_type="branch.delete.completed", decision="allowed", actor=actor,
        operation=OP_DELETE, old_commit_id=head, old_generation=generation,
        effective=effective,
    )
    db.commit()


def rename_reference(
    db: Session, *, repository: Repository, actor: Optional[User],
    old_name: str, new_name: str,
) -> Branch:
    branch = BranchCRUD.get_branch(db, repository.id, old_name)
    if not branch:
        raise ReferenceError(f"Branch '{old_name}' not found", "STALE_REFERENCE", 404)

    # Both names are checked: renaming out of a protected pattern must not be a way to
    # escape it, and renaming into one must not be a way to occupy it.
    effective = check_reference_operation(
        db, repository=repository, actor=actor, branch_name=old_name,
        operation=OP_RENAME, current_head=branch.head_commit_id,
    )
    check_reference_operation(
        db, repository=repository, actor=actor, branch_name=new_name,
        operation=OP_CREATE, current_head=None,
    )

    grant = active_unlock(db, repository.id, old_name, OP_RENAME)
    renamed = BranchCRUD.rename_branch(db, repository.id, old_name, new_name)
    if grant:
        grant.consumed_at = datetime.utcnow()
        grant.consumed_by_id = actor.id if actor else None

    record_event(
        db, repository_id=repository.id, reference=old_name,
        event_type="branch.rename.completed", decision="allowed", actor=actor,
        operation=OP_RENAME, old_commit_id=branch.head_commit_id,
        effective=effective, reason=f"renamed to {new_name}",
    )
    db.commit()
    return renamed


def update_reference_by_id(
    db: Session,
    *,
    repository_id: str,
    actor_username: Optional[str],
    branch_name: str,
    new_commit_id: str,
    operation: str = OP_UPDATE,
    expected_commit_id: Optional[str] = None,
    create_if_missing: bool = False,
) -> Branch:
    """`update_reference` for callers deep in the services that hold ids, not objects.

    Resolving here keeps those function signatures unchanged, which matters because the
    alternative -- threading a Repository and a User through every history operation --
    is the kind of churn that tempts someone to skip the choke point later.
    """
    from database.crud import RepositoryCRUD, UserCRUD

    repository = RepositoryCRUD.get_repository(db, repository_id)
    if repository is None:
        raise ReferenceError(
            f"Repository '{repository_id}' not found", "STALE_REFERENCE", 404
        )
    actor = UserCRUD.get_user_by_username(db, actor_username) if actor_username else None
    return update_reference(
        db,
        repository=repository,
        actor=actor,
        branch_name=branch_name,
        new_commit_id=new_commit_id,
        operation=operation,
        expected_commit_id=expected_commit_id,
        create_if_missing=create_if_missing,
    )
