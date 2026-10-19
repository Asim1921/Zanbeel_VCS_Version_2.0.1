"""Alembic environment.

Two ways in:

* **From the server or ``tools/schema.py``** (``app/db/upgrade.py``), which passes its
  own connection in ``config.attributes["connection"]``. That connection already holds
  the write lock and an open transaction, so every pending revision runs inside it and
  commits or rolls back as one.
* **From the ``alembic`` command line.** Opens the same kind of connection itself, so
  a hand-run ``alembic upgrade head`` is just as transactional. It does not take the
  pre-upgrade copy; ``tools/schema.py --upgrade`` does.

The database URL comes from ``DATABASE_URL`` -- the variable the application reads --
rather than alembic.ini, so a migration cannot be applied to a different database than
the one the server uses. ``target_metadata`` is the real models, which is what lets
``alembic revision --autogenerate`` see a change.
"""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

SERVER_ROOT = Path(__file__).resolve().parent.parent
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

# The application refuses to import without these; migrations do not use them.
os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from database.database import Base  # noqa: E402
import database.models  # noqa: E402,F401  (registers every model on Base.metadata)

config = context.config

if config.config_file_name is not None and not config.attributes.get("connection"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option(
        "sqlalchemy.url",
        os.getenv("DATABASE_URL", "sqlite:///./foxnest.db").replace("%", "%%"),
    )

target_metadata = Base.metadata


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most column properties in place; batch mode rebuilds the
        # table instead, so a type or constraint change is applicable rather than
        # failing at run time.
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
        on_version_apply=config.attributes.get("on_version_apply"),
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it, for review before applying."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    shared = config.attributes.get("connection")
    if shared is not None:
        # Already inside the caller's transaction: begin_transaction() is a no-op here
        # and the caller commits.
        _configure(shared)
        with context.begin_transaction():
            context.run_migrations()
        return

    from app.db.upgrade import migration_engine

    engine = migration_engine(config.get_main_option("sqlalchemy.url"))
    try:
        with engine.connect() as connection:
            with connection.begin():
                _configure(connection)
                with context.begin_transaction():
                    context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
