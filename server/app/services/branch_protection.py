"""What a branch will and will not accept, independently of who is asking.

Zanbeel already had a *scope* check -- whether an actor holds write or manage access on a
repository. That answers "may this person perform operations here", which is a different
question from "does this branch accept this operation at all". A verified release branch
has to refuse a force-push from its own owner, so the two questions cannot share an
answer.

This module owns the second question. Modes come from the branch-protection
specification:

    open       ordinary development; the repository's scope rules decide
    protected  no direct pushes and no force updates; merges may be allowed
    frozen     nothing that changes the reference is permitted, by anyone
    archived   frozen, and hidden from day-to-day listings

Several policies can match one branch -- an exact name, a pattern like ``releases/*``,
and the repository default. They are combined **most-restrictively**: the strictest mode
wins and any explicit deny beats any allow. A narrower rule can therefore only tighten a
broader one, never loosen it, which is what stops a per-branch setting from quietly
undoing a repository-wide guarantee.
"""

import fnmatch
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database.models import BranchProtectionPolicy, Repository

import branch_policies as legacy_policies


MODE_OPEN = "open"
MODE_PROTECTED = "protected"
MODE_FROZEN = "frozen"
MODE_ARCHIVED = "archived"

#: Ordered least to most restrictive. Combining two policies takes the higher index.
MODE_ORDER = (MODE_OPEN, MODE_PROTECTED, MODE_FROZEN, MODE_ARCHIVED)

VALID_MODES = frozenset(MODE_ORDER)

# Operations a reference update can represent.
OP_CREATE = "create"
OP_UPDATE = "update"
OP_MERGE = "merge"
OP_FORCE_UPDATE = "force_update"
OP_DELETE = "delete"
OP_RENAME = "rename"

ALL_OPERATIONS = (OP_CREATE, OP_UPDATE, OP_MERGE, OP_FORCE_UPDATE, OP_DELETE, OP_RENAME)


def _mode_rank(mode: str) -> int:
    try:
        return MODE_ORDER.index(mode)
    except ValueError:
        return 0


#: The decision matrix from section 9 of the specification, as data rather than branches.
#: True means "the mode itself permits this"; the actor still needs the scope for it.
_MATRIX: Dict[str, Dict[str, bool]] = {
    MODE_OPEN: {
        OP_CREATE: True, OP_UPDATE: True, OP_MERGE: True,
        OP_FORCE_UPDATE: True, OP_DELETE: True, OP_RENAME: True,
    },
    MODE_PROTECTED: {
        OP_CREATE: True, OP_UPDATE: False, OP_MERGE: True,
        OP_FORCE_UPDATE: False, OP_DELETE: False, OP_RENAME: False,
    },
    MODE_FROZEN: {
        OP_CREATE: True, OP_UPDATE: False, OP_MERGE: False,
        OP_FORCE_UPDATE: False, OP_DELETE: False, OP_RENAME: False,
    },
    MODE_ARCHIVED: {
        OP_CREATE: True, OP_UPDATE: False, OP_MERGE: False,
        OP_FORCE_UPDATE: False, OP_DELETE: False, OP_RENAME: False,
    },
}

#: Why each refusal happened, so callers can act on a code rather than a sentence.
_DENIAL_CODE = {
    MODE_PROTECTED: {
        OP_UPDATE: "DIRECT_PUSH_DENIED",
        OP_FORCE_UPDATE: "FORCE_UPDATE_DENIED",
        OP_DELETE: "BRANCH_PROTECTED",
        OP_RENAME: "BRANCH_PROTECTED",
    },
    MODE_FROZEN: {op: "BRANCH_FROZEN" for op in ALL_OPERATIONS},
    MODE_ARCHIVED: {op: "BRANCH_FROZEN" for op in ALL_OPERATIONS},
}


@dataclass
class EffectivePolicy:
    """The combined result of every policy matching one branch."""

    mode: str = MODE_OPEN
    policy_id: Optional[int] = None
    policy_version: Optional[int] = None
    #: Operations explicitly denied by a rule, on top of whatever the mode denies.
    denied_operations: set = field(default_factory=set)
    rules: Dict[str, Any] = field(default_factory=dict)
    matched_patterns: List[str] = field(default_factory=list)

    def permits(self, operation: str) -> bool:
        if operation in self.denied_operations:
            return False
        return _MATRIX.get(self.mode, _MATRIX[MODE_OPEN]).get(operation, True)

    def denial_code(self, operation: str) -> str:
        if operation in self.denied_operations:
            return "BRANCH_PROTECTED"
        return _DENIAL_CODE.get(self.mode, {}).get(operation, "BRANCH_PROTECTED")

    @property
    def is_restricted(self) -> bool:
        return self.mode != MODE_OPEN or bool(self.denied_operations)


def _load_rules(policy: BranchProtectionPolicy) -> Dict[str, Any]:
    if not policy.rules_json:
        return {}
    try:
        loaded = json.loads(policy.rules_json)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _pattern_specificity(pattern: str) -> int:
    """Exact names beat patterns; longer patterns beat shorter ones."""
    wildcards = pattern.count("*") + pattern.count("?")
    return (0 if wildcards else 1_000_000) + len(pattern)


def matching_policies(
    db: Session, repository_id: str, branch_name: str
) -> List[BranchProtectionPolicy]:
    """Stored policies whose pattern matches this branch, most specific first."""
    rows = (
        db.query(BranchProtectionPolicy)
        .filter(BranchProtectionPolicy.repository_id == repository_id)
        .all()
    )
    matched = [
        row for row in rows
        if row.branch_pattern == branch_name
        or fnmatch.fnmatchcase(branch_name, row.branch_pattern)
    ]
    matched.sort(key=lambda row: _pattern_specificity(row.branch_pattern), reverse=True)
    return matched


#: Review requirements live in a policy's ``rules_json`` alongside the operation rules,
#: so "main needs two approvals and a code owner" is expressible per branch pattern
#: rather than once per repository.
REVIEW_APPROVALS_KEY = "required_approvals"
REVIEW_OWNERS_KEY = "require_code_owners"
REVIEW_CHECKS_KEY = "require_status_checks"
REVIEW_KEYS = (REVIEW_APPROVALS_KEY, REVIEW_OWNERS_KEY, REVIEW_CHECKS_KEY)


def _merge_rules(current: Dict[str, Any], incoming: Dict[str, Any]) -> None:
    """Fold one policy's rules into the running total, most restrictive winning.

    Plain ``update`` would let whichever row happened to be read last decide, so a
    wildcard requiring no approvals could silently undo an exact-name rule requiring
    two. The review keys therefore combine rather than overwrite: counts take the
    higher, flags take True, and required checks take the union.
    """
    for key, value in incoming.items():
        if key == REVIEW_APPROVALS_KEY:
            try:
                incoming_count = max(0, int(value))
            except (TypeError, ValueError):
                continue
            existing = current.get(key)
            try:
                existing_count = max(0, int(existing)) if existing is not None else 0
            except (TypeError, ValueError):
                existing_count = 0
            current[key] = max(existing_count, incoming_count)
        elif key == REVIEW_OWNERS_KEY:
            current[key] = bool(current.get(key)) or bool(value)
        elif key == REVIEW_CHECKS_KEY:
            merged = list(current.get(key) or [])
            for check in (value or []):
                if check not in merged:
                    merged.append(check)
            current[key] = merged
        else:
            current[key] = value


def resolve_effective_policy(
    db: Session, repository: Repository, branch_name: str
) -> EffectivePolicy:
    """Combine every policy matching `branch_name`, most restrictive wins.

    The repository's older ``protected_branches`` list is folded in as a baseline so
    existing configuration keeps working: a branch listed there behaves as `protected`
    even if nobody has written a policy row for it yet.
    """
    effective = EffectivePolicy()

    rows = matching_policies(db, repository.id, branch_name)
    for row in rows:
        effective.matched_patterns.append(row.branch_pattern)
        if _mode_rank(row.mode) > _mode_rank(effective.mode):
            effective.mode = row.mode
        rules = _load_rules(row)
        # Any explicit deny survives the combination; an allow never overturns a deny.
        for operation in ALL_OPERATIONS:
            if rules.get(operation) == "deny":
                effective.denied_operations.add(operation)
        _merge_rules(effective.rules, rules)
        # Report the most specific match, which is the one a human edited for this branch.
        if effective.policy_id is None:
            effective.policy_id = row.id
            effective.policy_version = row.policy_version

    # Baseline from the legacy repository-level settings.
    legacy = legacy_policies.get_branch_policy(repository)
    if branch_name in set(legacy.get("protected_branches") or []):
        if _mode_rank(MODE_PROTECTED) > _mode_rank(effective.mode):
            effective.mode = MODE_PROTECTED
        effective.matched_patterns.append(f"{branch_name} (repository protected_branches)")

    return effective



def review_rules(db: Session, repository: Repository, branch_name: str) -> Dict[str, Any]:
    """Review requirements in force for one branch.

    Falls back to the repository-wide ``required_approvals`` when no policy pattern
    carries one, so a repository configured before per-branch policies existed keeps
    behaving exactly as it does today -- including the default of zero, which is what
    makes turning review on a deliberate act rather than a surprise.
    """
    effective = resolve_effective_policy(db, repository, branch_name)

    approvals = effective.rules.get(REVIEW_APPROVALS_KEY)
    if approvals is None:
        legacy = legacy_policies.get_branch_policy(repository)
        approvals = legacy.get(REVIEW_APPROVALS_KEY, 0)
    try:
        approvals = max(0, int(approvals))
    except (TypeError, ValueError):
        approvals = 0

    checks = effective.rules.get(REVIEW_CHECKS_KEY) or []
    if not isinstance(checks, list):
        checks = []

    return {
        "required_approvals": approvals,
        "require_code_owners": bool(effective.rules.get(REVIEW_OWNERS_KEY)),
        "require_status_checks": list(checks),
        "mode": effective.mode,
        "matched_patterns": list(effective.matched_patterns),
    }


def requires_review(db: Session, repository: Repository, branch_name: str) -> bool:
    """Whether landing on this branch needs a reviewed pull request at all."""
    rules = review_rules(db, repository, branch_name)
    return bool(
        rules["required_approvals"] > 0
        or rules["require_code_owners"]
        or rules["require_status_checks"]
    )

def describe(effective: EffectivePolicy) -> Dict[str, Any]:
    """Serialisable view for API responses and the CLI."""
    return {
        "mode": effective.mode,
        "policy_id": effective.policy_id,
        "policy_version": effective.policy_version,
        "matched_patterns": effective.matched_patterns,
        "denied_operations": sorted(effective.denied_operations),
        "permits": {op: effective.permits(op) for op in ALL_OPERATIONS},
        "rules": effective.rules,
    }
