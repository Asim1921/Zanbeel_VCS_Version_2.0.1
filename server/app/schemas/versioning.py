"""Request bodies for branches, tags, releases, pull requests and rollbacks."""

from typing import Dict, List, Optional

from pydantic import BaseModel


class BranchCreateRequest(BaseModel):
    name: str
    from_commit: Optional[str] = None
    # Branch off another branch by name. Resolved to that branch's head; callers usually
    # know a branch name, not a commit id.
    from_branch: Optional[str] = None

class BranchRenameRequest(BaseModel):
    new_name: str

class BranchHeadUpdateRequest(BaseModel):
    head_commit_id: str

class TagCreateRequest(BaseModel):
    name: str
    commit_id: Optional[str] = None
    message: Optional[str] = None

class ReleaseCreateRequest(BaseModel):
    version: str
    tag: Optional[str] = None
    title: Optional[str] = None
    notes: Optional[str] = None

class PullRequestCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    source_branch: str
    target_branch: str

class FileRollbackRequest(BaseModel):
    path: str
    target_commit_id: str
    branch: Optional[str] = None
    summary: Optional[str] = None
    expected_head_commit_id: Optional[str] = None

class BranchRollbackRequest(BaseModel):
    branch: str
    target_commit_id: str
    summary: Optional[str] = None
    expected_head_commit_id: Optional[str] = None

class MergePullRequestRequest(BaseModel):
    expected_head_commit_id: Optional[str] = None


class CherryPickRequest(BaseModel):
    commit_id: str
    branch: str
    # Required only when the source is a merge commit: which parent counts as "before".
    mainline: Optional[int] = None
    message: Optional[str] = None
    dry_run: bool = False
    expected_head_commit_id: Optional[str] = None


class RevertRequest(BaseModel):
    commit_id: str
    branch: str
    mainline: Optional[int] = None
    message: Optional[str] = None
    dry_run: bool = False
    expected_head_commit_id: Optional[str] = None


class RebaseRequest(BaseModel):
    branch: str
    onto: str
    dry_run: bool = False
    expected_head_commit_id: Optional[str] = None


class ResolveConflictsRequest(BaseModel):
    # {file_path: base64 content} -- every conflicted path must be present.
    resolutions: Dict[str, str]
    expected_head_commit_id: Optional[str] = None


class BranchPolicyRequest(BaseModel):
    create_branch_min_scope: Optional[str] = None
    push_min_scope: Optional[str] = None
    pull_min_scope: Optional[str] = None
    merge_min_scope: Optional[str] = None
    publish_min_scope: Optional[str] = None
    copy_files_min_scope: Optional[str] = None
    default_branch_push_min_scope: Optional[str] = None
    protected_branches: Optional[List[str]] = None
    required_approvals: Optional[int] = None
    # Status check contexts that must be green before a merge, e.g. ["ci/unit-tests"].
    required_status_checks: Optional[List[str]] = None


class BranchMergeRequest(BaseModel):
    source_branch: str
    target_branch: str
    expected_head_commit_id: Optional[str] = None
    dry_run: bool = False


class BranchPublishRequest(BaseModel):
    source_branch: str
    target_branch: str
    expected_head_commit_id: Optional[str] = None


class CopyFilesRequest(BaseModel):
    source_branch: str
    target_branch: str
    paths: List[str]
    expected_head_commit_id: Optional[str] = None


class SubmitReviewRequest(BaseModel):
    # approved | changes_requested | commented
    state: str
    body: Optional[str] = None


class AddPullRequestCommentRequest(BaseModel):
    file_path: Optional[str] = None
    line: Optional[int] = None
    side: str = "new"
    body: str
    # Replying inherits the parent's file/line, so those may be omitted.
    in_reply_to_id: Optional[int] = None


class ResolveCommentRequest(BaseModel):
    resolved: bool = True
