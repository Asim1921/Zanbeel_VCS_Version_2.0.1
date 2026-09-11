"""Branch name normalisation and branch/commit resolution."""

import re

from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from database.crud import BranchCRUD
from database.models import Branch, Commit, Repository


def _normalize_branch_name(name: str) -> str:
    normalized = (name or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Branch name is required")
    if normalized.startswith("-"):
        raise HTTPException(status_code=400, detail="Branch name cannot start with '-' ")
    if ".." in normalized or "//" in normalized or normalized.endswith("/"):
        raise HTTPException(status_code=400, detail="Invalid branch name")
    if not re.match(r"^[A-Za-z0-9._/-]+$", normalized):
        raise HTTPException(status_code=400, detail="Branch name contains invalid characters")
    return normalized

def _get_default_branch(db: Session, repo_id: str) -> Branch:
    branch = db.query(Branch).filter(Branch.repository_id == repo_id, Branch.is_default == True).first()
    if branch:
        return branch
    # Ensure a default branch exists
    branch = BranchCRUD.create_branch(db, repo_id, "main", is_default=True)
    return branch

def _resolve_branch(db: Session, repo_id: str, branch_name: Optional[str]) -> Branch:
    if branch_name:
        normalized = _normalize_branch_name(branch_name)
        branch = BranchCRUD.get_branch(db, repo_id, normalized)
        if not branch:
            raise HTTPException(status_code=404, detail="Branch not found")
        return branch
    return _get_default_branch(db, repo_id)


def _get_commit_for_branch(db: Session, repository: Repository, branch_name: Optional[str]) -> Optional[Commit]:
    """Resolve the commit object for the selected branch (or repository head by default)."""
    commit_id = repository.head_commit_id
    if branch_name:
        resolved_branch = _resolve_branch(db, repository.id, branch_name)
        commit_id = resolved_branch.head_commit_id
    if not commit_id:
        return None
    return db.query(Commit).filter(Commit.id == commit_id).first()
