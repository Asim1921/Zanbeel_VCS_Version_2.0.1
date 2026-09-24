from sqlalchemy import Column, Integer, BigInteger, String, Text, DateTime, ForeignKey, Boolean, LargeBinary, Index, UniqueConstraint
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

    # Branch protection policy as JSON. NULL means "use the defaults" -- see
    # branch_policies.default_branch_policy(). Stored as text so the policy can gain
    # fields without a migration each time.
    branch_policy_json = Column(Text)

    # Set when this repository was forked from another. A fork is a full copy that keeps
    # a pointer home, so a pull request can be opened back at the original without
    # anyone needing write access to it.
    forked_from_id = Column(String(16), ForeignKey("repositories.id"), nullable=True)
    
    # Relationships
    owner = relationship("User", back_populates="repositories")
    forked_from = relationship("Repository", remote_side=[id], foreign_keys=[forked_from_id])
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
    # The two sides of the commit->parents edge. Linked explicitly, otherwise SQLAlchemy
    # sees two independent relationships writing commit_parents.commit_id and warns.
    parent_links = relationship(
        "CommitParent",
        primaryjoin="Commit.id==CommitParent.commit_id",
        foreign_keys="CommitParent.commit_id",
        back_populates="commit",
    )

class CommitParent(Base):
    __tablename__ = "commit_parents"

    id = Column(Integer, primary_key=True, index=True)
    commit_id = Column(String(40), ForeignKey("commits.id"), nullable=False)
    parent_commit_id = Column(String(40), ForeignKey("commits.id"), nullable=False)
    parent_order = Column(Integer, default=0)

    commit = relationship("Commit", foreign_keys=[commit_id], back_populates="parent_links")
    parent_commit = relationship("Commit", foreign_keys=[parent_commit_id])

class PullRequestReview(Base):
    """One reviewer's verdict on a pull request.

    Reviews are append-only: submitting again adds a new row rather than editing the old
    one, so the record shows that someone approved, then asked for changes, then approved
    again. Only the most recent review per reviewer counts toward the merge gate.

    ``commit_id`` records the source-branch tip that was actually reviewed. When the
    branch moves the approval becomes *stale* -- it approved code that is no longer what
    would be merged, and treating it as current is how unreviewed changes slip in.
    """

    __tablename__ = "pull_request_reviews"

    id = Column(Integer, primary_key=True, index=True)
    pull_request_id = Column(Integer, ForeignKey("pull_requests.id"), nullable=False, index=True)
    reviewer_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # approved | changes_requested | commented
    state = Column(String(20), nullable=False)
    body = Column(Text)
    # Source-branch head at the moment of review.
    commit_id = Column(String(40))
    created_at = Column(DateTime, server_default=func.now())

    pull_request = relationship("PullRequest", foreign_keys=[pull_request_id])
    reviewer = relationship("User", foreign_keys=[reviewer_id])


class PullRequestComment(Base):
    """A comment anchored to one line of one file in a pull request.

    Threads form through ``in_reply_to_id`` rather than a separate thread table: the first
    comment on a line is the root, replies point at it.

    ``commit_id`` records the source-branch tip the comment was written against. When that
    file changes afterwards the comment is *outdated* -- it refers to a line that may no
    longer say what the commenter read. Outdated comments are kept and flagged rather than
    deleted, because the discussion usually still matters even when the code moved.
    """

    __tablename__ = "pull_request_comments"

    id = Column(Integer, primary_key=True, index=True)
    pull_request_id = Column(Integer, ForeignKey("pull_requests.id"), nullable=False, index=True)
    author_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    file_path = Column(String(500), nullable=False)
    # 1-based line number within the side being annotated.
    line = Column(Integer, nullable=False)
    # "new" annotates the proposed content, "old" the base it is replacing.
    side = Column(String(8), nullable=False, default="new")

    body = Column(Text, nullable=False)
    commit_id = Column(String(40))
    in_reply_to_id = Column(Integer, ForeignKey("pull_request_comments.id"), nullable=True)

    resolved = Column(Boolean, default=False)
    resolved_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    resolved_at = Column(DateTime)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    pull_request = relationship("PullRequest", foreign_keys=[pull_request_id])
    author = relationship("User", foreign_keys=[author_id])
    resolved_by = relationship("User", foreign_keys=[resolved_by_id])
    replies = relationship(
        "PullRequestComment",
        primaryjoin="PullRequestComment.id==PullRequestComment.in_reply_to_id",
        foreign_keys="PullRequestComment.in_reply_to_id",
    )

    __table_args__ = (
        Index("ix_pr_comments_pr_path", "pull_request_id", "file_path"),
    )


class MergeConflictSession(Base):
    """An in-progress manual resolution of a conflicted pull request merge.

    The merge engine can report conflicts but not settle them, so a session parks the
    three sides of each conflicted file until a human decides. It records the commits the
    conflict was computed against, so a resolution can be refused if either branch has
    moved underneath it rather than silently merging against stale content.
    """

    __tablename__ = "merge_conflict_sessions"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False)
    # NULL for a conflict raised by a direct branch merge, which has no pull request.
    # Those conflicts need the same three-sided resolution, and refusing them a session
    # is what left `fox merge` reporting a conflict and stopping.
    pull_request_id = Column(Integer, ForeignKey("pull_requests.id"), nullable=True)
    source_branch = Column(String(100), nullable=False)
    target_branch = Column(String(100), nullable=False)
    # NULL when the two branches share no history: there is no merge base to record,
    # but every conflicted path still has an ours and a theirs to reconcile.
    base_commit_id = Column(String(40), ForeignKey("commits.id"), nullable=True)
    source_head_commit_id = Column(String(40), ForeignKey("commits.id"), nullable=False)
    target_head_commit_id = Column(String(40), ForeignKey("commits.id"), nullable=False)
    status = Column(String(20), default="open")        # open, resolved, aborted, stale
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    repository = relationship("Repository", foreign_keys=[repository_id])
    pull_request = relationship("PullRequest", foreign_keys=[pull_request_id])
    created_by = relationship("User", foreign_keys=[created_by_id])
    files = relationship("MergeConflictFile", back_populates="session",
                         cascade="all, delete-orphan")


class MergeConflictFile(Base):
    """One conflicted path within a session, holding all three sides by content hash."""

    __tablename__ = "merge_conflict_files"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("merge_conflict_sessions.id"), nullable=False)
    file_path = Column(String(500), nullable=False)
    base_file_hash = Column(String(40), ForeignKey("file_objects.hash"))
    ours_file_hash = Column(String(40), ForeignKey("file_objects.hash"))
    theirs_file_hash = Column(String(40), ForeignKey("file_objects.hash"))
    resolved_file_hash = Column(String(40), ForeignKey("file_objects.hash"))
    is_binary = Column(Boolean, default=False)

    session = relationship("MergeConflictSession", back_populates="files")

    __table_args__ = (
        UniqueConstraint("session_id", "file_path", name="uq_merge_conflict_files_session_path"),
    )


class CommitSignature(Base):
    """Server attestation that a commit was accepted, unchanged, from an authenticated user.

    The snapshot columns (`*_at_signing`) are not what the signature is computed over --
    that is the canonical payload in app/services/signing.py. They exist so a failed
    verification can say *which* part of the commit changed instead of just "invalid".
    """

    __tablename__ = "commit_signatures"

    commit_id = Column(String(40), ForeignKey("commits.id"), primary_key=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False, index=True)

    # The authenticated identity that pushed, which may differ from the commit's author.
    pusher_username = Column(String(50), nullable=False)
    pusher_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    algorithm = Column(String(32), nullable=False)
    signature = Column(String(128), nullable=False)
    # Stored as text because it is part of the signed payload and must round-trip exactly.
    signed_at_text = Column(String(40), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    # Diagnostic snapshots, written at signing time.
    tree_digest_at_signing = Column(String(64))
    author_at_signing = Column(String(50))
    message_digest_at_signing = Column(String(64))

    commit = relationship("Commit", foreign_keys=[commit_id])
    pusher = relationship("User", foreign_keys=[pusher_id])


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
    """A content-addressed blob.

    Content lives on disk (see app/services/blob_store.py); the ``content`` column is
    kept only for rows written before that move and is NULL for anything new. The
    ``content`` attribute below reads from whichever holds the bytes, so callers never
    need to know which -- that is what let the storage change without touching the
    sixteen places that read it.
    """

    __tablename__ = "file_objects"

    hash = Column(String(40), primary_key=True, index=True)  # SHA-1 hash of content
    # Legacy inline storage. Nullable now: new rows keep their bytes on disk.
    _content = Column("content", LargeBinary, nullable=True)
    size = Column(Integer, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    # Content type detection
    mime_type = Column(String(100))

    @property
    def content(self):
        """Blob bytes, from the database for legacy rows or from disk for new ones."""
        if self._content is not None:
            return self._content
        if not self.hash:
            return None
        from app.services.blob_store import store

        return store.get(self.hash)

    @content.setter
    def content(self, value):
        # Assigning inline keeps the legacy path working (and is what the migration
        # clears). Normal writes go through FileObjectCRUD, which stores to disk.
        self._content = value

    @property
    def is_on_disk(self) -> bool:
        return self._content is None

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
    # Monotonic counter for compare-and-swap. A caller that read generation N and asks to
    # update at N will fail if anyone else moved the branch in between, so two concurrent
    # updates cannot silently lose one another.
    generation = Column(Integer, nullable=False, default=0)
    
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
    # NULL means the source branch is in this same repository, which is every pull
    # request created before forks existed.
    source_repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=True)
    status = Column(String(20), default="open")  # open, merged, closed
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    merged_at = Column(DateTime)
    merge_commit_id = Column(String(40), ForeignKey("commits.id"))
    created_by_id = Column(Integer, ForeignKey("users.id"))
    reviewed_by_id = Column(Integer, ForeignKey("users.id"))

    repository = relationship("Repository", foreign_keys=[repository_id])
    source_repository = relationship("Repository", foreign_keys=[source_repository_id])
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


# ---------------------------------------------------------------------------
# Automation and integration
#
# Five features share this section: personal access tokens, SSH keys, webhooks
# (plus their delivery log), commit status checks, and server-side hook
# policies. They are grouped because they are the surfaces a machine - rather
# than a person at a browser - uses to talk to the server.
# ---------------------------------------------------------------------------


class AccessToken(Base):
    """A named, revocable credential belonging to one user.

    Only the SHA-256 of the secret is stored; the plaintext is shown once at
    creation and is unrecoverable afterwards. ``prefix`` keeps a short readable
    fragment so a user can tell two tokens apart in a list without it being
    enough to authenticate with.
    """

    __tablename__ = "access_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    prefix = Column(String(16), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    # Comma-separated: repo:read, repo:write, admin
    scopes = Column(String(200), nullable=False, default="repo:read")
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])

    __table_args__ = (
        Index("ix_access_tokens_user_active", "user_id", "revoked_at"),
    )


class SSHKey(Base):
    """A public key a user can authenticate with instead of a password.

    Stores the key in OpenSSH single-line form plus its fingerprint. The
    fingerprint is unique server-wide: the same key registered by two accounts
    would make the challenge-response ambiguous about who is signing in.
    """

    __tablename__ = "ssh_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(100), nullable=False)
    key_type = Column(String(30), nullable=False)          # ssh-rsa, ssh-ed25519, ecdsa-...
    public_key = Column(Text, nullable=False)              # full OpenSSH line
    fingerprint = Column(String(80), nullable=False, unique=True, index=True)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", foreign_keys=[user_id])


class SSHChallenge(Base):
    """A one-time nonce issued for SSH-key sign-in.

    Rows are deleted the moment they are consumed, so a captured challenge
    cannot be replayed; ``expires_at`` bounds how long an unused one lives.
    """

    __tablename__ = "ssh_challenges"

    id = Column(Integer, primary_key=True, index=True)
    nonce = Column(String(64), nullable=False, unique=True, index=True)
    username = Column(String(50), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class Webhook(Base):
    """An HTTP endpoint notified when events happen.

    ``repository_id`` NULL means the hook is global and fires for every
    repository, which is what a company-wide chat or audit sink wants.
    """

    __tablename__ = "webhooks"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=True, index=True)
    url = Column(String(500), nullable=False)
    secret = Column(String(128), nullable=True)            # signs the body; may be blank
    # Comma-separated event names, or "*" for everything
    events = Column(String(300), nullable=False, default="*")
    content_type = Column(String(50), nullable=False, default="application/json")
    active = Column(Boolean, default=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    repository = relationship("Repository")
    created_by = relationship("User", foreign_keys=[created_by_id])


class WebhookDelivery(Base):
    """One attempt to deliver one event to one webhook.

    Kept even when it succeeds: "did the integration actually receive the push"
    is the first question asked when a pipeline does not run, and without a log
    the answer is unknowable.
    """

    __tablename__ = "webhook_deliveries"

    id = Column(Integer, primary_key=True, index=True)
    webhook_id = Column(Integer, ForeignKey("webhooks.id"), nullable=False, index=True)
    event = Column(String(60), nullable=False)
    payload = Column(Text, nullable=False)
    status_code = Column(Integer, nullable=True)
    response_body = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    success = Column(Boolean, default=False)
    attempt = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())

    webhook = relationship("Webhook")

    __table_args__ = (
        Index("ix_webhook_deliveries_hook_time", "webhook_id", "created_at"),
    )


class CommitStatus(Base):
    """A build/test result reported against one commit by one reporter.

    ``context`` names the check ("ci/unit-tests"); the newest row for a given
    (commit, context) is that check's current state, and older rows are kept as
    history rather than overwritten.
    """

    __tablename__ = "commit_statuses"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False, index=True)
    commit_id = Column(String(40), nullable=False, index=True)
    context = Column(String(120), nullable=False)
    state = Column(String(20), nullable=False)             # pending|success|failure|error
    description = Column(String(300), nullable=True)
    target_url = Column(String(500), nullable=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    repository = relationship("Repository")
    created_by = relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        Index("ix_commit_statuses_commit_context", "commit_id", "context"),
    )


class ServerHook(Base):
    """A declarative pre-receive or post-receive policy.

    Deliberately not an arbitrary script: the server would have to execute
    whatever a repository admin uploaded, which turns a write-access
    compromise into remote code execution. Rules are declared as JSON and
    evaluated by ``app/services/server_hooks.py``; a hook that needs real logic
    delegates to an external endpoint over HTTP instead.
    """

    __tablename__ = "server_hooks"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=True, index=True)
    name = Column(String(100), nullable=False)
    hook_type = Column(String(20), nullable=False, default="pre-receive")  # pre-receive|post-receive
    enabled = Column(Boolean, default=True)
    config_json = Column(Text, nullable=False, default="{}")
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    repository = relationship("Repository")
    created_by = relationship("User", foreign_keys=[created_by_id])


# ---------------------------------------------------------------------------
# Security and operations
# ---------------------------------------------------------------------------


class LoginAttempt(Base):
    """One authentication attempt, successful or not.

    Attempts are recorded against the *submitted* username whether or not that
    account exists. That is deliberate: if only real accounts could be locked,
    the lockout response itself would tell an attacker which usernames are
    real. Recording every submitted name makes the throttle behave identically
    for both.

    Rows are pruned once they fall outside the lockout window, so this stays a
    small hot table rather than an ever-growing audit log.
    """

    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(150), nullable=False, index=True)
    ip_address = Column(String(64), nullable=True, index=True)
    success = Column(Boolean, default=False, nullable=False)
    user_agent = Column(String(300), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)

    __table_args__ = (
        Index("ix_login_attempts_user_time", "username", "created_at"),
        Index("ix_login_attempts_ip_time", "ip_address", "created_at"),
    )


class AccountLock(Base):
    """An account currently locked out, and why.

    Kept as its own row rather than derived purely from attempt counts so an
    administrator can see and clear a lock explicitly, and so a lock survives
    the pruning of the attempts that caused it.
    """

    __tablename__ = "account_locks"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(150), nullable=False, unique=True, index=True)
    locked_until = Column(DateTime, nullable=False)
    failed_count = Column(Integer, default=0)
    reason = Column(String(200), nullable=True)
    last_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class BackupRecord(Base):
    """A completed backup of the database and its blob store.

    The two are one unit: the database holds only hashes and sizes, so a
    database copy without the blob tree restores an index pointing at nothing.
    This row records both halves together with the verification result, so a
    restore never has to guess whether a snapshot was complete.
    """

    __tablename__ = "backup_records"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String(120), nullable=False)
    path = Column(String(500), nullable=False)
    database_bytes = Column(BigInteger, default=0)
    blob_bytes = Column(BigInteger, default=0)
    blob_count = Column(Integer, default=0)
    verified = Column(Boolean, default=False)
    missing_blobs = Column(Integer, default=0)
    duration_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)

    created_by = relationship("User", foreign_keys=[created_by_id])


class PasswordResetOTP(Base):
    """A pending one-time code for a password reset.

    Only a hash of the code is stored, so reading this table does not let anyone
    complete a reset. `attempts` is what makes a six-digit code safe to expose: without
    a cap, the whole keyspace is walkable in minutes.
    """

    __tablename__ = "password_reset_otps"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    username = Column(String(50), nullable=False, index=True)
    email = Column(String(100), nullable=False)

    code_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, default=0)
    consumed_at = Column(DateTime, nullable=True)

    # Who asked for the reset -- an admin acting on someone else's account, usually.
    requested_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)

    user = relationship("User", foreign_keys=[user_id])


class BranchProtectionPolicy(Base):
    """Protection rules for a branch name or pattern.

    Several policies can match one branch; they combine most-restrictively, so a
    repository-level rule can never weaken a more specific one. `policy_version`
    increments on every edit and is quoted in approvals and audit events, which is what
    makes an approval go stale when the rules change underneath it.
    """

    __tablename__ = "branch_protection_policies"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False, index=True)
    # Exact branch name, or an fnmatch pattern such as "releases/*".
    branch_pattern = Column(String(200), nullable=False)
    mode = Column(String(20), nullable=False, default="open")
    policy_version = Column(Integer, nullable=False, default=1)
    rules_json = Column(Text, nullable=True)

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    repository = relationship("Repository")

    __table_args__ = (
        Index("ix_branch_policies_repo_pattern", "repository_id", "branch_pattern", unique=True),
    )


class BranchUnlock(Base):
    """A scoped, expiring permission to perform one operation on one protected branch.

    Protection binds administrators too, so without this a frozen branch could never be
    corrected. Kept as a separate grant rather than an edit to the policy: the policy
    stays the record of intent, and the grant is the audited exception to it.
    """

    __tablename__ = "branch_unlocks"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False, index=True)
    branch_name = Column(String(200), nullable=False, index=True)
    # Comma-separated operations this grant covers, e.g. "update,delete".
    operations = Column(String(200), nullable=False)
    reason = Column(Text, nullable=False)

    requested_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    # Single use: set the moment the grant is spent.
    consumed_at = Column(DateTime, nullable=True)
    consumed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)

    requested_by = relationship("User", foreign_keys=[requested_by_id])


class ImmutableRelease(Base):
    """A permanent name for one exact commit.

    There is deliberately no update or delete path anywhere in the codebase: the absence
    of one is the guarantee. A mistaken release is corrected by publishing a new version,
    never by moving an existing one.
    """

    __tablename__ = "immutable_releases"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    commit_id = Column(String(40), ForeignKey("commits.id"), nullable=False)
    manifest_json = Column(Text, nullable=True)
    signature = Column(String(128), nullable=True)

    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    repository = relationship("Repository")

    __table_args__ = (
        Index("ix_immutable_releases_repo_name", "repository_id", "name", unique=True),
    )


class RefAuditEvent(Base):
    """Append-only record of every reference decision, allowed or denied.

    Separate from `activities`, which carries no before/after state and no tamper
    evidence. Events are hash-chained per repository so a deletion or edit in the middle
    of the chain is detectable.
    """

    __tablename__ = "ref_audit_events"

    id = Column(Integer, primary_key=True, index=True)
    repository_id = Column(String(16), ForeignKey("repositories.id"), nullable=False, index=True)
    event_type = Column(String(60), nullable=False)
    reference = Column(String(200), nullable=False, index=True)
    operation = Column(String(30), nullable=True)

    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    actor_username = Column(String(50), nullable=True)

    old_commit_id = Column(String(40), nullable=True)
    new_commit_id = Column(String(40), nullable=True)
    old_generation = Column(Integer, nullable=True)
    new_generation = Column(Integer, nullable=True)

    policy_id = Column(Integer, nullable=True)
    policy_version = Column(Integer, nullable=True)
    mode = Column(String(20), nullable=True)

    decision = Column(String(20), nullable=False)  # allowed | denied
    error_code = Column(String(40), nullable=True)
    reason = Column(Text, nullable=True)

    occurred_at = Column(DateTime, server_default=func.now(), index=True)
    previous_event_hash = Column(String(64), nullable=True)
    event_hash = Column(String(64), nullable=False)
