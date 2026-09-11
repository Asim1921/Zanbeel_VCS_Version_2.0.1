"""Pydantic request/response bodies for the FoxNest API.

Grouped by domain in sibling modules and re-exported here so call sites can keep a
single import (``from app.schemas import LoginRequest``).
"""

from app.schemas.admin import (
    CreatePermissionRequest,
    MarkNotificationsReadRequest,
    ReviewCommitRequest,
)
from app.schemas.auth import (
    BootstrapPasswordRequest,
    ChangePasswordRequest,
    LoginRequest,
    RegistrationRequest,
    ReviewRegistrationRequest,
)
from app.schemas.issues import (
    IssueCommentCreateRequest,
    IssueCreateRequest,
    IssueLabelUpsertRequest,
    IssueUpdateRequest,
    IssueWatchRequest,
    MilestoneCreateRequest,
)
from app.schemas.repository import (
    CommitResponse,
    CrossRepoPullRequestRequest,
    CommitsResponse,
    CreateRepositoryRequest,
    ForkRepositoryRequest,
    PushCommitRequest,
    RepositoriesResponse,
    RepositoryResponse,
    UpdateRepositoryDetailsRequest,
    UpdateRepositoryDetailsResponse,
)
from app.schemas.versioning import (
    AddPullRequestCommentRequest,
    BranchCreateRequest,
    BranchHeadUpdateRequest,
    BranchMergeRequest,
    BranchPolicyRequest,
    BranchPublishRequest,
    BranchRenameRequest,
    BranchRollbackRequest,
    CherryPickRequest,
    CopyFilesRequest,
    FileRollbackRequest,
    MergePullRequestRequest,
    PullRequestCreateRequest,
    RebaseRequest,
    ReleaseCreateRequest,
    ResolveCommentRequest,
    ResolveConflictsRequest,
    RevertRequest,
    SubmitReviewRequest,
    TagCreateRequest,
)

__all__ = [
    # admin
    "CreatePermissionRequest",
    "MarkNotificationsReadRequest",
    "ReviewCommitRequest",
    # auth
    "BootstrapPasswordRequest",
    "ChangePasswordRequest",
    "LoginRequest",
    "RegistrationRequest",
    "ReviewRegistrationRequest",
    # issues
    "IssueCommentCreateRequest",
    "IssueCreateRequest",
    "IssueLabelUpsertRequest",
    "IssueUpdateRequest",
    "IssueWatchRequest",
    "MilestoneCreateRequest",
    # repository
    "CommitResponse",
    "CrossRepoPullRequestRequest",
    "CommitsResponse",
    "CreateRepositoryRequest",
    "ForkRepositoryRequest",
    "PushCommitRequest",
    "RepositoriesResponse",
    "RepositoryResponse",
    "UpdateRepositoryDetailsRequest",
    "UpdateRepositoryDetailsResponse",
    # versioning
    "AddPullRequestCommentRequest",
    "BranchCreateRequest",
    "BranchHeadUpdateRequest",
    "BranchMergeRequest",
    "BranchPolicyRequest",
    "BranchPublishRequest",
    "BranchRenameRequest",
    "BranchRollbackRequest",
    "CherryPickRequest",
    "CopyFilesRequest",
    "FileRollbackRequest",
    "MergePullRequestRequest",
    "PullRequestCreateRequest",
    "RebaseRequest",
    "ReleaseCreateRequest",
    "ResolveConflictsRequest",
    "RevertRequest",
    "SubmitReviewRequest",
    "TagCreateRequest",
]
