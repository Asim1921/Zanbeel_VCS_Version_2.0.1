"""Code search index: the text on every repository's default branch.

Searching file contents used to mean reading every blob of one repository off disk for
each query, so code search was limited to one repository at a time and still read up to
two gigabytes per request. GitHub indexes the default branch of every repository and
searches that; this does the same with SQLite FTS5's trigram tokenizer, which answers
substring queries ("find this string anywhere") from the index.

* **Its own file** (``FOXNEST_SEARCH_INDEX_PATH``, default ``<server>/search_index.db``).
  It holds nothing that is not derived from the object store, so it can be deleted and
  rebuilt at any time, is never backed up, and does not grow the main database. That
  holds with PostgreSQL as the main database too.
* **Kept current by branch moves.** ``ref_events.emit`` runs after every branch change;
  when the default branch moved, the repository is re-indexed on a single background
  thread. Only what changed is read: unchanged files keep their entry, and a blob shared
  by many repositories (forks) is stored once.
* **Bounded.** Text files up to ``MAX_FILE_BYTES`` are indexed; binary and larger files
  are recorded by path only, so the file finder still sees them.

Off until ``start()`` is called, which the application's startup does. Tests and tools
that import the server do not touch an index unless they configure one.
"""

import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
MAX_FILE_BYTES = 512 * 1024
MAX_FILES_PER_REPOSITORY = 50_000
#: Candidate files read back for line matching per query.
MAX_CANDIDATES = 2_000

_SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class _State:
    def __init__(self):
        self.enabled = False
        self.path: Optional[str] = None
        self.session_factory: Optional[Callable] = None
        self.background = True
        self.executor: Optional[ThreadPoolExecutor] = None
        self.pending: set = set()
        self.running: Optional[str] = None
        self.lock = threading.Lock()
        self.local = threading.local()
        self.last_error: Optional[str] = None


_state = _State()


# --- configuration -----------------------------------------------------------------

def default_path() -> str:
    return os.getenv("FOXNEST_SEARCH_INDEX_PATH") or os.path.join(_SERVER_ROOT, "search_index.db")


def configure(path: Optional[str] = None, session_factory: Optional[Callable] = None,
              background: bool = True) -> None:
    """Turn the index on. ``background=False`` indexes in the calling thread (tests)."""
    with _state.lock:
        _state.path = path or default_path()
        _state.session_factory = session_factory
        _state.background = background
        _state.enabled = True
        _state.pending.clear()
        _state.local = threading.local()
        if background and _state.executor is None:
            _state.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="search-index")
    _connection()  # create the schema now, so a bad path fails loudly at startup


def disable() -> None:
    with _state.lock:
        _state.enabled = False
        _state.pending.clear()
        conn = getattr(_state.local, "conn", None)
        if conn is not None:
            conn.close()
        _state.local = threading.local()


def enabled() -> bool:
    return _state.enabled


def stop() -> None:
    """Stop indexing at shutdown: drop the queue, let the repository in progress finish.

    A thread pool otherwise runs everything queued before the process may exit, so a
    server stopped during its first full build would wait for every repository.
    """
    with _state.lock:
        executor, _state.executor = _state.executor, None
        _state.pending.clear()
        _state.enabled = False
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def start() -> None:
    """Called at application startup: configure from the environment and catch up."""
    if (os.getenv("FOXNEST_SEARCH_INDEX", "1").strip().lower() in ("0", "false", "off", "no")):
        logger.info("Code search index disabled by FOXNEST_SEARCH_INDEX")
        return
    try:
        configure()
        schedule_stale()
    except Exception as exc:
        _state.enabled = False
        logger.exception("Code search index unavailable: %s", exc)
        print(f"[warn] Code search index unavailable: {exc}")


# --- storage ------------------------------------------------------------------------

def _connection() -> sqlite3.Connection:
    conn = getattr(_state.local, "conn", None)
    if conn is not None:
        return conn
    os.makedirs(os.path.dirname(os.path.abspath(_state.path)) or ".", exist_ok=True)
    conn = sqlite3.connect(_state.path, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS repos (
            repository_id TEXT PRIMARY KEY, branch TEXT, commit_id TEXT,
            files INTEGER, indexed_files INTEGER, indexed_bytes INTEGER,
            indexed_at TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS blobs (id INTEGER PRIMARY KEY, hash TEXT UNIQUE NOT NULL, size INTEGER);
        CREATE TABLE IF NOT EXISTS files (
            repository_id TEXT NOT NULL, path TEXT NOT NULL, blob_hash TEXT,
            indexed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (repository_id, path));
        CREATE INDEX IF NOT EXISTS ix_files_blob ON files(blob_hash);
        CREATE VIRTUAL TABLE IF NOT EXISTS content USING fts5(text, tokenize='trigram');
    """)
    version = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    if version is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema', ?)", (SCHEMA_VERSION,))
        conn.commit()
    _state.local.conn = conn
    return conn


def _session():
    factory = _state.session_factory
    if factory is None:
        from database.database import SessionLocal
        factory = SessionLocal
    return factory()


# --- scheduling ---------------------------------------------------------------------

def schedule(repository_id: str) -> None:
    """Queue a repository for (re)indexing. Repeated requests while queued coalesce."""
    if not _state.enabled or not repository_id:
        return
    if not _state.background:
        _run(repository_id)
        return
    with _state.lock:
        if repository_id in _state.pending:
            return
        _state.pending.add(repository_id)
        executor = _state.executor
    try:
        executor.submit(_run, repository_id)
    except (RuntimeError, AttributeError):
        # Shutting down (stop() got there first): the next start catches up.
        with _state.lock:
            _state.pending.discard(repository_id)


def branch_moved(db, repository, branch: str) -> None:
    """Called after any branch change; re-index only when the default branch moved."""
    if not _state.enabled:
        return
    from database.models import Branch
    default = db.query(Branch.name).filter(
        Branch.repository_id == repository.id, Branch.is_default == True,  # noqa: E712
    ).first()
    if default is None or default[0] == branch:
        schedule(repository.id)


def schedule_stale() -> int:
    """Queue every repository whose index is missing or behind its default branch."""
    if not _state.enabled:
        return 0
    from database.models import Branch, Repository
    db = _session()
    try:
        heads = {
            row.repository_id: (row.name, row.head_commit_id)
            for row in db.query(Branch).filter(Branch.is_default == True).all()  # noqa: E712
        }
        repo_ids = [row[0] for row in db.query(Repository.id).all()]
    finally:
        db.close()
    indexed = {row[0]: (row[1], row[2]) for row in
               _connection().execute("SELECT repository_id, branch, commit_id FROM repos")}
    queued = 0
    for repo_id in repo_ids:
        if indexed.get(repo_id) != heads.get(repo_id, (None, None)):
            schedule(repo_id)
            queued += 1
    for gone in set(indexed) - set(repo_ids):
        drop_repository(gone)
    return queued


def rebuild() -> int:
    """Forget everything and index every repository again."""
    if not _state.enabled:
        return 0
    conn = _connection()
    with conn:
        conn.execute("DELETE FROM files")
        conn.execute("DELETE FROM blobs")
        conn.execute("DELETE FROM content")
        conn.execute("DELETE FROM repos")
    return schedule_stale()


def wait_idle(timeout: float = 60.0) -> bool:
    """Block until nothing is queued or running (tests and tools)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _state.lock:
            busy = bool(_state.pending) or _state.running is not None
        if not busy:
            return True
        time.sleep(0.05)
    return False


# --- indexing -----------------------------------------------------------------------

def _run(repository_id: str) -> None:
    with _state.lock:
        _state.pending.discard(repository_id)
        _state.running = repository_id
    db = _session()
    try:
        index_repository(db, repository_id)
        _state.last_error = None
    except Exception as exc:
        _state.last_error = f"{repository_id}: {exc}"
        logger.exception("Indexing %s failed", repository_id)
        try:
            with _connection() as conn:
                conn.execute("UPDATE repos SET error = ? WHERE repository_id = ?", (str(exc)[:500], repository_id))
        except Exception:
            pass
    finally:
        db.close()
        with _state.lock:
            _state.running = None


def _decode(blob: bytes) -> Optional[str]:
    if not blob or b"\x00" in blob[:8192]:
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return blob.decode("latin-1")
        except Exception:
            return None


def index_repository(db, repository_id: str) -> Dict[str, Any]:
    """Bring one repository's entries in line with the head of its default branch."""
    from database.models import Branch, CommitFile, FileObject, Repository

    conn = _connection()
    repository = db.query(Repository).filter(Repository.id == repository_id).first()
    if repository is None:
        drop_repository(repository_id)
        return {"status": "dropped"}

    branch = db.query(Branch).filter(
        Branch.repository_id == repository_id, Branch.is_default == True,  # noqa: E712
    ).first()
    head = branch.head_commit_id if branch else None
    current = conn.execute("SELECT branch, commit_id FROM repos WHERE repository_id = ?",
                           (repository_id,)).fetchone()
    if current and current[0] == (branch.name if branch else None) and current[1] == head:
        return {"status": "current"}

    wanted: Dict[str, tuple] = {}
    if head:
        rows = (db.query(CommitFile.file_path, CommitFile.file_hash, FileObject.size)
                .outerjoin(FileObject, FileObject.hash == CommitFile.file_hash)
                .filter(CommitFile.commit_id == head)
                .limit(MAX_FILES_PER_REPOSITORY).all())
        wanted = {path: (file_hash, size or 0) for path, file_hash, size in rows if path}

    existing = {path: (blob_hash, indexed) for path, blob_hash, indexed in conn.execute(
        "SELECT path, blob_hash, indexed FROM files WHERE repository_id = ?", (repository_id,))}
    known_blobs = set()

    indexed_bytes = 0
    with conn:
        for path in set(existing) - set(wanted):
            conn.execute("DELETE FROM files WHERE repository_id = ? AND path = ?", (repository_id, path))
        for path, (file_hash, size) in wanted.items():
            previous = existing.get(path)
            if previous and previous[0] == file_hash:
                continue
            indexed = 0
            if file_hash and size <= MAX_FILE_BYTES:
                if file_hash in known_blobs or conn.execute(
                        "SELECT 1 FROM blobs WHERE hash = ?", (file_hash,)).fetchone():
                    indexed = 1
                else:
                    obj = db.query(FileObject).filter(FileObject.hash == file_hash).first()
                    text = None
                    try:
                        text = _decode(obj.content) if obj is not None else None
                    except Exception:
                        text = None
                    if text is not None:
                        cursor = conn.execute("INSERT INTO blobs (hash, size) VALUES (?, ?)",
                                              (file_hash, size))
                        conn.execute("INSERT INTO content (rowid, text) VALUES (?, ?)",
                                     (cursor.lastrowid, text))
                        indexed_bytes += size
                        indexed = 1
                if indexed:
                    known_blobs.add(file_hash)
            conn.execute(
                "INSERT OR REPLACE INTO files (repository_id, path, blob_hash, indexed) VALUES (?, ?, ?, ?)",
                (repository_id, path, file_hash, indexed),
            )
        _collect_garbage(conn)
        counts = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(indexed), 0) FROM files WHERE repository_id = ?",
            (repository_id,)).fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO repos (repository_id, branch, commit_id, files, indexed_files,"
            " indexed_bytes, indexed_at, error) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (repository_id, branch.name if branch else None, head, counts[0], counts[1],
             indexed_bytes, datetime.utcnow().isoformat()),
        )
    return {"status": "indexed", "files": counts[0], "indexed_files": counts[1]}


def _collect_garbage(conn: sqlite3.Connection) -> None:
    orphans = [row[0] for row in conn.execute(
        "SELECT b.id FROM blobs b WHERE NOT EXISTS (SELECT 1 FROM files f WHERE f.blob_hash = b.hash)")]
    for blob_id in orphans:
        conn.execute("DELETE FROM content WHERE rowid = ?", (blob_id,))
        conn.execute("DELETE FROM blobs WHERE id = ?", (blob_id,))


def drop_repository(repository_id: str) -> None:
    if not _state.enabled:
        return
    conn = _connection()
    with conn:
        conn.execute("DELETE FROM files WHERE repository_id = ?", (repository_id,))
        conn.execute("DELETE FROM repos WHERE repository_id = ?", (repository_id,))
        _collect_garbage(conn)


# --- reading ------------------------------------------------------------------------

def indexed_heads(repository_ids: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """What is indexed for each repository: branch, commit and counts."""
    if not _state.enabled:
        return {}
    rows = _connection().execute(
        "SELECT repository_id, branch, commit_id, files, indexed_files, indexed_at, error FROM repos").fetchall()
    wanted = set(repository_ids) if repository_ids is not None else None
    return {
        row[0]: {"branch": row[1], "commit_id": row[2], "files": row[3], "indexed_files": row[4],
                 "indexed_at": row[5], "error": row[6]}
        for row in rows if wanted is None or row[0] in wanted
    }


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def candidates(query: str, repository_ids: Optional[List[str]], path_filter: Optional[str] = None,
               limit: int = MAX_CANDIDATES) -> List[tuple]:
    """(repository_id, path, text) for indexed files containing ``query`` (case-insensitive).

    The query must be at least three characters: a trigram index cannot narrow anything
    shorter.
    """
    if not _state.enabled or len(query) < 3:
        return []
    conn = _connection()
    phrase = '"' + query.replace('"', '""') + '"'
    sql = ("SELECT f.repository_id, f.path, c.text FROM content c "
           "JOIN blobs b ON b.id = c.rowid "
           "JOIN files f ON f.blob_hash = b.hash AND f.indexed = 1 "
           "WHERE content MATCH ?")
    params: List[Any] = [phrase]
    if repository_ids is not None:
        if not repository_ids:
            return []
        sql += " AND f.repository_id IN (%s)" % ",".join("?" * len(repository_ids))
        params.extend(repository_ids)
    if path_filter:
        sql += " AND f.path LIKE ? ESCAPE '\\'"
        params.append(_like(path_filter))
    sql += " ORDER BY f.repository_id, f.path LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def paths(query: str, repository_ids: Optional[List[str]], limit: int = 200) -> List[tuple]:
    """(repository_id, path) whose path contains every space-separated term of ``query``."""
    if not _state.enabled:
        return []
    terms = [t for t in (query or "").split() if t]
    if not terms:
        return []
    sql = "SELECT repository_id, path FROM files WHERE 1 = 1"
    params: List[Any] = []
    for term in terms:
        sql += " AND path LIKE ? ESCAPE '\\'"
        params.append(_like(term))
    if repository_ids is not None:
        if not repository_ids:
            return []
        sql += " AND repository_id IN (%s)" % ",".join("?" * len(repository_ids))
        params.extend(repository_ids)
    sql += " LIMIT ?"
    params.append(limit * 5)
    return _connection().execute(sql, params).fetchall()


def status() -> Dict[str, Any]:
    if not _state.enabled:
        return {"enabled": False}
    conn = _connection()
    repos, files, indexed = conn.execute(
        "SELECT (SELECT COUNT(*) FROM repos), (SELECT COUNT(*) FROM files), "
        "(SELECT COUNT(*) FROM files WHERE indexed = 1)").fetchone()
    blobs, blob_bytes = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM blobs").fetchone()
    with _state.lock:
        pending = sorted(_state.pending)
        running = _state.running
    return {
        "enabled": True,
        "path": _state.path,
        "size_bytes": os.path.getsize(_state.path) if os.path.exists(_state.path) else 0,
        "repositories": repos,
        "files": files,
        "indexed_files": indexed,
        "unique_blobs": blobs,
        "indexed_text_bytes": blob_bytes,
        "queued": pending,
        "running": running,
        "last_error": _state.last_error,
    }
