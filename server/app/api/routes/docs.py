"""Documentation generation, status polling and generated-file serving.

Declaration order matters: /docs/archive.zip must stay ahead of the
/docs/{file_path:path} catch-all.

Generating needs write access to the repository; everything else needs read access.
Generation used to need only a sign-in -- anyone could have the server read any
repository and commit to it -- and the status route needed nothing beyond that. Each
read route also caught its own 401, 403 and 404 and reported them as 500.
"""

import io
import json
import os
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
from app.core.dependencies import get_current_user
from app.core.permissions import (
    _require_repository_read_access, can_manage_repository, can_write_repository,
    require_repository_access,
)
from app.services import repo_refs
from app.services.docs_jobs import (
    DOCS_GENERATION_JOBS, DOCS_GENERATION_LOCK, _generate_repository_docs_sync,
    _parse_selected_docs_param, _resolve_progress_steps, _run_generate_docs_job,
    _run_generate_project_docs_job, _update_docs_job, generated_files, read_manifest,
    selectable_documents,
)


router = APIRouter()

#: Generated documents are written by a model from repository content, so a browser
#: opening one directly must not run anything in it.
_SAFE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}


def _repository(db: Session, repo_id: str):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repository


def _permissions(db: Session, user: User, repository) -> dict:
    return {
        "can_generate": can_write_repository(db, user, repository),
        "can_write": can_write_repository(db, user, repository),
        "can_manage": can_manage_repository(db, user, repository),
    }


def _freshness(db: Session, repo_id: str, manifest: Optional[dict]) -> dict:
    """Whether the default branch has moved since the documents were generated.

    The docs commit itself moves the branch, so either the commit they were generated
    from or the commit that added them counts as current.
    """
    branch = repo_refs.default_branch(db, repo_id)
    head = branch.head_commit_id if branch else None
    if not manifest:
        return {"default_branch": branch.name if branch else None, "default_head": head,
                "up_to_date": None}
    current = head in {manifest.get("source_commit"), manifest.get("commit_id")}
    return {"default_branch": branch.name if branch else None, "default_head": head,
            "up_to_date": bool(head) and current}


def _require_generate(db: Session, user: User, repository) -> None:
    require_repository_access(db, user.username, repository, "generate documentation for",
                              required_scope="write")


def _queue_project_docs(repo_id: str, llm_model: str, selected_docs_raw: Optional[str],
                        commit: bool, actor: User, background_tasks: BackgroundTasks) -> dict:
    selected_docs_list = _parse_selected_docs_param(selected_docs_raw)
    progress_steps, selected_doc_names, invalid = _resolve_progress_steps(selected_docs_list)
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid doc names: {', '.join(invalid)}")

    with DOCS_GENERATION_LOCK:
        existing = DOCS_GENERATION_JOBS.get(repo_id)
        if existing and existing.get("status") in {"queued", "running"}:
            return {
                "success": True,
                "message": "Project documentation generation already in progress",
                "repo_id": repo_id,
                "status": existing.get("status", "running"),
                "mode": existing.get("mode", "project"),
                "llm_model": existing.get("llm_model", llm_model),
                "selected_docs": existing.get("selected_docs", selected_doc_names or ["all"]),
                "requested_by": existing.get("requested_by"),
                "docs_url": f"/api/repository/{repo_id}/docs/project",
                "status_url": f"/api/repository/{repo_id}/docs-status"
            }
        DOCS_GENERATION_JOBS[repo_id] = {
            "status": "queued",
            "mode": "project",
            "llm_model": llm_model,
            "queued_at": datetime.utcnow().isoformat(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "result": None,
            "lang_filter": None,
            "selected_docs": selected_doc_names,
            "requested_by": actor.username,
            "commit_requested": commit,
            "docs_progress": [
                {"name": step, "status": "pending", "started_at": None, "finished_at": None}
                for step in progress_steps
            ],
        }

    background_tasks.add_task(_run_generate_project_docs_job, repo_id, llm_model,
                              selected_docs_list, actor.id, commit)
    return {
        "success": True,
        "message": "Project documentation generation started in background",
        "repo_id": repo_id,
        "status": "processing",
        "mode": "project",
        "llm_model": llm_model,
        "selected_docs": selected_doc_names or ["all"],
        "requested_by": actor.username,
        "docs_url": f"/api/repository/{repo_id}/docs/project",
        "status_url": f"/api/repository/{repo_id}/docs-status"
    }


@router.post("/api/repository/{repo_id}/generate-docs")
async def generate_repository_docs(
    repo_id: str,
    lang_filter: Optional[str] = None,
    selected_docs: Optional[str] = None,
    background: bool = True,
    commit: bool = True,
    background_tasks: BackgroundTasks = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Generate documentation for a repository.

    ``commit=false`` keeps the documents on the server without committing them.
    """
    repository = _repository(db, repo_id)
    _require_generate(db, current_user, repository)

    # Backward-compatible behavior for older clients: with no language filter,
    # generate-docs means project-doc generation with the configured model.
    if not lang_filter:
        return _queue_project_docs(
            repo_id, os.getenv("FOXNEST_PROJECT_DOCS_MODEL", "qwen2.5-coder:32b"),
            selected_docs, commit, current_user, background_tasks,
        )

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
            mode="code",
            queued_at=datetime.utcnow().isoformat(),
            started_at=None,
            finished_at=None,
            error=None,
            result=None,
            lang_filter=lang_filter,
            requested_by=current_user.username,
            commit_requested=commit,
            docs_progress=[],
        )
        background_tasks.add_task(_run_generate_docs_job, repo_id, lang_filter, current_user.id, commit)

        return {
            "success": True,
            "message": "Documentation generation started in background",
            "repo_id": repo_id,
            "status": "processing",
            "docs_url": f"/api/repository/{repo_id}/docs",
            "status_url": f"/api/repository/{repo_id}/docs-status"
        }

    try:
        return await run_in_threadpool(_generate_repository_docs_sync, repo_id, lang_filter,
                                       current_user.id, commit)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Documentation generation failed: {str(e)}")


@router.get("/api/repository/{repo_id}/docs-status")
def get_repository_docs_status(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The running or last job, and what the documents on the server were made from."""
    repository = _repository(db, repo_id)
    _require_repository_read_access(db, current_user, repository, "view docs")

    with DOCS_GENERATION_LOCK:
        job = dict(DOCS_GENERATION_JOBS.get(repo_id) or {})

    manifest = read_manifest(repo_id)
    common = {
        "success": True,
        "repo_id": repo_id,
        "docs_available": bool(generated_files(DOCS_DIR / repo_id)),
        "last_generation": manifest,
        "available_documents": selectable_documents(),
        "permissions": _permissions(db, current_user, repository),
        **_freshness(db, repo_id, manifest),
    }
    if not job:
        return {**common, "status": "not_started"}

    return {
        **common,
        "status": job.get("status", "unknown"),
        "mode": job.get("mode"),
        "llm_model": job.get("llm_model"),
        "requested_by": job.get("requested_by"),
        "selected_docs": job.get("selected_docs"),
        "queued_at": job.get("queued_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "error": job.get("error"),
        "result": job.get("result"),
        "docs_progress": job.get("docs_progress", []),
    }


@router.get("/api/repository/{repo_id}/docs")
def get_repository_docs(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get list of generated documentation files for a repository."""
    repository = _repository(db, repo_id)
    _require_repository_read_access(db, current_user, repository, "view docs")

    docs_path = DOCS_DIR / repo_id
    files = generated_files(docs_path)
    manifest = read_manifest(repo_id)
    if not files:
        return {
            "success": False,
            "error": "No documentation generated yet.",
            "repo_id": repo_id,
            "last_generation": manifest,
            "permissions": _permissions(db, current_user, repository),
            **_freshness(db, repo_id, manifest),
        }

    doc_files = []
    for file_path in files:
        rel_path = file_path.relative_to(docs_path).as_posix()
        stat = file_path.stat()
        doc_files.append({
            "path": rel_path,
            "size": stat.st_size,
            "modified_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
            "url": f"/api/repository/{repo_id}/docs/{rel_path}"
        })

    # Metadata the per-symbol generator writes, if it ran.
    metadata = None
    meta_file = docs_path / "docs_meta.json"
    if meta_file.is_file():
        try:
            metadata = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = None

    return {
        "success": True,
        "repo_id": repo_id,
        "files": doc_files,
        "metadata": metadata,
        "last_generation": manifest,
        "permissions": _permissions(db, current_user, repository),
        **_freshness(db, repo_id, manifest),
        "index_url": f"/api/repository/{repo_id}/docs/index.md",
        "archive_url": f"/api/repository/{repo_id}/docs/archive.zip",
    }


@router.get("/api/repository/{repo_id}/docs/archive.zip")
def download_repository_docs_archive(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download all generated documentation files as a single ZIP."""
    repository = _repository(db, repo_id)
    _require_repository_read_access(db, current_user, repository, "download docs")
    docs_path = DOCS_DIR / repo_id
    files = generated_files(docs_path)
    if not files:
        raise HTTPException(status_code=404, detail="No documentation generated for this repository")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in files:
            zf.write(file_path, arcname=file_path.relative_to(docs_path).as_posix())
    buffer.seek(0)
    safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in (repository.name or repo_id))
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-documentation.zip"'},
    )


@router.get("/api/repository/{repo_id}/docs/{file_path:path}")
def serve_repository_doc_file(
    repo_id: str,
    file_path: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Serve a specific documentation file."""
    repository = _repository(db, repo_id)
    _require_repository_read_access(db, current_user, repository, "view docs")

    docs_path = (DOCS_DIR / repo_id).resolve()
    if not docs_path.is_dir():
        raise HTTPException(status_code=404, detail="No documentation generated for this repository")

    parts = [p for p in file_path.replace("\\", "/").split("/") if p]
    if not parts or any(p.startswith(".") for p in parts):
        raise HTTPException(status_code=404, detail="Documentation file not found")
    full_path = docs_path.joinpath(*parts).resolve()
    # A prefix test let "<repo>2/..." pass for "<repo>"; ask about the path itself.
    try:
        full_path.relative_to(docs_path)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="Documentation file not found")

    name = full_path.name.lower()
    media_type = "text/plain; charset=utf-8"
    if name.endswith(".md"):
        media_type = "text/markdown; charset=utf-8"
    elif name.endswith(".json"):
        media_type = "application/json"
    elif name.endswith(".docx"):
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif name.endswith(".pdf"):
        media_type = "application/pdf"
    # HTML, CSS and scripts are served as text: they are read, not run, from here.

    disposition = "inline" if media_type.startswith(("text/", "application/json")) else "attachment"
    return FileResponse(
        path=full_path,
        media_type=media_type,
        filename=full_path.name,
        content_disposition_type=disposition,
        headers=_SAFE_HEADERS,
    )


@router.post("/api/repository/{repo_id}/generate-project-docs")
async def generate_project_documentation(
    repo_id: str,
    llm_model: Optional[str] = None,
    selected_docs: Optional[str] = None,
    commit: bool = True,
    background_tasks: BackgroundTasks = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Generate comprehensive project documentation (README, API, Architecture, etc.) using Qwen LLM.

    ``commit=false`` keeps the documents on the server without committing them.
    """
    repository = _repository(db, repo_id)
    _require_generate(db, current_user, repository)
    resolved_llm_model = llm_model or os.getenv("FOXNEST_PROJECT_DOCS_MODEL", "qwen2.5-coder:32b")
    return _queue_project_docs(repo_id, resolved_llm_model, selected_docs, commit,
                               current_user, background_tasks)
