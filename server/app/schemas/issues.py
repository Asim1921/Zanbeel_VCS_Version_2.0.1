"""Request bodies for issue tracking and milestones."""

from typing import List, Optional

from pydantic import BaseModel


class IssueCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    issue_type: str = "task"
    priority: str = "medium"
    assigned_to: Optional[str] = None
    milestone_id: Optional[int] = None
    labels: Optional[List[str]] = None


class IssueUpdateRequest(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None
    issue_type: Optional[str] = None
    assigned_to: Optional[str] = None
    milestone_id: Optional[int] = None


class IssueCommentCreateRequest(BaseModel):
    body: str


class IssueWatchRequest(BaseModel):
    watch: bool = True


class IssueLabelUpsertRequest(BaseModel):
    name: str
    color: Optional[str] = None


class MilestoneCreateRequest(BaseModel):
    title: str
    description: Optional[str] = None
    due_date: Optional[str] = None
