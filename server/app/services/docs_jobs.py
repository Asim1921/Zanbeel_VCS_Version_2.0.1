"""Background documentation-generation jobs and their in-process job registry."""

import os
import re
import hashlib
import shutil
import threading

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from database.crud import ActivityCRUD, CommitCRUD, FileObjectCRUD, RepositoryCRUD
from database.database import SessionLocal

from app.config import DOCS_DIR
from app.services.files import _is_vendor_path
import autodocs_v2
import docs_generator
import docx_utils
import project_docs_generator


DOCS_GENERATION_JOBS: Dict[str, Dict[str, Any]] = {}
DOCS_GENERATION_LOCK = threading.Lock()


def _update_docs_job(repo_id: str, **fields):
    with DOCS_GENERATION_LOCK:
        existing = DOCS_GENERATION_JOBS.get(repo_id, {})
        existing.update(fields)
        DOCS_GENERATION_JOBS[repo_id] = existing


_ALL_DOC_STEPS = [
    "Setup",
    "README.md",
    "API Documentation",
    "Architecture",
    "Installation Guide",
    "Database Schema",
    "Requirements (SRS)",
    "Use Cases",
    "Release Notes",
    "User Manual",
    "API Usage Guide",
    "Database ER",
]


def _parse_selected_docs_param(selected_docs_raw: Optional[str]) -> Optional[List[str]]:
    if not selected_docs_raw:
        return None
    selected = [token.strip() for token in str(selected_docs_raw).split(",") if token.strip()]
    return selected or None


def _resolve_progress_steps(selected_docs: Optional[List[str]]) -> tuple[List[str], Optional[List[str]], List[str]]:
    selected_names, invalid = project_docs_generator.parse_selected_project_docs(selected_docs)
    if selected_names is None:
        return list(_ALL_DOC_STEPS), ["all"], invalid

    steps = ["Setup"] + [step for step in _ALL_DOC_STEPS[1:] if step in selected_names]
    return steps, sorted(selected_names), invalid


def _make_docs_progress_callback(repo_id: str):
    """Return a callback that updates per-document progress in the job dict."""
    def callback(doc_name: str, status: str):
        now = datetime.utcnow().isoformat()
        with DOCS_GENERATION_LOCK:
            job = DOCS_GENERATION_JOBS.get(repo_id)
            if not job:
                return
            progress = job.get("docs_progress", [])
            for entry in progress:
                if entry["name"] == doc_name:
                    entry["status"] = status
                    if status == "generating" and not entry.get("started_at"):
                        entry["started_at"] = now
                    elif status in {"done", "skipped", "error"}:
                        entry["finished_at"] = now
                    break
            job["docs_progress"] = progress
            DOCS_GENERATION_JOBS[repo_id] = job
    return callback


def _generate_repository_docs_sync(repo_id: str, lang_filter: Optional[str] = None):
    db = SessionLocal()
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise ValueError("Repository not found")

        commits_desc = CommitCRUD.get_commits_by_repository(db, repo_id, limit=200)
        if not commits_desc:
            return {
                "success": False,
                "error": "Repository has no commits. Push code first.",
                "repo_id": repo_id
            }

        latest_commit = commits_desc[0]
        source_commit = None
        for commit in commits_desc:
            if any(not f.file_path.startswith("docs/") for f in commit.files):
                source_commit = commit
                break
        if not source_commit:
            return {
                "success": False,
                "error": "No source code files found in repository commits.",
                "repo_id": repo_id
            }

        temp_dir = Path("/tmp") / f"foxnest_docs_{repo_id}"
        temp_dir.mkdir(parents=True, exist_ok=True)

        for commit_file in source_commit.files:
            if commit_file.file_path.startswith("docs/"):
                continue
            if _is_vendor_path(commit_file.file_path):
                continue
            file_path = temp_dir / commit_file.file_path
            file_path.parent.mkdir(parents=True, exist_ok=True)

            if commit_file.file_object:
                with open(file_path, "wb") as f:
                    f.write(commit_file.file_object.content)

        docs_output = DOCS_DIR / repo_id
        docs_output.mkdir(parents=True, exist_ok=True)

        results = docs_generator.generate_docs(
            str(temp_dir),
            output_dir=str(docs_output),
            lang_filter=lang_filter,
            verbose=False
        )

        docx_utils.convert_markdown_tree(docs_output)

        shutil.rmtree(temp_dir, ignore_errors=True)

        if isinstance(results, dict) and results.get("error"):
            return {
                "success": False,
                "error": results.get("error"),
                "repo_id": repo_id
            }

        languages = []
        total_files = 0
        total_symbols = 0
        total_documented = 0
        if isinstance(results, dict):
            languages = sorted(results.keys())
            for stats in results.values():
                if not isinstance(stats, dict):
                    continue
                total_files += stats.get("files", 0)
                documented = stats.get("documented", 0)
                total_documented += documented
                if "symbols" in stats:
                    total_symbols += stats.get("symbols", 0)
                else:
                    total_symbols += documented + stats.get("undocumented", 0)

        coverage = (total_documented / total_symbols * 100) if total_symbols > 0 else 0

        doc_files = []
        file_entries = []

        for commit_file in source_commit.files:
            if commit_file.file_path.startswith("docs/"):
                continue
            file_size = commit_file.file_size
            if file_size is None and commit_file.file_object:
                file_size = commit_file.file_object.size
            file_entries.append(SimpleNamespace(
                file_path=commit_file.file_path,
                file_hash=commit_file.file_hash,
                file_size=file_size or 0
            ))

        for doc_file_path in docs_output.rglob("*"):
            if doc_file_path.is_file():
                rel_path = doc_file_path.relative_to(docs_output)
                with open(doc_file_path, "rb") as f:
                    content = f.read()

                file_obj = FileObjectCRUD.store_file_object(db, content)
                doc_path = f"docs/{rel_path}"

                doc_files.append(doc_path)
                file_entries.append(SimpleNamespace(
                    file_path=doc_path,
                    file_hash=file_obj.hash,
                    file_size=len(content)
                ))

        if doc_files:
            commit_id = hashlib.sha256(
                f"{repo_id}:docs:{datetime.now().isoformat()}".encode()
            ).hexdigest()[:40]

            commit_data = {
                "id": commit_id,
                "repository_id": repo_id,
                "author": repository.owner.username,
                "message": f"Auto-generate documentation ({len(doc_files)} files, {coverage:.1f}% coverage)",
                "parent": latest_commit.id,
                "timestamp": datetime.now().isoformat()
            }

            CommitCRUD.create_commit_from_file_hashes(db, commit_data, file_entries)

        ActivityCRUD.create_activity(
            db=db,
            user_id=repository.owner_id,
            activity_type="generate_docs",
            description=f"Generated docs for repo {repo_id}. Languages: {', '.join(languages)}" if languages else f"Generated docs for repo {repo_id}.",
            repository_id=repo_id
        )

        return {
            "success": True,
            "message": "Documentation generated and committed to repository",
            "repo_id": repo_id,
            "docs_url": f"/api/repository/{repo_id}/docs",
            "coverage": round(coverage, 1),
            "files_analyzed": total_files,
            "languages": languages,
            "docs_files_added": len(doc_files),
            "results": results
        }
    except Exception as e:
        temp_dir = Path("/tmp") / f"foxnest_docs_{repo_id}"
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        db.close()


def _run_generate_docs_job(repo_id: str, lang_filter: Optional[str] = None):
    _update_docs_job(
        repo_id,
        status="running",
        started_at=datetime.utcnow().isoformat(),
        finished_at=None,
        error=None
    )
    try:
        result = _generate_repository_docs_sync(repo_id, lang_filter)
        if result.get("success"):
            _update_docs_job(
                repo_id,
                status="completed",
                finished_at=datetime.utcnow().isoformat(),
                result={
                    "coverage": result.get("coverage"),
                    "files_analyzed": result.get("files_analyzed"),
                    "languages": result.get("languages", []),
                    "docs_files_added": result.get("docs_files_added", 0)
                },
                error=None
            )
        else:
            _update_docs_job(
                repo_id,
                status="failed",
                finished_at=datetime.utcnow().isoformat(),
                error=result.get("error", "Documentation generation failed")
            )
    except Exception as e:
        _update_docs_job(
            repo_id,
            status="failed",
            finished_at=datetime.utcnow().isoformat(),
            error=str(e)
        )


def _generate_project_documentation_internal(repo_id: str, llm_model: str, db: Session, selected_docs: Optional[List[str]] = None, progress_callback=None) -> Dict[str, Any]:
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    commits_desc = CommitCRUD.get_commits_by_repository(db, repo_id, limit=200)
    if not commits_desc:
        return {
            "success": False,
            "error": "Repository has no commits. Push code first."
        }

    def _is_source_extension(path: str) -> bool:
        return Path(path).suffix.lower() in {
            ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts", ".go", ".rs",
            ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".php", ".rb", ".swift", ".sql",
            ".sh", ".ps1", ".yml", ".yaml", ".toml", ".json", ".md"
        }

    def _score_commit(commit_obj) -> tuple:
        non_doc_files = [
            f for f in commit_obj.files
            if f.file_path and not f.file_path.startswith("docs/") and f.file_object
        ]
        source_files = [f for f in non_doc_files if _is_source_extension(f.file_path)]

        approx_lines = 0
        sampled = 0
        for f in source_files:
            if sampled >= 120:
                break
            try:
                text = f.file_object.content.decode("utf-8", errors="ignore")
                approx_lines += len(text.splitlines())
            except Exception:
                continue
            sampled += 1

        return (len(source_files), approx_lines, len(non_doc_files))

    source_commit = None
    candidate_commits = [
        c for c in commits_desc
        if any(f.file_path and not f.file_path.startswith("docs/") for f in c.files)
    ]
    if candidate_commits:
        source_commit = max(candidate_commits, key=_score_commit)

    if not source_commit:
        return {
            "success": False,
            "error": "No source code files found in repository commits."
        }

    # ── Reconstruct the full HEAD file tree ──────────────────────────────────
    # Each commit only stores the files that changed in that push (delta model).
    # To get the COMPLETE project state at HEAD we must walk ALL commits newest→
    # oldest and keep the first (= latest) version of every path seen.
    _SKIP_DIR_PREFIXES = (
        "docs/", "node_modules/", "vendor/", ".git/", "venv/", ".venv/",
        "__pycache__/", "build/", "dist/", ".mypy_cache/", ".pytest_cache/",
        ".tox/", ".idea/", ".vscode/", ".next/", ".nuxt/", "_archived_",
        ".fox/",
    )
    _SKIP_BINARY_EXT = {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
        ".pdf", ".docx", ".xlsx", ".pptx", ".doc", ".xls",
        ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
        ".exe", ".dll", ".so", ".dylib", ".lib", ".a",
        ".pyc", ".pyo", ".class", ".o",
        ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".mp4", ".mp3", ".avi", ".mov", ".wav",
        ".db", ".sqlite", ".sqlite3",
    }
    _MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB per file

    # path → (content_bytes, file_hash, file_size)
    head_tree: Dict[str, tuple] = {}
    for commit in commits_desc:          # newest → oldest (already sorted)
        for cf in commit.files:
            path = cf.file_path
            if not path or path in head_tree:
                continue
            if any(path.startswith(pfx) for pfx in _SKIP_DIR_PREFIXES):
                continue
            if Path(path).suffix.lower() in _SKIP_BINARY_EXT:
                continue
            if cf.file_object and cf.file_object.content:
                raw = cf.file_object.content
                if len(raw) > _MAX_FILE_BYTES:
                    continue
                head_tree[path] = (raw, cf.file_hash, cf.file_size or len(raw))

    if not head_tree:
        return {
            "success": False,
            "error": "No readable source files found in repository history.",
        }

    print(f"  📂 HEAD tree: {len(head_tree)} files collected across all commits")

    temp_dir = Path("/tmp") / f"foxnest_project_docs_{repo_id}"
    # Always wipe the temp dir so stale files from a previous run don't pollute
    if temp_dir.exists():
        shutil.rmtree(temp_dir, ignore_errors=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        for rel_path, (content_bytes, _fhash, _fsize) in head_tree.items():
            file_path = temp_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(file_path, "wb") as f:
                    f.write(content_bytes)
            except Exception:
                pass

        docs_output = DOCS_DIR / repo_id / "project"

        # Isolated AutoDocs v2 pipeline with safe fallback to legacy generator.
        use_v2 = os.getenv("FOXNEST_AUTODOCS_V2_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
        if use_v2:
            try:
                service = autodocs_v2.AutoDocsV2Service(
                    templates_dir=Path(__file__).resolve().parent.parent / "docs" / "templates"
                )
                results = service.generate(
                    project_path=temp_dir,
                    output_dir=docs_output,
                    llm_model=llm_model,
                    selected_docs=selected_docs,
                    project_name_override=repository.name,
                )
            except Exception as v2_err:
                print(f"  ⚠ AutoDocs v2 failed, falling back to legacy generator: {v2_err}")
                results = project_docs_generator.generate_project_docs(
                    str(temp_dir),
                    output_dir=str(docs_output),
                    llm_model=llm_model,
                    project_name=repository.name,
                    selected_docs=selected_docs,
                    progress_callback=progress_callback
                )
        else:
            results = project_docs_generator.generate_project_docs(
                str(temp_dir),
                output_dir=str(docs_output),
                llm_model=llm_model,
                project_name=repository.name,
                selected_docs=selected_docs,
                progress_callback=progress_callback
            )

        if results.get("error"):
            return {
                "success": False,
                "error": results.get("error"),
                "repo_id": repo_id,
                "skipped_docs": results.get("skipped_docs", [])
            }

        if not results.get("generated_files"):
            return {
                "success": False,
                "error": "Failed to generate project documentation",
                "repo_id": repo_id,
                "skipped_docs": results.get("skipped_docs", [])
            }

        # Markdown → Word only after AutoDocs v2 has finished: Qwen, templates, repo-aware context,
        # and per-document validation/retries. Do not convert partial or failed runs.
        if (results.get("stats") or {}).get("pipeline") == "autodocs_v2":
            try:
                docx_utils.convert_project_autodocs_md_to_docx(docs_output)
                docx_utils.rewrite_autodocs_project_index(docs_output, repository.name)
            except Exception as conv_err:
                print(f"  ⚠ Project docs MD→DOCX (post-v2 only): {conv_err}")

        doc_files = []
        file_entries_by_path = {}

        def add_file_entry(path, file_hash, file_size):
            file_entries_by_path[path] = SimpleNamespace(
                file_path=path,
                file_hash=file_hash,
                file_size=file_size
            )

        # Register all HEAD-tree source files in the commit entries
        for rel_path, (_content, fhash, fsize) in head_tree.items():
            add_file_entry(rel_path, fhash, fsize)

        docs_root = DOCS_DIR / repo_id
        index_path = docs_root / "index.md"
        project_index_rel = "project/index.md"
        if index_path.exists():
            with open(index_path, "r", encoding="utf-8", errors="ignore") as f:
                index_content = f.read()
        else:
            index_content = "# Documentation\n\n"
            index_content += f"*Auto-generated by FoxNest on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"

        if project_index_rel not in index_content:
            index_content += "## Project Documentation\n\n"
            index_content += f"- [Project Docs]({project_index_rel})\n\n"

        api_index = docs_root / "api" / "index.md"
        if "api/index.md" not in index_content and api_index.exists():
            index_content += "## API Documentation\n\n"
            index_content += "- [API Reference](api/index.md)\n\n"

        dep_index = docs_root / "dependencies" / "index.md"
        if "dependencies/index.md" not in index_content and dep_index.exists():
            index_content += "## Dependencies\n\n"
            index_content += "- [Dependencies](dependencies/index.md)\n\n"

        index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(index_content)

        def _is_valid_generated_doc(path: Path, content: bytes) -> bool:
            if not content or len(content) < 64:
                return False
            if path.suffix.lower() == ".md":
                text_content = content.decode("utf-8", errors="ignore")
                if len(text_content.strip()) < 120:
                    return False
                if "## " not in text_content and "# " not in text_content:
                    return False
                if re.search(r"\[(Project Name|Your Name)\]|TODO|YYYY-MM-DD", text_content, flags=re.IGNORECASE):
                    return False
            return True

        valid_doc_files = []
        if docs_root.exists():
            for doc_file_path in docs_root.rglob("*"):
                if not doc_file_path.is_file():
                    continue
                rel_path = doc_file_path.relative_to(docs_root)
                with open(doc_file_path, "rb") as f:
                    content = f.read()
                if _is_valid_generated_doc(doc_file_path, content):
                    valid_doc_files.append((rel_path, content))

        for rel_path, content in valid_doc_files:
            file_obj = FileObjectCRUD.store_file_object(db, content)
            doc_path = f"docs/{rel_path}"
            doc_files.append(doc_path)
            add_file_entry(doc_path, file_obj.hash, len(content))

        file_entries = list(file_entries_by_path.values())

        if doc_files:
            commit_id = hashlib.sha256(
                f"{repo_id}:project_docs:{datetime.now().isoformat()}".encode()
            ).hexdigest()[:40]

            commit_data = {
                "id": commit_id,
                "repository_id": repo_id,
                "author": repository.owner.username,
                "message": f"Auto-generate project documentation ({len(doc_files)} files)",
                "parent": commits_desc[0].id,
                "timestamp": datetime.now().isoformat()
            }

            CommitCRUD.create_commit_from_file_hashes(db, commit_data, file_entries)
        else:
            return {
                "success": False,
                "error": "Generated documentation failed validation; no docs were committed.",
                "repo_id": repo_id,
                "skipped_docs": results.get("skipped_docs", [])
            }

        ActivityCRUD.create_activity(
            db=db,
            user_id=repository.owner_id,
            activity_type="generate_project_docs",
            description=f"Generated comprehensive project docs for repo {repo_id}",
            repository_id=repo_id
        )

        return {
            "success": True,
            "message": "Project documentation generated and committed to repository",
            "repo_id": repo_id,
            "llm_model": llm_model,
            "docs_url": f"/api/repository/{repo_id}/docs/project",
            "documents": [Path(f).name for f in results.get("generated_files", [])],
            "skipped_docs": results.get("skipped_docs", []),
            "stats": results.get("stats", {}),
            "selected_docs": results.get("stats", {}).get("selected_docs", ["all"]),
            "project_info": {
                "name": results.get("project_info", {}).get("name", repository.name),
                "languages": list(results.get("project_info", {}).get("languages", [])),
                "frameworks": list(results.get("project_info", {}).get("frameworks", [])),
                "files": results.get("project_info", {}).get("file_count", 0),
                "lines": results.get("project_info", {}).get("line_count", 0),
            }
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _run_generate_project_docs_job(repo_id: str, llm_model: str, selected_docs: Optional[List[str]] = None):
    progress_steps, selected_doc_names, invalid = _resolve_progress_steps(selected_docs)
    if invalid:
        _update_docs_job(
            repo_id,
            status="failed",
            mode="project",
            llm_model=llm_model,
            finished_at=datetime.utcnow().isoformat(),
            error=f"Invalid doc names: {', '.join(invalid)}"
        )
        return

    _update_docs_job(
        repo_id,
        status="running",
        mode="project",
        llm_model=llm_model,
        selected_docs=selected_doc_names,
        started_at=datetime.utcnow().isoformat(),
        finished_at=None,
        error=None,
        docs_progress=[
            {"name": step, "status": "pending", "started_at": None, "finished_at": None}
            for step in progress_steps
        ]
    )
    db = SessionLocal()
    callback = _make_docs_progress_callback(repo_id)
    try:
        result = _generate_project_documentation_internal(
            repo_id=repo_id,
            llm_model=llm_model,
            db=db,
            selected_docs=selected_docs,
            progress_callback=callback
        )

        if isinstance(result, dict) and result.get("success"):
            _update_docs_job(
                repo_id,
                status="completed",
                mode="project",
                llm_model=result.get("llm_model", llm_model),
                finished_at=datetime.utcnow().isoformat(),
                result={
                    "documents": result.get("documents", []),
                    "docs_url": result.get("docs_url"),
                    "project_info": result.get("project_info", {}),
                    "skipped_docs": result.get("skipped_docs", []),
                    "selected_docs": result.get("selected_docs", selected_doc_names or ["all"]),
                },
                error=None
            )
        else:
            _update_docs_job(
                repo_id,
                status="failed",
                mode="project",
                llm_model=llm_model,
                finished_at=datetime.utcnow().isoformat(),
                error=(result or {}).get("error", "Project docs generation failed") if isinstance(result, dict) else "Project docs generation failed"
            )
    except Exception as e:
        _update_docs_job(
            repo_id,
            status="failed",
            mode="project",
            llm_model=llm_model,
            finished_at=datetime.utcnow().isoformat(),
            error=str(e)
        )
    finally:
        db.close()
