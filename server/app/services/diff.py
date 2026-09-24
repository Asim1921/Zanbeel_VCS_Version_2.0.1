"""Side-by-side diff rendering with size guards."""

import difflib

from typing import Any, Dict, List

from app.config import DIFF_MAX_LINES


def _build_side_by_side_diff(previous_text: str, current_text: str) -> Dict[str, Any]:
    """Side-by-side rows, each carrying the line number it came from on each side.

    The line numbers are what a review comment anchors to. A comment records
    ``(file_path, line, side)``, so without them a row is just text and there is
    nothing to attach a thread to. They are computed here rather than counted in the
    client so the web client, the CLI and the server all agree on which line is
    line 42 -- a disagreement would silently move comments onto the wrong code.

    A side is ``None`` where that side has no line: the padding half of an uneven
    replacement, or the absent side of an addition or deletion.
    """
    previous_lines = previous_text.splitlines()
    current_lines = current_text.splitlines()
    matcher = difflib.SequenceMatcher(a=previous_lines, b=current_lines)

    rows: List[Dict[str, Any]] = []
    truncated = False

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if len(rows) >= DIFF_MAX_LINES:
            truncated = True
            break

        if tag == "equal":
            for idx in range(i2 - i1):
                rows.append({
                    "type": "same",
                    "previous": previous_lines[i1 + idx],
                    "current": current_lines[j1 + idx],
                    "previous_line": i1 + idx + 1,
                    "current_line": j1 + idx + 1,
                })
                if len(rows) >= DIFF_MAX_LINES:
                    truncated = True
                    break
        elif tag == "delete":
            for offset, line in enumerate(previous_lines[i1:i2]):
                rows.append({
                    "type": "removed",
                    "previous": line,
                    "current": "",
                    "previous_line": i1 + offset + 1,
                    "current_line": None,
                })
                if len(rows) >= DIFF_MAX_LINES:
                    truncated = True
                    break
        elif tag == "insert":
            for offset, line in enumerate(current_lines[j1:j2]):
                rows.append({
                    "type": "added",
                    "previous": "",
                    "current": line,
                    "previous_line": None,
                    "current_line": j1 + offset + 1,
                })
                if len(rows) >= DIFF_MAX_LINES:
                    truncated = True
                    break
        else:
            old_block = previous_lines[i1:i2]
            new_block = current_lines[j1:j2]
            max_len = max(len(old_block), len(new_block))
            for idx in range(max_len):
                has_old = idx < len(old_block)
                has_new = idx < len(new_block)
                rows.append({
                    "type": "changed",
                    "previous": old_block[idx] if has_old else "",
                    "current": new_block[idx] if has_new else "",
                    "previous_line": i1 + idx + 1 if has_old else None,
                    "current_line": j1 + idx + 1 if has_new else None,
                })
                if len(rows) >= DIFF_MAX_LINES:
                    truncated = True
                    break

    stats = {
        "added": sum(1 for row in rows if row["type"] in ["added", "changed"] and row["current"]),
        "removed": sum(1 for row in rows if row["type"] in ["removed", "changed"] and row["previous"]),
        "unchanged": sum(1 for row in rows if row["type"] == "same")
    }

    return {
        "rows": rows,
        "stats": stats,
        "truncated": truncated,
        "max_lines": DIFF_MAX_LINES
    }
