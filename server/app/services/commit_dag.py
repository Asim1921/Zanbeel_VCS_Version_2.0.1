"""The commit graph across every branch, ordered so it can be drawn.

``/commits`` answers for one branch by design, which is the right shape for a history
list and the wrong one for a graph: the whole point of the picture is where branches
left each other and where they came back. This assembles the reachable set from every
branch tip at once and puts it in an order a renderer can walk top to bottom.

The ordering is the part that has to be right. A renderer assigns lanes by looking at
which commits are still waiting to be drawn, so a commit must always appear *before*
its parents -- otherwise a parent claims a lane that its own child has not opened yet
and the rails cross wrongly. Sorting by timestamp alone does not guarantee that: clock
skew between machines, or a rebase that rewrites dates, is enough to put a parent
ahead of its child. So this topologically sorts, and only uses the timestamp to choose
between commits that are genuinely ready at the same moment.
"""

import heapq
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database.crud import BranchCRUD
from database.models import Commit, CommitParent, Repository, Tag

#: Enough to show the shape of a repository's history without sending all of it.
DEFAULT_LIMIT = 300
MAX_LIMIT = 2000


def _parents_of_many(db: Session, commit_ids: List[str]) -> Dict[str, List[str]]:
    """Parent ids for many commits at once, ordered, in one query per table."""
    if not commit_ids:
        return {}

    parents: Dict[str, List[str]] = {cid: [] for cid in commit_ids}

    links = (
        db.query(CommitParent)
        .filter(CommitParent.commit_id.in_(commit_ids))
        .order_by(CommitParent.commit_id, CommitParent.parent_order)
        .all()
    )
    for link in links:
        parents.setdefault(link.commit_id, []).append(link.parent_commit_id)

    # Older commits predate the parent table and carry a single parent column.
    rows = db.query(Commit.id, Commit.parent_commit_id).filter(Commit.id.in_(commit_ids)).all()
    for commit_id, parent_id in rows:
        if parent_id and not parents.get(commit_id):
            parents[commit_id] = [parent_id]

    return parents


def _reachable(db: Session, heads: List[str], limit: int) -> Dict[str, List[str]]:
    """Commits reachable from any head, with their parents, breadth-first.

    Breadth-first from every tip at once keeps the result near the tips when the limit
    bites: a truncated graph should show recent history across all branches rather than
    the whole of one branch and none of the others.
    """
    seen: Dict[str, List[str]] = {}
    frontier = [h for h in heads if h]

    while frontier and len(seen) < limit:
        batch = [cid for cid in frontier if cid not in seen][: max(0, limit - len(seen))]
        if not batch:
            break
        parents = _parents_of_many(db, batch)
        for cid in batch:
            seen[cid] = parents.get(cid, [])
        frontier = [p for cid in batch for p in seen[cid] if p and p not in seen]

    return seen


def _topological_order(
    graph: Dict[str, List[str]], timestamps: Dict[str, Any]
) -> List[str]:
    """Children before parents, newest first among those equally ready.

    Kahn's algorithm over the child-to-parent edges, with a heap so that whenever
    several commits could come next the most recent one does -- which is what makes the
    result read like history rather than like a traversal.
    """
    pending: Dict[str, int] = {cid: 0 for cid in graph}
    for cid, parents in graph.items():
        for parent in parents:
            if parent in pending:
                pending[parent] += 1

    def sort_key(cid: str):
        stamp = timestamps.get(cid)
        # Negated so the heap yields the newest first; id breaks ties reproducibly.
        return (-(stamp.timestamp() if stamp else 0), cid)

    ready = [(sort_key(cid), cid) for cid, count in pending.items() if count == 0]
    heapq.heapify(ready)

    order: List[str] = []
    while ready:
        _key, cid = heapq.heappop(ready)
        order.append(cid)
        for parent in graph.get(cid, []):
            if parent not in pending:
                continue
            pending[parent] -= 1
            if pending[parent] == 0:
                heapq.heappush(ready, (sort_key(parent), parent))

    # A cycle cannot occur in commit history, but a truncated graph can leave a parent
    # with a child that was cut: append the remainder rather than dropping it.
    if len(order) < len(graph):
        leftover = sorted(
            (cid for cid in graph if cid not in set(order)), key=sort_key
        )
        order.extend(leftover)

    return order


def build(
    db: Session,
    repository: Repository,
    limit: Optional[int] = None,
    branch: Optional[str] = None,
) -> Dict[str, Any]:
    """The commit graph for a repository, ready to draw."""
    limit = min(max(int(limit or DEFAULT_LIMIT), 1), MAX_LIMIT)

    branches = BranchCRUD.get_branches_by_repository(db, repository.id) or []
    if branch:
        branches = [b for b in branches if b.name == branch]

    heads = [b.head_commit_id for b in branches if b.head_commit_id]
    graph = _reachable(db, heads, limit)

    rows = (
        db.query(Commit)
        .filter(Commit.id.in_(list(graph.keys())))
        .all()
        if graph else []
    )
    by_id = {row.id: row for row in rows}
    timestamps = {cid: getattr(by_id.get(cid), "created_at", None) for cid in graph}

    order = _topological_order(graph, timestamps)

    tags = db.query(Tag).filter(Tag.repository_id == repository.id).all()
    tags_by_commit: Dict[str, List[str]] = {}
    for tag in tags:
        tags_by_commit.setdefault(tag.commit_id, []).append(tag.name)

    heads_by_commit: Dict[str, List[Dict[str, Any]]] = {}
    for b in branches:
        if b.head_commit_id:
            heads_by_commit.setdefault(b.head_commit_id, []).append(
                {"name": b.name, "is_default": bool(b.is_default)}
            )

    commits: List[Dict[str, Any]] = []
    for cid in order:
        row = by_id.get(cid)
        parents = graph.get(cid, [])
        commits.append({
            "id": cid,
            "short_id": cid[:8],
            "message": (row.message or "").split("\n")[0] if row else "",
            "author": row.author.username if (row and row.author) else None,
            "timestamp": row.created_at.isoformat() if (row and row.created_at) else None,
            "parents": parents,
            # A parent that was cut by the limit: the renderer draws the rail running
            # off the bottom rather than ending the lane as though history stopped.
            "truncated_parents": [p for p in parents if p not in graph],
            "is_merge": len(parents) > 1,
            "branch_heads": heads_by_commit.get(cid, []),
            "tags": sorted(tags_by_commit.get(cid, [])),
        })

    total = db.query(Commit).filter(Commit.repository_id == repository.id).count()
    return {
        "commits": commits,
        "branches": [
            {"name": b.name, "head_commit_id": b.head_commit_id,
             "is_default": bool(b.is_default)}
            for b in branches
        ],
        "returned": len(commits),
        "total": total,
        "truncated": total > len(commits),
        "limit": limit,
    }
