"""The gate every merge passes, pull request or not.

``pr_reviews`` decides whether a *pull request* has satisfied its reviewers. That is the
right question, but it was only ever asked on the two paths that merge through a pull
request. A branch merge asks nothing: ``branch_ops`` moves the target reference with
``refs.update_reference(operation=OP_MERGE)``, and ``MODE_PROTECTED`` permits ``merge``
by design -- protected means "changes arrive reviewed", not "nothing may land". So on a
protected branch, ``fox merge feature`` landed code with no approval, no code-owner
sign-off and no status checks: the gate was not weakened, it was walked around.

This module is the choke point for that road. ``refs.update_reference`` keeps deciding
whether the *reference* may move; ``enforce`` here decides whether *this content* may
arrive. Neither check belongs inside the other -- the same separation as scope versus
mode in branch protection -- because a reference move and a content merge fail for
different reasons and have to say so differently.

The arithmetic of "is this pull request approved" stays in ``pr_reviews``. This module
routes to it rather than reimplementing it, so there is one definition of approved.
"""

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from database.models import PullRequest, Repository, User


class MergeGateError(Exception):
    """A merge the review rules do not permit."""

    def __init__(self, message: str, status_code: int = 409,
                 payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


def open_pull_request_for(
    db: Session, repository: Repository, source_branch: str, target_branch: str
) -> Optional[PullRequest]:
    """The open pull request proposing source into target, if there is one."""
    return (
        db.query(PullRequest)
        .filter(
            PullRequest.repository_id == repository.id,
            PullRequest.source_branch == source_branch,
            PullRequest.target_branch == target_branch,
            PullRequest.status == "open",
        )
        .order_by(PullRequest.id.desc())
        .first()
    )


def review_requirements(
    db: Session, repository: Repository, target_branch: str
) -> Dict[str, Any]:
    """What landing on this branch requires. Exposed so callers can explain a refusal."""
    from app.services import branch_protection

    return branch_protection.review_rules(db, repository, target_branch)


def enforce(
    db: Session,
    *,
    repository: Repository,
    target_branch: str,
    source_branch: str,
    actor: Optional[User] = None,
    pull_request: Optional[PullRequest] = None,
    operation: str = "merge",
) -> None:
    """Refuse a merge into ``target_branch`` that the review rules do not permit.

    With a pull request, this is exactly the existing review gate. Without one, a branch
    whose rules require review refuses the merge outright and points at the pull request
    to use instead: an approved pull request is not a licence to land the same change by
    another route, because merging around it leaves the pull request open and the
    approval unspent, recording a review that never gated anything.
    """
    from app.services import branch_protection, pr_reviews

    if pull_request is not None:
        try:
            pr_reviews.enforce_merge_gate(db, repository, pull_request)
        except pr_reviews.ReviewError as exc:
            raise MergeGateError(exc.message, exc.status_code, exc.payload) from exc
        return

    if not branch_protection.requires_review(db, repository, target_branch):
        return

    rules = review_requirements(db, repository, target_branch)
    existing = open_pull_request_for(db, repository, source_branch, target_branch)

    reasons = []
    if rules["required_approvals"]:
        reasons.append(f"{rules['required_approvals']} approval(s)")
    if rules["require_code_owners"]:
        reasons.append("code owner sign-off")
    if rules["require_status_checks"]:
        reasons.append("status checks " + ", ".join(rules["require_status_checks"]))

    detail = " and ".join(reasons) if reasons else "review"
    if existing:
        remedy = f"Merge pull request #{existing.id} instead."
    else:
        remedy = (
            f"Open a pull request from '{source_branch}' into '{target_branch}' "
            f"and have it reviewed."
        )

    raise MergeGateError(
        f"'{target_branch}' requires {detail} before anything lands on it. {remedy}",
        409,
        {
            "code": "REVIEW_REQUIRED",
            "operation": operation,
            "target_branch": target_branch,
            "source_branch": source_branch,
            "review_rules": rules,
            "open_pull_request_id": existing.id if existing else None,
        },
    )
