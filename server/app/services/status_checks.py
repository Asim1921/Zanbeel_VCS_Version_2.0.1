"""Commit status checks: build and test results reported against a commit.

Why this exists: ``Repository.tested`` was a boolean someone set by hand. It
could not say *which* check passed, *what* it was checking, *when*, or against
*which commit* — so it could not be trusted and could not gate anything.

A status is (repository, commit, context, state). ``context`` names the check
("ci/unit-tests", "security/scan"); the newest row for a given triple is that
check's current state. Older rows are kept rather than overwritten, because
"this went red, then someone re-ran it green" is exactly the history you need
when a release goes wrong.

**Which checks are required** comes from two places, and both now count on every
merge path: the repository-wide list (``required_status_checks`` in the branch
policy, what the web screen edits) and the per-branch protection rules
(``require_status_checks``). Previously a pull-request merge read only the first and
a branch merge only the second, so either setting could be walked around.

**Who may satisfy a required check** can be pinned. An entry is either a context name
-- any report from someone with write access counts, as on GitHub -- or
``{"context": ..., "reporter": "<username>"}``, in which case only that user's reports
count. Without a pin, the author of a pull request can report its required check as
passing; with one, only the CI system's account can.

**Statuses belong to a repository.** The gate reads only statuses reported to the
repository being merged into. They used to be read by commit id alone, so a report
made through one repository counted towards another repository's gate.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database.models import Commit, CommitStatus, Repository

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


@dataclass(frozen=True)
class RequiredCheck:
    context: str
    reporter: Optional[str] = None      # username; None means any writer's report counts

    def as_dict(self) -> Dict[str, Any]:
        return {"context": self.context, "reporter": self.reporter}


def parse_required(raw: Any) -> List[RequiredCheck]:
    """Normalise a stored list: names, or {context, reporter} objects."""
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    checks: List[RequiredCheck] = []
    for item in raw or []:
        if isinstance(item, dict):
            context = str(item.get("context") or "").strip()
            reporter = str(item.get("reporter") or "").strip() or None
        else:
            context, reporter = str(item).strip(), None
        if context and all(c.context != context for c in checks):
            checks.append(RequiredCheck(context, reporter))
    return checks


def validate_required(raw: Any) -> List[Any]:
    """Clean a list someone is saving. Returns the stored form (names stay plain strings)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise StatusError("required_status_checks must be a list")
    stored: List[Any] = []
    for check in parse_required(raw):
        if len(check.context) > 120:
            raise StatusError("A status context must be 120 characters or fewer")
        stored.append(check.as_dict() if check.reporter else check.context)
    return stored


def required_checks(repository: Repository) -> List[str]:
    """Names of the repository-wide required checks (the branch policy list)."""
    return [check.context for check in _repository_wide(repository)]


def _repository_wide(repository: Repository) -> List[RequiredCheck]:
    from branch_policies import get_branch_policy

    return parse_required(get_branch_policy(repository).get(REQUIRED_CHECKS_KEY) or [])


def required_check_rules(
    db: Session, repository: Repository, branch: Optional[str] = None
) -> List[RequiredCheck]:
    """Every check that must pass before something lands on ``branch``.

    The repository-wide list plus the branch's protection rules. Where both name the
    same context, a pinned reporter wins over an unpinned one: a pin is a restriction,
    and combining rules must never loosen one.
    """
    checks = {check.context: check for check in _repository_wide(repository)}
    if branch:
        from app.services import branch_protection

        rules = branch_protection.review_rules(db, repository, branch)
        for check in parse_required(rules.get("require_status_checks") or []):
            existing = checks.get(check.context)
            if existing is None or (check.reporter and not existing.reporter):
                checks[check.context] = check
    return list(checks.values())


def commit_belongs(db: Session, repository: Repository, commit: Commit) -> bool:
    """Whether statuses about ``commit`` may be reported to ``repository``.

    Its own commits, and those of a direct fork relation: a fork's branches start at the
    upstream's commits, and a fork's pull request brings the fork's commits upstream.
    """
    if commit.repository_id == repository.id:
        return True
    other = db.query(Repository).filter(Repository.id == commit.repository_id).first()
    if other is None:
        return False
    return other.forked_from_id == repository.id or repository.forked_from_id == other.id


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

    target_url = (target_url or "").strip() or None
    if target_url and not target_url.lower().startswith(("http://", "https://")):
        # The link is rendered for everyone who views the commit; a javascript: or
        # data: URL there would run in their browser.
        raise StatusError("target_url must start with http:// or https://")

    # A status against a commit that does not exist can never satisfy a gate, so
    # accepting it would let a CI job with a typo'd SHA report green forever
    # while the real commit stayed unchecked. Fail loudly instead.
    commit = db.query(Commit).filter(Commit.id == commit_id).first()
    if not commit:
        raise StatusError(f"Commit '{commit_id}' does not exist", status_code=404)
    if not commit_belongs(db, repository, commit):
        raise StatusError(
            f"Commit '{commit_id}' is not part of this repository", status_code=404
        )

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


def _rows(db: Session, commit_id: str, repository_id: Optional[str]) -> List[CommitStatus]:
    query = db.query(CommitStatus).filter(CommitStatus.commit_id == commit_id)
    if repository_id is not None:
        query = query.filter(CommitStatus.repository_id == repository_id)
    return query.order_by(CommitStatus.id.asc()).all()


def latest_per_context(
    db: Session, commit_id: str, repository_id: Optional[str] = None
) -> Dict[str, CommitStatus]:
    """The current state of each check on a commit, in one repository.

    Rows are read oldest-first and later ones overwrite earlier ones in the
    dict, so what survives is the newest report for each context.
    """
    current: Dict[str, CommitStatus] = {}
    for row in _rows(db, commit_id, repository_id):
        current[row.context] = row
    return current


def _reporter_name(status: CommitStatus) -> Optional[str]:
    return status.created_by.username if status.created_by else None


def combined(
    db: Session,
    repository: Repository,
    commit_id: str,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """The rolled-up state of a commit, plus where each required check stands.

    Rollup precedence is deliberate and pessimistic: any failure or error makes
    the whole thing failing, and anything still pending keeps it pending.
    Reporting "success" while a check is mid-run would let a merge through on
    the strength of results that had not arrived.
    """
    rows = _rows(db, commit_id, repository.id)
    current: Dict[str, CommitStatus] = {}
    for row in rows:
        current[row.context] = row
    rules = required_check_rules(db, repository, branch)

    states = [status.state for status in current.values()]
    if not states:
        overall = STATE_PENDING if rules else "none"
    elif any(s in (STATE_FAILURE, STATE_ERROR) for s in states):
        overall = STATE_FAILURE
    elif any(s == STATE_PENDING for s in states):
        overall = STATE_PENDING
    else:
        overall = STATE_SUCCESS

    # A required check that has never reported is missing, not passing. Treating
    # silence as success would make the gate useless the moment CI failed to
    # start — the exact case it exists to catch.
    detail, missing, failing, pending = [], [], [], []
    for rule in rules:
        if rule.reporter:
            counted = None
            for row in rows:
                if row.context == rule.context and (_reporter_name(row) or "").lower() == rule.reporter.lower():
                    counted = row
        else:
            counted = current.get(rule.context)
        latest = current.get(rule.context)
        entry = {
            "context": rule.context,
            "reporter": rule.reporter,
            "state": counted.state if counted else None,
            "status": serialize(counted) if counted else None,
        }
        if rule.reporter and latest is not None and latest is not counted:
            # Shown so nobody wonders why a green report "did not count".
            entry["ignored_report"] = serialize(latest)
        detail.append(entry)
        if counted is None:
            missing.append(rule.context)
        elif counted.state in (STATE_FAILURE, STATE_ERROR):
            failing.append(rule.context)
        elif counted.state == STATE_PENDING:
            pending.append(rule.context)

    return {
        "commit_id": commit_id,
        "branch": branch,
        "state": overall,
        "total": len(current),
        "statuses": [serialize(status) for status in current.values()],
        "required": [rule.context for rule in rules],
        "required_checks": detail,
        "missing_required": missing,
        "failing_required": failing,
        "pending_required": pending,
        "required_satisfied": not (missing or failing or pending),
    }


def merge_blockers(
    db: Session,
    repository: Repository,
    commit_id: Optional[str],
    branch: Optional[str] = None,
) -> List[str]:
    """Human-readable reasons the required checks block a merge. Empty when clear."""
    rules = required_check_rules(db, repository, branch)
    if not rules:
        return []

    if not commit_id:
        return ["required status checks cannot be evaluated: the branch has no commits"]

    summary = combined(db, repository, commit_id, branch)
    pins = {rule.context: rule.reporter for rule in rules if rule.reporter}

    def label(names):
        return ", ".join(
            f"{name} (from {pins[name]})" if name in pins else name for name in sorted(names)
        )

    blockers: List[str] = []
    if summary["failing_required"]:
        blockers.append("failing required check(s): " + label(summary["failing_required"]))
    if summary["pending_required"]:
        blockers.append("required check(s) still running: " + label(summary["pending_required"]))
    if summary["missing_required"]:
        blockers.append("required check(s) have not reported: " + label(summary["missing_required"]))
    return blockers


def history(
    db: Session, commit_id: str, context: Optional[str] = None,
    repository_id: Optional[str] = None,
) -> List[CommitStatus]:
    """Every report against a commit, newest first."""
    query = db.query(CommitStatus).filter(CommitStatus.commit_id == commit_id)
    if repository_id is not None:
        query = query.filter(CommitStatus.repository_id == repository_id)
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
        "reported_by": _reporter_name(status),
        "created_at": status.created_at.isoformat() if status.created_at else None,
    }
