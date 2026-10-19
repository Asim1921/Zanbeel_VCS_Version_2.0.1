"""Apply the schema migrations: in order, once each, all or nothing, and on the record.

The audit's finding was that schema changes were ad-hoc statements that printed a
warning and continued on failure. Alembic was adopted in 2.0.1 but never run: the
server kept executing its startup patches, and every feature since added another one.
This module is what runs the migrations now, at startup and from ``tools/schema.py``:

* **Versioned.** Every change is a revision under ``alembic/versions/``; the database
  records the one it is at in ``alembic_version``. A revision runs once.
* **All or nothing.** Every pending revision runs inside one transaction. SQLite's
  DDL is transactional, but Python's sqlite3 driver does not open a transaction before
  DDL statements, so the migration engine issues ``BEGIN IMMEDIATE`` itself. A failure
  anywhere rolls the database back to exactly where it was.
* **One upgrader at a time.** ``BEGIN IMMEDIATE`` takes SQLite's write lock before the
  current revision is read, so a second server starting at the same moment waits and
  then finds nothing to do, instead of running the same revisions twice.
* **A copy first.** Before applying anything to a database that has data, a consistent
  copy is taken with ``VACUUM INTO`` beside the backups. The rollback already covers a
  revision that fails; the copy covers one that succeeds and is wrong.
* **Loud.** A failure raises ``MigrationError`` naming the revision, and the server
  refuses to start. A database at a revision this code does not know -- upgraded by a
  newer server -- is refused too, rather than served by code that predates it.
"""

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.pool import NullPool

from database.database import ACTIVE_DATABASE_URL as DATABASE_URL

SERVER_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = SERVER_ROOT / "alembic.ini"
ALEMBIC_DIR = SERVER_ROOT / "alembic"

#: Pre-upgrade copies kept. Each is a whole database, so they are rotated.
SNAPSHOTS_KEEP = max(1, int(os.getenv("FOXNEST_UPGRADE_SNAPSHOTS_KEEP") or "5"))

# An arbitrary constant: the advisory lock key every Zanbeel upgrader agrees on.
_PG_LOCK_KEY = 7_142_026_001


class MigrationError(RuntimeError):
    """The schema could not be brought to this server's revision."""


def _flag(name: str, default: str) -> bool:
    return (os.getenv(name, default) or "").strip().lower() not in {"0", "false", "no", "off"}


def auto_migrate_enabled() -> bool:
    """Whether startup applies pending revisions (FOXNEST_AUTO_MIGRATE, default on)."""
    return _flag("FOXNEST_AUTO_MIGRATE", "1")


def _is_sqlite(url: str) -> bool:
    return (url or "").strip().lower().startswith("sqlite")


def sqlite_path(url: str) -> Optional[Path]:
    """The file behind a sqlite:/// URL, or None for memory and non-SQLite URLs."""
    if not _is_sqlite(url):
        return None
    tail = url.split(":///", 1)[-1] if ":///" in url else ""
    if not tail or tail.startswith(":memory:"):
        return None
    return Path(tail.split("?", 1)[0]).resolve()


def alembic_config(url: Optional[str] = None):
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    if url:
        # ConfigParser interpolation: a literal % in a URL must be doubled.
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def migration_engine(url: str):
    """An engine whose transactions are real transactions, DDL included.

    Separate from the application engine on purpose: the driver behaviour changed here
    (no implicit transaction management, an immediate write lock, a long busy timeout
    so a second upgrader waits instead of failing) is right for a migration and wrong
    for request handling.
    """
    if not _is_sqlite(url):
        return create_engine(url, poolclass=NullPool)

    engine = create_engine(url, poolclass=NullPool, connect_args={"timeout": 600})

    @event.listens_for(engine, "connect")
    def _no_driver_transactions(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin_immediate(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def _script():
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config())


def _pending(script, current: Optional[str]) -> List[str]:
    """Revisions between ``current`` and head, oldest first."""
    head = script.get_current_head()
    if current == head:
        return []
    lower = current or "base"
    return [rev.revision for rev in reversed(list(script.iterate_revisions(head, lower)))]


def _user_tables(connection) -> List[str]:
    return [name for name in inspect(connection).get_table_names()
            if name != "alembic_version" and not name.startswith("sqlite_")]


def status(url: Optional[str] = None) -> Dict:
    """Where the database is, where this code is, and what lies between."""
    from alembic.runtime.migration import MigrationContext

    url = url or DATABASE_URL
    script = _script()
    known = {rev.revision for rev in script.walk_revisions()}
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            heads = list(MigrationContext.configure(connection).get_current_heads())
            tables = _user_tables(connection)
    finally:
        engine.dispose()

    current = heads[0] if heads else None
    unknown = [rev for rev in heads if rev not in known]
    return {
        "current": current,
        "head": script.get_current_head(),
        "pending": [] if unknown else _pending(script, current),
        "unknown": unknown,
        "multiple_heads": len(heads) > 1,
        "empty": not tables,
        "auto_migrate": auto_migrate_enabled(),
    }


# --- the copy taken before an upgrade -----------------------------------------------------

def snapshot_root() -> Path:
    from app.services.backup import backup_root

    return backup_root() / "pre-upgrade"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def take_snapshot(db_path: Path, from_revision: Optional[str], to_revision: str) -> Path:
    """A consistent copy of the database as it is before the upgrade.

    Runs on its own connection while the upgrader holds the write lock: SQLite still
    admits readers then, and the reader sees exactly the committed, pre-upgrade state.
    """
    root = snapshot_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    target = root / f"{stamp}-{from_revision or 'base'}-to-{to_revision}.sqlite3"
    reader = sqlite3.connect(str(db_path), timeout=60)
    try:
        reader.execute("VACUUM INTO ?", (str(target),))
    finally:
        reader.close()
    (target.with_suffix(".json")).write_text(json.dumps({
        "kind": "pre-upgrade",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "source_database": str(db_path),
        "from_revision": from_revision,
        "to_revision": to_revision,
        "database_bytes": target.stat().st_size,
        "database_sha256": _sha256(target),
        "restore": "Stop the server, then copy this file over the database file "
                   "(move the old one and any -wal/-shm/-journal files aside first).",
    }, indent=2), encoding="utf-8")
    _rotate_snapshots(root)
    return target


def _rotate_snapshots(root: Path) -> None:
    copies = sorted(root.glob("*.sqlite3"), key=lambda p: p.name, reverse=True)
    for stale in copies[SNAPSHOTS_KEEP:]:
        for path in (stale, stale.with_suffix(".json")):
            try:
                path.unlink()
            except OSError:
                pass


# --- the upgrade itself ---------------------------------------------------------------------

def upgrade_database(url: Optional[str] = None, snapshot: bool = True,
                     log: Callable[[str], None] = print) -> Dict:
    """Bring the database to head. Returns what was done; raises MigrationError on failure."""
    from alembic import command
    from alembic.runtime.migration import MigrationContext

    url = url or DATABASE_URL
    script = _script()
    head = script.get_current_head()
    known = {rev.revision for rev in script.walk_revisions()}
    started_at = datetime.utcnow()
    started = time.monotonic()
    applied: List[str] = []
    report = {"from": None, "to": head, "applied": applied, "snapshot": None}

    engine = migration_engine(url)
    try:
        with engine.connect() as connection:
            with connection.begin():
                if connection.dialect.name == "postgresql":
                    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                                       {"key": _PG_LOCK_KEY})

                heads = list(MigrationContext.configure(connection).get_current_heads())
                if len(heads) > 1:
                    raise MigrationError(f"The database records several revisions at once: {heads}.")
                current = heads[0] if heads else None
                report["from"] = current
                if current and current not in known:
                    raise MigrationError(
                        f"The database is at revision '{current}', which this server does not "
                        "have. It was upgraded by a newer version of Zanbeel; run that version, "
                        "or restore a backup taken before the upgrade."
                    )
                pending = _pending(script, current)
                if not pending:
                    return report

                db_path = sqlite_path(url)
                has_data = bool(_user_tables(connection))
                if snapshot and db_path and db_path.is_file() and has_data:
                    report["snapshot"] = str(take_snapshot(db_path, current, head))

                origin = current or ("no revision recorded" if has_data else "new database")
                log(f"Upgrading the database schema ({origin}) -> {head}: {', '.join(pending)}")
                if report["snapshot"]:
                    log(f"  copy taken first: {report['snapshot']}")

                config = alembic_config(url)
                config.attributes["connection"] = connection
                config.attributes["on_version_apply"] = (
                    lambda ctx, step, heads, run_args: applied.append(step.up_revision_id)
                )
                try:
                    command.upgrade(config, "head")
                except MigrationError:
                    raise
                except Exception as exc:
                    done = set(applied)
                    failed = next((rev for rev in pending if rev not in done), pending[-1])
                    raise MigrationError(
                        f"Revision {failed} failed and the upgrade was rolled back; the database "
                        f"is unchanged at {current or 'no revision'}. {type(exc).__name__}: {exc}"
                    ) from exc

                _record_run(connection, current, head, applied, report["snapshot"],
                            started_at, int((time.monotonic() - started) * 1000))
    finally:
        engine.dispose()

    log(f"  schema upgraded to {head} in {int((time.monotonic() - started) * 1000)} ms")
    return report


def _record_run(connection, from_revision, to_revision, applied, snapshot_path,
                started_at, duration_ms) -> None:
    from database.models import SchemaMigrationRun

    from app.config import SERVER_VERSION

    if not inspect(connection).has_table(SchemaMigrationRun.__tablename__):
        return
    connection.execute(SchemaMigrationRun.__table__.insert().values(
        from_revision=from_revision,
        to_revision=to_revision,
        revisions=json.dumps(applied),
        snapshot_path=snapshot_path,
        server_version=SERVER_VERSION,
        started_at=started_at,
        finished_at=datetime.utcnow(),
        duration_ms=duration_ms,
    ))


def recent_runs(db, limit: int = 10) -> List[Dict]:
    from database.models import SchemaMigrationRun

    try:
        rows = (db.query(SchemaMigrationRun)
                .order_by(SchemaMigrationRun.finished_at.desc(), SchemaMigrationRun.id.desc())
                .limit(limit).all())
    except Exception:
        db.rollback()
        return []
    return [{
        "from_revision": row.from_revision,
        "to_revision": row.to_revision,
        "revisions": json.loads(row.revisions or "[]"),
        "snapshot_path": row.snapshot_path,
        "server_version": row.server_version,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "duration_ms": row.duration_ms,
    } for row in rows]
