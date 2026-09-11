"""Garbage collection for the object store.

Two independent kinds of garbage accumulate:

* **Unreferenced blobs** — rows in ``file_objects`` that nothing points at: no commit, no
  pending commit, no rename lineage record and no open merge-conflict session. These arise
  when a commit is rejected in review, when a pending commit is discarded, or when history
  is rolled back. They are safe to delete once nothing references them, and this is what
  reclaims disk. ``BLOB_REFERENCE_COLUMNS`` below is the authoritative list; a column
  missing from it means deleting content that is still in use.

* **Dangling commits** — commits unreachable from any branch head, tag, release, pull
  request merge or repository head. Deleting these is a *history* change rather than a
  storage cleanup, so they are only reported unless explicitly requested.

Blobs are content-addressed and shared across every repository, so liveness is global:
a blob is garbage only when nothing anywhere references its hash. Scoping a sweep to one
repository would be wrong and is deliberately not offered.
"""

from typing import Any, Dict, List, Optional, Set

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from database.models import (
    Branch,
    Commit,
    CommitFile,
    CommitParent,
    FileLineage,
    FileObject,
    MergeConflictFile,
    PendingCommitFile,
    PullRequest,
    Repository,
    Tag,
)

#: Deleting more than this in one sweep requires an explicit higher limit, so an
#: unexpected result cannot wipe the store in a single call.
DEFAULT_DELETE_LIMIT = 5000


#: Every column anywhere that can name a blob. Missing one deletes content that is still
#: in use, so this list is the safety-critical part of garbage collection:
#:   - pending commits hold the only reference to their content until a reviewer approves
#:   - merge-conflict rows hold the only reference to the versions being reconciled
#:   - lineage rows pin the content a rename was detected through
BLOB_REFERENCE_COLUMNS = (
    CommitFile.file_hash,
    PendingCommitFile.file_hash,
    FileLineage.file_hash,
    MergeConflictFile.base_file_hash,
    MergeConflictFile.ours_file_hash,
    MergeConflictFile.theirs_file_hash,
    MergeConflictFile.resolved_file_hash,
)


def referenced_hashes(db: Session) -> Set[str]:
    """Every blob hash reachable from live metadata."""
    hashes: Set[str] = set()
    for column in BLOB_REFERENCE_COLUMNS:
        try:
            rows = db.query(column).distinct().all()
        except (OperationalError, ProgrammingError):
            # The table has not been created yet on this database. Nothing can reference
            # a blob through a table that does not exist, so treat it as contributing
            # nothing rather than failing the sweep.
            db.rollback()
            continue
        for (value,) in rows:
            if value:
                hashes.add(value)
    return hashes


def _ref_commit_ids(db: Session) -> Set[str]:
    """Commit ids named directly by a ref: branch, tag, release, PR merge, repo head."""
    roots: Set[str] = set()
    for (value,) in db.query(Branch.head_commit_id).distinct():
        if value:
            roots.add(value)
    for (value,) in db.query(Repository.head_commit_id).distinct():
        if value:
            roots.add(value)
    for (value,) in db.query(Tag.commit_id).distinct():
        if value:
            roots.add(value)
    for (value,) in db.query(PullRequest.merge_commit_id).distinct():
        if value:
            roots.add(value)
    return roots


def reachable_commit_ids(db: Session) -> Set[str]:
    """Every commit reachable by walking parents back from all refs."""
    parents: Dict[str, List[str]] = {}
    for commit_id, parent_id in db.query(Commit.id, Commit.parent_commit_id):
        if parent_id:
            parents.setdefault(commit_id, []).append(parent_id)
    for commit_id, parent_id in db.query(CommitParent.commit_id, CommitParent.parent_commit_id):
        if parent_id:
            bucket = parents.setdefault(commit_id, [])
            if parent_id not in bucket:
                bucket.append(parent_id)

    seen: Set[str] = set()
    stack = list(_ref_commit_ids(db))
    while stack:
        commit_id = stack.pop()
        if not commit_id or commit_id in seen:
            continue
        seen.add(commit_id)
        stack.extend(parents.get(commit_id, ()))
    return seen


def analyze(db: Session) -> Dict[str, Any]:
    """Report what a sweep would reclaim, without changing anything."""
    live = referenced_hashes(db)

    total_objects = 0
    total_bytes = 0
    garbage: List[Dict[str, Any]] = []
    garbage_bytes = 0
    for blob_hash, size in db.query(FileObject.hash, FileObject.size):
        total_objects += 1
        total_bytes += size or 0
        if blob_hash not in live:
            garbage.append({"hash": blob_hash, "size": size or 0})
            garbage_bytes += size or 0

    all_commits = {value for (value,) in db.query(Commit.id)}
    dangling = sorted(all_commits - reachable_commit_ids(db))

    garbage.sort(key=lambda item: item["size"], reverse=True)
    return {
        "objects_total": total_objects,
        "objects_bytes": total_bytes,
        "unreferenced_objects": len(garbage),
        "unreferenced_bytes": garbage_bytes,
        "largest_unreferenced": garbage[:20],
        "dangling_commits": dangling,
        "dangling_commit_count": len(dangling),
    }


def collect(
    db: Session,
    dry_run: bool = True,
    limit: Optional[int] = None,
    prune_dangling_commits: bool = False,
) -> Dict[str, Any]:
    """Delete unreferenced blobs (and optionally dangling commits).

    ``dry_run`` defaults to True: a caller has to ask for deletion explicitly.
    """
    report = analyze(db)
    limit = DEFAULT_DELETE_LIMIT if limit is None else max(0, limit)

    report["dry_run"] = dry_run
    report["limit"] = limit
    report["deleted_objects"] = 0
    report["deleted_bytes"] = 0
    report["deleted_commits"] = 0

    if dry_run:
        return report

    live = referenced_hashes(db)
    deleted_objects = 0
    deleted_bytes = 0

    # Re-read inside the mutating pass rather than trusting the analysis snapshot.
    doomed = [
        (blob_hash, size or 0)
        for blob_hash, size in db.query(FileObject.hash, FileObject.size)
        if blob_hash not in live
    ][:limit]

    from app.services.blob_store import store as blob_store

    for blob_hash, size in doomed:
        db.query(FileObject).filter(FileObject.hash == blob_hash).delete(synchronize_session=False)
        # Drop the on-disk copy too, otherwise the row goes but the bytes stay forever.
        # Legacy rows carried their content inline and simply have nothing on disk.
        blob_store.delete(blob_hash)
        deleted_objects += 1
        deleted_bytes += size

    if prune_dangling_commits:
        reachable = reachable_commit_ids(db)
        dangling = [value for (value,) in db.query(Commit.id) if value not in reachable]
        for commit_id in dangling[:limit]:
            db.query(CommitFile).filter(CommitFile.commit_id == commit_id).delete(synchronize_session=False)
            db.query(CommitParent).filter(CommitParent.commit_id == commit_id).delete(synchronize_session=False)
            db.query(CommitParent).filter(CommitParent.parent_commit_id == commit_id).delete(synchronize_session=False)
            db.query(FileLineage).filter(FileLineage.commit_id == commit_id).delete(synchronize_session=False)
            db.query(Commit).filter(Commit.id == commit_id).delete(synchronize_session=False)
            report["deleted_commits"] += 1

    db.commit()

    report["deleted_objects"] = deleted_objects
    report["deleted_bytes"] = deleted_bytes
    report["truncated"] = len(doomed) >= limit > 0
    return report
