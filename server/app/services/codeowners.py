"""CODEOWNERS: routing review to whoever is responsible for a path.

Permissions in this system are per-repository, so there was no way to say "changes under
``server/database/`` need Alice's sign-off". A ``CODEOWNERS`` file at the repository root
(or under ``.fox/``) maps path patterns to owners, and those owners become *required*
reviewers on any pull request that touches a matching file.

    # comments and blanks are ignored
    *                       @lead
    /server/database/       @alice @bob
    *.sql                   @dba
    /docs/                  @writer

The last matching rule wins, which is the convention every implementation of this file
follows and the one people expect: general rules first, specific overrides after.

Pattern matching reuses the ``.foxignore`` engine, so the glob syntax is identical across
the system rather than being a second dialect that behaves subtly differently.

Ownership requirements are additive to the branch policy's numeric threshold: a pull
request needs both enough approvals *and* an approval from each required owner. Requiring
a count alone lets the wrong people approve; requiring owners alone lets one person wave
through work nobody else saw.
"""

from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from database.crud import BranchCRUD
from database.models import PullRequest, Repository, User

from app.services.branches import _get_default_branch
from app.services.commit_graph import _find_merge_base, _get_commit_tree
from app.services.ignore_rules import IgnoreMatcher

#: Where the file may live, in the order it is looked for.
CODEOWNERS_PATHS = ("CODEOWNERS", ".fox/CODEOWNERS", "docs/CODEOWNERS")

MAX_RULES = 500


class OwnerRule:
    __slots__ = ("pattern", "owners", "matcher")

    def __init__(self, pattern: str, owners: List[str]):
        self.pattern = pattern
        self.owners = owners
        # One-rule matcher; reusing the ignore engine keeps the glob dialect identical.
        self.matcher = IgnoreMatcher.from_lines([pattern])

    def matches(self, path: str) -> bool:
        return self.matcher.is_ignored(path)


def parse(text: Optional[str]) -> List[OwnerRule]:
    """Parse CODEOWNERS content into ordered rules. Malformed lines are skipped."""
    rules: List[OwnerRule] = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue                     # a pattern with no owner names nobody
        pattern, owners = parts[0], [o.lstrip("@") for o in parts[1:] if o.strip()]
        owners = [o for o in owners if o]
        if not owners:
            continue
        rules.append(OwnerRule(pattern, owners))
        if len(rules) >= MAX_RULES:
            break
    return rules


def load(db: Session, repository: Repository, commit_id: Optional[str] = None) -> List[OwnerRule]:
    """Read CODEOWNERS from a commit, defaulting to the default branch's head."""
    if commit_id is None:
        branch = _get_default_branch(db, repository.id)
        commit_id = branch.head_commit_id if branch else None
    if not commit_id:
        return []

    tree = _get_commit_tree(db, commit_id)
    for candidate in CODEOWNERS_PATHS:
        blob = tree.get(candidate)
        if blob is not None:
            try:
                return parse(blob.decode("utf-8", errors="replace"))
            except Exception:
                return []
    return []


def owners_for(rules: List[OwnerRule], path: str) -> List[str]:
    """Owners of one path. The last matching rule wins."""
    winner: Optional[OwnerRule] = None
    for rule in rules:
        if rule.matches(path):
            winner = rule
    return list(winner.owners) if winner else []


def changed_paths(db: Session, repository: Repository, pr: PullRequest) -> List[str]:
    """Paths a pull request would change, relative to the merge base."""
    source = BranchCRUD.get_branch(db, repository.id, pr.source_branch)
    target = BranchCRUD.get_branch(db, repository.id, pr.target_branch)
    if not source or not target:
        return []

    base_id = _find_merge_base(db, target.head_commit_id, source.head_commit_id)
    base_tree = _get_commit_tree(db, base_id)
    source_tree = _get_commit_tree(db, source.head_commit_id)

    changed = {
        path for path in set(base_tree) | set(source_tree)
        if base_tree.get(path) != source_tree.get(path)
    }
    return sorted(changed)


def required_owners(
    db: Session,
    repository: Repository,
    pr: PullRequest,
) -> Dict[str, Any]:
    """Which owners must approve this pull request, and why.

    The pull request's own author never counts as a required owner: they cannot approve
    their own work, so listing them would make the pull request unmergeable.
    """
    rules = load(db, repository)
    if not rules:
        return {"enabled": False, "required": [], "by_path": {}, "changed_paths": []}

    paths = changed_paths(db, repository, pr)
    by_path: Dict[str, List[str]] = {}
    required: Set[str] = set()
    for path in paths:
        owners = owners_for(rules, path)
        if owners:
            by_path[path] = owners
            required.update(owners)

    author = pr.created_by.username if pr.created_by else None
    if author:
        required.discard(author)

    # Only usernames that exist can be expected to approve.
    known = {
        row.username for row in
        db.query(User).filter(User.username.in_(list(required) or [""])).all()
    } if required else set()

    return {
        "enabled": True,
        "required": sorted(known),
        "unknown_owners": sorted(required - known),
        "by_path": by_path,
        "changed_paths": paths,
    }
