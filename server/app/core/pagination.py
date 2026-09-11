"""Opaque cursor encoding for keyset pagination."""

import json
import base64
import binascii

from typing import Any, Dict, Optional

from fastapi import HTTPException


def _encode_cursor(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")

def _decode_cursor(cursor: Optional[str]) -> Dict[str, Any]:
    if not cursor:
        return {}
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        pass
    raise HTTPException(status_code=400, detail="Invalid cursor")


# ---------------------------------------------------------------------------
# Offset pagination for list endpoints
#
# Keyset cursors are better for deep, stable paging and file history uses them.
# These listings are different: callers want a total ("63 repositories"), they
# sort by fields that are not unique, and nobody pages 400 deep. Offset paging
# gives a total and a page number honestly; pretending otherwise would add a
# cursor nobody can act on.
# ---------------------------------------------------------------------------

#: Applied when a caller names no limit. Chosen above the current dataset sizes
#: so adding pagination changes no existing screen's behaviour, while still
#: bounding a response that would otherwise grow without limit.
DEFAULT_PAGE_SIZE = 200

#: Hard ceiling regardless of what a caller asks for.
MAX_PAGE_SIZE = 1000


def resolve_page(limit: Optional[int], offset: Optional[int]) -> tuple:
    """Clamp caller-supplied paging into a safe (limit, offset) pair."""
    try:
        size = int(limit) if limit is not None else DEFAULT_PAGE_SIZE
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_SIZE
    size = max(1, min(size, MAX_PAGE_SIZE))

    try:
        start = int(offset) if offset is not None else 0
    except (TypeError, ValueError):
        start = 0
    return size, max(0, start)


def paginate_list(items, limit: Optional[int], offset: Optional[int]) -> tuple:
    """Slice an already-materialised list. Returns (page, meta).

    Used where the candidate set is filtered in Python (permission checks that
    cannot be expressed in SQL). The full list is still built, so this bounds
    the *response*, not the query — which is the honest description, and why
    ``total`` is exact rather than estimated.
    """
    size, start = resolve_page(limit, offset)
    total = len(items)
    page = items[start:start + size]
    return page, {
        "total": total,
        "limit": size,
        "offset": start,
        "returned": len(page),
        "has_more": start + len(page) < total,
        "next_offset": (start + size) if start + len(page) < total else None,
    }


def paginate_query(query, limit: Optional[int], offset: Optional[int]) -> tuple:
    """Page a SQLAlchemy query in the database. Returns (rows, meta).

    Preferred over ``paginate_list`` wherever the filter is expressible in SQL:
    only one page of rows leaves the database.
    """
    size, start = resolve_page(limit, offset)
    total = query.order_by(None).count()
    rows = query.limit(size).offset(start).all()
    return rows, {
        "total": total,
        "limit": size,
        "offset": start,
        "returned": len(rows),
        "has_more": start + len(rows) < total,
        "next_offset": (start + size) if start + len(rows) < total else None,
    }
