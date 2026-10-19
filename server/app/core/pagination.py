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
#
# Every paged response says so three ways, so no client has to guess whether it
# saw everything: a ``pagination`` object in the body (total, has_more,
# next_offset), an ``X-Total-Count`` header, and a ``Link`` header with first,
# prev, next and last (RFC 8288, as GitHub sends). A client that wants the whole
# collection follows ``next`` until there is none. Ordering always ends in a
# unique column, so pages neither overlap nor skip rows when sort keys tie.
# ---------------------------------------------------------------------------

#: Applied when a caller names no limit. Chosen above the current dataset sizes
#: so adding pagination changes no existing screen's behaviour, while still
#: bounding a response that would otherwise grow without limit.
DEFAULT_PAGE_SIZE = 200

#: Hard ceiling regardless of what a caller asks for.
MAX_PAGE_SIZE = 1000


def resolve_page(limit: Optional[int], offset: Optional[int],
                 default: int = DEFAULT_PAGE_SIZE, maximum: int = MAX_PAGE_SIZE) -> tuple:
    """Clamp caller-supplied paging into a safe (limit, offset) pair."""
    try:
        size = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        size = default
    size = max(1, min(size, maximum))

    try:
        start = int(offset) if offset is not None else 0
    except (TypeError, ValueError):
        start = 0
    return size, max(0, start)


def page_params(default: int = DEFAULT_PAGE_SIZE, maximum: int = MAX_PAGE_SIZE):
    """A FastAPI dependency yielding a clamped (limit, offset) for one endpoint.

        page = Depends(page_params(default=50))
        rows, meta = paginate_query(query, *page)
    """
    from fastapi import Query

    # Integers, so `limit=abc` is a 422 rather than a silent default; out-of-range
    # values are clamped, the way GitHub caps per_page.
    def dependency(
        limit: Optional[int] = Query(None, description=f"Page size, 1-{maximum} (default {default})"),
        offset: Optional[int] = Query(None, description="Rows to skip (default 0)"),
    ) -> tuple:
        return resolve_page(limit, offset, default=default, maximum=maximum)

    return dependency


def page_meta(total: int, size: int, start: int, returned: int) -> Dict[str, Any]:
    """The ``pagination`` object for a page an endpoint fetched itself."""
    more = start + returned < total
    return {
        "total": total,
        "limit": size,
        "offset": start,
        "returned": returned,
        "has_more": more,
        "next_offset": (start + size) if more else None,
    }


def paginate_list(items, limit: Optional[int], offset: Optional[int],
                  maximum: int = MAX_PAGE_SIZE) -> tuple:
    """Slice an already-materialised list. Returns (page, meta).

    Used where the candidate set is filtered in Python (permission checks that
    cannot be expressed in SQL). The full list is still built, so this bounds
    the *response*, not the query — which is the honest description, and why
    ``total`` is exact rather than estimated.
    """
    size, start = resolve_page(limit, offset, maximum=maximum)
    total = len(items)
    page = items[start:start + size]
    return page, page_meta(total, size, start, len(page))


def paginate_query(query, limit: Optional[int], offset: Optional[int],
                   maximum: int = MAX_PAGE_SIZE) -> tuple:
    """Page a SQLAlchemy query in the database. Returns (rows, meta).

    Preferred over ``paginate_list`` wherever the filter is expressible in SQL:
    only one page of rows leaves the database.
    """
    size, start = resolve_page(limit, offset, maximum=maximum)
    total = query.order_by(None).count()
    rows = query.limit(size).offset(start).all()
    return rows, page_meta(total, size, start, len(rows))


def set_page_headers(request, response, meta: Dict[str, Any]) -> None:
    """``X-Total-Count`` and an RFC 8288 ``Link`` header for one page.

    The links are relative (path and query only), so they stay correct behind a proxy
    that rewrites the host. Browsers can read both headers cross-origin because the
    CORS middleware exposes them.
    """
    total, size, start = meta["total"], meta["limit"], meta["offset"]

    def link(offset: int, rel: str) -> str:
        url = request.url.include_query_params(limit=size, offset=max(0, offset))
        return f'<{url.path}?{url.query}>; rel="{rel}"'

    last = max(0, ((total - 1) // size) * size) if total else 0
    links = [link(0, "first")]
    if start > 0:
        links.append(link(max(0, start - size), "prev"))
    if meta.get("has_more"):
        links.append(link(start + size, "next"))
    links.append(link(last, "last"))
    response.headers["Link"] = ", ".join(links)
    response.headers["X-Total-Count"] = str(total)
