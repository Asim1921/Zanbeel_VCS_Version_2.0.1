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
- The full suite is ~471 tests and takes roughly 10 minutes; it is not hung, just serial.
- Run a single file while iterating: `..\venv\Scripts\python.exe -m pytest tests/test_blame.py -q`

## Configuration

- Backend env vars live in `server/.env` (`FOXNEST_AUTH_SECRET`, `SERVER_PORT`, `DATABASE_URL`, etc.) — already configured, no placeholder values.
- Frontend env vars live in `foxnestFrontend/.env` (`VITE_API_URL`, `VITE_API_SERVER_URL`) — already pointed at `http://127.0.0.1:33333`.
- Default backend port is `33333` (overridable via `SERVER_PORT` in `server/.env`); the deployment guide (`DEPLOYMENT.md`) describes a systemd/Linux setup using port `5000` instead — that doesn't apply to this local Windows script.

## Troubleshooting

- **`RuntimeError: Form data requires "python-multipart"`** — already fixed by adding `python-multipart` to `requirements.txt`; re-run `pip install -r requirements.txt` if you see this on an old venv.
- **`UnicodeEncodeError` on startup** — set `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1` before running `server.py` (the script above does this for you).
- **Port already in use** — another process is bound to 33333 or 5173; stop it or change `SERVER_PORT` in `server/.env` (and `VITE_API_URL`/`VITE_API_SERVER_URL` in `foxnestFrontend/.env` to match).
