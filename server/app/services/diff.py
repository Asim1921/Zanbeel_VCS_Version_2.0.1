"""Side-by-side diff rendering with size guards."""

import difflib

from typing import Any, Dict, List

from app.config import DIFF_MAX_LINES


def _build_side_by_side_diff(previous_text: str, current_text: str) -> Dict[str, Any]:
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
                    "current": current_lines[j1 + idx]
                })
                if len(rows) >= DIFF_MAX_LINES:
                    truncated = True
                    break
        elif tag == "delete":
            for line in previous_lines[i1:i2]:
                rows.append({"type": "removed", "previous": line, "current": ""})
                if len(rows) >= DIFF_MAX_LINES:
                    truncated = True
                    break
        elif tag == "insert":
            for line in current_lines[j1:j2]:
                rows.append({"type": "added", "previous": "", "current": line})
                if len(rows) >= DIFF_MAX_LINES:
                    truncated = True
                    break
        else:
            old_block = previous_lines[i1:i2]
            new_block = current_lines[j1:j2]
            max_len = max(len(old_block), len(new_block))
            for idx in range(max_len):
                rows.append({
                    "type": "changed",
                    "previous": old_block[idx] if idx < len(old_block) else "",
                    "current": new_block[idx] if idx < len(new_block) else ""
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
