"""The diff a pull request proposes, with its review threads already attached.

A review page needs three things per file: what changed, which line each row came from,
and what people have said about those lines. Fetching them separately means one request
per file and a client that has to re-derive line numbers to line the comments up, so this
assembles all three server-side and answers in one payload.

The comparison runs from the *merge base* rather than from the target tip. Comparing
against the tip would show every change that landed on the target since this branch
started as though this pull request had made them, which is how a one-line change comes
to look like a hundred-file review.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.services import tree_diff
from app.services.commit_graph import _find_merge_base, _get_commit_tree
from app.services.paths import normalize
from database.crud import BranchCRUD
from database.models import PullRequest, Repository


def _branch_head(db: Session, repository_id: str, branch_name: str) -> Optional[str]:
    branch = BranchCRUD.get_branch(db, repository_id, branch_name)
    return branch.head_commit_id if branch else None


def build(db: Session, repository: Repository, pr: PullRequest) -> Dict[str, Any]:
    """Per-file diff for a pull request, each file carrying its comment threads."""
    from app.services import codeowners, pr_comments

    # A fork's branch lives in the fork, not here; the commits are reachable either way.
    source_repo_id = pr.source_repository_id or pr.repository_id
    source_head = _branch_head(db, source_repo_id, pr.source_branch)
    target_head = _branch_head(db, repository.id, pr.target_branch)
    base = _find_merge_base(db, target_head, source_head) if (target_head and source_head) else None

    base_tree = _get_commit_tree(db, base) if base else {}
    source_tree = _get_commit_tree(db, source_head) if source_head else {}

    thread_bundle = pr_comments.threads(db, repository, pr)
    threads_by_path: Dict[str, List[Dict[str, Any]]] = {}
    for thread in thread_bundle["threads"]:
        threads_by_path.setdefault(normalize(thread["file_path"]), []).append(thread)

    try:
        owner_rules = codeowners.load(db, repository)
    except Exception:
        owner_rules = []

    # The comparison itself lives in tree_diff so the single-commit view and this one
    # cannot drift: the same two snapshots must produce the same diff either way.
    files = tree_diff.compare_trees(base_tree, source_tree)
    for entry in files:
        path = entry["file_path"]
        entry["owners"] = codeowners.owners_for(owner_rules, path) if owner_rules else []
        entry["threads"] = threads_by_path.get(path, [])

    totals = tree_diff.totals(files)

    return {
        "pull_request_id": pr.id,
        "base_commit_id": base,
        "source_head_commit_id": source_head,
        "target_head_commit_id": target_head,
        "files": files,
        "totals": totals,
        "comments": {
            "total": thread_bundle["total"],
            "unresolved": thread_bundle["unresolved"],
            "outdated": thread_bundle["outdated"],
        },
    }
