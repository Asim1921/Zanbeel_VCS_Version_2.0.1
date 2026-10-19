# Running FoxNest Locally (Windows dev)

## One-command start

From the repo root, in PowerShell:

```powershell
.\run-dev.ps1
```

This will (only the first time, subsequent runs skip already-done steps):
1. Create `.\venv` and install backend dependencies from `requirements.txt`.
2. Install frontend dependencies (`npm install`) in `foxnestFrontend/` if `node_modules` is missing.
3. Open two new PowerShell windows:
   - **Backend** — FastAPI/uvicorn at http://localhost:33333 (Swagger docs at `/docs`)
   - **Frontend** — Vite dev server at http://localhost:5173

Close a window (or `Ctrl+C` inside it) to stop that service.

## Manual equivalent (if you don't want to use the script)

Backend:

```powershell
cd server
$env:PYTHONIOENCODING = "utf-8"   # avoids a UnicodeEncodeError from startup log checkmarks on Windows
$env:PYTHONUTF8 = "1"
..\venv\Scripts\python.exe server.py
```

Frontend (separate terminal):

```powershell
cd foxnestFrontend
npm run dev
```

## First-time setup (if `run-dev.ps1` hasn't been run yet)

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
cd foxnestFrontend
npm install
cd ..
```

## Running the tests

The `server/tests` suite is the regression baseline. From the repo root:

```powershell
cd server
$env:PYTHONIOENCODING = "utf-8"; $env:PYTHONUTF8 = "1"
..\venv\Scripts\python.exe -m pytest tests/ -q
```

Notes:
- The full suite is about 1,000 tests and takes roughly 17 minutes; it is not hung, just serial.
  Run it from Git Bash with `< /dev/null` so `ssh-keygen` never waits for a passphrase.
- Run a single file while iterating: `..\venv\Scripts\python.exe -m pytest tests/test_blame.py -q`
- End-to-end checks against a live server, each on a throwaway database:
  `..\venv\Scripts\python.exe tools/e2e_security_operations.py` (and the other `tools/e2e_*.py`).

## Configuration

- Backend env vars live in `server/.env` (`FOXNEST_AUTH_SECRET`, `SERVER_PORT`, `DATABASE_URL`, etc.) — already configured, no placeholder values.
- Frontend env vars live in `foxnestFrontend/.env` (`VITE_API_URL`, `VITE_API_SERVER_URL`) — already pointed at `http://127.0.0.1:33333`.
- Default backend port is `33333` (overridable via `SERVER_PORT` in `server/.env`); the deployment guide (`DEPLOYMENT.md`) describes a systemd/Linux setup using port `5000` instead — that doesn't apply to this local Windows script.

## Operating a server

Everything below is also on the **Operations** page (administrators), and from any
machine through `fox admin ...`. The terminal commands run in `server/` on the server.

### Backups

A backup holds the database, every blob it references and the generated documents. It is
*verified* by reading every blob back and checking it against its hash, plus SQLite's own
integrity check of the snapshot. Unchanged blobs are hard-linked from the previous verified
backup, so each one costs only what changed.

```powershell
..\venv\Scripts\python.exe tools/backup.py --create --drill     # take one, then test-restore it
..\venv\Scripts\python.exe tools/backup.py --list
..\venv\Scripts\python.exe tools/backup.py --verify <id>         # re-read it (add --quick for presence only)
..\venv\Scripts\python.exe tools/backup.py --test-restore <id>   # restore into a scratch folder and read it back
```

- **Schedule:** set `FOXNEST_BACKUP_INTERVAL_HOURS=24` and the server takes, verifies and
  test-restores a backup itself, keeping the newest `FOXNEST_BACKUP_KEEP` (7).
- **Another disk:** set `FOXNEST_BACKUP_DIR` to a different disk, or copy backups off the
  machine. A backup on the data's own disk does not survive losing that disk.
- **The secret:** commits are signed with `FOXNEST_AUTH_SECRET`. Keep a copy of `server/.env`
  somewhere safe, apart from the backups: restored without it, every signature reports invalid.
- **Not backed up:** the code search index, which is rebuilt from the data on start.

### Restoring

1. Stop the server.
2. `..\venv\Scripts\python.exe tools/backup.py --restore <id>` (type `restore` to confirm).
   It refuses a backup that does not verify (`--force` overrides). The database it replaces,
   with its `-wal`/`-shm`/`-journal` files, the documents and the search index are moved aside
   as `*.replaced-<time>`, not deleted.
3. Start the server. A backup from an older version is upgraded to this version's schema on
   start (a copy is taken first); the search index rebuilds in the background.

### Schema upgrades

Schema changes are Alembic revisions under `server/alembic/versions/`. The server applies
pending ones when it starts: all in one transaction, after copying the database to
`<backup dir>/pre-upgrade/`. If one fails, nothing is changed and the server does not start.

```powershell
..\venv\Scripts\python.exe tools/schema.py --status    # which revision, what is pending, past upgrades
..\venv\Scripts\python.exe tools/schema.py --check     # does the database match the code?
..\venv\Scripts\python.exe tools/schema.py --upgrade   # upgrade now (with FOXNEST_AUTO_MIGRATE=0)
```

To change the schema: change the model in `database/models.py`, then add a revision that
brings an existing database to it, checking before it changes anything (see
`alembic/versions/0003_security_and_operations.py` and `app/db/schema_ops.py`).

### Sign-in limits

Five failures from one address stop that address guessing at that account for fifteen
minutes; twenty from anywhere lock the account, each consecutive lock twice as long. One
address failing across many accounts is throttled on its own. The account's owner is
notified of a lock, and a password reset by email lifts it. Clear one early on the
Operations page, or `fox admin unlock <user>` / `fox admin unlock --ip <address>`.
Behind a reverse proxy, set `FOXNEST_TRUST_PROXY=1` so limits apply to the real client address.

### Paged lists

Every list the API returns is paged (200 by default, at most 1000): the body's `pagination`
object, an `X-Total-Count` header and a `Link` header (`rel="next"`) say whether there is more.
Scripts that need everything follow `next` until there is none.

## Troubleshooting

- **The server exits at startup with "SCHEMA DRIFT DETECTED"** — the database does not match
  the code. The message names what is missing; `tools/schema.py --check` shows the rest. A
  missing *unique* rule usually means existing rows break it: resolve the duplicates and start
  again.
- **"The database is at revision ..., which this server does not have"** — a newer version of
  Zanbeel upgraded this database. Run that version, or restore a backup taken before it.

- **`RuntimeError: Form data requires "python-multipart"`** — already fixed by adding `python-multipart` to `requirements.txt`; re-run `pip install -r requirements.txt` if you see this on an old venv.
- **`UnicodeEncodeError` on startup** — set `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1` before running `server.py` (the script above does this for you).
- **Port already in use** — another process is bound to 33333 or 5173; stop it or change `SERVER_PORT` in `server/.env` (and `VITE_API_URL`/`VITE_API_SERVER_URL` in `foxnestFrontend/.env` to match).
