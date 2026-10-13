"""Search across repositories, commits, file contents and file paths.

The gap this closes: only issues were searchable. Finding a string across a
repository's files, or locating a commit by its message, was impossible from either
the web client or the CLI -- across 60 repositories and 37,466 tracked files.

**Code search** answers from the search index (``search_index``): the text on every
repository's default branch, so a literal search covers every repository the caller can
read at once. What the index cannot answer -- a regular expression, another branch, a
query under three characters, a repository not indexed yet -- falls back to scanning
one repository's files at a branch tip. The scan is bounded in files, bytes and time,
and patterns run under the ``regex`` module with a timeout: a search box that accepts
regular expressions from every reader must not be a way to stall the server.

Every result is filtered through the same read rule the rest of the API uses, so search
can never reveal the existence or contents of a repository the caller cannot open.
"""

import os
import re
import time
from typing import Any, Dict, List, Optional

import regex as safe_regex
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from database.models import Branch, Commit, CommitFile, FileObject, Repository, User
from app.services.paths import normalize as normalize_path
from app.services import search_index

# Guards. The scan reads real bytes off disk, so every dimension is bounded: a
# pathological query must degrade to "fewer results", never to a hung request.
MAX_FILES_SCANNED = 4000
MAX_FILE_BYTES = 512 * 1024
MAX_BYTES_SCANNED = 64 * 1024 * 1024
MAX_MATCHES = 200
MAX_MATCHES_PER_FILE_GLOBAL = 20
MAX_LINE_CHARS = 400
CONTEXT_CHARS = 120
PATTERN_TIMEOUT = 0.25          # seconds for one line
_HEX = re.compile(r"[0-9a-f]{7,40}")


def _scan_seconds() -> float:
    try:
        return max(0.5, float(os.getenv("FOXNEST_SEARCH_SCAN_SECONDS", "3")))
    except ValueError:
        return 3.0


class SearchError(Exception):
    """A caller mistake; carries the HTTP status to answer with."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class _Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds
        self.expired = False

    def remaining(self) -> float:
        left = self.end - time.monotonic()
        if left <= 0:
            self.expired = True
        return left


def _compile(query: str, regex: bool, case_sensitive: bool):
    """Build the matcher, treating a plain query as a literal.

    A user typing ``a.b`` means those three characters, not "a, any char, b" -- so
    non-regex queries are escaped. An invalid regex is reported as a caller error.
    """
    if not query or not query.strip():
        raise SearchError("A search query is required")
    pattern = query if regex else safe_regex.escape(query)
    flags = safe_regex.V0 | (0 if case_sensitive else safe_regex.IGNORECASE)
    try:
        return safe_regex.compile(pattern, flags)
    except safe_regex.error as exc:
        raise SearchError(f"Invalid regular expression: {exc}")


def _like(value: str) -> str:
    """A LIKE pattern that matches ``value`` literally (with ESCAPE '\\')."""
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _readable(db: Session, actor: User) -> Optional[set]:
    from app.core.permissions import readable_repository_ids
    return readable_repository_ids(db, actor)


def _readable_repositories(db: Session, actor: User) -> List[Repository]:
    readable = _readable(db, actor)
    q = db.query(Repository).options(joinedload(Repository.owner))
    if readable is not None:
        if not readable:
            return []
        q = q.filter(Repository.id.in_(readable))
    return q.all()


def _require_readable(db: Session, actor: User, repository_id: str) -> Repository:
    from app.core.permissions import _user_can_read_repository_contents
    repository = db.query(Repository).filter(Repository.id == repository_id).first()
    if not repository or not _user_can_read_repository_contents(db, actor, repository):
        # 404 rather than 403: a caller who cannot read the repository should not
        # learn that it exists.
        raise SearchError("Repository not found", status_code=404)
    return repository


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
    offset: int = 0,
) -> Dict[str, Any]:
    """Filter repositories by name, description, owner and archived state.

    ``query`` is optional: with only ``owner`` or ``archived`` set this becomes a
    filter rather than a search, which is what "show me everything Asim owns" needs.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    needle = (query or "").strip().lower()
    results = []
    for repo in _readable_repositories(db, actor):
        if archived is not None and bool(repo.is_archived) != archived:
            continue
        owner_name = repo.owner.username if repo.owner else None
        if owner and (owner_name or "").lower() != owner.lower():
            continue
        if needle:
            haystack = " ".join(filter(None, [repo.name, repo.description, owner_name])).lower()
            if needle not in haystack:
                continue
        results.append(repo)

    # Exact name matches first, then prefix, then the rest -- otherwise a search for
    # "web" buries the repository actually called "web" under every description that
    # mentions the word.
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
    page = results[offset:offset + limit]
    return {
        "total": total,
        "offset": offset,
        "truncated": offset + len(page) < total,
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
            for r in page
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
    offset: int = 0,
) -> Dict[str, Any]:
    """Find commits whose message matches, or whose id starts with the query.

    The query is matched literally: ``100%`` and ``fix_typo`` used to be read as SQL
    wildcards. Seven or more hex characters also find a commit by its id, as Git does.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    needle = (query or "").strip()
    if not needle and not author:
        raise SearchError("Provide a query or an author to search commits")

    q = db.query(Commit).options(joinedload(Commit.author), joinedload(Commit.repository))
    if repository_id:
        _require_readable(db, actor, repository_id)
        q = q.filter(Commit.repository_id == repository_id)
    else:
        readable = _readable(db, actor)
        if readable is not None:
            if not readable:
                return {"total": 0, "offset": offset, "truncated": False, "commits": []}
            q = q.filter(Commit.repository_id.in_(readable))

    if needle:
        condition = Commit.message.ilike(_like(needle), escape="\\")
        if _HEX.fullmatch(needle.lower()):
            condition = or_(condition, Commit.id.like(needle.lower() + "%"))
        q = q.filter(condition)
    if author:
        q = q.join(User, Commit.author_id == User.id).filter(User.username.ilike(author.strip()))

    total = q.count()
    rows = q.order_by(Commit.created_at.desc(), Commit.id).offset(offset).limit(limit).all()
    return {
        "total": total,
        "offset": offset,
        "truncated": offset + len(rows) < total,
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
    """A NUL byte in the first kilobyte -- the same heuristic diff tools use."""
    return b"\x00" in blob[:1024]


class _Timeout(Exception):
    pass


def _match_lines(text: str, matcher, budget: int, deadline: Optional[_Deadline] = None) -> List[Dict[str, Any]]:
    """Every matching line in one file, up to ``budget`` matches."""
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        if len(hits) >= budget:
            break
        timeout = PATTERN_TIMEOUT
        if deadline is not None:
            left = deadline.remaining()
            if left <= 0:
                raise _Timeout()
            timeout = min(timeout, left)
        try:
            found = matcher.search(line, timeout=timeout)
        except TimeoutError:
            raise _Timeout()
        if not found:
            continue
        # Long minified lines would otherwise dominate the response; keep a window
        # around the match instead of the whole line.
        start, end = found.span()
        if len(line) > MAX_LINE_CHARS:
            left_edge = max(0, start - CONTEXT_CHARS)
            right_edge = min(len(line), end + CONTEXT_CHARS)
            snippet = ("…" if left_edge else "") + line[left_edge:right_edge] + ("…" if right_edge < len(line) else "")
            shift = left_edge - (1 if left_edge else 0)
            start, end = start - shift, end - shift
        else:
            snippet = line
        hits.append({"line": number, "text": snippet, "start": start, "end": end})
    return hits


def _decode(blob: bytes) -> Optional[str]:
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return blob.decode("latin-1")
        except Exception:
            return None


def _index_is_current(db: Session, repository: Repository, branch: Optional[str], commit: Commit) -> bool:
    """Whether the index holds exactly this commit of this repository's default branch."""
    if not search_index.enabled():
        return False
    entry = search_index.indexed_heads([repository.id]).get(repository.id)
    if not entry or entry.get("commit_id") != commit.id:
        return False
    return branch is None or branch == entry.get("branch")


def _from_index(
    rows, matcher, limit: int, per_file: int, names: Dict[str, str], deadline: _Deadline,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    total = scanned = 0
    truncated = timed_out = False
    for repository_id, path, text in rows:
        if total >= limit:
            truncated = True
            break
        scanned += 1
        try:
            hits = _match_lines(text, matcher, min(per_file, limit - total), deadline)
        except _Timeout:
            truncated = timed_out = True
            break
        if hits:
            total += len(hits)
            results.append({"repository_id": repository_id, "repository_name": names.get(repository_id),
                            "path": path, "matches": hits, "match_count": len(hits)})
    return {"results": results, "total_matches": total, "files_scanned": scanned,
            "truncated": truncated, "timed_out": timed_out}


def search_code(
    db: Session,
    actor: User,
    repository_id: Optional[str],
    query: str,
    branch: Optional[str] = None,
    path_filter: Optional[str] = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = MAX_MATCHES,
) -> Dict[str, Any]:
    """Search file contents: every readable repository, or one at a branch tip."""
    limit = max(1, min(limit, MAX_MATCHES))
    matcher = _compile(query, regex, case_sensitive)
    path_needle = normalize_path(path_filter or "").strip()
    deadline = _Deadline(_scan_seconds())

    if not repository_id:
        return _search_everywhere(db, actor, query, matcher, regex, path_needle, limit, deadline)

    from app.services.branches import _get_commit_for_branch

    repository = _require_readable(db, actor, repository_id)
    commit = _get_commit_for_branch(db, repository, branch)
    base = {
        "repository_id": repository_id,
        "repository_name": repository.name,
        "branch": branch,
        "commit_id": commit.id if commit else None,
    }
    if not commit:
        return {**base, "source": "scan", "files_scanned": 0, "files_skipped": 0, "truncated": False,
                "timed_out": False, "total_matches": 0, "results": []}

    if not regex and len(query) >= 3 and _index_is_current(db, repository, branch, commit):
        rows = search_index.candidates(query, [repository_id], path_needle or None)
        found = _from_index(rows, matcher, limit, limit, {repository_id: repository.name}, deadline)
        entry = search_index.indexed_heads([repository_id]).get(repository_id) or {}
        skipped = max(0, (entry.get("files") or 0) - (entry.get("indexed_files") or 0))
        found["results"].sort(key=lambda r: (-r["match_count"], r["path"]))
        return {**base, "branch": branch or entry.get("branch"), "source": "index",
                "files_skipped": skipped, **found,
                "truncated": found["truncated"] or found["total_matches"] >= limit}

    return {**base, **_scan(db, commit, matcher, path_needle, limit, deadline)}


def _scan(db: Session, commit: Commit, matcher, path_needle: str, limit: int, deadline: _Deadline) -> Dict[str, Any]:
    """Read one commit's files and match line by line, within the guards."""
    rows = (
        db.query(CommitFile, FileObject)
        .join(FileObject, CommitFile.file_hash == FileObject.hash)
        .filter(CommitFile.commit_id == commit.id)
        .all()
    )
    needle = path_needle.lower()
    results: List[Dict[str, Any]] = []
    scanned = skipped = total_matches = bytes_read = 0
    truncated = timed_out = False

    for commit_file, file_object in rows:
        if scanned >= MAX_FILES_SCANNED or total_matches >= limit or bytes_read >= MAX_BYTES_SCANNED:
            truncated = True
            break
        if deadline.remaining() <= 0:
            truncated = timed_out = True
            break

        path = normalize_path(commit_file.file_path or "")
        if needle and needle not in path.lower():
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
        text = _decode(blob)
        if text is None:
            skipped += 1
            continue

        scanned += 1
        bytes_read += len(blob)
        try:
            hits = _match_lines(text, matcher, limit - total_matches, deadline)
        except _Timeout:
            truncated = timed_out = True
            break
        if hits:
            total_matches += len(hits)
            results.append({"path": path, "matches": hits, "match_count": len(hits)})

    results.sort(key=lambda r: (-r["match_count"], r["path"]))
    return {
        "source": "scan",
        "files_scanned": scanned,
        "files_skipped": skipped,
        "truncated": truncated or total_matches >= limit,
        "timed_out": timed_out,
        "total_matches": total_matches,
        "results": results,
    }


def _search_everywhere(db, actor, query, matcher, regex, path_needle, limit, deadline) -> Dict[str, Any]:
    """Every readable repository's default branch, from the index."""
    if regex:
        raise SearchError("A regular expression searches one repository at a time: choose a repository.")
    if len(query) < 3:
        raise SearchError("Searching every repository needs at least 3 characters.")
    if not search_index.enabled():
        raise SearchError("The search index is not available on this server: choose a repository.",
                          status_code=503)

    repositories = _readable_repositories(db, actor)
    names = {r.id: r.name for r in repositories}
    rows = search_index.candidates(query, list(names), path_needle or None)
    found = _from_index(rows, matcher, limit, MAX_MATCHES_PER_FILE_GLOBAL, names, deadline)
    found["results"].sort(key=lambda r: (-r["match_count"], r["repository_name"] or "", r["path"]))

    heads = {row.repository_id: row.head_commit_id for row in db.query(Branch).filter(
        Branch.is_default == True, Branch.repository_id.in_(list(names) or [""])).all()}  # noqa: E712
    indexed = search_index.indexed_heads(names)
    behind = sorted(names[r] for r in names
                    if heads.get(r) and (indexed.get(r) or {}).get("commit_id") != heads.get(r))
    return {
        "repository_id": None,
        "repository_name": None,
        "branch": None,
        "commit_id": None,
        "source": "index",
        "repositories_searched": len(names),
        "repositories_not_indexed": behind,
        "files_skipped": 0,
        **found,
        "truncated": found["truncated"] or found["total_matches"] >= limit,
    }


# ---------------------------------------------------------------------------
# File paths ("go to file")
# ---------------------------------------------------------------------------

def _rank_path(path: str, terms: List[str]) -> tuple:
    name = path.rsplit("/", 1)[-1].lower()
    last = terms[-1].lower()
    if name == last:
        tier = 0
    elif name.startswith(last):
        tier = 1
    elif last in name:
        tier = 2
    else:
        tier = 3
    return (tier, len(path), path)


def search_paths(
    db: Session,
    actor: User,
    query: str,
    repository_id: Optional[str] = None,
    branch: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Files whose path contains every space-separated word of ``query``.

    Within one repository it reads the branch's file list (no file contents); across
    repositories it uses the search index, which lists every file of each default branch.
    """
    limit = max(1, min(limit, 200))
    terms = [t.lower() for t in (query or "").split() if t]
    if not terms:
        raise SearchError("A search query is required")

    if repository_id:
        from app.services.branches import _get_commit_for_branch
        repository = _require_readable(db, actor, repository_id)
        commit = _get_commit_for_branch(db, repository, branch)
        if not commit:
            return {"total": 0, "truncated": False, "files": []}
        found = [
            (repository_id, path) for (path,) in
            db.query(CommitFile.file_path).filter(CommitFile.commit_id == commit.id).all()
            if path and all(term in path.lower() for term in terms)
        ]
        names = {repository_id: repository.name}
        source = "branch"
    else:
        if not search_index.enabled():
            raise SearchError("The search index is not available on this server: choose a repository.",
                              status_code=503)
        names = {r.id: r.name for r in _readable_repositories(db, actor)}
        found = [(r, p) for r, p in search_index.paths(" ".join(terms), list(names), limit)]
        source = "index"

    found.sort(key=lambda item: _rank_path(item[1], terms))
    total = len(found)
    return {
        "total": total,
        "truncated": total > limit,
        "source": source,
        "files": [
            {"repository_id": repo, "repository_name": names.get(repo), "path": path,
             "branch": branch if repository_id else None}
            for repo, path in found[:limit]
        ],
    }
