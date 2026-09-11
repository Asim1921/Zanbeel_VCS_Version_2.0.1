"""Request/response bodies for repository and commit endpoints."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class CreateRepositoryRequest(BaseModel):
    username: str
    repo_name: str
    description: Optional[str] = None

class PushCommitRequest(BaseModel):
    commit: Dict[str, Any]
    archive: Optional[bool] = False
    branch: Optional[str] = None
    pusher: Optional[str] = None
    expected_head_commit_id: Optional[str] = None


class RepositoryResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    repo_id: Optional[str] = None

class CommitResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    commit_id: Optional[str] = None

class CommitsResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    commits: Optional[List[Dict[str, Any]]] = None

class RepositoriesResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    repositories: Optional[List[Dict[str, Any]]] = None

class UpdateRepositoryDetailsRequest(BaseModel):
    g1_coordinator: Optional[str] = None
    tested: Optional[bool] = None

class UpdateRepositoryDetailsResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    repository: Optional[Dict[str, Any]] = None


class ForkRepositoryRequest(BaseModel):
    # Defaults to the source repository's name when omitted.
    name: Optional[str] = None


class CrossRepoPullRequestRequest(BaseModel):
    # The fork the change lives in; the URL names the upstream being proposed to.
    source_repo_id: str
    source_branch: str
    target_branch: str
    title: str
    description: Optional[str] = None
