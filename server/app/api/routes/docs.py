"""Documentation generation, status polling and generated-file serving.

Declaration order matters: /docs/archive.zip must stay ahead of the
/docs/{file_path:path} catch-all."""

import os
import io
import json
import zipfile

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from database.crud import RepositoryCRUD
from database.database import get_db
from database.models import User

from app.config import DOCS_DIR
from app.core.dependencies import get_current_user, get_optional_user
from app.core.permissions import _require_repository_read_access
from app.services.docs_jobs import (
    DOCS_GENERATION_JOBS, DOCS_GENERATION_LOCK, _generate_repository_docs_sync,
    _parse_selected_docs_param, _resolve_progress_steps, _run_generate_docs_job,
    _run_generate_project_docs_job, _update_docs_job,
)


router = APIRouter()


@router.post("/api/repository/{repo_id}/generate-docs")
async def generate_repository_docs(
    repo_id: str,
    lang_filter: Optional[str] = None,
    selected_docs: Optional[str] = None,
    background: bool = True,
    background_tasks: BackgroundTasks = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Generate documentation for a repository."""
    try:
        # Backward-compatible behavior for older clients:
        # If no explicit language filter is provided, treat generate-docs as
        # project-doc generation using the configured Qwen model.
        if not lang_filter:
            selected_docs_list = _parse_selected_docs_param(selected_docs)
            progress_steps, selected_doc_names, invalid = _resolve_progress_steps(selected_docs_list)
            if invalid:
                raise HTTPException(status_code=400, detail=f"Invalid doc names: {', '.join(invalid)}")

            # Verify repository exists quickly using request session
            repository = RepositoryCRUD.get_repository(db, repo_id)
            if not repository:
                raise HTTPException(status_code=404, detail="Repository not found")

            resolved_llm_model = os.getenv("FOXNEST_PROJECT_DOCS_MODEL", "qwen2.5-coder:32b")

            with DOCS_GENERATION_LOCK:
                existing = DOCS_GENERATION_JOBS.get(repo_id)
                if existing and existing.get("status") in {"queued", "running"}:
                    return {
                        "success": True,
                        "message": "Project documentation generation already in progress",
                        "repo_id": repo_id,
                        "status": existing.get("status", "running"),
                        "mode": existing.get("mode", "project"),
                        "llm_model": existing.get("llm_model", resolved_llm_model),
                        "selected_docs": existing.get("selected_docs", selected_doc_names or ["all"]),
                        "docs_url": f"/api/repository/{repo_id}/docs/project",
                        "status_url": f"/api/repository/{repo_id}/docs-status"
                    }

            _update_docs_job(
                repo_id,
                status="queued",
                mode="project",
                llm_model=resolved_llm_model,
                queued_at=datetime.utcnow().isoformat(),
                started_at=None,
                finished_at=None,
                error=None,
                result=None,
                lang_filter=None,
                selected_docs=selected_doc_names,
                docs_progress=[
                    {"name": step, "status": "pending", "started_at": None, "finished_at": None}
                    for step in progress_steps
                ]
            )
            background_tasks.add_task(_run_generate_project_docs_job, repo_id, resolved_llm_model, selected_docs_list)

            return {
                "success": True,
                "message": "Project documentation generation started in background",
                "repo_id": repo_id,
                "status": "processing",
                "mode": "project",
                "llm_model": resolved_llm_model,
                "selected_docs": selected_doc_names or ["all"],
                "docs_url": f"/api/repository/{repo_id}/docs/project",
                "status_url": f"/api/repository/{repo_id}/docs-status"
            }

        # Verify repository exists quickly using request session
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")
        if background:
            with DOCS_GENERATION_LOCK:
                existing = DOCS_GENERATION_JOBS.get(repo_id)
                if existing and existing.get("status") in {"queued", "running"}:
                    return {
                        "success": True,
                        "message": "Documentation generation already in progress",
                        "repo_id": repo_id,
                        "status": existing.get("status", "running"),
                        "docs_url": f"/api/repository/{repo_id}/docs",
                        "status_url": f"/api/repository/{repo_id}/docs-status"
                    }

            _update_docs_job(
                repo_id,
                status="queued",
                queued_at=datetime.utcnow().isoformat(),
                started_at=None,
                finished_at=None,
                error=None,
                result=None,
                lang_filter=lang_filter
            )
            background_tasks.add_task(_run_generate_docs_job, repo_id, lang_filter)

            return {
                "success": True,
                "message": "Documentation generation started in background",
                "repo_id": repo_id,
                "status": "processing",
                "docs_url": f"/api/repository/{repo_id}/docs",
                "status_url": f"/api/repository/{repo_id}/docs-status"
            }

        result = await run_in_threadpool(_generate_repository_docs_sync, repo_id, lang_filter)
        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Documentation generation failed: {str(e)}")


@router.get("/api/repository/{repo_id}/docs-status")
async def get_repository_docs_status(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    with DOCS_GENERATION_LOCK:
        job = DOCS_GENERATION_JOBS.get(repo_id)

    docs_path = DOCS_DIR / repo_id
    docs_exist = docs_path.exists() and any(docs_path.rglob("*.md"))

    if not job:
        return {
            "success": True,
            "repo_id": repo_id,
            "status": "not_started",
            "docs_available": bool(docs_exist)
        }

    return {
        "success": True,
        "repo_id": repo_id,
        "status": job.get("status", "unknown"),
        "queued_at": job.get("queued_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "result": job.get("result"),
        "docs_progress": job.get("docs_progress", []),
        "docs_available": bool(docs_exist)
    }


@router.get("/api/repository/{repo_id}/docs")
async def get_repository_docs(
    repo_id: str,
    db: Session = Depends(get_db),
    actor: Optional[User] = Depends(get_optional_user),
):
    """Get list of generated documentation files for a repository."""
    try:
        # Verify repository exists
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        if not actor:
            raise HTTPException(status_code=401, detail="Authentication required")
        _require_repository_read_access(db, actor, repository, "view docs")
        
        docs_path = DOCS_DIR / repo_id
        if not docs_path.exists():
            return {
                "success": False,
                "error": "No documentation generated yet. Use POST /api/repository/{repo_id}/generate-docs first.",
                "repo_id": repo_id
            }
        
        # List all doc files
        doc_files = []
        for file_path in docs_path.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(docs_path)
                doc_files.append({
                    "path": str(rel_path),
                    "size": file_path.stat().st_size,
                    "url": f"/api/repository/{repo_id}/docs/{rel_path}"
                })
        
        # Load metadata if exists
        meta_file = docs_path / "docs_meta.json"
        metadata = None
        if meta_file.exists():
            with open(meta_file, "r") as f:
                metadata = json.load(f)
        
        return {
            "success": True,
            "repo_id": repo_id,
            "docs_path": str(docs_path),
            "files": doc_files,
            "metadata": metadata,
            "index_url": f"/api/repository/{repo_id}/docs/index.md",
            "archive_url": f"/api/repository/{repo_id}/docs/archive.zip",
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve docs: {str(e)}")


@router.get("/api/repository/{repo_id}/docs/archive.zip")
async def download_repository_docs_archive(
    repo_id: str,
    db: Session = Depends(get_db),
    actor: Optional[User] = Depends(get_optional_user),
):
    """Download all generated documentation files as a single ZIP."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not actor:
        raise HTTPException(status_code=401, detail="Authentication required")
    _require_repository_read_access(db, actor, repository, "download docs")
    docs_path = DOCS_DIR / repo_id
    if not docs_path.exists() or not docs_path.is_dir():
        raise HTTPException(status_code=404, detail="No documentation generated for this repository")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in docs_path.rglob("*"):
            if file_path.is_file():
                arc = str(file_path.relative_to(docs_path))
                zf.write(file_path, arcname=arc)
    buffer.seek(0)
    safe_name = f"{repository.name or repo_id}-documentation.zip"
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.get("/api/repository/{repo_id}/docs/{file_path:path}")
async def serve_repository_doc_file(
    repo_id: str,
    file_path: str,
    db: Session = Depends(get_db),
    actor: Optional[User] = Depends(get_optional_user),
):
    """Serve a specific documentation file."""
    try:
        # Verify repository exists
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")
        if not actor:
            raise HTTPException(status_code=401, detail="Authentication required")
        _require_repository_read_access(db, actor, repository, "view docs")
        
        docs_path = DOCS_DIR / repo_id
        if not docs_path.exists():
            raise HTTPException(status_code=404, detail="No documentation generated for this repository")
        
        full_path = docs_path / file_path
        
        # Security check: prevent path traversal
        if not str(full_path.resolve()).startswith(str(docs_path.resolve())):
            raise HTTPException(status_code=403, detail="Access denied")
        
        if not full_path.exists() or not full_path.is_file():
            raise HTTPException(status_code=404, detail="Documentation file not found")
        
        # Determine media type
        media_type = "text/plain"
        if file_path.endswith(".md"):
            media_type = "text/markdown"
        elif file_path.endswith(".html"):
            media_type = "text/html"
        elif file_path.endswith(".json"):
            media_type = "application/json"
        elif file_path.endswith(".css"):
            media_type = "text/css"
        elif file_path.endswith(".js"):
            media_type = "application/javascript"
        
        # Prefer inline viewing in the browser for text docs (avoid "only one download" confusion).
        disposition = "inline" if media_type.startswith("text/") or media_type in (
            "application/javascript",
            "application/json",
        ) else "attachment"

        return FileResponse(
            path=full_path,
            media_type=media_type,
            filename=full_path.name,
            content_disposition_type=disposition,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to serve documentation file: {str(e)}")


@router.post("/api/repository/{repo_id}/generate-project-docs")
async def generate_project_documentation(
    repo_id: str,
    llm_model: Optional[str] = None,
    selected_docs: Optional[str] = None,
    background_tasks: BackgroundTasks = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Generate comprehensive project documentation (README, API, Architecture, etc.) using Qwen LLM."""
    try:
        # Verify repository exists
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")
        
        resolved_llm_model = llm_model or os.getenv("FOXNEST_PROJECT_DOCS_MODEL", "qwen2.5-coder:32b")
        selected_docs_list = _parse_selected_docs_param(selected_docs)
        progress_steps, selected_doc_names, invalid = _resolve_progress_steps(selected_docs_list)
        if invalid:
            raise HTTPException(status_code=400, detail=f"Invalid doc names: {', '.join(invalid)}")
        
        # Check if docs generation is already in progress
        with DOCS_GENERATION_LOCK:
            existing = DOCS_GENERATION_JOBS.get(repo_id)
            if existing and existing.get("status") in {"queued", "running"}:
                return {
                    "success": True,
                    "message": "Project documentation generation already in progress",
                    "repo_id": repo_id,
                    "status": existing.get("status", "processing"),
                    "llm_model": resolved_llm_model,
                    "selected_docs": existing.get("selected_docs", selected_doc_names or ["all"]),
                    "docs_url": f"/api/repository/{repo_id}/docs/project",
                    "status_url": f"/api/repository/{repo_id}/docs-status"
                }
        
        # Queue the docs generation for background processing
        _update_docs_job(
            repo_id,
            status="queued",
            mode="project",
            llm_model=resolved_llm_model,
            queued_at=datetime.utcnow().isoformat(),
            started_at=None,
            finished_at=None,
            error=None,
            result=None,
            selected_docs=selected_doc_names,
            docs_progress=[
                {"name": step, "status": "pending", "started_at": None, "finished_at": None}
                for step in progress_steps
            ]
        )
        
        # Add task to background queue - don't wait for it
        background_tasks.add_task(_run_generate_project_docs_job, repo_id, resolved_llm_model, selected_docs_list)
        
        return {
            "success": True,
            "message": "Project documentation generation started in background",
            "repo_id": repo_id,
            "status": "processing",
            "llm_model": resolved_llm_model,
            "selected_docs": selected_doc_names or ["all"],
            "docs_url": f"/api/repository/{repo_id}/docs/project",
            "status_url": f"/api/repository/{repo_id}/docs-status"
        }
    except HTTPException:
        raise
    except Exception as e:
        return {
            "success": False,
            "error": f"Error queuing documentation generation: {str(e)}"
        }
