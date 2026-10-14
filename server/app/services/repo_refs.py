"""What a name in a repository points at, and what belongs to a repository at all.

Branches, tags and releases all end in a commit id, and every route that accepted one
checked only that the id existed *somewhere* on the server. A tag in one repository could
therefore name a commit of another -- including one the caller could not read -- and the
tag, the release built on it and the source archive downloaded from it would then serve
that other repository's files.

"Belongs to this repository" has to be asked carefully, because commit rows are shared:
``Commit.id`` is the whole primary key, so a fork's branches point at the upstream's rows,
and a pull request merged from a fork brings the fork's rows into the upstream's history.
A commit belongs to a repository when the repository owns the row or can reach it from
one of its own branches or tags -- which is what ``git cat-file -e`` would answer.

The same graph answers how far a branch is ahead of and behind the default branch, for
the branch list. It is loaded once per request from the repository's own rows, and only
commits from elsewhere (an upstream, a merged fork) are fetched as the walk meets them.
"""

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

from fastapi import HTTPException
from sqlalchemy.orm import Session

from database.models import Branch, Commit, CommitParent, PullRequest, Repository, Tag

from app.services import branch_protection as protection
from app.services.commit_dag import _parents_of_many


#: How many commits one walk may visit before ahead/behind is reported as unknown.
MAX_WALK = 50_000

#: A branch whose last commit is older than this is "stale", as GitHub reckons it.
STALE_AFTER_DAYS = 90

_HEX = re.compile(r"^[0-9a-fA-F]{7,40}$")


class CommitGraph:
    """Parent links for one repository's history, loaded once and walked in memory."""

    def __init__(self, db: Session, repository: Repository):
        self.db = db
        self.parents: Dict[str, List[str]] = {}
        self._reach_cache: Dict[str, Optional[Set[str]]] = {}
        owners = _lineage(db, repository)
        rows = (
            db.query(Commit.id, Commit.parent_commit_id)
            .filter(Commit.repository_id.in_(owners))
            .all()
        )
        for commit_id, parent_id in rows:
            self.parents[commit_id] = [parent_id] if parent_id else []
        links = (
            db.query(CommitParent.commit_id, CommitParent.parent_commit_id)
            .join(Commit, Commit.id == CommitParent.commit_id)
            .filter(Commit.repository_id.in_(owners))
            .order_by(CommitParent.commit_id, CommitParent.parent_order)
            .all()
        )
        explicit: Dict[str, List[str]] = {}
        for commit_id, parent_id in links:
            explicit.setdefault(commit_id, []).append(parent_id)
        # The parent table is authoritative where it has rows; older commits only have
        # the single parent column.
        self.parents.update(explicit)

    def _load(self, commit_ids: Iterable[str]) -> None:
        missing = [cid for cid in commit_ids if cid and cid not in self.parents]
        if missing:
            found = _parents_of_many(self.db, missing)
            for cid in missing:
                self.parents[cid] = found.get(cid, [])

    def reachable(self, head: Optional[str]) -> Optional[Set[str]]:
        """Every commit reachable from ``head``, or None if the walk is too large."""
        if not head:
            return set()
        if head in self._reach_cache:
            return self._reach_cache[head]
        seen: Set[str] = set()
        frontier = [head]
        while frontier:
            self._load(frontier)
            following = []
            for cid in frontier:
                if cid in seen:
                    continue
                seen.add(cid)
                following.extend(p for p in self.parents.get(cid, []) if p and p not in seen)
            if len(seen) > MAX_WALK:
                self._reach_cache[head] = None
                return None
            frontier = following
        self._reach_cache[head] = seen
        return seen

    def ahead_behind(self, base: Optional[str], head: Optional[str]) -> Dict[str, Optional[int]]:
        """Commits on ``head`` not on ``base`` (ahead), and the other way round (behind)."""
        if not base or not head:
            return {"ahead": None, "behind": None}
        if base == head:
            return {"ahead": 0, "behind": 0}
        base_set, head_set = self.reachable(base), self.reachable(head)
        if base_set is None or head_set is None:
            return {"ahead": None, "behind": None}
        return {"ahead": len(head_set - base_set), "behind": len(base_set - head_set)}


def _lineage(db: Session, repository: Repository) -> List[str]:
    """This repository and every repository it was forked from, nearest first."""
    ids, current, guard = [], repository, 0
    while current is not None and current.id not in ids and guard < 16:
        ids.append(current.id)
        parent_id = getattr(current, "forked_from_id", None)
        current = db.query(Repository).filter(Repository.id == parent_id).first() if parent_id else None
        guard += 1
    return ids


def default_branch(db: Session, repository_id: str) -> Optional[Branch]:
    """The default branch, without creating one as ``_get_default_branch`` does."""
    branches = db.query(Branch).filter(Branch.repository_id == repository_id).all()
    return (
        next((b for b in branches if b.is_default), None)
        or next((b for b in branches if b.name == "main"), None)
        or (sorted(branches, key=lambda b: b.name)[0] if branches else None)
    )


def commit_in_repository(db: Session, repository: Repository, commit_id: Optional[str]) -> Optional[Commit]:
    """The commit, if ``repository`` owns it or can reach it from a branch or tag."""
    if not commit_id:
        return None
    commit = db.query(Commit).filter(Commit.id == commit_id).first()
    if commit is None:
        return None
    if commit.repository_id == repository.id:
        return commit
    heads = [
        row[0] for row in
        db.query(Branch.head_commit_id).filter(Branch.repository_id == repository.id).all()
        if row[0]
    ]
    heads += [
        row[0] for row in
        db.query(Tag.commit_id).filter(Tag.repository_id == repository.id).all()
        if row[0]
    ]
    graph = CommitGraph(db, repository)
    for head in heads:
        reach = graph.reachable(head)
        if reach is not None and commit.id in reach:
            return commit
    return None


def require_commit(db: Session, repository: Repository, commit_id: str) -> Commit:
    commit = commit_in_repository(db, repository, (commit_id or "").strip())
    if commit is None:
        raise HTTPException(status_code=404, detail="Commit not found in this repository")
    return commit


def resolve(db: Session, repository: Repository, ref: Optional[str]) -> Dict[str, Any]:
    """What ``ref`` names: a branch, a tag, or a commit (full id or an unambiguous prefix).

    Branches win over tags and tags over commit ids, the order ``git rev-parse`` uses.
    No ref means the default branch.
    """
    ref = (ref or "").strip()
    if not ref:
        branch = default_branch(db, repository.id)
        if branch is None or not branch.head_commit_id:
            raise HTTPException(status_code=404, detail="This repository has no commits yet")
        return {"kind": "branch", "name": branch.name, "commit_id": branch.head_commit_id}

    branch = (
        db.query(Branch)
        .filter(Branch.repository_id == repository.id, Branch.name == ref)
        .first()
    )
    if branch is not None:
        if not branch.head_commit_id:
            raise HTTPException(status_code=404, detail=f"Branch '{ref}' has no commits yet")
        return {"kind": "branch", "name": branch.name, "commit_id": branch.head_commit_id}

    tag = db.query(Tag).filter(Tag.repository_id == repository.id, Tag.name == ref).first()
    if tag is not None:
        return {"kind": "tag", "name": tag.name, "commit_id": tag.commit_id}

    if _HEX.match(ref):
        prefix = ref.lower()
        if len(prefix) == 40:
            commit = commit_in_repository(db, repository, prefix)
            if commit is not None:
                return {"kind": "commit", "name": commit.id[:12], "commit_id": commit.id}
        else:
            candidates = [
                row[0] for row in
                db.query(Commit.id).filter(Commit.id.like(f"{prefix}%")).limit(50).all()
            ]
            matches = [cid for cid in candidates if commit_in_repository(db, repository, cid)]
            if len(matches) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{ref}' is ambiguous: it starts {len(matches)} commits. Use more characters.",
                )
            if matches:
                return {"kind": "commit", "name": matches[0][:12], "commit_id": matches[0]}

    raise HTTPException(status_code=404, detail=f"No branch, tag or commit named '{ref}' in this repository")


def normalize_tag_name(name: str) -> str:
    """Tag names follow the branch rules, with errors that say "tag"."""
    from app.services.branches import _normalize_branch_name
    try:
        return _normalize_branch_name(name)
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc.detail).replace("Branch name", "Tag name"),
        )


# --- the branch list ---------------------------------------------------------------

def _subject(message: Optional[str]) -> str:
    return (message or "").strip().splitlines()[0][:200] if message else ""


def commit_summary(commit: Optional[Commit]) -> Optional[Dict[str, Any]]:
    if commit is None:
        return None
    return {
        "id": commit.id,
        "short_id": commit.id[:8],
        "message": _subject(commit.message),
        "author": commit.author.username if commit.author else None,
        "timestamp": commit.created_at.isoformat() if commit.created_at else None,
    }


def open_pull_requests_for(db: Session, repository: Repository) -> Dict[str, List[Dict[str, Any]]]:
    """Open pull requests that use each branch of ``repository``, as source or target."""
    rows = (
        db.query(PullRequest)
        .filter(
            PullRequest.status == "open",
            (PullRequest.repository_id == repository.id)
            | (PullRequest.source_repository_id == repository.id),
        )
        .all()
    )
    uses: Dict[str, List[Dict[str, Any]]] = {}
    for pr in rows:
        entry = {"id": pr.id, "title": pr.title, "repository_id": pr.repository_id,
                 "source_branch": pr.source_branch, "target_branch": pr.target_branch}
        source_repo = pr.source_repository_id or pr.repository_id
        if source_repo == repository.id:
            uses.setdefault(pr.source_branch, []).append({**entry, "role": "source"})
        if pr.repository_id == repository.id:
            uses.setdefault(pr.target_branch, []).append({**entry, "role": "target"})
    return uses


def branch_overview(db: Session, repository: Repository, branches: List[Branch]) -> List[Dict[str, Any]]:
    """Each branch with its last commit, its distance from the default branch, its
    protection and the open pull requests that use it."""
    base = next((b for b in branches if b.is_default), None) or default_branch(db, repository.id)
    base_head = base.head_commit_id if base else None
    graph = CommitGraph(db, repository)

    heads = [b.head_commit_id for b in branches if b.head_commit_id]
    commits = {
        c.id: c for c in
        (db.query(Commit).filter(Commit.id.in_(heads)).all() if heads else [])
    }
    prs = open_pull_requests_for(db, repository)
    stale_before = datetime.utcnow().timestamp() - STALE_AFTER_DAYS * 86400

    out = []
    for b in branches:
        head = commits.get(b.head_commit_id)
        effective = protection.resolve_effective_policy(db, repository, b.name)
        counts = (
            {"ahead": 0, "behind": 0} if base is not None and b.name == base.name
            else graph.ahead_behind(base_head, b.head_commit_id)
        )
        updated = head.created_at if head is not None and head.created_at else b.updated_at
        out.append({
            "name": b.name,
            "head_commit_id": b.head_commit_id,
            "is_default": bool(b.is_default),
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "updated_at": b.updated_at.isoformat() if b.updated_at else None,
            "head_commit": commit_summary(head),
            "ahead": counts["ahead"],
            "behind": counts["behind"],
            "compared_to": base.name if base is not None else None,
            "stale": bool(updated and updated.timestamp() < stale_before),
            "protection": {
                "mode": effective.mode,
                "restricted": effective.is_restricted,
                "matched_patterns": list(effective.matched_patterns),
                "permits": {op: effective.permits(op) for op in protection.ALL_OPERATIONS},
            },
            "pull_requests": prs.get(b.name, []),
        })
    return out
