"""Pull request review, approval, and the gate that stops an unreviewed merge.

Before this, ``PullRequest`` carried a ``reviewed_by_id`` column and nothing else: there
were no review records, no way to approve or request changes, and no check at merge time.
Any pull request could be merged by anyone with access without a single review.

Three rules shape the design, each because the obvious alternative is unsafe:

* **Only the latest review per reviewer counts.** Reviews are append-only, so the history
  shows someone approving, then asking for changes, then approving again -- but the gate
  reads only their current position.

* **An approval is tied to the commit it reviewed.** When the source branch moves, that
  approval goes *stale*: it approved code that is no longer what would be merged. Stale
  approvals do not count. Without this, a developer could get approval and then push
  anything they liked before merging.

* **Authors cannot approve their own pull request.** Self-approval would make the gate
  decorative for anyone who could open a PR.

The number of approvals required lives in the repository's branch policy, so it is set
per repository rather than hard-coded, and defaults to 0 -- enabling the gate is a choice,
not something that silently starts blocking existing workflows on upgrade.

A CODEOWNERS file adds a second, independent requirement: sign-off from whoever owns the
paths a pull request touches. The two are deliberately additive, because enough approvals
from the wrong people is not the same thing as approval from the right ones.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import branch_policies
from database.crud import BranchCRUD
from database.models import PullRequest, PullRequestReview, Repository, User

VALID_STATES = ("approved", "changes_requested", "commented")

#: Policy key holding how many current approvals a merge needs.
REQUIRED_APPROVALS_KEY = "required_approvals"
DEFAULT_REQUIRED_APPROVALS = 0


class ReviewError(Exception):
    def __init__(self, message: str, status_code: int = 400, payload: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


def required_approvals(repository: Repository) -> int:
    policy = branch_policies.get_branch_policy(repository)
    try:
        value = int(policy.get(REQUIRED_APPROVALS_KEY, DEFAULT_REQUIRED_APPROVALS))
    except (TypeError, ValueError):
        return DEFAULT_REQUIRED_APPROVALS
    return max(0, value)


def required_approvals_for(db: Session, repository: Repository, branch_name: str) -> int:
    """Approvals required to land on one specific branch.

    Branch protection policies carry review requirements per pattern, so `main` can
    demand two approvals while feature branches demand none. When no pattern sets a
    count this falls through to the repository-wide value above, which is what keeps
    every repository configured before per-branch policies behaving as it does now.
    """
    from app.services import branch_protection

    return branch_protection.review_rules(db, repository, branch_name)["required_approvals"]


def submit(
    db: Session,
    repository: Repository,
    pr: PullRequest,
    reviewer: User,
    state: str,
    body: Optional[str] = None,
) -> PullRequestReview:
    """Record a review. Appends rather than replacing an earlier one."""
    if state not in VALID_STATES:
        raise ReviewError(f"state must be one of {', '.join(VALID_STATES)}")
    if pr.status != "open":
        raise ReviewError(f"Pull request is {pr.status}; it can no longer be reviewed.", 409)
    if pr.created_by_id == reviewer.id and state == "approved":
        raise ReviewError(
            "You cannot approve your own pull request.", 403,
            {"code": "SELF_APPROVAL"},
        )

    source = BranchCRUD.get_branch(db, repository.id, pr.source_branch)
    review = PullRequestReview(
        pull_request_id=pr.id,
        reviewer_id=reviewer.id,
        state=state,
        body=(body or "").strip() or None,
        commit_id=source.head_commit_id if source else None,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


def _latest_per_reviewer(db: Session, pr_id: int) -> Dict[int, PullRequestReview]:
    """The current position of each reviewer, ignoring superseded ones.

    'commented' does not change a reviewer's position -- leaving a note should not
    silently withdraw an earlier approval.
    """
    rows = (
        db.query(PullRequestReview)
        .filter(PullRequestReview.pull_request_id == pr_id)
        .order_by(PullRequestReview.id)
        .all()
    )
    latest: Dict[int, PullRequestReview] = {}
    for row in rows:
        if row.state == "commented":
            continue
        latest[row.reviewer_id] = row
    return latest


def summarize(db: Session, repository: Repository, pr: PullRequest) -> Dict[str, Any]:
    """Review state for a pull request, including whether it may be merged."""
    source = BranchCRUD.get_branch(db, repository.id, pr.source_branch)
    head = source.head_commit_id if source else None

    latest = _latest_per_reviewer(db, pr.id)
    approvals, stale_approvals, changes_requested = [], [], []

    for review in latest.values():
        entry = {
            "reviewer": review.reviewer.username if review.reviewer else None,
            "state": review.state,
            "commit_id": review.commit_id,
            "submitted_at": review.created_at.isoformat() if review.created_at else None,
            "body": review.body,
        }
        if review.state == "changes_requested":
            changes_requested.append(entry)
        elif review.state == "approved":
            # An approval of a commit that is no longer the tip approved different code.
            if head and review.commit_id and review.commit_id != head:
                entry["stale"] = True
                stale_approvals.append(entry)
            else:
                approvals.append(entry)

    # Resolved against the target branch, because that is the branch whose rules
    # this merge has to satisfy -- not the repository as a whole.
    needed = required_approvals_for(db, repository, pr.target_branch)
    blockers: List[str] = []
    if changes_requested:
        who = ", ".join(sorted(e["reviewer"] or "?" for e in changes_requested))
        blockers.append(f"changes requested by {who}")
    if len(approvals) < needed:
        short = needed - len(approvals)
        detail = f"{short} more approval(s) needed"
        if stale_approvals:
            detail += (
                f" ({len(stale_approvals)} approval(s) went stale when "
                f"'{pr.source_branch}' moved)"
            )
        blockers.append(detail)

    # Code owners are additive to the numeric threshold: enough approvals from the wrong
    # people is not the same as sign-off from whoever is responsible for the code.
    from app.services import codeowners

    ownership = codeowners.required_owners(db, repository, pr)
    approved_by = {entry["reviewer"] for entry in approvals}
    missing_owners = [o for o in ownership.get("required", []) if o not in approved_by]
    if missing_owners:
        blockers.append(
            "code owner approval needed from " + ", ".join(sorted(missing_owners))
        )

    # Required status checks are evaluated against the source tip, the same
    # commit approvals are bound to. Checking any other commit would let a green
    # run on older code authorise whatever is there now.
    from app.services import status_checks

    check_summary = status_checks.combined(db, repository, head) if head else None
    blockers.extend(status_checks.merge_blockers(db, repository, head))

    return {
        "pull_request_id": pr.id,
        "source_head_commit_id": head,
        "required_approvals": needed,
        "approvals": approvals,
        "stale_approvals": stale_approvals,
        "changes_requested": changes_requested,
        "code_owners": {
            "enabled": ownership.get("enabled", False),
            "required": ownership.get("required", []),
            "missing": sorted(missing_owners),
            "by_path": ownership.get("by_path", {}),
            "unknown_owners": ownership.get("unknown_owners", []),
        },
        "status_checks": check_summary,
        "can_merge": not blockers,
        "blockers": blockers,
    }


def history(db: Session, pr_id: int) -> List[Dict[str, Any]]:
    """Every review ever submitted, oldest first."""
    rows = (
        db.query(PullRequestReview)
        .filter(PullRequestReview.pull_request_id == pr_id)
        .order_by(PullRequestReview.id)
        .all()
    )
    return [
        {
            "id": row.id,
            "reviewer": row.reviewer.username if row.reviewer else None,
            "state": row.state,
            "body": row.body,
            "commit_id": row.commit_id,
            "submitted_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def enforce_merge_gate(db: Session, repository: Repository, pr: PullRequest) -> None:
    """Refuse a merge that the review rules do not permit.

    Called from every path that completes a merge, so the gate cannot be sidestepped by
    resolving conflicts instead of merging cleanly.
    """
    summary = summarize(db, repository, pr)
    if summary["can_merge"]:
        return
    raise ReviewError(
        "This pull request cannot be merged yet: " + "; ".join(summary["blockers"]) + ".",
        409,
        {"code": "REVIEW_REQUIRED", **summary},
    )
