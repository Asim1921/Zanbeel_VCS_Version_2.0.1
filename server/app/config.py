"""Environment-derived configuration, paths and shared constants.

Importing this module validates the auth secrets and refuses to start the
process when they are missing or left at a known placeholder value."""

import os
import re
import logging

from pathlib import Path


from dotenv import load_dotenv

load_dotenv()


SERVER_ROOT = Path("/tmp/foxnest_server")  # Keep for backward compatibility
REPOS_DIR = SERVER_ROOT / "repositories"
DOCS_DIR = SERVER_ROOT / "docs"  # Store generated docs
DOCS_DIR.mkdir(parents=True, exist_ok=True)


_INSECURE_AUTH_SECRETS = {
    "",
    "foxnest-dev-secret-change-me",
    "change-me-to-a-long-random-string",
    "change-me",
}
_INSECURE_SETUP_KEYS = {
    "",
    "change-me-to-another-random-string",
    "change-me",
}

AUTH_SECRET = (os.getenv("FOXNEST_AUTH_SECRET") or "").strip()
PASSWORD_SETUP_KEY = (os.getenv("FOXNEST_PASSWORD_SETUP_KEY") or "").strip()
_ALLOW_INSECURE_AUTH = os.getenv("FOXNEST_ALLOW_INSECURE_AUTH", "").strip().lower() in {"1", "true", "yes"}

if AUTH_SECRET in _INSECURE_AUTH_SECRETS:
    if _ALLOW_INSECURE_AUTH:
        AUTH_SECRET = AUTH_SECRET or "foxnest-dev-secret-change-me"
        print(
            "WARNING: FOXNEST_AUTH_SECRET is a known insecure placeholder. "
            "Token forgery is trivial. Set a long random secret before any shared/network use."
        )
    else:
        raise SystemExit(
            "Refusing to start: FOXNEST_AUTH_SECRET is missing or a known placeholder "
            "(e.g. change-me-to-a-long-random-string). Set a long random value in server/.env, "
            "or set FOXNEST_ALLOW_INSECURE_AUTH=1 for local-only development."
        )

if PASSWORD_SETUP_KEY in _INSECURE_SETUP_KEYS:
    # Placeholder keys must never enable bootstrap-password in a shared environment.
    if PASSWORD_SETUP_KEY and _ALLOW_INSECURE_AUTH:
        print(
            "WARNING: FOXNEST_PASSWORD_SETUP_KEY is a known insecure placeholder. "
            "Bootstrap password endpoint remains enabled only because FOXNEST_ALLOW_INSECURE_AUTH=1."
        )
    else:
        if PASSWORD_SETUP_KEY:
            print(
                "WARNING: FOXNEST_PASSWORD_SETUP_KEY is a known insecure placeholder; "
                "password bootstrap is disabled until you set a strong key."
            )
        PASSWORD_SETUP_KEY = ""

TOKEN_EXPIRY_HOURS = int(os.getenv("FOXNEST_TOKEN_EXPIRY_HOURS", "12"))
PWD_ITERATIONS = int(os.getenv("FOXNEST_PASSWORD_ITERATIONS", "200000"))


logger = logging.getLogger("foxnest.versioning")


SEMVER_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
DIFF_MAX_LINES = 5000
DIFF_MAX_BYTES = 1024 * 1024  # 1 MB per side for inline diffs


def _size_env(name: str, default_mb: int) -> int:
    """Read a size limit in MB from the environment. 0 (or negative) disables the limit."""
    try:
        megabytes = int(os.getenv(name, str(default_mb)))
    except ValueError:
        megabytes = default_mb
    return megabytes * 1024 * 1024 if megabytes > 0 else 0


# Push admission limits. A source repository has no business carrying installers or
# release archives; without these a single push can add a gigabyte to the object store.
# Set either to 0 to disable that check.
MAX_PUSH_FILE_BYTES = _size_env("FOXNEST_MAX_FILE_MB", 50)
MAX_PUSH_TOTAL_BYTES = _size_env("FOXNEST_MAX_PUSH_MB", 250)

# Enforce the repository's .foxignore (or the built-in defaults) server-side, so a
# stale or patched client cannot smuggle build output into the store.
ENFORCE_IGNORE_ON_PUSH = os.getenv("FOXNEST_ENFORCE_IGNORE", "1").strip().lower() not in {
    "0", "false", "no", "off",
}

# Where file contents live. Blobs are content-addressed on disk rather than inline in
# file_objects.content, so the database stays small enough to back up and migrate.
# Defaults to a directory beside the server package; point it at a data volume in
# production. Reads fall back to the database column for rows written before the move.
BLOB_DIR = Path(
    os.getenv("FOXNEST_BLOB_DIR") or (Path(__file__).resolve().parent.parent / "blobstore")
)


# CORS: reflect configured origins only. Never pair Access-Control-Allow-Origin: * with credentials.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]
