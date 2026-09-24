"""Line-level authorship (blame).

Answers "which commit last changed this line, and who wrote it" for one file.

The walk runs backwards from a starting commit along first parents. At each step the
file is diffed against its version in the parent: lines that survive unchanged are
carried further back, and lines that do not appear in the parent were introduced by the
commit under examination, so they are attributed to it. The loop ends when every line is
attributed or history runs out.

Renames are followed through the ``file_lineage`` table, which already records
same-content path changes, so blame does not stop dead at the commit where a file moved.

Attribution is tracked as (original line index, index in the version currently being
examined) pairs. Carrying both is what keeps the answer anchored to the file the caller
asked about even as the content shifts underneath the walk.
"""

from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from database.models import Commit, CommitFile, FileLineage, FileObject

from app.services.merge import _try_decode_text
from app.services.paths import normalize as normalize_path, variants as path_variants

#: Blame is O(history x file size); refuse absurd inputs rather than stalling a worker.
MAX_BLAME_LINES = 50_000
MAX_BLAME_DEPTH = 1_000


def _file_bytes(db: Session, commit_id: Optional[str], path: str) -> Optional[bytes]:
    """Content of one path at one commit, or None when the file is absent there."""
    if not commit_id:
        return None
    row = (
        db.query(FileObject)
        .join(CommitFile, CommitFile.file_hash == FileObject.hash)
        # Both spellings: history pushed from Windows stores backslashes, and the
        # caller works in the forward-slash form every read endpoint hands out.
        .filter(
            CommitFile.commit_id == commit_id,
            CommitFile.file_path.in_(path_variants(path)),
        )
        .first()
    )
    return row.content if row else None


def _file_lines(db: Session, commit_id: Optional[str], path: str) -> Optional[List[str]]:
    raw = _file_bytes(db, commit_id, path)
    if raw is None:
        return None
    text = _try_decode_text(raw)
    if text is None:
        return None          # binary
    return text.splitlines()


def _previous_path(db: Session, repo_id: str, commit_id: str, path: str) -> str:
    """The path this file had before `commit_id`, following a recorded rename."""
    lineage = (
        db.query(FileLineage)
        .filter(
            FileLineage.repository_id == repo_id,
            FileLineage.commit_id == commit_id,
            FileLineage.new_path.in_(path_variants(path)),
        )
        .first()
    )
    return normalize_path(lineage.old_path) if lineage else path


def _first_parent(db: Session, commit_id: str) -> Optional[str]:
    commit = db.query(Commit).filter(Commit.id == commit_id).first()
    if not commit:
        return None
    if commit.parent_commit_id:
        return commit.parent_commit_id
    link = sorted(
        commit.parent_links or [], key=lambda l: getattr(l, "parent_order", 0)
    )
    return link[0].parent_commit_id if link else None


def _commit_meta(db: Session, commit_ids) -> Dict[str, Dict[str, Any]]:
    if not commit_ids:
        return {}
    meta: Dict[str, Dict[str, Any]] = {}
    for commit in db.query(Commit).filter(Commit.id.in_(list(commit_ids))).all():
        message = commit.message or ""
        meta[commit.id] = {
            "commit_id": commit.id,
            "author": commit.author.username if commit.author else None,
            "author_name": commit.author.full_name if commit.author else None,
            "date": commit.created_at.isoformat() if commit.created_at else None,
            "summary": message.splitlines()[0][:120] if message else "",
        }
    return meta


def blame_file(
    db: Session,
    repo_id: str,
    path: str,
    start_commit_id: str,
) -> Dict[str, Any]:
    """Attribute every line of `path` at `start_commit_id` to the commit that introduced it."""
    lines = _file_lines(db, start_commit_id, path)
    if lines is None:
        if _file_bytes(db, start_commit_id, path) is not None:
            return {"path": path, "binary": True, "lines": [], "commits": {}}
        return {"path": path, "not_found": True, "lines": [], "commits": {}}

    if len(lines) > MAX_BLAME_LINES:
        return {
            "path": path,
            "too_large": True,
            "line_count": len(lines),
            "limit": MAX_BLAME_LINES,
            "lines": [],
            "commits": {},
        }

    attribution: List[Optional[str]] = [None] * len(lines)
    # (index in the file the caller asked about, index in the version being examined)
    pending: List[Tuple[int, int]] = [(i, i) for i in range(len(lines))]

    current_commit: Optional[str] = start_commit_id
    current_path = path
    current_lines = lines
    depth = 0

    while pending and current_commit and depth < MAX_BLAME_DEPTH:
        depth += 1
        parent = _first_parent(db, current_commit)
        parent_path = _previous_path(db, repo_id, current_commit, current_path)
        parent_lines = _file_lines(db, parent, parent_path) if parent else None

        if parent_lines is None:
            # The file did not exist (or was binary) in the parent: this commit introduced
            # everything still unattributed.
            for original, _cur in pending:
                attribution[original] = current_commit
            pending = []
            break

        # Which lines of the current version also exist, unchanged, in the parent.
        carried: Dict[int, int] = {}
        for tag, i1, i2, j1, j2 in SequenceMatcher(
            None, parent_lines, current_lines, autojunk=False
        ).get_opcodes():
            if tag == "equal":
                for offset in range(j2 - j1):
                    carried[j1 + offset] = i1 + offset

        still_pending: List[Tuple[int, int]] = []
        for original, cur in pending:
            if cur in carried:
                still_pending.append((original, carried[cur]))
            else:
                attribution[original] = current_commit

        pending = still_pending
        current_commit = parent
        current_path = parent_path
        current_lines = parent_lines

    # Anything still unattributed ran past the depth limit; credit the oldest commit seen.
    for original, _cur in pending:
        attribution[original] = current_commit

    meta = _commit_meta(db, {c for c in attribution if c})
    return {
        "path": path,
        "commit_id": start_commit_id,
        "line_count": len(lines),
        "truncated_history": depth >= MAX_BLAME_DEPTH and bool(pending),
        "lines": [
            {
                "line": index + 1,
                "content": text,
                "commit_id": attribution[index],
            }
            for index, text in enumerate(lines)
        ],
        "commits": meta,
    }
