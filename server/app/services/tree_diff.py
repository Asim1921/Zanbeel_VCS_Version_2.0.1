"""Comparing two trees into per-file diffs.

Both the pull request view and the single-commit view answer the same question — what
changed between these two snapshots — and they have to answer it identically, or a
commit read through a pull request looks different from the same commit read on its
own. So the comparison lives here once and both call it.
"""

from typing import Any, Dict, List, Optional

from app.config import DIFF_MAX_BYTES
from app.services.diff import _build_side_by_side_diff
from app.services.files import _is_binary_or_large
from app.services.merge import _try_decode_text

EMPTY_STATS = {"added": 0, "removed": 0, "unchanged": 0}


def classify(previous: Optional[bytes], current: Optional[bytes]) -> str:
    if previous is None:
        return "added"
    if current is None:
        return "removed"
    return "modified"


def compare_trees(
    base_tree: Dict[str, bytes], head_tree: Dict[str, bytes]
) -> List[Dict[str, Any]]:
    """One entry per path that differs, with its side-by-side diff.

    Paths that are identical on both sides are omitted: a commit view listing every
    unchanged file would bury the handful that actually moved.
    """
    entries: List[Dict[str, Any]] = []

    for path in sorted(set(base_tree) | set(head_tree)):
        previous_content = base_tree.get(path)
        current_content = head_tree.get(path)
        if previous_content == current_content:
            continue

        binary = (
            _is_binary_or_large(previous_content)
            or _is_binary_or_large(current_content)
        )
        entry: Dict[str, Any] = {
            "file_path": path,
            "status": classify(previous_content, current_content),
            "is_binary": binary,
        }

        if binary:
            entry["diff"] = {
                "rows": [],
                "stats": dict(EMPTY_STATS),
                "truncated": (
                    len(previous_content or b"") > DIFF_MAX_BYTES
                    or len(current_content or b"") > DIFF_MAX_BYTES
                ),
                "reason": "binary_or_large",
            }
        else:
            # A missing side is treated as empty, which renders an added file as all
            # additions and a deleted one as all deletions -- requiring both sides sent
            # every added and removed file down the binary path with no rows at all.
            previous_text = (
                _try_decode_text(previous_content) if previous_content is not None else ""
            )
            current_text = (
                _try_decode_text(current_content) if current_content is not None else ""
            )
            entry["diff"] = _build_side_by_side_diff(previous_text or "", current_text or "")

        entries.append(entry)

    return entries


def totals(entries: List[Dict[str, Any]]) -> Dict[str, int]:
    """Counts a header can show without the caller walking the rows again."""
    return {
        "files": len(entries),
        "added": sum(e["diff"]["stats"]["added"] for e in entries),
        "removed": sum(e["diff"]["stats"]["removed"] for e in entries),
        "files_added": sum(1 for e in entries if e["status"] == "added"),
        "files_modified": sum(1 for e in entries if e["status"] == "modified"),
        "files_removed": sum(1 for e in entries if e["status"] == "removed"),
    }
