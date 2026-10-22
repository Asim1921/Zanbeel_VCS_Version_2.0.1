#!/usr/bin/env bash
#
# Create an administrator account in this deployment's database (server/foxnest.db, or
# DATABASE_URL in server/.env). A new install has no accounts, and the web app has no
# sign-up for administrators, so the first one is made here; the rest from the Users page.
#
#   ./deploy/create-admin.sh            asks for the user name, email and password
#
# The password is typed at a prompt, never passed on the command line, so it stays out of
# the shell history and the process list.

set -Eeuo pipefail
# shellcheck source=lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[ "$(id -u)" -ne 0 ] || die "Run this as the user that owns the app, not as root."
[ -x "$VENV_DIR/bin/python" ] || die "venv/ is missing: run ./deploy.sh first."
[ -t 0 ] || die "This asks for a password: run it in a terminal."

cd "$SERVER_DIR"
"$VENV_DIR/bin/python" - <<'PY'
import getpass
import sys

from database.database import SessionLocal
from database.crud import UserCRUD
from app.core.security import hash_password

username = input("Administrator user name: ").strip()
if not username:
    sys.exit("No user name given.")
email = input("Email (optional, for password resets): ").strip() or None

db = SessionLocal()
try:
    if UserCRUD.get_user_by_username(db, username):
        sys.exit(f"A user named {username!r} already exists; nothing was changed.")
    password = getpass.getpass("Password (8 characters or more): ")
    if len(password) < 8:
        sys.exit("The password must be at least 8 characters; nothing was changed.")
    if getpass.getpass("Password again: ") != password:
        sys.exit("The passwords do not match; nothing was changed.")
    UserCRUD.create_user(db, username, email=email, role="admin",
                         password_hash=hash_password(password))
    print(f"Created administrator {username!r}. Sign in with it on the web app.")
finally:
    db.close()
PY
