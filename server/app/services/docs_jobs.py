"""Background documentation-generation jobs and their in-process job registry.

Generated documentation is kept in two places: under ``DOCS_DIR/<repo>`` on the server,
where the web client and ``/docs`` read it, and -- when the person asking may land
changes on the default branch directly -- committed under ``docs/`` on that branch, which
is what ``fox generate-docs`` pulls afterwards.

The commit used to be built by hand, and wrongly in four ways:

* its tree was the union of every file seen in the last 200 commits on *any* branch, so
  generating docs put deleted files back and copied other branches' files onto the
  default branch -- and it skipped binaries, large files and vendored directories while
  doing so, which deleted them from the branch;
* its parent was the newest commit in the repository, whichever branch that was on;
* it moved ``repository.head_commit_id`` without moving any branch, past branch
  protection, push rules, the activity log and webhooks;
* and it was attributed to the repository owner whoever asked, with no permission
  checked at all -- anyone signed in could generate for any repository.

Now the docs are generated from the default branch's tip, and committed the way every
other server-side write is (push rules, protection, compare-and-swap, the reference
service), as the person who asked -- or not at all, with the reason recorded, when they
could not have pushed that change themselves.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from database.crud import BranchCRUD, CommitCRUD, FileObjectCRUD, RepositoryCRUD, UserPermissionCRUD
from database.database import SessionLocal
from database.models import Commit, Repository, User

from app.config import DOCS_DIR
from app.services import audit, branch_ops, refs, repo_refs, server_hooks
from app.services.commit_graph import _get_commit_tree
from app.services.files import _is_vendor_path
from app.services.paths import normalize as normalize_path
from app.services.signing import sign_commit
import autodocs_v2
import docs_generator
import docx_utils
import project_docs_generator


DOCS_GENERATION_JOBS: Dict[str, Dict[str, Any]] = {}
DOCS_GENERATION_LOCK = threading.Lock()

#: What the last successful generation was made from and where it went. A dotfile, so
#: listings, archives and the commit leave it out.
MANIFEST_NAME = ".zanbeel-docs.json"

#: The document templates, where the generator itself looks for them (<repo>/docs/
#: templates). This was computed relative to this file, which pointed at app/docs/
#: once the code moved out of server.py: AutoDocs v2 then skipped every document as
#: "Missing template" and every generation from the server failed.
TEMPLATES_DIR = Path(os.getenv("FOXNEST_DOCS_TEMPLATES_DIR") or project_docs_generator._TEMPLATES_DIR)


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


#: The documents a caller may choose, by the key ``selected_docs`` takes (the same keys
#: ``fox generate-docs --docs`` accepts).
SELECTABLE_DOCUMENTS = (
    ("readme", "README.md"),
    ("api", "API Documentation"),
    ("architecture", "Architecture"),
    ("installation", "Installation Guide"),
    ("database", "Database Schema"),
    ("requirements", "Requirements (SRS)"),
    ("use_cases", "Use Cases"),
    ("release_notes", "Release Notes"),
    ("user_manual", "User Manual"),
    ("api_usage", "API Usage Guide"),
    ("database_er", "Database ER"),
)


def selectable_documents() -> List[Dict[str, str]]:
    return [{"key": key, "name": name} for key, name in SELECTABLE_DOCUMENTS]


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


# --- the manifest -------------------------------------------------------------------

def read_manifest(repo_id: str) -> Optional[Dict[str, Any]]:
    path = DOCS_DIR / repo_id / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_manifest(repo_id: str, data: Dict[str, Any]) -> None:
    root = DOCS_DIR / repo_id
    root.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST_NAME).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def generated_files(docs_root: Path) -> List[Path]:
    """Every generated file, without the manifest or any other dotfile."""
    if not docs_root.is_dir():
        return []
    out = []
    for path in sorted(docs_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(docs_root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        out.append(path)
    return out


# --- what the docs are generated from -------------------------------------------------

class DocsError(Exception):
    pass


def _docs_base(db: Session, repository: Repository) -> Tuple[Any, Commit]:
    """The default branch and its tip: what the documentation describes."""
    branch = repo_refs.default_branch(db, repository.id)
    if branch is None or not branch.head_commit_id:
        raise DocsError("Repository has no commits. Push code first.")
    commit = db.query(Commit).filter(Commit.id == branch.head_commit_id).first()
    if commit is None:
        raise DocsError(f"The tip of '{branch.name}' is missing.")
    return branch, commit


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


def _source_files(commit: Commit) -> Dict[str, bytes]:
    """The files of ``commit`` worth showing a documentation generator.

    These filters choose what the generator *reads*. They used to choose what the docs
    commit *kept* as well, which is how generating documentation deleted every image,
    archive and large file from the branch.
    """
    chosen: Dict[str, bytes] = {}
    for commit_file in commit.files:
        path = normalize_path(commit_file.file_path)
        if not path or path in chosen and commit_file.file_path != path:
            continue
        if any(path.startswith(prefix) for prefix in _SKIP_DIR_PREFIXES) or _is_vendor_path(path):
            continue
        if Path(path).suffix.lower() in _SKIP_BINARY_EXT:
            continue
        if not commit_file.file_object:
            continue
        content = commit_file.file_object.content
        if not content or len(content) > _MAX_FILE_BYTES:
            continue
        chosen[path] = content
    return chosen


def _materialise(files: Dict[str, bytes], prefix: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix=prefix))
    for rel_path, content in files.items():
        target = root / rel_path
        try:
            target.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_bytes(content)
        except OSError:
            pass
    return root


# --- landing the docs on the branch ---------------------------------------------------

def _landing_refusal(db: Session, repository: Repository, actor: Optional[User],
                     branch_name: str) -> Optional[str]:
    """Why ``actor`` could not push this change to ``branch_name`` themselves, if so.

    The same rules as ``POST /push``: a developer's pushes wait for review, the branch
    policy's scope applies, and only the owner or a team lead pushes directly.
    """
    if actor is None:
        return "There is no one to attribute the commit to."
    if repository.is_archived:
        return "The repository is archived."
    if (getattr(actor, "role", None) or "developer") == "developer":
        return (f"Changes you push to '{branch_name}' go through review, so the documents "
                "were not committed. They are available here, and a team lead can "
                "generate them again to commit them.")
    try:
        branch_ops.enforce(db, actor, repository, "push", branch_name)
    except branch_ops.BranchOpError as exc:
        return exc.message
    if repository.owner_id != actor.id and not UserPermissionCRUD.has_permission(
            db, actor.username, repository.id, "team_lead"):
        return f"Committing to '{branch_name}' directly needs team lead permission on this repository."
    return None


def _commit_docs(db: Session, repository: Repository, actor: Optional[User], branch_name: str,
                 docs_files: Dict[str, bytes], message: str) -> Dict[str, Any]:
    """Put ``docs_files`` on ``branch_name`` as one commit, the way any server write is.

    Generation takes minutes, so the branch may have moved meanwhile. The documents
    then land on top of whatever is there now -- they only touch ``docs/`` -- rather
    than a long job failing at the last step.
    """
    refusal = _landing_refusal(db, repository, actor, branch_name)
    if refusal:
        return {"committed": False, "reason": refusal}

    branch = BranchCRUD.get_branch(db, repository.id, branch_name)
    if branch is None or not branch.head_commit_id:
        return {"committed": False, "reason": f"'{branch_name}' no longer exists."}
    base_id = branch.head_commit_id
    base = db.query(Commit).filter(Commit.id == base_id).first()

    old_tree = _get_commit_tree(db, base_id)
    new_tree = dict(old_tree)
    new_tree.update(docs_files)
    if new_tree == old_tree:
        return {"committed": False, "unchanged": True,
                "reason": f"The documents on '{branch_name}' are already up to date."}

    try:
        server_hooks.check_server_write(db, repository, branch_name, base_id, new_tree, actor,
                                        message=message)
    except server_hooks.HookRejection as rejection:
        return {"committed": False,
                "reason": f"Push rule '{rejection.hook_name}' refused the documents: "
                          + "; ".join(rejection.reasons)}
    try:
        refs.check_reference_operation(db, repository=repository, actor=actor,
                                       branch_name=branch_name, operation=refs.OP_UPDATE,
                                       current_head=base_id)
    except refs.ReferenceError as exc:
        return {"committed": False, "reason": exc.message}

    # The branch's own rows, except the paths the documents replace: content that is not
    # documentation passes through untouched, by hash, without being read.
    replaced = set(docs_files)
    entries = [
        SimpleNamespace(file_path=cf.file_path, file_hash=cf.file_hash, file_size=cf.file_size)
        for cf in base.files if normalize_path(cf.file_path) not in replaced
    ]
    for path, content in sorted(docs_files.items()):
        stored = FileObjectCRUD.store_file_object(db, content)
        entries.append(SimpleNamespace(file_path=path, file_hash=stored.hash, file_size=len(content)))

    commit_id = hashlib.sha256(
        f"{repository.id}:docs:{base_id}:{datetime.utcnow().isoformat()}".encode()
    ).hexdigest()[:40]
    commit = CommitCRUD.create_commit_from_file_hashes(db, {
        "id": commit_id,
        "repository_id": repository.id,
        "author": actor.username,
        "message": message,
        "parents": [base_id],
        "timestamp": datetime.utcnow().isoformat(),
    }, entries)
    try:
        refs.update_reference(db, repository=repository, actor=actor, branch_name=branch_name,
                              new_commit_id=commit.id, expected_commit_id=base_id)
    except refs.ReferenceError as exc:
        # Stored, but no branch took it. create_commit_from_file_hashes() moved the
        # repository head to it; put that back where the branch is.
        db.rollback()
        current = BranchCRUD.get_branch(db, repository.id, branch_name)
        fresh = RepositoryCRUD.get_repository(db, repository.id)
        if fresh is not None and current is not None and current.is_default:
            fresh.head_commit_id = current.head_commit_id
            db.commit()
        return {"committed": False, "reason": exc.message}
    sign_commit(db, commit, actor.username, actor.id)
    return {"committed": True, "commit_id": commit.id, "branch": branch_name, "based_on": base_id}


def _valid_generated_doc(path: Path, content: bytes) -> bool:
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


def _docs_to_commit(docs_root: Path, validate: bool) -> Dict[str, bytes]:
    files: Dict[str, bytes] = {}
    for path in generated_files(docs_root):
        content = path.read_bytes()
        if validate and not _valid_generated_doc(path, content):
            continue
        files[f"docs/{path.relative_to(docs_root).as_posix()}"] = content
    return files


def _finish(db: Session, repository: Repository, actor: Optional[User], *, action: str,
            branch, source: Commit, docs_files: Dict[str, bytes], commit_docs: bool,
            message: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    """Commit (or not), write the manifest, record the activity."""
    with audit.background():
        if not commit_docs:
            landing = {"committed": False, "reason": "Generated without committing, as asked."}
        else:
            landing = _commit_docs(db, repository, actor, branch.name, docs_files, message)

        manifest = {
            "mode": extra.get("mode"),
            "generated_at": datetime.utcnow().isoformat(),
            "generated_by": getattr(actor, "username", None),
            "source_branch": branch.name,
            "source_commit": source.id,
            "llm_model": extra.get("llm_model"),
            "documents": sorted(docs_files),
            "committed": landing.get("committed", False),
            "commit_id": landing.get("commit_id"),
            "commit_skipped_reason": None if landing.get("committed") else landing.get("reason"),
        }
        _write_manifest(repository.id, manifest)

        user_id = getattr(actor, "id", None) or repository.owner_id
        description = (
            f"Generated {len(docs_files)} documentation file(s) from {source.id[:8]} on '{branch.name}'"
            + ("" if landing.get("committed") else f" (not committed: {landing.get('reason')})")
        )
        audit.record(db, user_id, action, repository_id=repository.id, description=description,
                     target_type="branch", target_id=branch.name,
                     commit_id=landing.get("commit_id") or source.id,
                     payload={"documents": len(docs_files), "committed": landing.get("committed", False),
                              "source_commit": source.id})
    return {"landing": landing, "manifest": manifest}


# --- per-symbol code documentation (generate-docs with a language filter) --------------

def _generate_repository_docs_sync(repo_id: str, lang_filter: Optional[str] = None,
                                   actor_id: Optional[int] = None, commit_docs: bool = True):
    db = SessionLocal()
    temp_dir = None
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise ValueError("Repository not found")
        actor = db.query(User).filter(User.id == actor_id).first() if actor_id else None

        try:
            branch, source_commit = _docs_base(db, repository)
        except DocsError as exc:
            return {"success": False, "error": str(exc), "repo_id": repo_id}

        sources = {path: content for path, content in _source_files(source_commit).items()}
        if not sources:
            return {
                "success": False,
                "error": f"No source code files found on '{branch.name}'.",
                "repo_id": repo_id
            }

        temp_dir = _materialise(sources, f"foxnest_docs_{repo_id}_")
        docs_output = DOCS_DIR / repo_id
        docs_output.mkdir(parents=True, exist_ok=True)

        results = docs_generator.generate_docs(
            str(temp_dir),
            output_dir=str(docs_output),
            lang_filter=lang_filter,
            verbose=False
        )

        docx_utils.convert_markdown_tree(docs_output)

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

        docs_files = _docs_to_commit(docs_output, validate=False)
        finished = _finish(
            db, repository, actor, action="generate_docs", branch=branch, source=source_commit,
            docs_files=docs_files, commit_docs=commit_docs,
            message=(f"Auto-generate documentation ({len(docs_files)} files, {coverage:.1f}% coverage)"
                     f"\n\nGenerated from {source_commit.id[:12]} on {branch.name}."),
            extra={"mode": "code"},
        )

        return {
            "success": True,
            "message": ("Documentation generated and committed to repository"
                        if finished["landing"].get("committed") else
                        "Documentation generated; not committed: " + str(finished["landing"].get("reason"))),
            "repo_id": repo_id,
            "docs_url": f"/api/repository/{repo_id}/docs",
            "coverage": round(coverage, 1),
            "files_analyzed": total_files,
            "languages": languages,
            "docs_files_added": len(docs_files) if finished["landing"].get("committed") else 0,
            "commit": finished["landing"],
            "source_commit": source_commit.id,
            "branch": branch.name,
            "results": results
        }
    finally:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        db.close()


def _run_generate_docs_job(repo_id: str, lang_filter: Optional[str] = None,
                           actor_id: Optional[int] = None, commit_docs: bool = True):
    _update_docs_job(
        repo_id,
        status="running",
        started_at=datetime.utcnow().isoformat(),
        finished_at=None,
        error=None
    )
    try:
        result = _generate_repository_docs_sync(repo_id, lang_filter, actor_id, commit_docs)
        if result.get("success"):
            _update_docs_job(
                repo_id,
                status="completed",
                finished_at=datetime.utcnow().isoformat(),
                result={
                    "coverage": result.get("coverage"),
                    "files_analyzed": result.get("files_analyzed"),
                    "languages": result.get("languages", []),
                    "docs_files_added": result.get("docs_files_added", 0),
                    "commit": result.get("commit"),
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


# --- project documentation (README, architecture, ... through the LLM) -----------------

def _generate_project_documentation_internal(repo_id: str, llm_model: str, db: Session,
                                             selected_docs: Optional[List[str]] = None,
                                             progress_callback=None,
                                             actor_id: Optional[int] = None,
                                             commit_docs: bool = True) -> Dict[str, Any]:
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    actor = db.query(User).filter(User.id == actor_id).first() if actor_id else None

    try:
        branch, source_commit = _docs_base(db, repository)
    except DocsError as exc:
        return {"success": False, "error": str(exc)}

    # The default branch's tip, as it is: a commit is a whole snapshot, so this is the
    # project's current state with nothing deleted brought back and nothing from other
    # branches mixed in.
    head_tree = _source_files(source_commit)
    if not head_tree:
        return {
            "success": False,
            "error": f"No readable source files found on '{branch.name}'.",
        }

    print(f"  📂 {branch.name} @ {source_commit.id[:8]}: {len(head_tree)} files for documentation")

    temp_dir = _materialise(head_tree, f"foxnest_project_docs_{repo_id}_")
    try:
        docs_output = DOCS_DIR / repo_id / "project"

        # Isolated AutoDocs v2 pipeline with safe fallback to legacy generator.
        use_v2 = os.getenv("FOXNEST_AUTODOCS_V2_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
        if use_v2:
            try:
                service = autodocs_v2.AutoDocsV2Service(templates_dir=TEMPLATES_DIR)
                # AutoDocs v2 selects by document name ("README.md"); callers send the
                # keys the CLI and the API take ("readme"). Handed the keys, it matched
                # none of them and wrote only the index for any selection at all.
                selected_names, _invalid = project_docs_generator.parse_selected_project_docs(selected_docs)
                results = service.generate(
                    project_path=temp_dir,
                    output_dir=docs_output,
                    llm_model=llm_model,
                    selected_docs=sorted(selected_names) if selected_names else None,
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
        # (This called two docx_utils functions that were never written; the AttributeError
        # was caught below, so no Word file was ever produced.) The index stays Markdown:
        # its links are what the web client follows between documents.
        if (results.get("stats") or {}).get("pipeline") == "autodocs_v2":
            try:
                docx_utils.convert_markdown_tree(docs_output, exclude_names=["index.md"])
            except Exception as conv_err:
                print(f"  ⚠ Project docs MD→DOCX (post-v2 only): {conv_err}")

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

        docs_files = _docs_to_commit(docs_root, validate=True)
        if not docs_files:
            return {
                "success": False,
                "error": "Generated documentation failed validation; no docs were committed.",
                "repo_id": repo_id,
                "skipped_docs": results.get("skipped_docs", [])
            }

        finished = _finish(
            db, repository, actor, action="generate_project_docs", branch=branch,
            source=source_commit, docs_files=docs_files, commit_docs=commit_docs,
            message=(f"Auto-generate project documentation ({len(docs_files)} files)"
                     f"\n\nGenerated from {source_commit.id[:12]} on {branch.name}."),
            extra={"mode": "project", "llm_model": llm_model},
        )

        return {
            "success": True,
            "message": ("Project documentation generated and committed to repository"
                        if finished["landing"].get("committed") else
                        "Project documentation generated; not committed: "
                        + str(finished["landing"].get("reason"))),
            "repo_id": repo_id,
            "llm_model": llm_model,
            "docs_url": f"/api/repository/{repo_id}/docs/project",
            "documents": [Path(f).name for f in results.get("generated_files", [])],
            "skipped_docs": results.get("skipped_docs", []),
            "stats": results.get("stats", {}),
            "selected_docs": results.get("stats", {}).get("selected_docs", ["all"]),
            "commit": finished["landing"],
            "source_commit": source_commit.id,
            "branch": branch.name,
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


def _run_generate_project_docs_job(repo_id: str, llm_model: str, selected_docs: Optional[List[str]] = None,
                                   actor_id: Optional[int] = None, commit_docs: bool = True):
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
            progress_callback=callback,
            actor_id=actor_id,
            commit_docs=commit_docs,
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
                    "commit": result.get("commit"),
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
