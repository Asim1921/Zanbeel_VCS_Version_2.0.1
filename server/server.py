#!/usr/bin/env python3
"""FoxNest Server entrypoint.

The application itself lives in the ``app`` package:

    app/config.py         environment-derived settings and shared constants
    app/main.py           the FastAPI instance, CORS and startup wiring
    app/api/routes/       one module per group of HTTP endpoints
    app/core/             security, auth dependencies and permission rules
    app/services/         domain logic shared by the route modules
    app/schemas/          pydantic request/response bodies
    app/db/migrations.py  idempotent schema patches applied at startup

This module stays as a thin shim so that both existing entrypoints keep working:
``python3 server.py`` (the systemd unit) and the test suite, which loads this file by
path and reaches for ``app``, ``get_db`` and the password/token helpers on it.

``get_db`` in particular must be re-exported as the *same object* the routers depend on,
otherwise ``app.dependency_overrides[server_module.get_db]`` in the tests would silently
fail to match.
"""

import os
import sys

# Allow `python3 /path/to/server.py` from any working directory.
_SERVER_ROOT = os.path.dirname(os.path.abspath(__file__))
if _SERVER_ROOT not in sys.path:
    sys.path.insert(0, _SERVER_ROOT)

import uvicorn

from database.database import get_db
from app.core.dependencies import get_current_user, get_optional_user
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
# Re-exported for tests/test_branch_features_api.py, which imports it from here.
from app.db.migrations import ensure_repositories_branch_policy_column
from app.main import app

__all__ = [
    "app",
    "get_db",
    "get_current_user",
    "get_optional_user",
    "hash_password",
    "verify_password",
    "create_access_token",
    "decode_access_token",
    "ensure_repositories_branch_policy_column",
]

if __name__ == "__main__":
    print(f"Starting FoxNest Server v2.0 with SQL Database...")
    print(f"Database URL: {os.getenv('DATABASE_URL', 'sqlite:///./foxnest.db')}")

    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("SERVER_PORT", "33333"))
    debug = os.getenv("DEBUG", "False").lower() == "true"

    # Increase timeout for large file uploads
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info" if not debug else "debug",
        timeout_keep_alive=300,  # 5 minutes keep-alive
    )
