"""Repository file browsing, download, history and commit comparison."""

import base64

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from database.crud import CommitCRUD, RepositoryCRUD
from database.database import get_db
from database.models import Commit, CommitFile, FileLineage, PendingCommitFile, User

from app.config import DIFF_MAX_BYTES, logger
from app.core.dependencies import get_current_user
from app.core.pagination import _decode_cursor, _encode_cursor
from app.core.permissions import _require_repository_read_access
from app.services.blame import blame_file
from app.services.branches import _get_commit_for_branch, _resolve_branch
from app.services.commit_graph import _collect_reachable_commits, _get_commit_tree
from app.services.diff import _build_side_by_side_diff
from app.services.files import _build_files_payload_from_commit, _is_binary_or_large
from app.services.merge import _try_decode_text


router = APIRouter()


@router.get("/api/repository/{repo_id}/files")
async def get_repository_files(
    repo_id: str,
    include_content: bool = True,
    branch: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get all files in repository from latest commit or pending commit"""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view repository files")
    
    try:
        files_dict = {}
        folders_set = set()
        commit_id = None
        commit_message = None
        is_pending = False
        
        selected_commit = _get_commit_for_branch(db, repository, branch)
        if selected_commit:
            commit_id = selected_commit.id
            commit_message = selected_commit.message
            payload = _build_files_payload_from_commit(selected_commit, include_content)
            files_dict = payload["files"]
            folders_set = set(payload["folders"])
        
        # If no approved commits, check for pending commits
        if not files_dict and not branch:
            from database.models import PendingCommit, FileObject
            pending_commits = db.query(PendingCommit).filter(
                PendingCommit.repository_id == repo_id,
                PendingCommit.status == 'pending'
            ).order_by(PendingCommit.created_at.desc()).all()
            
            if pending_commits:
                # Get files from the latest pending commit
                latest_pending = pending_commits[0]
                commit_id = latest_pending.id
                commit_message = latest_pending.message
                is_pending = True
                
                if latest_pending.files:
                    # New format: pending commit files stored by hash
                    for pending_file in latest_pending.files:
                        file_path = pending_file.file_path.replace('\\', '/')
                        path_parts = file_path.split('/')
                        for i in range(len(path_parts) - 1):
                            folder_path = '/'.join(path_parts[:i+1])
                            folders_set.add(folder_path)
                        
                        file_object = db.query(FileObject).filter(FileObject.hash == pending_file.file_hash).first()
                        if not file_object:
                            continue
                        
                        file_entry = {
                            "size": pending_file.file_size,
                            "hash": pending_file.file_hash,
                            "mime_type": file_object.mime_type,
                            "is_binary": None
                        }

                        if include_content:
                            try:
                                content = file_object.content.decode('utf-8')
                                is_binary = False
                            except UnicodeDecodeError:
                                content = base64.b64encode(file_object.content).decode('utf-8')
                                is_binary = True

                            file_entry["content"] = content
                            file_entry["is_binary"] = is_binary

                        files_dict[file_path] = file_entry
                else:
                    # Legacy format: files_data contains base64 content
                    import json
                    files_data = json.loads(latest_pending.files_data) if latest_pending.files_data else {}
                    
                    for file_path, file_content_b64 in files_data.items():
                        file_path = file_path.replace('\\', '/')
                        # Extract folder structure
                        path_parts = file_path.split('/')
                        for i in range(len(path_parts) - 1):
                            folder_path = '/'.join(path_parts[:i+1])
                            folders_set.add(folder_path)
                        
                        try:
                            # Decode base64 content
                            content_bytes = base64.b64decode(file_content_b64.encode())

                            file_entry = {
                                "size": len(content_bytes),
                                "hash": None,
                                "mime_type": None,
                                "is_binary": None
                            }

                            if include_content:
                                try:
                                    content = content_bytes.decode('utf-8')
                                    is_binary = False
                                except UnicodeDecodeError:
                                    content = file_content_b64
                                    is_binary = True

                                file_entry["content"] = content
                                file_entry["is_binary"] = is_binary

                            files_dict[file_path] = file_entry
                        except Exception as e:
                            print(f"Error processing file {file_path}: {e}")
                            continue
        
        if not files_dict:
            return {
                "success": True,
                "files": {},
                "folders": [],
                "message": "Repository is empty"
            }
        
        return {
            "success": True,
            "files": files_dict,
            "folders": sorted(list(folders_set)),
            "commit_id": commit_id,
            "commit_message": commit_message,
            "is_pending": is_pending
        }
    
    except Exception as e:
        import traceback
        print(f"Error in get_repository_files: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/repository/{repo_id}/file")
async def get_repository_file(
    repo_id: str,
    path: str,
    branch: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a single file from the latest commit or pending commit"""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view repository file")

    normalized_path = path.replace('\\', '/')
    path_variants = {normalized_path, normalized_path.replace('/', '\\')}

    try:
        selected_commit = _get_commit_for_branch(db, repository, branch)
        if selected_commit:
                commit_file = db.query(CommitFile).filter(
                    CommitFile.commit_id == selected_commit.id,
                    CommitFile.file_path.in_(list(path_variants))
                ).first()

                if commit_file and commit_file.file_object:
                    try:
                        content = commit_file.file_object.content.decode('utf-8')
                        is_binary = False
                    except UnicodeDecodeError:
                        content = base64.b64encode(commit_file.file_object.content).decode('utf-8')
                        is_binary = True

                    return {
                        "success": True,
                        "file": {
                            "path": normalized_path,
                            "content": content,
                            "is_binary": is_binary,
                            "size": commit_file.file_size,
                            "hash": commit_file.file_hash,
                            "mime_type": commit_file.file_object.mime_type
                        }
                    }

        # Fall back to latest pending commit only when branch is not explicitly selected
        from database.models import PendingCommit, FileObject
        pending_commits = []
        if not branch:
            pending_commits = db.query(PendingCommit).filter(
                PendingCommit.repository_id == repo_id,
                PendingCommit.status == 'pending'
            ).order_by(PendingCommit.created_at.desc()).all()

        if pending_commits:
            latest_pending = pending_commits[0]

            if latest_pending.files:
                pending_file = db.query(PendingCommitFile).filter(
                    PendingCommitFile.pending_commit_id == latest_pending.id,
                    PendingCommitFile.file_path.in_(list(path_variants))
                ).first()

                if pending_file:
                    file_object = db.query(FileObject).filter(FileObject.hash == pending_file.file_hash).first()
                    if file_object:
                        try:
                            content = file_object.content.decode('utf-8')
                            is_binary = False
                        except UnicodeDecodeError:
                            content = base64.b64encode(file_object.content).decode('utf-8')
                            is_binary = True

                        return {
                            "success": True,
                            "file": {
                                "path": normalized_path,
                                "content": content,
                                "is_binary": is_binary,
                                "size": pending_file.file_size,
                                "hash": pending_file.file_hash,
                                "mime_type": file_object.mime_type
                            }
                        }
            else:
                import json
                files_data = json.loads(latest_pending.files_data) if latest_pending.files_data else {}
                file_content_b64 = None
                for file_path, file_content in files_data.items():
                    if file_path.replace('\\', '/') == normalized_path:
                        file_content_b64 = file_content
                        break

                if file_content_b64:
                    content_bytes = base64.b64decode(file_content_b64.encode())
                    try:
                        content = content_bytes.decode('utf-8')
                        is_binary = False
                    except UnicodeDecodeError:
                        content = file_content_b64
                        is_binary = True

                    return {
                        "success": True,
                        "file": {
                            "path": normalized_path,
                            "content": content,
                            "is_binary": is_binary,
                            "size": len(content_bytes),
                            "hash": None,
                            "mime_type": None
                        }
                    }

        raise HTTPException(status_code=404, detail="File not found")

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"Error in get_repository_file: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/repository/{repo_id}/file/download")
async def download_repository_file(
    repo_id: str,
    path: str,
    branch: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download a single file from the latest commit or pending commit"""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "download repository file")

    normalized_path = path.replace('\\', '/')
    path_variants = {normalized_path, normalized_path.replace('/', '\\')}
    filename = normalized_path.split('/')[-1]

    try:
        selected_commit = _get_commit_for_branch(db, repository, branch)
        if selected_commit:
                commit_file = db.query(CommitFile).filter(
                    CommitFile.commit_id == selected_commit.id,
                    CommitFile.file_path.in_(list(path_variants))
                ).first()

                if commit_file and commit_file.file_object:
                    content_bytes = commit_file.file_object.content
                    media_type = commit_file.file_object.mime_type or 'application/octet-stream'
                    headers = {
                        "Content-Disposition": f'attachment; filename="{filename}"'
                    }
                    return Response(content=content_bytes, media_type=media_type, headers=headers)

        # Fall back to latest pending commit only when branch is not explicitly selected
        from database.models import PendingCommit, FileObject
        pending_commits = []
        if not branch:
            pending_commits = db.query(PendingCommit).filter(
                PendingCommit.repository_id == repo_id,
                PendingCommit.status == 'pending'
            ).order_by(PendingCommit.created_at.desc()).all()

        if pending_commits:
            latest_pending = pending_commits[0]

            if latest_pending.files:
                pending_file = db.query(PendingCommitFile).filter(
                    PendingCommitFile.pending_commit_id == latest_pending.id,
                    PendingCommitFile.file_path.in_(list(path_variants))
                ).first()

                if pending_file:
                    file_object = db.query(FileObject).filter(FileObject.hash == pending_file.file_hash).first()
                    if file_object:
                        media_type = file_object.mime_type or 'application/octet-stream'
                        headers = {
                            "Content-Disposition": f'attachment; filename="{filename}"'
                        }
                        return Response(content=file_object.content, media_type=media_type, headers=headers)
            else:
                import json
                files_data = json.loads(latest_pending.files_data) if latest_pending.files_data else {}
                file_content_b64 = None
                for file_path, file_content in files_data.items():
                    if file_path.replace('\\', '/') == normalized_path:
                        file_content_b64 = file_content
                        break

                if file_content_b64:
                    content_bytes = base64.b64decode(file_content_b64.encode())
                    headers = {
                        "Content-Disposition": f'attachment; filename="{filename}"'
                    }
                    return Response(content=content_bytes, media_type='application/octet-stream', headers=headers)

        raise HTTPException(status_code=404, detail="File not found")

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(f"Error in download_repository_file: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/repository/{repo_id}/file-history")
async def get_file_history(
    repo_id: str,
    path: str,
    branch: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
    follow_renames: bool = False,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view file history")

    normalized_path = path.replace('\\', '/')
    page_size = max(1, min(limit, 500))

    commits = db.query(Commit).filter(Commit.repository_id == repo_id).order_by(Commit.created_at.asc(), Commit.id.asc()).all()
    if branch:
        resolved_branch = _resolve_branch(db, repo_id, branch)
        reachable = set(_collect_reachable_commits(db, resolved_branch.head_commit_id))
        commits = [commit for commit in commits if commit.id in reachable]

    tracked_paths = {normalized_path}
    if follow_renames:
        changed = True
        while changed:
            changed = False
            lineage_links = db.query(FileLineage).filter(
                FileLineage.repository_id == repo_id,
                FileLineage.old_path.in_(list(tracked_paths))
            ).all()
            for link in lineage_links:
                if link.new_path not in tracked_paths:
                    tracked_paths.add(link.new_path)
                    changed = True

            reverse_links = db.query(FileLineage).filter(
                FileLineage.repository_id == repo_id,
                FileLineage.new_path.in_(list(tracked_paths))
            ).all()
            for link in reverse_links:
                if link.old_path not in tracked_paths:
                    tracked_paths.add(link.old_path)
                    changed = True

    tracked_variants = set()
    for tracked in tracked_paths:
        tracked_variants.add(tracked)
        tracked_variants.add(tracked.replace('/', '\\'))

    commit_files = db.query(CommitFile).join(Commit, Commit.id == CommitFile.commit_id).filter(
        Commit.repository_id == repo_id,
        CommitFile.file_path.in_(list(tracked_variants))
    ).all()

    files_by_commit: Dict[str, List[CommitFile]] = {}
    for commit_file in commit_files:
        if branch and commit_file.commit_id not in reachable:
            continue
        files_by_commit.setdefault(commit_file.commit_id, []).append(commit_file)

    versions: List[Dict[str, Any]] = []
    last_included_hash: Optional[str] = None
    last_included_path: Optional[str] = None

    for commit in commits:
        commit_files_for_commit = files_by_commit.get(commit.id, [])
        if not commit_files_for_commit:
            continue

        commit_file = sorted(commit_files_for_commit, key=lambda item: item.file_path)[0]
        observed_path = commit_file.file_path.replace('\\', '/')

        # Keep per-file history focused on actual file revisions.
        if commit_file.file_hash == last_included_hash and observed_path == last_included_path:
            continue

        previous_hash = last_included_hash
        change_type = "added" if previous_hash is None else "modified"

        versions.append({
            "version_number": len(versions) + 1,
            "commit_id": commit.id,
            "timestamp": commit.created_at.isoformat() if commit.created_at else None,
            "author": commit.author.username if commit.author else None,
            "message": commit.message,
            "file_path": normalized_path,
            "observed_path": observed_path,
            "file_name": normalized_path.split('/')[-1],
            "file_hash": commit_file.file_hash,
            "file_size": commit_file.file_size,
            "change_type": change_type,
            "changed_from_hash": previous_hash,
            "lineage": {
                "requested_path": normalized_path,
                "observed_path": observed_path,
                "renamed": observed_path != normalized_path
            }
        })
        last_included_hash = commit_file.file_hash
        last_included_path = observed_path

    versions.reverse()

    cursor_data = _decode_cursor(cursor)
    start = int(cursor_data.get("offset", 0) or 0)
    if start < 0:
        start = 0
    page_versions = versions[start:start + page_size]
    next_offset = start + page_size
    has_more = next_offset < len(versions)
    next_cursor = _encode_cursor({"offset": next_offset}) if has_more else None

    logger.info(
        "file_history repo=%s path=%s branch=%s versions=%d page_size=%d start=%d",
        repo_id,
        normalized_path,
        branch or "main",
        len(versions),
        page_size,
        start,
    )
    return {
        "success": True,
        "path": normalized_path,
        "history_scope": "branch" if branch else "repository",
        "follow_renames": follow_renames,
        "lineage_paths": sorted(list(tracked_paths)) if follow_renames else [normalized_path],
        "versions": page_versions,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }

@router.get("/api/repository/{repo_id}/compare")
async def compare_commits(
    repo_id: str,
    from_commit: str,
    to_commit: str,
    path: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "compare commits")

    from_obj = CommitCRUD.get_commit(db, from_commit)
    to_obj = CommitCRUD.get_commit(db, to_commit)
    if not from_obj or from_obj.repository_id != repo_id:
        raise HTTPException(status_code=404, detail="from_commit not found in repository")
    if not to_obj or to_obj.repository_id != repo_id:
        raise HTTPException(status_code=404, detail="to_commit not found in repository")

    from_tree = _get_commit_tree(db, from_commit)
    to_tree = _get_commit_tree(db, to_commit)
    paths = set(from_tree.keys()) | set(to_tree.keys())
    if path:
        normalized_path = path.replace('\\', '/')
        paths = {normalized_path}

    comparisons: List[Dict[str, Any]] = []
    for file_path in sorted(paths):
        previous_content = from_tree.get(file_path)
        current_content = to_tree.get(file_path)
        if previous_content == current_content:
            continue

        status = "modified"
        if previous_content is None:
            status = "added"
        elif current_content is None:
            status = "removed"

        previous_text = _try_decode_text(previous_content)
        current_text = _try_decode_text(current_content)
        is_binary_or_large = _is_binary_or_large(previous_content) or _is_binary_or_large(current_content)

        file_diff: Dict[str, Any] = {
            "file_path": file_path,
            "status": status,
            "is_binary": is_binary_or_large,
            "previous": {
                "exists": previous_content is not None,
                "size": len(previous_content) if previous_content is not None else 0
            },
            "current": {
                "exists": current_content is not None,
                "size": len(current_content) if current_content is not None else 0
            }
        }

        if not is_binary_or_large and previous_text is not None and current_text is not None:
            side_by_side = _build_side_by_side_diff(previous_text, current_text)
            file_diff["diff"] = side_by_side
            file_diff["previous"]["content"] = previous_text
            file_diff["current"]["content"] = current_text
        else:
            file_diff["diff"] = {
                "rows": [],
                "stats": {"added": 0, "removed": 0, "unchanged": 0},
                "truncated": len(previous_content or b"") > DIFF_MAX_BYTES or len(current_content or b"") > DIFF_MAX_BYTES,
                "reason": "binary_or_large"
            }

        comparisons.append(file_diff)

    return {
        "success": True,
        "from_commit": from_commit,
        "to_commit": to_commit,
        "files": comparisons
    }


@router.get("/api/repository/{repo_id}/blame")
async def blame_file_lines(
    repo_id: str,
    path: str,
    commit: Optional[str] = None,
    branch: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Attribute each line of a file to the commit that last changed it.

    Resolves against an explicit `commit`, else the head of `branch`, else the default
    branch. Renames are followed, so blame does not stop at the commit where a file moved.
    """
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view blame")

    start = commit
    if not start:
        target = _get_commit_for_branch(db, repository, branch)
        if not target:
            raise HTTPException(status_code=404, detail="No commits on this branch")
        start = target.id

    result = blame_file(db, repo_id, path, start)

    if result.get("not_found"):
        raise HTTPException(
            status_code=404, detail=f"'{path}' does not exist at commit {start[:12]}"
        )
    if result.get("binary"):
        return {"success": True, "path": path, "binary": True,
                "message": "Blame is not available for binary files."}
    if result.get("too_large"):
        raise HTTPException(
            status_code=413,
            detail=(
                f"'{path}' has {result['line_count']:,} lines, over the "
                f"{result['limit']:,}-line blame limit."
            ),
        )

    return {"success": True, **result}
