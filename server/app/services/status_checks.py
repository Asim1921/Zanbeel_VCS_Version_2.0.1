"""Commit status checks: build and test results reported against a commit.

Why this exists: ``Repository.tested`` was a boolean someone set by hand. It
could not say *which* check passed, *what* it was checking, *when*, or against
*which commit* — so it could not be trusted and could not gate anything.

A status is (commit, context, state). ``context`` names the check
("ci/unit-tests", "security/scan"); the newest row for a given pair is that
check's current state. Older rows are kept rather than overwritten, because
"this went red, then someone re-ran it green" is exactly the history you need
when a release goes wrong.

The gate is opt-in per repository through the branch policy's
``required_status_checks``. An empty list means no gating, so upgrading does not
silently start blocking merges that were fine yesterday.
"""

from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from database.models import CommitStatus, Repository

STATE_PENDING = "pending"
STATE_SUCCESS = "success"
STATE_FAILURE = "failure"
STATE_ERROR = "error"

VALID_STATES = (STATE_PENDING, STATE_SUCCESS, STATE_FAILURE, STATE_ERROR)

REQUIRED_CHECKS_KEY = "required_status_checks"


class StatusError(Exception):
    """Raised for a caller mistake; carries the HTTP status to answer with."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def required_checks(repository: Repository) -> List[str]:
    """Contexts that must be green before this repository's PRs can merge."""
    from branch_policies import get_branch_policy

    policy = get_branch_policy(repository)
    raw = policy.get(REQUIRED_CHECKS_KEY) or []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    return [str(item).strip() for item in raw if str(item).strip()]


def report(
    db: Session,
    repository: Repository,
    commit_id: str,
    context: str,
    state: str,
    description: Optional[str] = None,
    target_url: Optional[str] = None,
    reporter_id: Optional[int] = None,
) -> CommitStatus:
    """Record a status. Appends — it never overwrites an earlier report."""
    context = (context or "").strip()
    if not context:
        raise StatusError("A status context is required, e.g. 'ci/unit-tests'")
    if len(context) > 120:
        raise StatusError("Status context must be 120 characters or fewer")

    state = (state or "").strip().lower()
    if state not in VALID_STATES:
        raise StatusError(
            f"Invalid state '{state}'. Valid states: {', '.join(VALID_STATES)}"
        )

    commit_id = (commit_id or "").strip()
    if not commit_id:
        raise StatusError("A commit id is required")

    # A status against a commit that does not exist can never satisfy a gate, so
    # accepting it would let a CI job with a typo'd SHA report green forever
    # while the real commit stayed unchecked. Fail loudly instead.
    from database.models import Commit

    if not db.query(Commit).filter(Commit.id == commit_id).first():
        raise StatusError(f"Commit '{commit_id}' does not exist", status_code=404)

    status = CommitStatus(
        repository_id=repository.id,
        commit_id=commit_id,
        context=context,
        state=state,
        description=(description or "")[:300] or None,
        target_url=(target_url or "")[:500] or None,
        created_by_id=reporter_id,
    )
    db.add(status)
    db.commit()
    db.refresh(status)
    return status


def latest_per_context(db: Session, commit_id: str) -> Dict[str, CommitStatus]:
    """The current state of each check on a commit.

    Rows are read oldest-first and later ones overwrite earlier ones in the
    dict, so what survives is the newest report for each context.
    """
    rows = (
        db.query(CommitStatus)
        .filter(CommitStatus.commit_id == commit_id)
        .order_by(CommitStatus.id.asc())
        .all()
    )
    current: Dict[str, CommitStatus] = {}
    for row in rows:
        current[row.context] = row
    return current


def combined(
    db: Session,
    repository: Repository,
    commit_id: str,
) -> Dict[str, Any]:
    """The rolled-up state of a commit, plus which required checks are missing.

    Rollup precedence is deliberate and pessimistic: any failure or error makes
    the whole thing failing, and anything still pending keeps it pending.
    Reporting "success" while a check is mid-run would let a merge through on
    the strength of results that had not arrived.
    """
    current = latest_per_context(db, commit_id)
    needed = required_checks(repository)

    states = [status.state for status in current.values()]
    if not states:
        overall = STATE_PENDING if needed else "none"
    elif any(s in (STATE_FAILURE, STATE_ERROR) for s in states):
        overall = STATE_FAILURE
    elif any(s == STATE_PENDING for s in states):
        overall = STATE_PENDING
    else:
        overall = STATE_SUCCESS

    # A required check that has never reported is missing, not passing. Treating
    # silence as success would make the gate useless the moment CI failed to
    # start — the exact case it exists to catch.
    missing = [name for name in needed if name not in current]
    failing = [
        name
        for name in needed
        if name in current and current[name].state in (STATE_FAILURE, STATE_ERROR)
    ]
    pending = [
        name for name in needed if name in current and current[name].state == STATE_PENDING
    ]

    return {
        "commit_id": commit_id,
        "state": overall,
        "total": len(current),
        "statuses": [serialize(status) for status in current.values()],
        "required": needed,
        "missing_required": missing,
        "failing_required": failing,
        "pending_required": pending,
        "required_satisfied": not (missing or failing or pending),
    }


def merge_blockers(db: Session, repository: Repository, commit_id: Optional[str]) -> List[str]:
    """Human-readable reasons the required checks block a merge. Empty when clear."""
    needed = required_checks(repository)
    if not needed:
        return []

    if not commit_id:
        return ["required status checks cannot be evaluated: the branch has no commits"]

    summary = combined(db, repository, commit_id)
    blockers: List[str] = []
    if summary["failing_required"]:
        blockers.append(
            "failing required check(s): " + ", ".join(sorted(summary["failing_required"]))
        )
    if summary["pending_required"]:
        blockers.append(
            "required check(s) still running: " + ", ".join(sorted(summary["pending_required"]))
        )
    if summary["missing_required"]:
        blockers.append(
            "required check(s) have not reported: "
            + ", ".join(sorted(summary["missing_required"]))
        )
    return blockers


def history(db: Session, commit_id: str, context: Optional[str] = None) -> List[CommitStatus]:
    """Every report against a commit, newest first."""
    query = db.query(CommitStatus).filter(CommitStatus.commit_id == commit_id)
    if context:
        query = query.filter(CommitStatus.context == context)
    return query.order_by(CommitStatus.id.desc()).all()


def serialize(status: CommitStatus) -> dict:
    return {
        "id": status.id,
        "commit_id": status.commit_id,
        "context": status.context,
        "state": status.state,
        "description": status.description,
        "target_url": status.target_url,
        "reported_by": status.created_by.username if status.created_by else None,
        "created_at": status.created_at.isoformat() if status.created_at else None,
    }
