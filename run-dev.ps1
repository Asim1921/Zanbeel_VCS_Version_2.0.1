<#
.SYNOPSIS
    Starts FoxNest (backend API + frontend dev server) for local development on Windows.

.DESCRIPTION
    - Creates ./venv (Python virtual environment) and installs backend deps if missing.
    - Installs frontend node_modules if missing.
    - Launches the backend (FastAPI/uvicorn, port 33333) and frontend (Vite, port 5173)
      each in their own PowerShell window so logs stay visible and Ctrl+C stops just that one.

.USAGE
    From the repo root:
        .\run-dev.ps1
#>

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# --- Backend: venv + deps ---
$venvPython = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating Python virtual environment (venv)..." -ForegroundColor Cyan
    python -m venv (Join-Path $root "venv")
}

Write-Host "Ensuring backend dependencies are installed..." -ForegroundColor Cyan
$requirements = Join-Path $root "docs\requirements.txt"
if (-not (Test-Path $requirements)) { $requirements = Join-Path $root "requirements.txt" }
& $venvPython -m pip install -q -r $requirements

# --- Frontend: node_modules ---
$frontendDir = Join-Path $root "foxnestFrontend"
if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
    Write-Host "Installing frontend dependencies (npm install)..." -ForegroundColor Cyan
    Push-Location $frontendDir
    npm install
    Pop-Location
}

# --- Launch backend in its own window ---
# PYTHONIOENCODING/PYTHONUTF8: the server prints unicode checkmarks during startup;
# Windows' default console codepage (cp1252) can't encode them without this.
Write-Host "Starting backend on http://localhost:33333 ..." -ForegroundColor Green
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\server'; `$env:PYTHONIOENCODING='utf-8'; `$env:PYTHONUTF8='1'; & '$venvPython' server.py"
)

# --- Launch frontend in its own window ---
Write-Host "Starting frontend on http://localhost:5173 ..." -ForegroundColor Green
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$frontendDir'; npm run dev"
)

Write-Host ""
Write-Host "FoxNest is starting up in two new windows:" -ForegroundColor Yellow
Write-Host "  Backend:  http://localhost:33333  (API docs at /docs)"
Write-Host "  Frontend: http://localhost:5173"
Write-Host "Close those windows (or Ctrl+C inside them) to stop each service."
