"""What one commit changed.

The commit list answers "what happened"; this answers "what did it do". A commit is
compared against its first parent, which for ordinary history is simply the state
before it.

Merges are the exception worth stating: a merge has two parents, and diffing against
both would list every change that arrived from the branch being merged, which is not
what the merge itself did. Comparing against the first parent shows what landed on the
target branch as a result — the same choice git makes for `git show` — and the other
parents are reported so a caller can offer them explicitly.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.services import tree_diff
from app.services.commit_graph import _get_commit_tree
from database.models import Branch, Commit, CommitParent, Repository, Tag


class CommitNotFound(Exception):
    pass


def _parents_of(db: Session, commit: Commit) -> List[str]:
    links = (
        db.query(CommitParent)
        .filter(CommitParent.commit_id == commit.id)
        .order_by(CommitParent.parent_order)
        .all()
    )
    if links:
        return [link.parent_commit_id for link in links]
    return [commit.parent_commit_id] if commit.parent_commit_id else []


def _containing_branches(db: Session, repository_id: str, commit_id: str) -> List[str]:
    """Branches whose history includes this commit.

    Answers "where did this land", which is the question someone reading a commit out
    of a list actually has.
    """
    from app.services.commit_graph import _collect_reachable_commits

    names = []
    for branch in db.query(Branch).filter(Branch.repository_id == repository_id).all():
        if not branch.head_commit_id:
            continue
        if commit_id == branch.head_commit_id:
            names.append(branch.name)
            continue
        if commit_id in set(_collect_reachable_commits(db, branch.head_commit_id)):
            names.append(branch.name)
    return sorted(names)


#: Diff rows sent in one response. A single real commit in this repository produces
#: 55,000 rows and 7.5 MB, which no browser renders usefully; past this budget the
#: files are still listed with their counts and their rows are fetched on demand.
DEFAULT_MAX_ROWS = 4000


def build(
    db: Session,
    repository: Repository,
    commit_id: str,
    against: Optional[str] = None,
    max_rows: Optional[int] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """The files one commit changed, with their diffs.

    ``path`` returns one file's diff in full, which is how a client loads a file that
    the row budget left out.
    """
    commit = (
        db.query(Commit)
        .filter(Commit.id == commit_id, Commit.repository_id == repository.id)
        .first()
    )
    if not commit:
        raise CommitNotFound(f"Commit '{commit_id}' not found in this repository")

    parents = _parents_of(db, commit)

    # `against` lets a caller ask for a merge's other side explicitly; anything not an
    # actual parent is ignored rather than honoured, so a arbitrary commit id cannot be
    # used to fabricate a diff that was never a real transition.
    base_id = against if (against and against in parents) else (parents[0] if parents else None)

    base_tree = _get_commit_tree(db, base_id) if base_id else {}
    head_tree = _get_commit_tree(db, commit.id)
    entries = tree_diff.compare_trees(base_tree, head_tree)

    # Totals are taken before anything is trimmed, so the header still reports what the
    # commit actually did rather than what fitted in the response.
    computed_totals = tree_diff.totals(entries)

    if path is not None:
        entries = [entry for entry in entries if entry["file_path"] == path]
    else:
        budget = DEFAULT_MAX_ROWS if max_rows is None else max(0, int(max_rows))
        omitted = 0
        for entry in entries:
            rows = entry["diff"].get("rows") or []
            if len(rows) <= budget:
                budget -= len(rows)
                continue
            # Keep the stats, drop the rows: the file still reports +N -M in the list,
            # and asking for it by path returns it in full.
            entry["diff"] = {**entry["diff"], "rows": [], "reason": "not_loaded"}
            entry["diff_omitted"] = True
            omitted += 1
        computed_totals["files_not_loaded"] = omitted

    return {
        "commit": {
            "id": commit.id,
            "short_id": commit.id[:8],
            "message": commit.message or "",
            "subject": (commit.message or "").split("\n")[0],
            "author": commit.author.username if commit.author else None,
            "timestamp": commit.created_at.isoformat() if commit.created_at else None,
            "parents": parents,
            "is_merge": len(parents) > 1,
            "is_root": not parents,
            "branches": _containing_branches(db, repository.id, commit.id),
            "tags": sorted(
                t.name for t in db.query(Tag).filter(
                    Tag.repository_id == repository.id, Tag.commit_id == commit.id
                ).all()
            ),
        },
        "compared_against": base_id,
        "files": entries,
        "requested_path": path,
        "totals": computed_totals,
    }
