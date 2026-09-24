"""Branch protection: policies, freezing, unlock grants, immutable releases, audit.

The reference service in app/services/refs.py enforces protection on every mutation.
These routes are how a policy gets written, inspected and, exceptionally, suspended.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database.crud import ActivityCRUD, BranchCRUD, CommitCRUD, RepositoryCRUD
from database.database import get_db
from database.models import (
    BranchProtectionPolicy, BranchUnlock, ImmutableRelease, RefAuditEvent, User,
)

from app.config import AUTH_SECRET
from app.core.dependencies import get_current_user
from app.core.permissions import require_repository_access
from app.services import branch_protection as protection
from app.services import refs
from app.services.branches import _normalize_branch_name


router = APIRouter()

#: Longest an unlock may last. Short by design: it is an exception, not a mode.
MAX_UNLOCK_MINUTES = 240


class PolicyRequest(BaseModel):
    branch_pattern: str
    mode: str = protection.MODE_OPEN
    rules: Optional[Dict[str, Any]] = None
    #: The version the caller believes it is editing; a mismatch means someone else
    #: changed the policy first and this edit was written against stale rules.
    expected_policy_version: Optional[int] = None


class UnlockRequest(BaseModel):
    reason: str
    operations: List[str] = ["update"]
    expires_in_minutes: int = 30


class ImmutableReleaseRequest(BaseModel):
    name: str
    commit_id: str
    notes: Optional[str] = None


def _repo_or_404(db: Session, repo_id: str):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repository


@router.get("/api/repository/{repo_id}/branch-protection")
async def list_branch_protection(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stored policies, plus the effective policy for every existing branch."""
    repository = _repo_or_404(db, repo_id)
    require_repository_access(db, current_user.username, repository,
                              "view branch protection for", required_scope="read")

    policies = (
        db.query(BranchProtectionPolicy)
        .filter(BranchProtectionPolicy.repository_id == repo_id)
        .order_by(BranchProtectionPolicy.branch_pattern)
        .all()
    )
    branches = BranchCRUD.get_branches_by_repository(db, repo_id) or []
    effective = {
        branch.name: protection.describe(
            protection.resolve_effective_policy(db, repository, branch.name)
        )
        for branch in branches
    }
    return {
        "success": True,
        "policies": [
            {
                "id": p.id,
                "branch_pattern": p.branch_pattern,
                "mode": p.mode,
                "policy_version": p.policy_version,
                "rules": json.loads(p.rules_json) if p.rules_json else {},
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }
            for p in policies
        ],
        "branches": effective,
        "modes": list(protection.MODE_ORDER),
    }


@router.put("/api/repository/{repo_id}/branch-protection")
async def upsert_branch_protection(
    repo_id: str,
    request: PolicyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create or update the policy for one branch pattern.

    Weakening a policy that currently freezes a branch needs an unlock grant, otherwise
    "freeze" would mean nothing -- anyone who could edit the policy could simply turn the
    protection off and then do as they liked.
    """
    repository = _repo_or_404(db, repo_id)
    require_repository_access(db, current_user.username, repository,
                              "change branch protection for", required_scope="manage")

    if request.mode not in protection.VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"mode must be one of {', '.join(protection.MODE_ORDER)}",
        )

    pattern = request.branch_pattern.strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="branch_pattern is required")

    existing = (
        db.query(BranchProtectionPolicy)
        .filter(
            BranchProtectionPolicy.repository_id == repo_id,
            BranchProtectionPolicy.branch_pattern == pattern,
        )
        .first()
    )

    if existing:
        if (request.expected_policy_version is not None
                and request.expected_policy_version != existing.policy_version):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "STALE_POLICY",
                    "message": "The policy changed after you read it.",
                    "current_policy_version": existing.policy_version,
                },
            )
        # Relaxing a frozen or archived rule is itself a protected operation.
        relaxing = (
            protection.MODE_ORDER.index(request.mode)
            < protection.MODE_ORDER.index(existing.mode)
        )
        if relaxing and existing.mode in (protection.MODE_FROZEN, protection.MODE_ARCHIVED):
            grant = refs.active_unlock(db, repo_id, pattern, "update")
            if not grant:
                raise refs.ReferenceError(
                    f"'{pattern}' is {existing.mode}; weakening its policy requires an "
                    f"approved unlock.",
                    "UNLOCK_REQUIRED", 403,
                    {"branch": pattern, "mode": existing.mode},
                )
            # Spend it here. Relaxing the policy is what the grant was for; leaving it
            # unconsumed would let the same one-time exception also authorise the next
            # push, which is exactly the hole a single-use grant is meant to close.
            grant.consumed_at = datetime.utcnow()
            grant.consumed_by_id = current_user.id
        existing.mode = request.mode
        existing.rules_json = json.dumps(request.rules or {})
        existing.policy_version = (existing.policy_version or 1) + 1
        existing.updated_by_id = current_user.id
        policy = existing
    else:
        policy = BranchProtectionPolicy(
            repository_id=repo_id,
            branch_pattern=pattern,
            mode=request.mode,
            policy_version=1,
            rules_json=json.dumps(request.rules or {}),
            created_by_id=current_user.id,
            updated_by_id=current_user.id,
        )
        db.add(policy)

    db.flush()
    refs.record_event(
        db, repository_id=repo_id, reference=pattern,
        event_type="branch.policy.changed", decision="allowed", actor=current_user,
        reason=f"mode={request.mode}",
    )
    db.commit()
    db.refresh(policy)

    ActivityCRUD.create_activity(
        db, current_user.id, "branch_policy_changed",
        f"Set '{pattern}' protection to {request.mode}", repo_id,
    )
    return {
        "success": True,
        "policy": {
            "id": policy.id,
            "branch_pattern": policy.branch_pattern,
            "mode": policy.mode,
            "policy_version": policy.policy_version,
        },
    }


@router.post("/api/repository/{repo_id}/branches/{branch_name}/freeze")
async def freeze_branch(
    repo_id: str,
    branch_name: str,
    mode: str = protection.MODE_FROZEN,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Shorthand for setting an exact-name policy to frozen (or archived)."""
    if mode not in (protection.MODE_FROZEN, protection.MODE_ARCHIVED,
                    protection.MODE_PROTECTED, protection.MODE_OPEN):
        raise HTTPException(status_code=400, detail="Unsupported mode")
    return await upsert_branch_protection(
        repo_id,
        PolicyRequest(branch_pattern=_normalize_branch_name(branch_name), mode=mode),
        current_user, db,
    )


@router.post("/api/repository/{repo_id}/branches/{branch_name}/unlock-requests")
async def create_unlock(
    repo_id: str,
    branch_name: str,
    request: UnlockRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Grant a scoped, expiring, single-use exception for one branch."""
    repository = _repo_or_404(db, repo_id)
    require_repository_access(db, current_user.username, repository,
                              "unlock branches in", required_scope="manage")

    reason = (request.reason or "").strip()
    if len(reason) < 10:
        raise HTTPException(
            status_code=400,
            detail="A written reason of at least 10 characters is required.",
        )
    minutes = max(1, min(int(request.expires_in_minutes or 30), MAX_UNLOCK_MINUTES))
    operations = [op.strip() for op in (request.operations or []) if op.strip()]
    unknown = [op for op in operations if op not in protection.ALL_OPERATIONS and op != "*"]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown operations: {unknown}")

    grant = BranchUnlock(
        repository_id=repo_id,
        branch_name=_normalize_branch_name(branch_name),
        operations=",".join(operations or ["update"]),
        reason=reason,
        requested_by_id=current_user.id,
        expires_at=datetime.utcnow() + timedelta(minutes=minutes),
    )
    db.add(grant)
    db.flush()
    refs.record_event(
        db, repository_id=repo_id, reference=grant.branch_name,
        event_type="branch.unlock.granted", decision="allowed", actor=current_user,
        reason=f"{grant.operations} for {minutes}m: {reason}",
    )
    db.commit()
    db.refresh(grant)
    return {
        "success": True,
        "unlock": {
            "id": grant.id,
            "branch": grant.branch_name,
            "operations": grant.operations.split(","),
            "expires_at": grant.expires_at.isoformat(),
            "single_use": True,
        },
    }


@router.post("/api/repository/{repo_id}/releases/immutable")
async def create_immutable_release(
    repo_id: str,
    request: ImmutableReleaseRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Bind a name to one commit, permanently.

    There is no route that moves or deletes one. Re-using a name is refused even when
    the commit matches, so a release identifier can only ever mean one thing.
    """
    repository = _repo_or_404(db, repo_id)
    require_repository_access(db, current_user.username, repository,
                              "create releases in", required_scope="manage")

    name = (request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Release name is required")

    commit = CommitCRUD.get_commit(db, request.commit_id)
    if not commit or commit.repository_id != repo_id:
        raise HTTPException(status_code=404, detail="Commit not found in this repository")

    existing = (
        db.query(ImmutableRelease)
        .filter(ImmutableRelease.repository_id == repo_id, ImmutableRelease.name == name)
        .first()
    )
    if existing:
        raise refs.ReferenceError(
            f"Release '{name}' already exists and points at "
            f"{existing.commit_id[:12]}. Releases never move -- publish a new version "
            f"instead.",
            "IMMUTABLE_REFERENCE", 409,
            {"release": name, "commit_id": existing.commit_id},
        )

    manifest = {
        "schema_version": 1,
        "release": name,
        "repository_id": repo_id,
        "commit": commit.id,
        "tree_hash": getattr(commit, "tree_hash", None),
        "created_at": datetime.utcnow().isoformat(),
        "created_by": current_user.username,
        "notes": request.notes or "",
    }
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(
        AUTH_SECRET.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    release = ImmutableRelease(
        repository_id=repo_id, name=name, commit_id=commit.id,
        manifest_json=canonical, signature=signature,
        created_by_id=current_user.id,
    )
    db.add(release)
    db.flush()
    refs.record_event(
        db, repository_id=repo_id, reference=f"releases/{name}",
        event_type="release.created", decision="allowed", actor=current_user,
        new_commit_id=commit.id, reason=name,
    )
    db.commit()
    db.refresh(release)
    return {
        "success": True,
        "release": {
            "name": release.name,
            "commit_id": release.commit_id,
            "manifest": manifest,
            "signature": signature,
            "created_at": release.created_at.isoformat() if release.created_at else None,
        },
    }


@router.get("/api/repository/{repo_id}/releases/immutable")
async def list_immutable_releases(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = _repo_or_404(db, repo_id)
    require_repository_access(db, current_user.username, repository,
                              "view releases in", required_scope="read")
    rows = (
        db.query(ImmutableRelease)
        .filter(ImmutableRelease.repository_id == repo_id)
        .order_by(ImmutableRelease.created_at.desc())
        .all()
    )
    return {
        "success": True,
        "releases": [
            {
                "name": r.name,
                "commit_id": r.commit_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "signature": r.signature,
            }
            for r in rows
        ],
    }


@router.get("/api/repository/{repo_id}/audit/references")
async def reference_audit(
    repo_id: str,
    reference: Optional[str] = None,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The reference audit trail, newest first, with its hash chain verified."""
    repository = _repo_or_404(db, repo_id)
    require_repository_access(db, current_user.username, repository,
                              "read the audit log for", required_scope="read")

    query = db.query(RefAuditEvent).filter(RefAuditEvent.repository_id == repo_id)
    if reference:
        query = query.filter(RefAuditEvent.reference == _normalize_branch_name(reference))
    rows = query.order_by(RefAuditEvent.id.desc()).limit(max(1, min(limit, 500))).all()

    # Walk the chain oldest-first: each event should name its predecessor's hash.
    chain_intact = True
    ordered = list(reversed(rows))
    for index, event in enumerate(ordered):
        if index == 0:
            continue
        if event.previous_event_hash != ordered[index - 1].event_hash:
            chain_intact = False
            break

    return {
        "success": True,
        "chain_intact": chain_intact,
        "events": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "reference": e.reference,
                "operation": e.operation,
                "actor": e.actor_username,
                "decision": e.decision,
                "error_code": e.error_code,
                "old_commit_id": e.old_commit_id,
                "new_commit_id": e.new_commit_id,
                "old_generation": e.old_generation,
                "new_generation": e.new_generation,
                "mode": e.mode,
                "policy_version": e.policy_version,
                "reason": e.reason,
                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            }
            for e in rows
        ],
    }
