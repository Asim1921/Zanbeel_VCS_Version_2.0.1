"""Search across repositories, commits and file contents.

The gap this closes: only issues were searchable. Finding a string across a
repository's files, or locating a commit by its message, was impossible from
either the web client or the CLI — across 60 repositories and 37,466 tracked
files.

**Why code search is scoped to one repository.** Scanning every blob in the
store would mean reading gigabytes off disk per query, and a request that takes
a minute is not search. Code search therefore runs against one repository at one
branch tip, with hard caps on files scanned, bytes read per file and matches
returned. Repository and commit search are metadata queries and stay global.

Every result is filtered through the same read-permission check the rest of the
API uses, so search can never become a way to discover the existence or contents
of a repository the caller cannot already open.
"""

import re
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from database.models import Branch, Commit, CommitFile, FileObject, Repository, User

# Guards. Code search reads real bytes off disk, so every dimension is bounded:
# a pathological query must degrade to "fewer results", never to a hung request.
MAX_FILES_SCANNED = 4000
MAX_FILE_BYTES = 512 * 1024
MAX_MATCHES = 200
MAX_LINE_CHARS = 400
CONTEXT_CHARS = 120


class SearchError(Exception):
    """A caller mistake; carries the HTTP status to answer with."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _compile(query: str, regex: bool, case_sensitive: bool) -> re.Pattern:
    """Build the matcher, treating a plain query as a literal.

    A user typing ``a.b`` means those three characters, not "a, any char, b" —
    so non-regex queries are escaped. An invalid regex is reported as a caller
    error rather than a 500.
    """
    if not query or not query.strip():
        raise SearchError("A search query is required")

    pattern = query if regex else re.escape(query)
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise SearchError(f"Invalid regular expression: {exc}")


def _visible_repositories(db: Session, actor: User) -> List[Repository]:
    """Every repository this user may read.

    Filtering here rather than in SQL keeps one definition of "can read" — the
    same helper the file and commit endpoints use — instead of a second, subtly
    different rule that search alone would apply.
    """
    from app.core.permissions import _user_can_read_repository_contents

    repositories = db.query(Repository).all()
    return [r for r in repositories if _user_can_read_repository_contents(db, actor, r)]


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------

def search_repositories(
    db: Session,
    actor: User,
    query: Optional[str] = None,
    owner: Optional[str] = None,
    archived: Optional[bool] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Filter repositories by name, description, owner and archived state.

    ``query`` is optional: with only ``owner`` or ``archived`` set this becomes
    a filter rather than a search, which is what "show me everything Asim owns"
    needs.
    """
    limit = max(1, min(limit, 200))
    visible = _visible_repositories(db, actor)

    needle = (query or "").strip().lower()
    results = []
    for repo in visible:
        if archived is not None and bool(repo.is_archived) != archived:
            continue
        owner_name = repo.owner.username if repo.owner else None
        if owner and (owner_name or "").lower() != owner.lower():
            continue
        if needle:
            haystack = " ".join(
                filter(None, [repo.name, repo.description, owner_name])
            ).lower()
            if needle not in haystack:
                continue
        results.append(repo)

    # Exact name matches first, then prefix, then the rest — otherwise a search
    # for "web" buries the repository actually called "web" under every
    # description that mentions the word.
    def rank(repo: Repository) -> tuple:
        name = (repo.name or "").lower()
        if needle and name == needle:
            return (0, name)
        if needle and name.startswith(needle):
            return (1, name)
        if needle and needle in name:
            return (2, name)
        return (3, name)

    results.sort(key=rank)
    total = len(results)
    return {
        "total": total,
        "truncated": total > limit,
        "repositories": [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "owner": r.owner.username if r.owner else None,
                "is_archived": bool(r.is_archived),
                "size_bytes": r.size_bytes,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in results[:limit]
        ],
    }


# ---------------------------------------------------------------------------
# Commits
# ---------------------------------------------------------------------------

def search_commits(
    db: Session,
    actor: User,
    query: str,
    repository_id: Optional[str] = None,
    author: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Find commits whose message matches, optionally scoped to one repository."""
    limit = max(1, min(limit, 200))
    if not (query or "").strip() and not author:
        raise SearchError("Provide a query or an author to search commits")

    visible_ids = {r.id for r in _visible_repositories(db, actor)}
    if repository_id:
        if repository_id not in visible_ids:
            raise SearchError("Repository not found", status_code=404)
        visible_ids = {repository_id}
    if not visible_ids:
        return {"total": 0, "truncated": False, "commits": []}

    q = db.query(Commit).filter(Commit.repository_id.in_(visible_ids))
    needle = (query or "").strip()
    if needle:
        # SQL LIKE keeps this an indexed-ish scan in the database rather than
        # pulling every commit row into Python.
        q = q.filter(Commit.message.ilike(f"%{needle}%"))
    if author:
        q = q.join(User, Commit.author_id == User.id).filter(
            User.username.ilike(author)
        )

    rows = q.order_by(Commit.created_at.desc()).limit(limit + 1).all()
    truncated = len(rows) > limit
    rows = rows[:limit]

    return {
        "total": len(rows),
        "truncated": truncated,
        "commits": [
            {
                "id": c.id,
                "message": c.message,
                "author": c.author.username if c.author else None,
                "repository_id": c.repository_id,
                "repository_name": c.repository.name if c.repository else None,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ],
    }


# ---------------------------------------------------------------------------
# Code
# ---------------------------------------------------------------------------

def _looks_binary(blob: bytes) -> bool:
    """A NUL byte in the first kilobyte — the same heuristic diff tools use."""
    return b"\x00" in blob[:1024]


def _match_lines(text: str, matcher: re.Pattern, budget: int) -> List[Dict[str, Any]]:
    """Every matching line in one file, up to ``budget`` matches."""
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        if len(hits) >= budget:
            break
        found = matcher.search(line)
        if not found:
            continue
        # Long minified lines would otherwise dominate the response; keep a
        # window around the match instead of the whole line.
        start, end = found.span()
        if len(line) > MAX_LINE_CHARS:
            left = max(0, start - CONTEXT_CHARS)
            right = min(len(line), end + CONTEXT_CHARS)
            snippet = ("…" if left else "") + line[left:right] + ("…" if right < len(line) else "")
            start, end = start - left + (1 if left else 0), end - left + (1 if left else 0)
        else:
            snippet = line
        hits.append({"line": number, "text": snippet, "start": start, "end": end})
    return hits


def search_code(
    db: Session,
    actor: User,
    repository_id: str,
    query: str,
    branch: Optional[str] = None,
    path_filter: Optional[str] = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = MAX_MATCHES,
) -> Dict[str, Any]:
    """Search file contents at one branch tip.

    Reads the head commit's file list and scans each blob. Binary files and
    anything over ``MAX_FILE_BYTES`` are skipped rather than searched: a regex
    over a 40 MB archive costs seconds and can only produce noise.
    """
    from app.core.permissions import _user_can_read_repository_contents
    from app.services.branches import _get_commit_for_branch

    limit = max(1, min(limit, MAX_MATCHES))
    matcher = _compile(query, regex, case_sensitive)

    repository = db.query(Repository).filter(Repository.id == repository_id).first()
    if not repository or not _user_can_read_repository_contents(db, actor, repository):
        # 404 rather than 403: a caller who cannot read the repository should
        # not learn that it exists.
        raise SearchError("Repository not found", status_code=404)

    commit = _get_commit_for_branch(db, repository, branch)
    if not commit:
        return {
            "repository_id": repository_id,
            "branch": branch,
            "commit_id": None,
            "files_scanned": 0,
            "files_skipped": 0,
            "truncated": False,
            "total_matches": 0,
            "results": [],
        }

    rows = (
        db.query(CommitFile, FileObject)
        .join(FileObject, CommitFile.file_hash == FileObject.hash)
        .filter(CommitFile.commit_id == commit.id)
        .all()
    )

    path_needle = (path_filter or "").strip().lower()
    results: List[Dict[str, Any]] = []
    scanned = skipped = total_matches = 0
    truncated = False

    for commit_file, file_object in rows:
        if scanned >= MAX_FILES_SCANNED:
            truncated = True
            break
        if total_matches >= limit:
            truncated = True
            break

        path = commit_file.file_path or ""
        if path_needle and path_needle not in path.lower():
            continue

        size = file_object.size or 0
        if size > MAX_FILE_BYTES:
            skipped += 1
            continue

        try:
            blob = file_object.content
        except Exception:
            skipped += 1
            continue
        if not blob or _looks_binary(blob):
            skipped += 1
            continue

        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = blob.decode("latin-1")
            except Exception:
                skipped += 1
                continue

        scanned += 1
        hits = _match_lines(text, matcher, limit - total_matches)
        if not hits:
            continue

        total_matches += len(hits)
        results.append({"path": path, "matches": hits, "match_count": len(hits)})

    results.sort(key=lambda r: (-r["match_count"], r["path"]))

    return {
        "repository_id": repository_id,
        "repository_name": repository.name,
        "branch": branch or (commit and getattr(commit, "branch", None)),
        "commit_id": commit.id,
        "files_scanned": scanned,
        "files_skipped": skipped,
        "truncated": truncated or total_matches >= limit,
        "total_matches": total_matches,
        "results": results,
    }
