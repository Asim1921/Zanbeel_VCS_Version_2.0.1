"""Conversion of ORM rows into the JSON shapes the API returns."""

import base64

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from database.crud import RepositoryStarCRUD, contributor_count_for_repository
from database.models import Branch, Commit, Repository, User

from app.core.permissions import can_manage_repository, can_write_repository


def repository_to_dict(repo: Repository, db: Optional[Session] = None, actor: Optional[User] = None) -> Dict[str, Any]:
    """Convert Repository model to dictionary"""
    can_write = False
    can_manage = False
    if db is not None and actor is not None:
        can_write = can_write_repository(db, actor, repo)
        can_manage = can_manage_repository(db, actor, repo)

    out = {
        "id": repo.id,
        "name": repo.name,
        "description": repo.description,
        "owner": repo.owner.username,
        "created_at": repo.created_at.isoformat() if repo.created_at else None,
        "updated_at": repo.updated_at.isoformat() if repo.updated_at else None,
        "commits": [commit.id for commit in repo.commits],
        "head": repo.head_commit_id,
        "is_archived": repo.is_archived,
        "archived_at": repo.archived_at.isoformat() if repo.archived_at else None,
        "archived_reason": repo.archived_reason,
        "language": repo.language,
        "size": repo.size_bytes,
        "is_public": repo.is_public,
        "g1_coordinator": repo.g1_coordinator,
        "tested": repo.tested,
        "instruction_manual_filename": repo.instruction_manual_filename,
        "has_instruction_manual": bool(repo.instruction_manual_path),
        "current_user_can_write": can_write,
        "current_user_can_manage": can_manage,
    }
    if db is not None:
        out["branch_count"] = (
            db.query(Branch).filter(Branch.repository_id == repo.id).count()
        )
        out["star_count"] = RepositoryStarCRUD.count_for_repository(db, repo.id)
        out["contributor_count"] = contributor_count_for_repository(db, repo)
        if actor is not None:
            out["starred_by_me"] = RepositoryStarCRUD.is_starred_by_user(
                db, actor.id, repo.id
            )
        else:
            out["starred_by_me"] = False
    else:
        out["branch_count"] = 0
        out["star_count"] = 0
        out["contributor_count"] = 1
        out["starred_by_me"] = False
    return out

def commit_to_dict(commit: Commit, include_files: bool = False) -> Dict[str, Any]:
    """Convert Commit model to dictionary"""
    parents = []
    if getattr(commit, "parent_links", None):
        parents = [link.parent_commit_id for link in sorted(commit.parent_links, key=lambda item: item.parent_order)]
    elif commit.parent_commit_id:
        parents = [commit.parent_commit_id]

    commit_dict = {
        "id": commit.id,
        "repository_id": commit.repository_id,
        "author": commit.author.username,
        "parent": commit.parent_commit_id,
        "parents": parents,
        "message": commit.message,
        "timestamp": commit.created_at.isoformat() if commit.created_at else None,
        "tree_hash": commit.tree_hash
    }
    
    if include_files:
        files = {}
        for commit_file in commit.files:
            if commit_file.file_object:
                content_b64 = base64.b64encode(commit_file.file_object.content).decode()
                files[commit_file.file_hash] = {
                    "path": commit_file.file_path,
                    "content": content_b64,
                    "size": commit_file.file_size,
                    "mime_type": getattr(commit_file.file_object, "mime_type", None)
                }
        commit_dict["files"] = files
    else:
        commit_dict["files"] = [
            {
                "path": cf.file_path,
                "hash": cf.file_hash,
                "size": cf.file_size
            }
            for cf in commit.files
        ]
    
    return commit_dict
