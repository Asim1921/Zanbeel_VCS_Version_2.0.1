"""Health check, API index and client download endpoints.

These are the only endpoints a machine can reach before it has credentials, so
they are deliberately unauthenticated: you cannot require a token to fetch the
thing that lets you obtain a token. Nothing served here is secret — it is the
same client source and extension package every developer already runs.
"""

import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response


router = APIRouter()

#: Repository root: routes -> api -> app -> server -> repo.
#: Computed once and named, because this path was previously written inline as
#: `Path(__file__).parent.parent` and lost a level when the single-file server
#: was split into `app/`. The client download had been returning 404 ever since.
REPO_ROOT = Path(__file__).resolve().parents[4]
SERVER_ROOT = Path(__file__).resolve().parents[3]

CLIENT_VERSION = "1.0.5"


def _extension_dir() -> Path:
    """Where packaged `.vsix` files are kept."""
    configured = os.getenv("FOXNEST_EXTENSION_DIR")
    if configured:
        return Path(configured)
    return REPO_ROOT / "vscode-extension"


def _latest_vsix() -> Path | None:
    """The newest `.vsix` on disk, or None.

    Sorted by modification time rather than by the version in the filename:
    string ordering would put 0.10.0 before 0.9.0, and a rebuilt package should
    win over an older one regardless of how it is numbered.
    """
    directory = _extension_dir()
    if not directory.is_dir():
        return None
    candidates = sorted(
        directory.glob("*.vsix"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


@router.get("/")
async def health_check():
    """Health check endpoint"""
    return {"status": "FoxNest Server v2.0 is running with SQL Database", "version": "2.0.0"}


@router.get("/api/download/client")
async def download_client():
    """Download the latest Fox client (fox.py)."""
    for candidate in (
        REPO_ROOT / "client" / "fox.py",
        REPO_ROOT / "release" / "fox.py",
        REPO_ROOT / "client" / "client1" / "fox.py",
    ):
        if candidate.is_file():
            return FileResponse(
                path=str(candidate),
                filename="fox.py",
                media_type="text/x-python",
                headers={"X-Fox-Client-Version": CLIENT_VERSION},
            )
    raise HTTPException(status_code=404, detail="Client file not found")


@router.get("/api/download/extension")
async def download_extension():
    """Download the Zanbeel VS Code extension as a `.vsix`.

    This is the supported way to install the client: the extension bundles the
    CLI and puts `fox` on the PATH of every VS Code terminal, so a developer
    never has to place `fox.py` or a launcher by hand.

    Install with:
        code --install-extension zanbeel-vcs-<version>.vsix
    or in VS Code: Extensions -> ... -> Install from VSIX.
    """
    package = _latest_vsix()
    if package is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No extension package is available on this server. Build one with "
                "'npm run package' in vscode-extension/, or set FOXNEST_EXTENSION_DIR "
                "to the directory holding the .vsix."
            ),
        )
    return FileResponse(
        path=str(package),
        filename=package.name,
        media_type="application/octet-stream",
        headers={"X-Zanbeel-Extension-Version": _version_of(package)},
    )


def _version_of(package: Path) -> str:
    """Pull the version out of a filename like `zanbeel-vcs-0.1.0.vsix`."""
    match = re.search(r"-(\d+\.\d+\.\d+)\.vsix$", package.name)
    return match.group(1) if match else "unknown"


@router.get("/api/download/extension/info")
async def extension_info():
    """What extension package this server is offering.

    Exposed separately so the extension can check whether it is behind without
    downloading several megabytes to find out.
    """
    package = _latest_vsix()
    if package is None:
        return {
            "success": True,
            "available": False,
            "reason": "No .vsix found on the server",
            "directory": str(_extension_dir()),
        }
    stat = package.stat()
    return {
        "success": True,
        "available": True,
        "filename": package.name,
        "version": _version_of(package),
        "size_bytes": stat.st_size,
        "built_at": __import__("datetime").datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
        "download_url": "/api/download/extension",
        "install_command": f"code --install-extension {package.name}",
    }


@router.get("/api/download/fox.bat")
async def download_fox_bat():
    """Download a Windows .bat launcher that runs the local fox.py with Python.

    Superseded by the VS Code extension, which needs no launcher placed by hand.
    Kept for machines that do not run VS Code, such as build agents.
    """
    bat_content = (
        "@echo off\r\n"
        ":: FoxNest client launcher - runs fox.py from the same directory\r\n"
        ":: Prefer the VS Code extension: /api/download/extension\r\n"
        "setlocal\r\n"
        "set SCRIPT_DIR=%~dp0\r\n"
        "set FOX_PY=%SCRIPT_DIR%fox.py\r\n"
        "set PYTHONIOENCODING=utf-8\r\n"
        "if not exist \"%FOX_PY%\" (\r\n"
        "    echo ERROR: fox.py not found in %SCRIPT_DIR%\r\n"
        "    echo Download it from this server: /api/download/client\r\n"
        "    exit /b 1\r\n"
        ")\r\n"
        "python \"%FOX_PY%\" %*\r\n"
    )
    return Response(
        content=bat_content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=fox.bat"},
    )


@router.get("/api/")
async def api_root():
    """API root endpoint"""
    return {"status": "FoxNest API v2.0 is running", "version": "2.0.0", "endpoints": [
        "/api/repository/create",
        "/api/repository/list",
        "/api/repositories/all",
        "/api/repository/{repo_id}",
        "/api/repository/{repo_id}/push",
        "/api/repository/{repo_id}/pull",
        "/api/repository/{repo_id}/commits",
        "/api/users",
        "/api/activities",
        "/api/download/client",
        "/api/download/extension",
        "/api/download/extension/info",
    ]}
