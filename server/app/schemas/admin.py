"""Request bodies for permission management, review queues and notifications."""

from typing import List, Optional

from pydantic import BaseModel


class CreatePermissionRequest(BaseModel):
    username: str
    repo_id: str
    permission_level: str  # read, write, team_lead, admin
    granted_by: Optional[str] = None

class ReviewCommitRequest(BaseModel):
    reviewer_username: Optional[str] = None
    action: str  # approve or reject
    comment: Optional[str] = None


class MarkNotificationsReadRequest(BaseModel):
    ids: List[int]
