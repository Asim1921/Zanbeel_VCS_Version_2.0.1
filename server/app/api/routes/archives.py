"""Source archives: a repository's files at a branch, tag or commit, as one .zip.

A release that cannot be downloaded is only a name. Until now the only way to get the
files at a tag was the CLI; this serves them as GitHub's "Source code (zip)" does, with
every file under one top-level folder named after the repository and the ref.
"""

import re
import tempfile
import zipfile
from typing import Iterator, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from database.crud import RepositoryCRUD
from database.database import get_db
from database.models import Commit, User

from app.core.dependencies import get_current_user
from app.core.permissions import (
    _require_repository_checkout_access, _user_can_read_repository_contents,
)
from app.services import repo_refs
from app.services.paths import normalize as normalize_path


router = APIRouter()

#: Archives are built in memory up to this size, then on disk.
_SPOOL_BYTES = 32 * 1024 * 1024
_CHUNK = 1024 * 1024


def _safe_name(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-.")
    return cleaned or "source"


def _entries(commit: Commit):
    """Each file once, by its canonical path, skipping anything that would escape the
    archive's folder when extracted."""
    chosen = {}
    for commit_file in commit.files:
        if not commit_file.file_object:
            continue
        raw = commit_file.file_path or ""
        path = normalize_path(raw).lstrip("/")
        parts = [p for p in path.split("/") if p not in ("", ".")]
        if not parts or ".." in parts or ":" in parts[0]:
            continue
        key = "/".join(parts)
        # Two spellings of one file: keep the canonically spelled row, as the tree does.
        if key in chosen and raw != key:
            continue
        chosen[key] = commit_file
    return sorted(chosen.items())


@router.get("/api/repository/{repo_id}/source.zip")
def download_source_archive(
    repo_id: str,
    ref: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The files at ``ref`` (a branch, tag or commit; default branch if omitted) as a zip."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    # Downloading is checking out. Someone who may only fetch the default branch to work
    # on an issue may download that, and nothing else.
    if not _user_can_read_repository_contents(db, current_user, repository):
        _require_repository_checkout_access(db, current_user, repository, ref or None,
                                            "download source from")

    target = repo_refs.resolve(db, repository, ref)
    commit = db.query(Commit).filter(Commit.id == target["commit_id"]).first()
    if commit is None:
        raise HTTPException(status_code=404, detail="Commit not found")

    label = target["name"] if target["kind"] != "commit" else commit.id[:12]
    folder = f"{_safe_name(repository.name)}-{_safe_name(label)}"

    spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES)
    with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.comment = commit.id.encode("ascii")
        for path, commit_file in _entries(commit):
            archive.writestr(f"{folder}/{path}", commit_file.file_object.content)
    size = spool.tell()
    spool.seek(0)

    def stream() -> Iterator[bytes]:
        try:
            while True:
                chunk = spool.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            spool.close()

    filename = f"{folder}.zip"
    return StreamingResponse(
        stream(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; "
                                   f"filename*=UTF-8''{quote(filename)}",
            "Content-Length": str(size),
            "X-Zanbeel-Commit": commit.id,
        },
    )
