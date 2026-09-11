"""File payload assembly and vendor/binary classification."""

import base64

from typing import Any, Dict, Optional

from database.models import Commit

from app.config import DIFF_MAX_BYTES
from app.services.merge import _try_decode_text


def _build_files_payload_from_commit(commit: Commit, include_content: bool) -> Dict[str, Any]:
    files_dict: Dict[str, Any] = {}
    folders_set = set()

    for commit_file in commit.files:
        file_path = commit_file.file_path.replace('\\', '/')

        path_parts = file_path.split('/')
        for i in range(len(path_parts) - 1):
            folders_set.add('/'.join(path_parts[:i + 1]))

        file_entry = {
            "size": commit_file.file_size,
            "hash": commit_file.file_hash,
            "mime_type": commit_file.file_object.mime_type if commit_file.file_object else None,
            "is_binary": None
        }

        if include_content and commit_file.file_object:
            try:
                content = commit_file.file_object.content.decode('utf-8')
                is_binary = False
            except UnicodeDecodeError:
                content = base64.b64encode(commit_file.file_object.content).decode('utf-8')
                is_binary = True

            file_entry["content"] = content
            file_entry["is_binary"] = is_binary

        files_dict[file_path] = file_entry

    return {
        "files": files_dict,
        "folders": sorted(list(folders_set))
    }


def _is_vendor_path(path: str) -> bool:
    """Return True for dependency/vendor directories that should be ignored for docs."""
    normalized = (path or "").replace('\\', '/').lower()
    vendor_markers = [
        'node_modules/',
        'venv/',
        '.venv/',
        '__pycache__/',
        '.git/',
        'dist/',
        'build/'
    ]
    return any(marker in normalized for marker in vendor_markers)

def _is_binary_or_large(content: Optional[bytes]) -> bool:
    if content is None:
        return False
    if len(content) > DIFF_MAX_BYTES:
        return True
    return _try_decode_text(content) is None
