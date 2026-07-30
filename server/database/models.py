from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, LargeBinary, Index, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database.database import Base
import uuid

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), index=True)  # Removed unique=True to allow multiple NULL emails
    password_hash = Column(String(255), nullable=True)
    full_name = Column(String(100))
    role = Column(String(20), default='developer')  # 'developer' or 'team_lead'
    team_lead_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # For developers, reference to their team lead
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    last_login_at = Column(DateTime, nullable=True)  # Updated on successful web/CLI login
    is_active = Column(Boolean, default=True)
    
    # Relationships
    repositories = relationship("Repository", back_populates="owner")
    commits = relationship("Commit", back_populates="author")
    team_lead = relationship("User", remote_side=[id], foreign_keys=[team_lead_id])  # Self-referential relationship

class Repository(Base):
    __tablename__ = "repositories"
    
    id = Column(String(16), primary_key=True, index=True)  # Using the hash-based ID
    name = Column(String(100), nullable=False)
    description = Column(Text)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    head_commit_id = Column(String(40), ForeignKey("commits.id"))
    is_archived = Column(Boolean, default=False)
    archived_at = Column(DateTime)
    archived_reason = Column(Text)
    
    # Repository settings
    is_public = Column(Boolean, default=True)
    language = Column(String(50))
    size_bytes = Column(Integer, default=0)
    
    # G1 Coordinator and Testing fields
    g1_coordinator = Column(String(100))  # Name of G1 coordinator
    tested = Column(Boolean, default=False)  # Yes/No testing status
    instruction_manual_path = Column(String(500))  # Path to uploaded PDF instruction manual
    instruction_manual_filename = Column(String(255))  # Original filename of the manual
    
    # Relationships
    owner = relationship("User", back_populates="repositories")
    commits = relationship("Commit", back_populates="repository", foreign_keys="Commit.repository_id")
    head_commit = relationship("Commit", foreign_keys=[head_commit_id], post_update=True)
    tags = relationship("RepositoryTag", back_populates="repository")


class RepositoryStar(Base):
    """User stars on repositories (GitHub-style bookmark)."""
    __tablename__ = "repository_stars"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    repository_id = Column(String(16), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])
    repository = relationship("Repository")

    __table_args__ = (
        UniqueConstraint("user_id", "repository_id", name="uq_repository_stars_user_repo"),
        Index("ix_repository_stars_repo", "repository_id"),
    )


class Commit(Base):
    __tablename__ = "commits"
    
    id = Column(String(40), primary_key=True, index=True)  # SHA-1 hash
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    parent_commit_id = Column(String(40), ForeignKey("commits.id"))
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    
    # Commit metadata
    tree_hash = Column(String(40))  # Hash of the file tree state
    
    # Relationships
    repository = relationship("Repository", back_populates="commits", foreign_keys=[repository_id])
    author = relationship("User", back_populates="commits")
    parent_commit = relationship("Commit", remote_side=[id])
    files = relationship("CommitFile", back_populates="commit")
    parent_links = relationship("CommitParent", primaryjoin="Commit.id==CommitParent.commit_id")

class CommitParent(Base):
    __tablename__ = "commit_parents"

    id = Column(Integer, primary_key=True, index=True)
    commit_id = Column(String(40), ForeignKey("commits.id"), nullable=False)
    parent_commit_id = Column(String(40), ForeignKey("commits.id"), nullable=False)
    parent_order = Column(Integer, default=0)

    commit = relationship("Commit", foreign_keys=[commit_id])
    parent_commit = relationship("Commit", foreign_keys=[parent_commit_id])

class CommitFile(Base):
    __tablename__ = "commit_files"
    
    id = Column(Integer, primary_key=True, index=True)
    commit_id = Column(String(40), ForeignKey("commits.id"), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_hash = Column(String(40), nullable=False)
    file_size = Column(Integer)
    file_mode = Column(String(10))  # File permissions
    
    # Relationships
    commit = relationship("Commit", back_populates="files")
    file_object = relationship("FileObject", foreign_keys=[file_hash], primaryjoin="CommitFile.file_hash == FileObject.hash")

    __table_args__ = (
        Index("ix_commit_files_commit_path", "commit_id", "file_path"),
        Index("ix_commit_files_path", "file_path"),
    )

class FileObject(Base):
    __tablename__ = "file_objects"
    
    hash = Column(String(40), primary_key=True, index=True)  # SHA-1 hash of content
    content = Column(LargeBinary, nullable=False)  # Binary file content
    size = Column(Integer, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    
    # Content type detection
    mime_type = Column(String(100))

class RepositoryTag(Base):
    __tablename__ = "repository_tags"
    
    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False)
    name = Column(String(50), nullable=False)
    color = Column(String(7))  # Hex color code
    
    # Relationships
    repository = relationship("Repository", back_populates="tags")

class Branch(Base):
    __tablename__ = "branches"
    
    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False)
    name = Column(String(100), nullable=False)
    head_commit_id = Column(String(40), ForeignKey("commits.id"))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    is_default = Column(Boolean, default=False)
    
    # Relationships
    repository = relationship("Repository")
    head_commit = relationship("Commit")

    __table_args__ = (
        Index("ix_branches_repo_name", "repository_id", "name"),
    )

class FileLineage(Base):
    __tablename__ = "file_lineage"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False)
    commit_id = Column(String(40), ForeignKey("commits.id"), nullable=False)
    old_path = Column(String(500), nullable=False)
    new_path = Column(String(500), nullable=False)
    file_hash = Column(String(40), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    repository = relationship("Repository")
    commit = relationship("Commit")

    __table_args__ = (
        Index("ix_file_lineage_repo_old", "repository_id", "old_path"),
        Index("ix_file_lineage_repo_new", "repository_id", "new_path"),
        Index("ix_file_lineage_repo_commit", "repository_id", "commit_id"),
    )

class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False)
    name = Column(String(100), nullable=False)
    commit_id = Column(String(40), ForeignKey("commits.id"), nullable=False)
    message = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    created_by_id = Column(Integer, ForeignKey("users.id"))

    repository = relationship("Repository")
    commit = relationship("Commit")
    created_by = relationship("User")

class Release(Base):
    __tablename__ = "releases"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False)
    tag_id = Column(Integer, ForeignKey("tags.id"), nullable=False)
    version = Column(String(50), nullable=False)
    title = Column(String(200))
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    created_by_id = Column(Integer, ForeignKey("users.id"))

    repository = relationship("Repository")
    tag = relationship("Tag")
    created_by = relationship("User")

class PullRequest(Base):
    __tablename__ = "pull_requests"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    source_branch = Column(String(100), nullable=False)
    target_branch = Column(String(100), nullable=False)
    status = Column(String(20), default="open")  # open, merged, closed
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    merged_at = Column(DateTime)
    merge_commit_id = Column(String(40), ForeignKey("commits.id"))
    created_by_id = Column(Integer, ForeignKey("users.id"))
    reviewed_by_id = Column(Integer, ForeignKey("users.id"))

    repository = relationship("Repository")
    merge_commit = relationship("Commit", foreign_keys=[merge_commit_id])
    created_by = relationship("User", foreign_keys=[created_by_id])
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_id])

class Activity(Base):
    __tablename__ = "activities"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    repository_id = Column(String(16), ForeignKey("repositories.id"))
    activity_type = Column(String(50), nullable=False)  # commit, create_repo, etc.
    description = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    
    # Relationships
    user = relationship("User")
    repository = relationship("Repository")

class PendingCommit(Base):
    __tablename__ = "pending_commits"
    
    id = Column(String(40), primary_key=True, index=True)  # SHA-1 hash
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    parent_commit_id = Column(String(40))
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    
    # Approval status
    status = Column(String(20), default='pending')  # pending, approved, rejected
    reviewed_by_id = Column(Integer, ForeignKey("users.id"))
    reviewed_at = Column(DateTime)
    review_comment = Column(Text)
    
    # Commit metadata
    tree_hash = Column(String(40))
    files_data = Column(Text)  # JSON string containing file data
    
    # Relationships
    repository = relationship("Repository")
    author = relationship("User", foreign_keys=[author_id])
    reviewer = relationship("User", foreign_keys=[reviewed_by_id])
    files = relationship("PendingCommitFile", back_populates="pending_commit")

class PendingCommitFile(Base):
    __tablename__ = "pending_commit_files"
    
    id = Column(Integer, primary_key=True, index=True)
    pending_commit_id = Column(String(40), ForeignKey("pending_commits.id"), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_hash = Column(String(40), nullable=False)
    file_size = Column(Integer)
    
    # Relationships
    pending_commit = relationship("PendingCommit", back_populates="files")

class PendingRepository(Base):
    __tablename__ = "pending_repositories"
    
    id = Column(Integer, primary_key=True, index=True)
    repo_name = Column(String(100), nullable=False)
    description = Column(Text)
    requested_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # Team lead who will own it
    created_at = Column(DateTime, server_default=func.now())
    
    # Approval status
    status = Column(String(20), default='pending')  # pending, approved, rejected
    reviewed_by_id = Column(Integer, ForeignKey("users.id"))
    reviewed_at = Column(DateTime)
    review_comment = Column(Text)
    
    # Relationships
    requested_by = relationship("User", foreign_keys=[requested_by_id])
    owner = relationship("User", foreign_keys=[owner_id])
    reviewer = relationship("User", foreign_keys=[reviewed_by_id])

class PendingUserRegistration(Base):
    __tablename__ = "pending_user_registrations"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), nullable=False, index=True)
    email = Column(String(100), index=True)
    full_name = Column(String(100))
    password_hash = Column(String(255), nullable=False)
    requested_role = Column(String(20), default='developer')
    requested_team_lead_username = Column(String(50), nullable=True)
    status = Column(String(20), default='pending')  # pending, approved, rejected
    review_comment = Column(Text)
    reviewed_by_id = Column(Integer, ForeignKey("users.id"))
    reviewed_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())

    reviewer = relationship("User", foreign_keys=[reviewed_by_id])

class UserPermission(Base):
    __tablename__ = "user_permissions"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False)
    permission_level = Column(String(20), nullable=False) 
    granted_by_id = Column(Integer, ForeignKey("users.id"))
    granted_at = Column(DateTime, server_default=func.now())
    # When False, repo is omitted from main web "Repositories" list (CLI/access still works).
    # Issue-access workflow sets this False so tagged users are not flooded in the UI.
    show_in_web_ui = Column(Boolean, default=True, server_default="1")
    # When set, this row was granted for that issue only; closing the issue revokes the row.
    issue_id = Column(Integer, ForeignKey("issues.id"), nullable=True, index=True)
    
    # Relationships
    user = relationship("User", foreign_keys=[user_id])
    repository = relationship("Repository")
    granted_by = relationship("User", foreign_keys=[granted_by_id])


# ==========================
# Issue Tracking Models
# ==========================

class Milestone(Base):
    __tablename__ = "milestones"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    due_date = Column(DateTime)
    is_closed = Column(Boolean, default=False)
    closed_at = Column(DateTime)
    created_by_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    repository = relationship("Repository")
    created_by = relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        Index("ix_milestones_repo_closed", "repository_id", "is_closed"),
    )


class Issue(Base):
    __tablename__ = "issues"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False, index=True)

    # Human-facing per-repository number, e.g. #123
    number = Column(Integer, nullable=False)

    title = Column(String(300), nullable=False)
    description = Column(Text)
    issue_type = Column(String(20), default="task")  # bug, feature, task, question
    priority = Column(String(20), default="medium")  # critical, high, medium, low
    status = Column(String(20), default="open")  # open, in_progress, resolved, closed

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    assigned_to_id = Column(Integer, ForeignKey("users.id"))
    milestone_id = Column(Integer, ForeignKey("milestones.id"))

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    resolved_at = Column(DateTime)
    closed_at = Column(DateTime)

    repository = relationship("Repository")
    created_by = relationship("User", foreign_keys=[created_by_id])
    assigned_to = relationship("User", foreign_keys=[assigned_to_id])
    milestone = relationship("Milestone")

    comments = relationship("IssueComment", back_populates="issue")
    label_links = relationship("IssueLabelLink", back_populates="issue")
    watchers = relationship("IssueWatcher", back_populates="issue")
    events = relationship("IssueEvent", back_populates="issue")

    __table_args__ = (
        UniqueConstraint("repository_id", "number", name="uq_issues_repo_number"),
        Index("ix_issues_repo_status", "repository_id", "status"),
        Index("ix_issues_repo_priority", "repository_id", "priority"),
        Index("ix_issues_repo_type", "repository_id", "issue_type"),
    )


class IssueComment(Base):
    __tablename__ = "issue_comments"

    id = Column(Integer, primary_key=True, index=True)
    issue_id = Column(Integer, ForeignKey("issues.id"), nullable=False, index=True)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    issue = relationship("Issue", back_populates="comments")
    author = relationship("User", foreign_keys=[author_id])


class IssueLabel(Base):
    __tablename__ = "issue_labels"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False, index=True)
    name = Column(String(50), nullable=False)
    color = Column(String(7))  # Hex, e.g. #ff00aa
    created_at = Column(DateTime, server_default=func.now())

    repository = relationship("Repository")
    links = relationship("IssueLabelLink", back_populates="label")

    __table_args__ = (
        UniqueConstraint("repository_id", "name", name="uq_issue_labels_repo_name"),
        Index("ix_issue_labels_repo", "repository_id"),
    )


class IssueLabelLink(Base):
    __tablename__ = "issue_label_links"

    id = Column(Integer, primary_key=True, index=True)
    issue_id = Column(Integer, ForeignKey("issues.id"), nullable=False, index=True)
    label_id = Column(Integer, ForeignKey("issue_labels.id"), nullable=False, index=True)

    issue = relationship("Issue", back_populates="label_links")
    label = relationship("IssueLabel", back_populates="links")

    __table_args__ = (
        UniqueConstraint("issue_id", "label_id", name="uq_issue_label_links_issue_label"),
    )


class IssueWatcher(Base):
    __tablename__ = "issue_watchers"

    id = Column(Integer, primary_key=True, index=True)
    issue_id = Column(Integer, ForeignKey("issues.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())

    issue = relationship("Issue", back_populates="watchers")
    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        UniqueConstraint("issue_id", "user_id", name="uq_issue_watchers_issue_user"),
        Index("ix_issue_watchers_user", "user_id"),
    )


class IssueCommitLink(Base):
    __tablename__ = "issue_commit_links"

    id = Column(Integer, primary_key=True, index=True)
    issue_id = Column(Integer, ForeignKey("issues.id"), nullable=False, index=True)
    commit_id = Column(String(40), ForeignKey("commits.id"), nullable=False, index=True)
    link_type = Column(String(20), default="ref")  # ref, fixes, closes
    created_at = Column(DateTime, server_default=func.now())

    issue = relationship("Issue")
    commit = relationship("Commit")

    __table_args__ = (
        UniqueConstraint("issue_id", "commit_id", "link_type", name="uq_issue_commit_link"),
    )


class IssuePullRequestLink(Base):
    __tablename__ = "issue_pull_request_links"

    id = Column(Integer, primary_key=True, index=True)
    issue_id = Column(Integer, ForeignKey("issues.id"), nullable=False, index=True)
    pull_request_id = Column(Integer, ForeignKey("pull_requests.id"), nullable=False, index=True)
    link_type = Column(String(20), default="ref")  # ref, fixes, closes
    created_at = Column(DateTime, server_default=func.now())

    issue = relationship("Issue")
    pull_request = relationship("PullRequest")

    __table_args__ = (
        UniqueConstraint("issue_id", "pull_request_id", "link_type", name="uq_issue_pr_link"),
    )


class IssueBranchLink(Base):
    __tablename__ = "issue_branch_links"

    id = Column(Integer, primary_key=True, index=True)
    issue_id = Column(Integer, ForeignKey("issues.id"), nullable=False, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False, index=True)
    branch_name = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    issue = relationship("Issue")
    repository = relationship("Repository")

    __table_args__ = (
        UniqueConstraint("repository_id", "branch_name", "issue_id", name="uq_issue_branch_link"),
        Index("ix_issue_branch_repo_branch", "repository_id", "branch_name"),
    )


class IssueEvent(Base):
    __tablename__ = "issue_events"

    id = Column(Integer, primary_key=True, index=True)
    issue_id = Column(Integer, ForeignKey("issues.id"), nullable=False, index=True)
    actor_id = Column(Integer, ForeignKey("users.id"))
    event_type = Column(String(50), nullable=False)  # created, comment, status_changed, assigned, labeled, etc.
    payload_json = Column(Text)  # JSON string (kept Text for SQLite/Postgres portability)
    created_at = Column(DateTime, server_default=func.now())

    issue = relationship("Issue", back_populates="events")
    actor = relationship("User", foreign_keys=[actor_id])

    __table_args__ = (
        Index("ix_issue_events_issue_created", "issue_id", "created_at"),
    )


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    notification_type = Column(String(50), nullable=False)  # mention, issue_status, issue_comment, issue_assigned, etc.
    title = Column(String(200))
    body = Column(Text)
    payload_json = Column(Text)
    dedupe_key = Column(String(200))  # optional idempotency key
    is_read = Column(Boolean, default=False)
    read_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_notifications_user_read", "user_id", "is_read"),
        UniqueConstraint("user_id", "dedupe_key", name="uq_notifications_user_dedupe"),
    )


# ==========================
# Issue Access Request Model
# ==========================

class IssueAccessRequest(Base):
    __tablename__ = "issue_access_requests"
    
    id = Column(Integer, primary_key=True, index=True)
    issue_id = Column(Integer, ForeignKey("issues.id"), nullable=False, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False, index=True)
    
    # Who initiated the request (user who created issue and tagged @someone)
    requested_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    # Who needs access (the @mentioned user)
    requested_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    # Admin decision
    status = Column(String(20), default="pending")  # pending, approved, denied
    admin_id = Column(Integer, ForeignKey("users.id"))  # Who approved/denied
    reviewed_at = Column(DateTime)
    review_comment = Column(Text)
    
    request_reason = Column(Text)  # Why access is needed
    
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    
    # Relationships
    issue = relationship("Issue")
    repository = relationship("Repository")
    requested_by = relationship("User", foreign_keys=[requested_by_id])
    requested_user = relationship("User", foreign_keys=[requested_user_id])
    admin = relationship("User", foreign_keys=[admin_id])
    
    __table_args__ = (
        UniqueConstraint(
            "issue_id", "requested_user_id",
            name="uq_issue_access_req_issue_user"
        ),
        Index("ix_access_req_repo_status", "repository_id", "status"),
        Index("ix_access_req_user_status", "requested_user_id", "status"),
        Index("ix_access_req_pending", "repository_id", "status"),
    )
