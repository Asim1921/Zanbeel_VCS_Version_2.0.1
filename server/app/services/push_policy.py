"""Admission control for incoming commits.

Two checks run before a commit reaches the object store:

* **Ignore rules** — paths excluded by the repository's ``.foxignore`` (or the built-in
  defaults) are dropped from the payload rather than rejected, so an older client that
  does not know about ``.foxignore`` still pushes successfully; it just cannot smuggle
  build output into the store.
* **Size limits** — a file over the per-file cap, or a push over the total cap, is
  refused outright with 413 and a message naming the offending paths. Refusing is the
  right call here: silently dropping a file the developer believes they committed would
  corrupt their history.

Both are enforced server-side because the client cannot be trusted to have run them.
"""

import base64
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from app.config import ENFORCE_IGNORE_ON_PUSH, MAX_PUSH_FILE_BYTES, MAX_PUSH_TOTAL_BYTES
from app.services.ignore_rules import IGNORE_FILENAME, IgnoreMatcher, build_matcher


def _decoded_size(payload: Any) -> int:
    """Byte length of a commit file payload without materialising the decoded bytes."""
    if payload is None:
        return 0
    if isinstance(payload, (bytes, bytearray)):
        return len(payload)
    if isinstance(payload, dict):  # {"content": "<b64>", ...}
        payload = payload.get("content") or payload.get("data") or ""
    if not isinstance(payload, str):
        return 0
    # base64 encodes 3 bytes per 4 characters; subtract the padding.
    text = payload.strip()
    if not text:
        return 0
    try:
        return len(base64.b64decode(text, validate=False))
    except Exception:
        return len(text.encode("utf-8", errors="ignore"))


def _human(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def resolve_ignore_matcher(
    files: Dict[str, Any],
    fallback_foxignore_text: Optional[str] = None,
) -> IgnoreMatcher:
    """Ignore rules for this push.

    A ``.foxignore`` in the incoming commit wins, so a developer adding one sees it take
    effect on the very same push. Otherwise the copy already on the branch is used, and
    failing that the built-in defaults.
    """
    incoming = files.get(IGNORE_FILENAME)
    if incoming is not None:
        try:
            text = base64.b64decode(incoming, validate=False).decode("utf-8", errors="replace")
            return build_matcher(text)
        except Exception:
            pass
    return build_matcher(fallback_foxignore_text)


def apply_ignore_rules(
    files: Dict[str, Any],
    matcher: IgnoreMatcher,
) -> Tuple[Dict[str, Any], List[Tuple[str, str]]]:
    """Drop ignored paths. Returns the kept files and a list of (path, matched pattern)."""
    if not ENFORCE_IGNORE_ON_PUSH or not matcher:
        return files, []

    kept: Dict[str, Any] = {}
    dropped: List[Tuple[str, str]] = []
    for path, payload in files.items():
        # The ignore file itself is always tracked, otherwise a rule could hide it.
        if path == IGNORE_FILENAME:
            kept[path] = payload
            continue
        pattern = matcher.matched_rule(path)
        if pattern:
            dropped.append((path, pattern))
        else:
            kept[path] = payload
    return kept, dropped


def enforce_size_limits(files: Dict[str, Any]) -> int:
    """Reject the push when a file or the payload as a whole is too large.

    Returns the total decoded size so the caller can log or record it.
    """
    total = 0
    oversized: List[Tuple[str, int]] = []

    for path, payload in files.items():
        size = _decoded_size(payload)
        total += size
        if MAX_PUSH_FILE_BYTES and size > MAX_PUSH_FILE_BYTES:
            oversized.append((path, size))

    if oversized:
        oversized.sort(key=lambda item: item[1], reverse=True)
        listed = ", ".join(f"{path} ({_human(size)})" for path, size in oversized[:5])
        more = f" and {len(oversized) - 5} more" if len(oversized) > 5 else ""
        raise HTTPException(
            status_code=413,
            detail=(
                f"Push rejected: {len(oversized)} file(s) exceed the "
                f"{_human(MAX_PUSH_FILE_BYTES)} per-file limit — {listed}{more}. "
                f"Add these paths to {IGNORE_FILENAME}, or ask an administrator to raise "
                f"FOXNEST_MAX_FILE_MB if they genuinely belong in version control."
            ),
        )

    if MAX_PUSH_TOTAL_BYTES and total > MAX_PUSH_TOTAL_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Push rejected: the commit totals {_human(total)}, over the "
                f"{_human(MAX_PUSH_TOTAL_BYTES)} limit for a single push. Split it into "
                f"smaller commits, or exclude build output via {IGNORE_FILENAME}."
            ),
        )

    return total


def screen_commit_files(
    commit_data: Dict[str, Any],
    fallback_foxignore_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Run both checks over ``commit_data['files']`` and return a report.

    Mutates ``commit_data`` in place so ignored paths never reach the object store.
    Raises HTTPException(413) when a size limit is exceeded.
    """
    files = commit_data.get("files") or {}
    if not isinstance(files, dict) or not files:
        return {"kept": 0, "dropped": [], "total_bytes": 0}

    matcher = resolve_ignore_matcher(files, fallback_foxignore_text)
    kept, dropped = apply_ignore_rules(files, matcher)

    # Size limits apply to what would actually be stored, so screen after filtering.
    total = enforce_size_limits(kept)

    commit_data["files"] = kept
    return {"kept": len(kept), "dropped": dropped, "total_bytes": total}
