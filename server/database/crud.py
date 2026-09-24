from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, desc, func
from database.models import (
    User, Repository, RepositoryStar, Commit, CommitFile, FileObject, RepositoryTag, Branch, Activity,
    PendingCommit, PendingCommitFile, PendingRepository, PendingUserRegistration, UserPermission,
    CommitParent, Tag, Release, PullRequest, FileLineage,
    Issue, IssueComment, IssueLabel, IssueLabelLink, IssueWatcher, Milestone,
    IssueCommitLink, IssuePullRequestLink, IssueBranchLink, IssueEvent, Notification, IssueAccessRequest
)
from typing import List, Optional
from datetime import datetime, timedelta
import hashlib
import json
import re

# Imported as a module so tests can repoint `blob_store.store` at a temp directory.
from app.services import blob_store

class UserCRUD:
    @staticmethod
    def create_user(db: Session, username: str, email: str = None, full_name: str = None, role: str = 'developer', team_lead_id: int = None, password_hash: str = None) -> User:
        """Create a new user"""
        user = User(
            username=username,
            email=email,
            password_hash=password_hash,
            full_name=full_name,
            role=role,
            team_lead_id=team_lead_id
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    
    @staticmethod
    def get_user_by_username(db: Session, username: str) -> Optional[User]:
        """Get user by username"""
        return db.query(User).filter(User.username == username).first()
    
    @staticmethod
    def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
        """Get user by ID"""
        return db.query(User).filter(User.id == user_id).first()
    
    @staticmethod
    def get_or_create_user(db: Session, username: str, email: str = None, full_name: str = None) -> User:
        """Get existing user or create new one"""
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            user = UserCRUD.create_user(db, username, email, full_name)
        return user

class RepositoryCRUD:
    @staticmethod
    def generate_repo_id(username: str, repo_name: str) -> str:
        """Generate unique repository ID"""
        return hashlib.md5(f"{username}_{repo_name}".encode()).hexdigest()[:16]
    
    @staticmethod
    def create_repository(db: Session, username: str, repo_name: str, description: str = None) -> Repository:
        """Create a new repository"""
        # Get user - DO NOT auto-create
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            raise ValueError(f"User '{username}' does not exist")
        
        repo_id = RepositoryCRUD.generate_repo_id(username, repo_name)
        
        # Check if repository already exists
        existing_repo = db.query(Repository).filter(Repository.id == repo_id).first()
        if existing_repo:
            raise ValueError("Repository already exists")
        
        repository = Repository(
            id=repo_id,
            name=repo_name,
            description=description,
            owner_id=user.id
        )
        
        db.add(repository)
        db.commit()
        db.refresh(repository)
        
        # Create default branch
        BranchCRUD.create_branch(db, repo_id, "main", is_default=True)
        
        return repository
    
    @staticmethod
    def get_repository(db: Session, repo_id: str) -> Optional[Repository]:
        """Get repository by ID"""
        return db.query(Repository).filter(Repository.id == repo_id).first()
    
    @staticmethod
    def get_repositories_by_user(db: Session, username: str) -> List[Repository]:
        """Get all repositories for a user"""
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            return []
        return db.query(Repository).filter(Repository.owner_id == user.id).all()
    
    @staticmethod
    def get_all_repositories(db: Session) -> List[Repository]:
        """Get all repositories"""
        return db.query(Repository).all()

    @staticmethod
    def get_accessible_repositories_for_user(db: Session, user: User) -> List[Repository]:
        """Get all repositories accessible to a user:
        repos they own + repos they have committed to + repos they have explicit permissions for."""
        # Subquery: repo IDs where user has authored at least one commit
        authored_repo_ids = db.query(Commit.repository_id).filter(
            Commit.author_id == user.id
        ).distinct().subquery()

        # Subquery: repo IDs where user has an explicit permission entry
        permitted_repo_ids = db.query(UserPermission.repository_id).filter(
            UserPermission.user_id == user.id
        ).distinct().subquery()

        return db.query(Repository).filter(
            or_(
                Repository.owner_id == user.id,
                Repository.id.in_(authored_repo_ids),
                Repository.id.in_(permitted_repo_ids),
            )
        ).all()

    @staticmethod
    def get_repositories_for_web_browser(db: Session, user: User) -> List[Repository]:
        """Repositories to show in the main web UI for a developer.

        Includes:
        - owned repos
        - repos with a *global* (non-issue) UserPermission row where show_in_web_ui is true
          (or NULL for legacy rows).

        Omits repos where the user only has issue-scoped permission (issue_id set) — those
        should be accessed via the issue tracking UI rather than flooding the main repo list.
        Also omits any permission rows explicitly marked show_in_web_ui=false.
        """
        if not user:
            return []

        repo_ids = set()
        for (rid,) in db.query(Repository.id).filter(Repository.owner_id == user.id).all():
            repo_ids.add(rid)
        q_perm = db.query(UserPermission.repository_id).filter(UserPermission.user_id == user.id)
        # Only global (non-issue) collaborators belong in the main repository dashboard.
        q_perm = q_perm.filter(UserPermission.issue_id.is_(None))
        q_perm = q_perm.filter(
            or_(UserPermission.show_in_web_ui.is_(None), UserPermission.show_in_web_ui == True)  # noqa: E712
        )
        for (rid,) in q_perm.distinct().all():
            repo_ids.add(rid)

        if not repo_ids:
            return []
        return (
            db.query(Repository)
            .filter(Repository.id.in_(list(repo_ids)))
            .order_by(desc(Repository.updated_at))
            .all()
        )

    @staticmethod
    def archive_repository(db: Session, repo_id: str, reason: str = None) -> Repository:
        """Archive a repository"""
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise ValueError("Repository not found")
        
        repository.is_archived = True
        repository.archived_at = datetime.utcnow()
        repository.archived_reason = reason
        
        db.commit()
        db.refresh(repository)
        return repository
    
    @staticmethod
    def unarchive_repository(db: Session, repo_id: str) -> Repository:
        """Unarchive a repository (move back to active repositories)"""
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise ValueError("Repository not found")
        
        repository.is_archived = False
        repository.archived_at = None
        repository.archived_reason = None
        
        db.commit()
        db.refresh(repository)
        return repository
    
    @staticmethod
    def delete_repository(db: Session, repo_id: str) -> bool:
        """Delete a repository and all associated data"""
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise ValueError("Repository not found")
        
        # Delete all associated data (cascade should handle this, but being explicit)
        # Delete commits, commit files, branches, tags, activities, permissions, pending commits
        from database.models import Commit, CommitFile, Branch, RepositoryTag, Activity, PendingCommit, UserPermission
        
        # Delete pending commits
        db.query(PendingCommit).filter(PendingCommit.repository_id == repo_id).delete()
        
        # Delete user permissions
        db.query(UserPermission).filter(UserPermission.repository_id == repo_id).delete()
        
        # Delete activities
        db.query(Activity).filter(Activity.repository_id == repo_id).delete()
        
        # Delete repository tags
        db.query(RepositoryTag).filter(RepositoryTag.repository_id == repo_id).delete()
        
        # Delete branches
        db.query(Branch).filter(Branch.repository_id == repo_id).delete()
        
        # Delete commit files for commits in this repository
        commit_ids = [c.id for c in db.query(Commit).filter(Commit.repository_id == repo_id).all()]
        if commit_ids:
            db.query(CommitFile).filter(CommitFile.commit_id.in_(commit_ids)).delete(synchronize_session=False)
        
        # Delete commits
        db.query(Commit).filter(Commit.repository_id == repo_id).delete()
        
        # Finally delete the repository itself
        db.delete(repository)
        db.commit()
        
        return True
    
    @staticmethod
    def calculate_repository_size(db: Session, repo_id: str) -> int:
        """Calculate repository size (bytes) for the current HEAD snapshot.

        This is used for UI display only. We define "repository size" as the sum of
        file sizes in the current head commit (i.e. latest snapshot), not the full
        historical storage cost across all commits.
        """
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository or not repository.head_commit_id:
            return 0

        try:
            # Prefer the persisted file_size on CommitFile (fast).
            total = (
                db.query(func.coalesce(func.sum(CommitFile.file_size), 0))
                .filter(CommitFile.commit_id == repository.head_commit_id)
                .scalar()
            )
            return int(total or 0)
        except Exception:
            # Fallback: sum FileObject sizes for the commit's file hashes.
            try:
                total = (
                    db.query(func.coalesce(func.sum(FileObject.size), 0))
                    .join(CommitFile, CommitFile.file_hash == FileObject.hash)
                    .filter(CommitFile.commit_id == repository.head_commit_id)
                    .scalar()
                )
                return int(total or 0)
            except Exception:
                return 0
    
    @staticmethod
    def update_repository_size(db: Session, repo_id: str) -> Repository:
        """Update repository size based on current head commit"""
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise ValueError("Repository not found")
        
        size = RepositoryCRUD.calculate_repository_size(db, repo_id)
        repository.size_bytes = size
        repository.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(repository)
        return repository

    @staticmethod
    def update_repository_details(db: Session, repo_id: str, g1_coordinator: str = None, 
                                tested: bool = None, instruction_manual_path: str = None, 
                                instruction_manual_filename: str = None) -> Repository:
        """Update repository G1 coordinator, testing status, and instruction manual"""
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise ValueError("Repository not found")
        
        if g1_coordinator is not None:
            repository.g1_coordinator = g1_coordinator
        if tested is not None:
            repository.tested = tested
        if instruction_manual_path is not None:
            repository.instruction_manual_path = instruction_manual_path
        if instruction_manual_filename is not None:
            repository.instruction_manual_filename = instruction_manual_filename
        
        db.commit()
        db.refresh(repository)
        return repository

class CommitCRUD:
    @staticmethod
    def _record_file_lineage(db: Session, repository_id: str, commit_id: str, parent_commit_id: Optional[str]):
        """Detect path-only renames (same content hash, different path) and persist lineage links."""
        if not parent_commit_id:
            return

        parent_files = db.query(CommitFile).filter(CommitFile.commit_id == parent_commit_id).all()
        current_files = db.query(CommitFile).filter(CommitFile.commit_id == commit_id).all()

        parent_by_path = {f.file_path: f.file_hash for f in parent_files}
        current_by_path = {f.file_path: f.file_hash for f in current_files}

        parent_hash_to_paths = {}
        for path, file_hash in parent_by_path.items():
            parent_hash_to_paths.setdefault(file_hash, set()).add(path)

        removed_paths = set(parent_by_path.keys()) - set(current_by_path.keys())
        added_paths = set(current_by_path.keys()) - set(parent_by_path.keys())

        for new_path in sorted(added_paths):
            file_hash = current_by_path[new_path]
            candidates = sorted(path for path in parent_hash_to_paths.get(file_hash, set()) if path in removed_paths and path != new_path)
            if not candidates:
                continue

            old_path = candidates[0]
            db.add(FileLineage(
                repository_id=repository_id,
                commit_id=commit_id,
                old_path=old_path,
                new_path=new_path,
                file_hash=file_hash,
            ))

    @staticmethod
    def create_commit(db: Session, commit_data: dict) -> Commit:
        """Create a new commit"""
        repository = RepositoryCRUD.get_repository(db, commit_data["repository_id"])
        if not repository:
            raise ValueError("Repository not found")
        
        author = UserCRUD.get_user_by_username(db, commit_data["author"])
        if not author:
            raise ValueError(f"User '{commit_data['author']}' does not exist. Please contact an administrator to create your account.")
        
        commit = Commit(
            id=commit_data["id"],
            repository_id=commit_data["repository_id"],
            author_id=author.id,
            parent_commit_id=(commit_data.get("parents") or [commit_data.get("parent")])[0],
            message=commit_data["message"],
            tree_hash=commit_data.get("tree_hash")
        )
        
        db.add(commit)
        db.flush()  # Get the commit ID without committing
        
        # Store files
        for file_path, file_content in commit_data.get("files", {}).items():
            commit_file = FileObjectCRUD.store_file_and_create_commit_file(
                db, commit.id, file_path, file_content
            )

        # Store parent relationships
        parents = commit_data.get("parents") or []
        if not parents and commit_data.get("parent"):
            parents = [commit_data.get("parent")]
        for idx, parent_id in enumerate([p for p in parents if p]):
            db.add(CommitParent(commit_id=commit.id, parent_commit_id=parent_id, parent_order=idx))
        
        # Ensure commit files are flushed before size calculation
        db.flush()

        # Capture rename lineage before commit finalization.
        CommitCRUD._record_file_lineage(db, repository.id, commit.id, commit.parent_commit_id)
        
        # Update repository head
        repository.head_commit_id = commit.id
        repository.updated_at = datetime.utcnow()
        
        # Recalculate repository size based on all files in the repository
        repository.size_bytes = RepositoryCRUD.calculate_repository_size(db, repository.id)
        
        db.commit()
        db.refresh(commit)
        return commit

    @staticmethod
    def create_commit_from_file_hashes(db: Session, commit_data: dict, file_entries: List[PendingCommitFile]) -> Commit:
        """Create a new commit using existing file hashes (no base64 payloads)."""
        repository = RepositoryCRUD.get_repository(db, commit_data["repository_id"])
        if not repository:
            raise ValueError("Repository not found")
        
        author = UserCRUD.get_user_by_username(db, commit_data["author"])
        if not author:
            raise ValueError(f"User '{commit_data['author']}' does not exist. Please contact an administrator to create your account.")
        
        commit = Commit(
            id=commit_data["id"],
            repository_id=commit_data["repository_id"],
            author_id=author.id,
            parent_commit_id=(commit_data.get("parents") or [commit_data.get("parent")])[0],
            message=commit_data["message"],
            tree_hash=commit_data.get("tree_hash")
        )
        
        db.add(commit)
        db.flush()
        
        for entry in file_entries:
            commit_file = CommitFile(
                commit_id=commit.id,
                file_path=entry.file_path,
                file_hash=entry.file_hash,
                file_size=entry.file_size
            )
            file_object = db.query(FileObject).filter(FileObject.hash == entry.file_hash).first()
            if file_object:
                commit_file.file_object = file_object
            db.add(commit_file)

        parents = commit_data.get("parents") or []
        if not parents and commit_data.get("parent"):
            parents = [commit_data.get("parent")]
        for idx, parent_id in enumerate([p for p in parents if p]):
            db.add(CommitParent(commit_id=commit.id, parent_commit_id=parent_id, parent_order=idx))
        
        # Ensure commit files are flushed before size calculation
        db.flush()

        # Capture rename lineage before commit finalization.
        CommitCRUD._record_file_lineage(db, repository.id, commit.id, commit.parent_commit_id)
        
        repository.head_commit_id = commit.id
        repository.updated_at = datetime.utcnow()
        repository.size_bytes = RepositoryCRUD.calculate_repository_size(db, repository.id)
        
        db.commit()
        db.refresh(commit)
        return commit
    
    @staticmethod
    def get_commit(db: Session, commit_id: str) -> Optional[Commit]:
        """Get commit by ID"""
        return db.query(Commit).filter(Commit.id == commit_id).first()
    
    @staticmethod
    def get_commits_by_repository(
        db: Session, repo_id: str, limit: Optional[int] = 50
    ) -> List[Commit]:
        """Commits for a repository, newest first. `limit=None` returns all of them.

        Callers that filter by branch or paginate afterwards must pass None: a SQL
        limit applied here truncates the history *before* those steps, which silently
        hid everything past the newest 50 commits no matter what the caller asked for.
        """
        query = db.query(Commit).filter(
            Commit.repository_id == repo_id
        ).order_by(desc(Commit.created_at))
        if limit is not None:
            query = query.limit(limit)
        return query.all()

class FileObjectCRUD:
    @staticmethod
    def calculate_file_hash(content: bytes) -> str:
        """Calculate SHA-1 hash of file content"""
        return hashlib.sha1(content).hexdigest()
    
    @staticmethod
    def store_file_object(db: Session, content: bytes, mime_type: str = None) -> FileObject:
        """Store a file object.

        Bytes go to the on-disk blob store; the row keeps only hash, size and mime type.
        Content is written before the row is committed, so a row can never reference a
        blob that is not on disk (the reverse -- a blob with no row -- is just garbage
        and is what `fox gc` reclaims).
        """
        file_hash = FileObjectCRUD.calculate_file_hash(content)

        # Check if file already exists
        existing_file = db.query(FileObject).filter(FileObject.hash == file_hash).first()
        if existing_file:
            # A legacy row may still hold its bytes inline; leave it alone. Otherwise make
            # sure the blob is really on disk before handing the row back.
            if existing_file.is_on_disk and not blob_store.store.exists(file_hash):
                blob_store.store.put(content, file_hash)
            return existing_file

        blob_store.store.put(content, file_hash)

        file_object = FileObject(
            hash=file_hash,
            size=len(content),
            mime_type=mime_type,
        )

        db.add(file_object)
        db.commit()
        db.refresh(file_object)
        return file_object
    
    @staticmethod
    def store_file_and_create_commit_file(db: Session, commit_id: str, file_path: str, file_content: str):
        """Store file content and create commit file entry"""
        import base64
        
        # Decode base64 content
        content = base64.b64decode(file_content.encode())
        
        # Store file object
        file_object = FileObjectCRUD.store_file_object(db, content)
        
        # Create commit file entry
        commit_file = CommitFile(
            commit_id=commit_id,
            file_path=file_path,
            file_hash=file_object.hash,
            file_size=file_object.size
        )
        # Keep relationship populated for downstream consumers
        commit_file.file_object = file_object
        
        db.add(commit_file)
        return commit_file

class BranchCRUD:
    @staticmethod
    def create_branch(db: Session, repo_id: str, branch_name: str, head_commit_id: str = None, is_default: bool = False) -> Branch:
        """Create a new branch"""
        branch = Branch(
            repository_id=repo_id,
            name=branch_name,
            head_commit_id=head_commit_id,
            is_default=is_default
        )
        
        db.add(branch)
        db.commit()
        db.refresh(branch)
        return branch
    
    @staticmethod
    def get_branches_by_repository(db: Session, repo_id: str) -> List[Branch]:
        """Get all branches for a repository"""
        return db.query(Branch).filter(Branch.repository_id == repo_id).all()

    @staticmethod
    def get_branch(db: Session, repo_id: str, branch_name: str) -> Optional[Branch]:
        return db.query(Branch).filter(
            Branch.repository_id == repo_id,
            Branch.name == branch_name
        ).first()

    @staticmethod
    def update_branch_head(db: Session, repo_id: str, branch_name: str, head_commit_id: str) -> Branch:
        branch = BranchCRUD.get_branch(db, repo_id, branch_name)
        if not branch:
            raise ValueError("Branch not found")
        branch.head_commit_id = head_commit_id
        # Keep repository head aligned with default branch
        if branch.is_default:
            repository = RepositoryCRUD.get_repository(db, repo_id)
            if repository:
                repository.head_commit_id = head_commit_id
        db.commit()
        db.refresh(branch)
        return branch

    @staticmethod
    def rename_branch(db: Session, repo_id: str, old_name: str, new_name: str) -> Branch:
        branch = BranchCRUD.get_branch(db, repo_id, old_name)
        if not branch:
            raise ValueError("Branch not found")
        branch.name = new_name
        db.commit()
        db.refresh(branch)
        return branch

    @staticmethod
    def delete_branch(db: Session, repo_id: str, branch_name: str) -> bool:
        branch = BranchCRUD.get_branch(db, repo_id, branch_name)
        if not branch:
            raise ValueError("Branch not found")
        db.delete(branch)
        db.commit()
        return True

    @staticmethod
    def set_default_branch(db: Session, repo_id: str, branch_name: str) -> Branch:
        branch = BranchCRUD.get_branch(db, repo_id, branch_name)
        if not branch:
            raise ValueError("Branch not found")
        db.query(Branch).filter(Branch.repository_id == repo_id).update({Branch.is_default: False})
        branch.is_default = True
        db.commit()
        db.refresh(branch)
        return branch

class TagCRUD:
    @staticmethod
    def create_tag(db: Session, repo_id: str, name: str, commit_id: str, message: str = None, created_by_id: int = None) -> Tag:
        existing = db.query(Tag).filter(Tag.repository_id == repo_id, Tag.name == name).first()
        if existing:
            raise ValueError("Tag already exists")
        tag = Tag(
            repository_id=repo_id,
            name=name,
            commit_id=commit_id,
            message=message,
            created_by_id=created_by_id
        )
        db.add(tag)
        db.commit()
        db.refresh(tag)
        return tag

    @staticmethod
    def get_tag(db: Session, repo_id: str, name: str) -> Optional[Tag]:
        return db.query(Tag).filter(Tag.repository_id == repo_id, Tag.name == name).first()

    @staticmethod
    def list_tags(db: Session, repo_id: str) -> List[Tag]:
        return db.query(Tag).filter(Tag.repository_id == repo_id).order_by(Tag.created_at.desc()).all()

    @staticmethod
    def delete_tag(db: Session, repo_id: str, name: str) -> bool:
        tag = TagCRUD.get_tag(db, repo_id, name)
        if not tag:
            raise ValueError("Tag not found")
        db.delete(tag)
        db.commit()
        return True

class ReleaseCRUD:
    @staticmethod
    def create_release(db: Session, repo_id: str, tag_id: int, version: str, title: str = None, notes: str = None, created_by_id: int = None) -> Release:
        release = Release(
            repository_id=repo_id,
            tag_id=tag_id,
            version=version,
            title=title,
            notes=notes,
            created_by_id=created_by_id
        )
        db.add(release)
        db.commit()
        db.refresh(release)
        return release

    @staticmethod
    def list_releases(db: Session, repo_id: str) -> List[Release]:
        return db.query(Release).filter(Release.repository_id == repo_id).order_by(Release.created_at.desc()).all()

    @staticmethod
    def get_release(db: Session, repo_id: str, version: str) -> Optional[Release]:
        return db.query(Release).filter(Release.repository_id == repo_id, Release.version == version).first()

class PullRequestCRUD:
    @staticmethod
    def create_pull_request(db: Session, repo_id: str, title: str, description: str, source_branch: str, target_branch: str, created_by_id: int) -> PullRequest:
        pr = PullRequest(
            repository_id=repo_id,
            title=title,
            description=description,
            source_branch=source_branch,
            target_branch=target_branch,
            created_by_id=created_by_id,
            status="open"
        )
        db.add(pr)
        db.commit()
        db.refresh(pr)
        return pr

    @staticmethod
    def get_pull_request(db: Session, repo_id: str, pr_id: int) -> Optional[PullRequest]:
        return db.query(PullRequest).filter(PullRequest.repository_id == repo_id, PullRequest.id == pr_id).first()

    @staticmethod
    def list_pull_requests(db: Session, repo_id: str, status: Optional[str] = None) -> List[PullRequest]:
        query = db.query(PullRequest).filter(PullRequest.repository_id == repo_id)
        if status:
            query = query.filter(PullRequest.status == status)
        return query.order_by(desc(PullRequest.created_at)).all()

    @staticmethod
    def close_pull_request(db: Session, pr: PullRequest, reviewer_id: int = None, comment: str = None) -> PullRequest:
        pr.status = "closed"
        pr.reviewed_by_id = reviewer_id
        pr.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(pr)
        return pr

    @staticmethod
    def mark_merged(db: Session, pr: PullRequest, merge_commit_id: str, reviewer_id: int = None) -> PullRequest:
        pr.status = "merged"
        pr.merge_commit_id = merge_commit_id
        pr.reviewed_by_id = reviewer_id
        pr.merged_at = datetime.utcnow()
        pr.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(pr)
        return pr

class ActivityCRUD:
    @staticmethod
    def create_activity(db: Session, user_id: int, activity_type: str, description: str = None, repository_id: str = None):
        """Create an activity record"""
        activity = Activity(
            user_id=user_id,
            repository_id=repository_id,
            activity_type=activity_type,
            description=description
        )
        
        db.add(activity)
        db.commit()
        return activity
    
    @staticmethod
    def get_recent_activities(db: Session, limit: int = 20) -> List[Activity]:
        """Get recent activities"""
        return db.query(Activity).order_by(desc(Activity.created_at)).limit(limit).all()


class UserEngagementCRUD:
    """Admin-facing engagement / performance metrics per user."""

    @staticmethod
    def _max_dt(*values):
        present = [v for v in values if v is not None]
        return max(present) if present else None

    @staticmethod
    def _status(last_login_at, last_work_at, now: datetime) -> str:
        """Active / Idle / Never used based on login + real work."""
        last_seen = UserEngagementCRUD._max_dt(last_login_at, last_work_at)
        if last_seen is None:
            return "never_used"
        if last_seen >= now - timedelta(days=7):
            return "active"
        return "idle"

    @staticmethod
    def _activity_score(commits_7d: int, commits_30d: int, last_login_at, last_work_at, now: datetime) -> str:
        """High / Medium / Low quick ranking from recent output + recency."""
        last_work = last_work_at
        if commits_30d >= 10 or commits_7d >= 3 or (last_work and last_work >= now - timedelta(days=3)):
            return "high"
        if (
            commits_30d >= 3
            or (last_work and last_work >= now - timedelta(days=14))
            or (last_login_at and last_login_at >= now - timedelta(days=7))
        ):
            return "medium"
        return "low"

    @staticmethod
    def get_all_user_engagement(db: Session) -> List[dict]:
        """Build engagement rows for every user (admin Users page)."""
        now = datetime.utcnow()
        since_7d = now - timedelta(days=7)
        since_30d = now - timedelta(days=30)

        users = db.query(User).order_by(User.username.asc()).all()
        if not users:
            return []

        user_ids = [u.id for u in users]

        commit_7d_rows = (
            db.query(Commit.author_id, func.count(Commit.id))
            .filter(Commit.author_id.in_(user_ids), Commit.created_at >= since_7d)
            .group_by(Commit.author_id)
            .all()
        )
        commit_30d_rows = (
            db.query(Commit.author_id, func.count(Commit.id))
            .filter(Commit.author_id.in_(user_ids), Commit.created_at >= since_30d)
            .group_by(Commit.author_id)
            .all()
        )
        last_commit_rows = (
            db.query(Commit.author_id, func.max(Commit.created_at))
            .filter(Commit.author_id.in_(user_ids))
            .group_by(Commit.author_id)
            .all()
        )
        pending_wait_rows = (
            db.query(PendingCommit.author_id, func.count(PendingCommit.id))
            .filter(
                PendingCommit.author_id.in_(user_ids),
                PendingCommit.status == "pending",
            )
            .group_by(PendingCommit.author_id)
            .all()
        )
        last_pending_rows = (
            db.query(PendingCommit.author_id, func.max(PendingCommit.created_at))
            .filter(PendingCommit.author_id.in_(user_ids))
            .group_by(PendingCommit.author_id)
            .all()
        )
        last_activity_rows = (
            db.query(Activity.user_id, func.max(Activity.created_at))
            .filter(Activity.user_id.in_(user_ids))
            .group_by(Activity.user_id)
            .all()
        )
        repo_count_rows = (
            db.query(Repository.owner_id, func.count(Repository.id))
            .filter(Repository.owner_id.in_(user_ids))
            .group_by(Repository.owner_id)
            .all()
        )

        commits_7d = {uid: n for uid, n in commit_7d_rows}
        commits_30d = {uid: n for uid, n in commit_30d_rows}
        last_commit = {uid: ts for uid, ts in last_commit_rows}
        pending_waits = {uid: n for uid, n in pending_wait_rows}
        last_pending = {uid: ts for uid, ts in last_pending_rows}
        last_activity = {uid: ts for uid, ts in last_activity_rows}
        repos_owned = {uid: n for uid, n in repo_count_rows}

        rows = []
        for user in users:
            uid = user.id
            last_work_at = UserEngagementCRUD._max_dt(
                last_commit.get(uid),
                last_pending.get(uid),
                last_activity.get(uid),
            )
            last_login_at = getattr(user, "last_login_at", None)
            c7 = int(commits_7d.get(uid, 0) or 0)
            c30 = int(commits_30d.get(uid, 0) or 0)
            status = UserEngagementCRUD._status(last_login_at, last_work_at, now)
            score = UserEngagementCRUD._activity_score(c7, c30, last_login_at, last_work_at, now)

            rows.append({
                "id": user.id,
                "username": user.username,
                "full_name": user.full_name,
                "email": user.email,
                "role": getattr(user, "role", "developer") or "developer",
                "team_lead_id": user.team_lead_id,
                "team_lead_name": user.team_lead.username if user.team_lead else None,
                "is_active": user.is_active,
                "created_at": user.created_at.isoformat() if user.created_at else None,
                "last_login_at": last_login_at.isoformat() if last_login_at else None,
                "last_work_at": last_work_at.isoformat() if last_work_at else None,
                "commits_7d": c7,
                "commits_30d": c30,
                "pending_waits": int(pending_waits.get(uid, 0) or 0),
                "repos_owned": int(repos_owned.get(uid, 0) or 0),
                "status": status,
                "activity_score": score,
            })
        return rows

    @staticmethod
    def get_user_detail(db: Session, username: str, limit: int = 100) -> Optional[dict]:
        """Engagement + activity feed + history timeline for one user (admin detail tabs)."""
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            return None

        engagement_rows = UserEngagementCRUD.get_all_user_engagement(db)
        engagement = next((r for r in engagement_rows if r["username"] == username), None)
        if not engagement:
            return None

        activities = (
            db.query(Activity)
            .filter(Activity.user_id == user.id)
            .order_by(desc(Activity.created_at))
            .limit(limit)
            .all()
        )
        activity_feed = [
            {
                "id": a.id,
                "type": a.activity_type,
                "description": a.description,
                "repository": a.repository.name if a.repository else None,
                "repository_id": a.repository_id,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in activities
        ]

        timeline = []

        commits = (
            db.query(Commit)
            .filter(Commit.author_id == user.id)
            .order_by(desc(Commit.created_at))
            .limit(limit)
            .all()
        )
        for c in commits:
            timeline.append({
                "kind": "commit",
                "title": c.message[:120] if c.message else "Commit",
                "detail": f"Commit {c.id[:12]}",
                "repository": c.repository.name if c.repository else None,
                "repository_id": c.repository_id,
                "status": "approved",
                "created_at": c.created_at.isoformat() if c.created_at else None,
            })

        pending = (
            db.query(PendingCommit)
            .filter(PendingCommit.author_id == user.id)
            .order_by(desc(PendingCommit.created_at))
            .limit(limit)
            .all()
        )
        for p in pending:
            timeline.append({
                "kind": "pending_commit",
                "title": p.message[:120] if p.message else "Pending commit",
                "detail": f"Pending {p.id[:12]} · {p.status}",
                "repository": p.repository.name if p.repository else None,
                "repository_id": p.repository_id,
                "status": p.status,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            })

        repos = (
            db.query(Repository)
            .filter(Repository.owner_id == user.id)
            .order_by(desc(Repository.created_at))
            .limit(limit)
            .all()
        )
        for r in repos:
            timeline.append({
                "kind": "repository",
                "title": f"Created repository {r.name}",
                "detail": r.description[:120] if r.description else "Repository ownership",
                "repository": r.name,
                "repository_id": r.id,
                "status": "archived" if r.is_archived else "active",
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })

        pending_repos = (
            db.query(PendingRepository)
            .filter(PendingRepository.requested_by_id == user.id)
            .order_by(desc(PendingRepository.created_at))
            .limit(limit)
            .all()
        )
        for pr in pending_repos:
            timeline.append({
                "kind": "repo_request",
                "title": f"Requested repository {pr.repo_name}",
                "detail": f"Status: {pr.status}",
                "repository": pr.repo_name,
                "repository_id": None,
                "status": pr.status,
                "created_at": pr.created_at.isoformat() if pr.created_at else None,
            })

        for a in activities:
            timeline.append({
                "kind": "activity",
                "title": a.activity_type.replace("_", " ").title(),
                "detail": a.description,
                "repository": a.repository.name if a.repository else None,
                "repository_id": a.repository_id,
                "status": None,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })

        timeline.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        timeline = timeline[:limit]

        return {
            "user": engagement,
            "activity": activity_feed,
            "history": timeline,
        }


class PendingCommitCRUD:
    @staticmethod
    def create_pending_commit(db: Session, commit_data: dict) -> PendingCommit:
        """Create a new pending commit awaiting approval"""

        existing = PendingCommitCRUD.get_pending_commit(db, commit_data["id"])
        if existing:
            # Tag so callers can avoid treating this as a new submission.
            try:
                setattr(existing, "_foxnest_existing", True)
            except Exception:
                pass
            return existing
        
        repository = RepositoryCRUD.get_repository(db, commit_data["repository_id"])
        if not repository:
            raise ValueError("Repository not found")
        
        author = UserCRUD.get_user_by_username(db, commit_data["author"])
        if not author:
            raise ValueError(f"User '{commit_data['author']}' does not exist. Please contact an administrator to create your account.")
        
        pending_commit = PendingCommit(
            id=commit_data["id"],
            repository_id=commit_data["repository_id"],
            author_id=author.id,
            parent_commit_id=commit_data.get("parent"),
            message=commit_data["message"],
            tree_hash=commit_data.get("tree_hash"),
            status='pending'
        )
        
        db.add(pending_commit)
        db.flush()
        
        # Store file objects and metadata without huge JSON payloads
        import base64
        import json
        files_meta = {}
        branch_name = commit_data.get("branch")
        for file_path, file_content in commit_data.get("files", {}).items():
            content = base64.b64decode(file_content.encode())
            file_object = FileObjectCRUD.store_file_object(db, content)
            pending_file = PendingCommitFile(
                pending_commit_id=pending_commit.id,
                file_path=file_path,
                file_hash=file_object.hash,
                file_size=file_object.size
            )
            db.add(pending_file)
            files_meta[file_path] = file_object.hash
        
        payload = {
            "files": files_meta,
            "meta": {
                "branch": branch_name
            }
        }
        # Store lightweight metadata (path -> hash) for compatibility
        pending_commit.files_data = json.dumps(payload)
        
        db.commit()
        db.refresh(pending_commit)
        return pending_commit
    
    @staticmethod
    def get_pending_commit(db: Session, commit_id: str):
        """Get pending commit by ID"""
        return db.query(PendingCommit).filter(PendingCommit.id == commit_id).first()
    
    @staticmethod
    def get_pending_commits_by_repository(db: Session, repo_id: str, status: str = None):
        """Get pending commits for a repository"""
        query = db.query(PendingCommit).filter(PendingCommit.repository_id == repo_id)
        if status:
            query = query.filter(PendingCommit.status == status)
        return query.order_by(desc(PendingCommit.created_at)).all()
    
    @staticmethod
    def get_all_pending_commits(db: Session, status: str = 'pending'):
        """Get all pending commits across all repositories"""
        query = db.query(PendingCommit)
        if status:
            query = query.filter(PendingCommit.status == status)
        return query.order_by(desc(PendingCommit.created_at)).all()
    
    @staticmethod
    def approve_pending_commit(db: Session, commit_id: str, reviewer_username: str, comment: str = None):
        """Approve a pending commit and convert it to a real commit"""
        import json
        
        pending = PendingCommitCRUD.get_pending_commit(db, commit_id)
        if not pending:
            raise ValueError("Pending commit not found")
        
        if pending.status != 'pending':
            raise ValueError(f"Commit is already {pending.status}")
        
        reviewer = UserCRUD.get_user_by_username(db, reviewer_username)
        if not reviewer:
            raise ValueError("Reviewer not found")
        
        # Update pending commit status
        pending.status = 'approved'
        pending.reviewed_by_id = reviewer.id
        pending.reviewed_at = datetime.utcnow()
        pending.review_comment = comment
        
        # Create actual commit
        commit_data = {
            "id": pending.id,
            "repository_id": pending.repository_id,
            "author": pending.author.username,
            "parent": pending.parent_commit_id,
            "message": pending.message,
            "tree_hash": pending.tree_hash,
        }
        
        branch_name = None
        if pending.files_data:
            try:
                raw = json.loads(pending.files_data)
                if isinstance(raw, dict) and "meta" in raw:
                    branch_name = raw.get("meta", {}).get("branch")
            except Exception:
                branch_name = None

        if pending.files:
            commit = CommitCRUD.create_commit_from_file_hashes(db, commit_data, list(pending.files))
        else:
            # Fallback to legacy JSON payloads if present
            files_data = json.loads(pending.files_data) if pending.files_data else {}
            if isinstance(files_data, dict) and "files" in files_data:
                commit_data["files"] = files_data.get("files", {})
                branch_name = files_data.get("meta", {}).get("branch")
            else:
                commit_data["files"] = files_data
            commit = CommitCRUD.create_commit(db, commit_data)
        
        db.commit()
        db.refresh(pending)
        if branch_name:
            try:
                from app.services import refs as _refs

                _refs.update_reference_by_id(
                    db,
                    repository_id=commit.repository_id,
                    actor_username=getattr(commit.author, "username", None),
                    branch_name=branch_name,
                    new_commit_id=commit.id,
                    create_if_missing=True,
                )
            except Exception:
                # A refused reference change must not be swallowed here: approving a
                # queued commit would otherwise report success while the branch stayed
                # put, or worse, move a frozen branch.
                raise

        return pending, commit
    
    @staticmethod
    def reject_pending_commit(db: Session, commit_id: str, reviewer_username: str, comment: str = None):
        """Reject a pending commit"""
        
        pending = PendingCommitCRUD.get_pending_commit(db, commit_id)
        if not pending:
            raise ValueError("Pending commit not found")
        
        if pending.status != 'pending':
            raise ValueError(f"Commit is already {pending.status}")
        
        reviewer = UserCRUD.get_user_by_username(db, reviewer_username)
        if not reviewer:
            raise ValueError("Reviewer not found")
        
        pending.status = 'rejected'
        pending.reviewed_by_id = reviewer.id
        pending.reviewed_at = datetime.utcnow()
        pending.review_comment = comment
        
        db.commit()
        db.refresh(pending)
        return pending

class UserPermissionCRUD:
    @staticmethod
    def create_permission(
        db: Session,
        username: str,
        repo_id: str,
        permission_level: str,
        granted_by_username: str = None,
        show_in_web_ui: bool = True,
        issue_id: Optional[int] = None,
    ):
        """Grant a user permission to a repository.

        Global grants use issue_id=None (one row per user+repo). Issue access approvals use a
        distinct row per (user, repo, issue) so multiple issues can grant concurrently and
        closing an issue only revokes that row.
        """
        
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            raise ValueError(f"User {username} not found")
        
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise ValueError("Repository not found")
        
        granted_by = None
        if granted_by_username:
            granted_by = UserCRUD.get_user_by_username(db, granted_by_username)
        
        if issue_id is not None:
            existing = db.query(UserPermission).filter(
                and_(
                    UserPermission.user_id == user.id,
                    UserPermission.repository_id == repo_id,
                    UserPermission.issue_id == issue_id,
                )
            ).first()
        else:
            existing = db.query(UserPermission).filter(
                and_(
                    UserPermission.user_id == user.id,
                    UserPermission.repository_id == repo_id,
                    UserPermission.issue_id.is_(None),
                )
            ).first()
        
        if existing:
            existing.permission_level = permission_level
            existing.granted_by_id = granted_by.id if granted_by else None
            existing.granted_at = datetime.utcnow()
            existing.show_in_web_ui = show_in_web_ui
            db.commit()
            db.refresh(existing)
            return existing
        
        permission = UserPermission(
            user_id=user.id,
            repository_id=repo_id,
            permission_level=permission_level,
            granted_by_id=granted_by.id if granted_by else None,
            show_in_web_ui=show_in_web_ui,
            issue_id=issue_id,
        )
        
        db.add(permission)
        db.commit()
        db.refresh(permission)
        return permission
    
    @staticmethod
    def get_user_permission(db: Session, username: str, repo_id: str):
        """Best effective permission for a user on a repository (highest level if multiple rows)."""
        
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            return None
        
        perms = db.query(UserPermission).filter(
            and_(UserPermission.user_id == user.id, UserPermission.repository_id == repo_id)
        ).all()
        if not perms:
            return None
        rank = {"read": 0, "write": 1, "team_lead": 2, "admin": 3}
        return max(perms, key=lambda p: rank.get(p.permission_level, 0))
    
    @staticmethod
    def get_repository_permissions(db: Session, repo_id: str):
        """Get all permissions for a repository"""
        return db.query(UserPermission).filter(UserPermission.repository_id == repo_id).all()
    
    @staticmethod
    def get_user_permissions(db: Session, username: str):
        """Get all permissions for a user"""
        
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            return []
        
        return db.query(UserPermission).filter(UserPermission.user_id == user.id).all()
    
    @staticmethod
    def revoke_permission(db: Session, username: str, repo_id: str):
        """Revoke all permission rows for a user on a repository (global + issue-scoped)."""
        
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            raise ValueError(f"User {username} not found")
        
        n = db.query(UserPermission).filter(
            and_(UserPermission.user_id == user.id, UserPermission.repository_id == repo_id)
        ).delete(synchronize_session=False)
        
        if n:
            db.commit()
            return True
        return False
    
    @staticmethod
    def revoke_issue_scoped_for_issue(db: Session, issue_id: int) -> int:
        """Remove permissions that were granted only for this issue (access ends when issue closes)."""
        return db.query(UserPermission).filter(UserPermission.issue_id == issue_id).delete(
            synchronize_session=False
        )
    
    @staticmethod
    def has_permission(db: Session, username: str, repo_id: str, required_level: str = 'write') -> bool:
        """Check if a user has permission to access a repository"""
        # Define permission hierarchy
        permission_hierarchy = {
            'read': 0,
            'write': 1,
            'team_lead': 2,
            'admin': 3
        }
        
        # Repository owner always has admin access
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if repository and repository.owner.username == username:
            return True
        
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            return False
        perms = db.query(UserPermission).filter(
            and_(UserPermission.user_id == user.id, UserPermission.repository_id == repo_id)
        ).all()
        if not perms:
            return False
        
        required_rank = permission_hierarchy.get(required_level, 1)
        user_rank = max(permission_hierarchy.get(p.permission_level, 0) for p in perms)
        
        return user_rank >= required_rank


class RepositoryStarCRUD:
    @staticmethod
    def count_for_repository(db: Session, repo_id: str) -> int:
        return int(
            db.query(RepositoryStar).filter(RepositoryStar.repository_id == repo_id).count()
        )

    @staticmethod
    def is_starred_by_user(db: Session, user_id: int, repo_id: str) -> bool:
        if not user_id:
            return False
        return (
            db.query(RepositoryStar)
            .filter(
                RepositoryStar.repository_id == repo_id,
                RepositoryStar.user_id == user_id,
            )
            .first()
            is not None
        )

    @staticmethod
    def add_star(db: Session, user_id: int, repo_id: str) -> bool:
        """Return True if a new star row was created."""
        if (
            db.query(RepositoryStar)
            .filter(
                RepositoryStar.repository_id == repo_id,
                RepositoryStar.user_id == user_id,
            )
            .first()
        ):
            return False
        db.add(RepositoryStar(user_id=user_id, repository_id=repo_id))
        db.commit()
        return True

    @staticmethod
    def remove_star(db: Session, user_id: int, repo_id: str) -> bool:
        """Return True if a star was removed."""
        n = (
            db.query(RepositoryStar)
            .filter(
                RepositoryStar.repository_id == repo_id,
                RepositoryStar.user_id == user_id,
            )
            .delete(synchronize_session=False)
        )
        if n:
            db.commit()
            return True
        return False


def _contributor_ids_for_repository(db: Session, repository: Repository) -> set:
    """Distinct user IDs: owner, commit authors, issue participants, explicit collaborators."""
    ids = set()
    if repository.owner_id:
        ids.add(repository.owner_id)
    rid = repository.id
    for (aid,) in db.query(Commit.author_id).filter(Commit.repository_id == rid).distinct():
        if aid is not None:
            ids.add(aid)
    for (iid,) in (
        db.query(Issue.assigned_to_id)
        .filter(Issue.repository_id == rid, Issue.assigned_to_id.isnot(None))
        .distinct()
    ):
        if iid is not None:
            ids.add(iid)
    for (cid,) in (
        db.query(Issue.created_by_id)
        .filter(Issue.repository_id == rid, Issue.created_by_id.isnot(None))
        .distinct()
    ):
        if cid is not None:
            ids.add(cid)
    for (wid,) in (
        db.query(IssueWatcher.user_id)
        .join(Issue, IssueWatcher.issue_id == Issue.id)
        .filter(Issue.repository_id == rid, IssueWatcher.user_id.isnot(None))
        .distinct()
    ):
        if wid is not None:
            ids.add(wid)
    for (uid,) in db.query(UserPermission.user_id).filter(UserPermission.repository_id == rid).distinct():
        if uid is not None:
            ids.add(uid)
    return ids


def contributor_count_for_repository(db: Session, repository: Repository) -> int:
    """Distinct people: owner, commit authors, issue creators/assignees/watchers, explicit collaborators."""
    return len(_contributor_ids_for_repository(db, repository))


def contributors_for_repository(db: Session, repository: Repository) -> List[dict]:
    """Named contributors for a repository (same set as contributor_count_for_repository)."""
    ids = _contributor_ids_for_repository(db, repository)
    if not ids:
        return []

    rid = repository.id
    commit_author_ids = {
        aid for (aid,) in db.query(Commit.author_id).filter(Commit.repository_id == rid).distinct()
        if aid is not None
    }
    collaborator_ids = {
        uid for (uid,) in db.query(UserPermission.user_id).filter(UserPermission.repository_id == rid).distinct()
        if uid is not None
    }
    issue_participant_ids = set()
    for (iid,) in (
        db.query(Issue.assigned_to_id)
        .filter(Issue.repository_id == rid, Issue.assigned_to_id.isnot(None))
        .distinct()
    ):
        if iid is not None:
            issue_participant_ids.add(iid)
    for (cid,) in (
        db.query(Issue.created_by_id)
        .filter(Issue.repository_id == rid, Issue.created_by_id.isnot(None))
        .distinct()
    ):
        if cid is not None:
            issue_participant_ids.add(cid)
    for (wid,) in (
        db.query(IssueWatcher.user_id)
        .join(Issue, IssueWatcher.issue_id == Issue.id)
        .filter(Issue.repository_id == rid, IssueWatcher.user_id.isnot(None))
        .distinct()
    ):
        if wid is not None:
            issue_participant_ids.add(wid)

    users = db.query(User).filter(User.id.in_(ids)).all()
    contributors = []
    for user in users:
        roles = []
        if repository.owner_id == user.id:
            roles.append("owner")
        if user.id in commit_author_ids:
            roles.append("commit_author")
        if user.id in collaborator_ids:
            roles.append("collaborator")
        if user.id in issue_participant_ids:
            roles.append("issue_participant")
        contributors.append({
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "roles": roles,
        })

    contributors.sort(key=lambda c: (
        0 if "owner" in c["roles"] else 1,
        (c["full_name"] or c["username"] or "").lower(),
    ))
    return contributors


class PendingRepositoryCRUD:
    @staticmethod
    def create_pending_repository(db: Session, repo_name: str, description: str, requested_by_username: str, owner_username: str):
        """Create a new pending repository awaiting approval"""
        
        requested_by = UserCRUD.get_user_by_username(db, requested_by_username)
        if not requested_by:
            raise ValueError(f"User '{requested_by_username}' not found")
        
        owner = UserCRUD.get_user_by_username(db, owner_username)
        if not owner:
            raise ValueError(f"Owner '{owner_username}' not found")
        
        pending_repo = PendingRepository(
            repo_name=repo_name,
            description=description,
            requested_by_id=requested_by.id,
            owner_id=owner.id,
            status='pending'
        )
        
        db.add(pending_repo)
        db.commit()
        db.refresh(pending_repo)
        return pending_repo
    
    @staticmethod
    def get_pending_repository(db: Session, pending_id: int):
        """Get pending repository by ID"""
        return db.query(PendingRepository).filter(PendingRepository.id == pending_id).first()
    
    @staticmethod
    def get_all_pending_repositories(db: Session, status: str = 'pending'):
        """Get all pending repositories"""
        query = db.query(PendingRepository)
        if status:
            query = query.filter(PendingRepository.status == status)
        return query.order_by(desc(PendingRepository.created_at)).all()
    
    @staticmethod
    def get_pending_repositories_by_team_lead(db: Session, team_lead_username: str, status: str = 'pending'):
        """Get pending repositories for a specific team lead"""
        team_lead = UserCRUD.get_user_by_username(db, team_lead_username)
        if not team_lead:
            return []
        
        query = db.query(PendingRepository).filter(PendingRepository.owner_id == team_lead.id)
        if status:
            query = query.filter(PendingRepository.status == status)
        return query.order_by(desc(PendingRepository.created_at)).all()
    
    @staticmethod
    def approve_pending_repository(db: Session, pending_id: int, reviewer_username: str, comment: str = None):
        """Approve a pending repository and create it.

        Note: Repo-creation approval is now deprecated (repos are created immediately),
        but older pending rows may still exist in the DB. If the repository already
        exists, approving should simply mark the request approved and return the
        existing repository so the UI clears.
        """
        
        pending = PendingRepositoryCRUD.get_pending_repository(db, pending_id)
        if not pending:
            raise ValueError("Pending repository not found")
        
        if pending.status != 'pending':
            raise ValueError(f"Repository request is already {pending.status}")
        
        reviewer = UserCRUD.get_user_by_username(db, reviewer_username)
        if not reviewer:
            raise ValueError("Reviewer not found")
        
        # Update pending repository status
        pending.status = 'approved'
        pending.reviewed_by_id = reviewer.id
        pending.reviewed_at = datetime.utcnow()
        pending.review_comment = comment
        
        # If repository already exists (new flow), return it and just mark approved.
        repo_id = RepositoryCRUD.generate_repo_id(pending.requested_by.username, pending.repo_name)
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            # Create actual repository (legacy flow).
            # Policy: the requester owns the repository; the team lead/admin approves it.
            repository = RepositoryCRUD.create_repository(
                db,
                pending.requested_by.username,
                pending.repo_name,
                pending.description
            )
        
        # Grant manage permission to the reviewer chain (the team lead stored in pending.owner).
        # This preserves the review workflow without taking ownership away from the developer.
        try:
            UserPermissionCRUD.create_permission(
                db,
                username=pending.owner.username,
                repo_id=repository.id,
                permission_level='manage',
                granted_by_username=pending.owner.username
            )
        except Exception:
            # Best-effort; don't fail repo creation if permissions already exist.
            pass
        
        db.commit()
        db.refresh(pending)
        return pending, repository
    
    @staticmethod
    def reject_pending_repository(db: Session, pending_id: int, reviewer_username: str, comment: str = None):
        """Reject a pending repository"""
        
        pending = PendingRepositoryCRUD.get_pending_repository(db, pending_id)
        if not pending:
            raise ValueError("Pending repository not found")
        
        if pending.status != 'pending':
            raise ValueError(f"Repository request is already {pending.status}")
        
        reviewer = UserCRUD.get_user_by_username(db, reviewer_username)
        if not reviewer:
            raise ValueError("Reviewer not found")
        
        # Update pending repository status
        pending.status = 'rejected'
        pending.reviewed_by_id = reviewer.id
        pending.reviewed_at = datetime.utcnow()
        pending.review_comment = comment
        
        db.commit()
        db.refresh(pending)
        return pending

class PendingUserRegistrationCRUD:
    @staticmethod
    def create_request(
        db: Session,
        username: str,
        password_hash: str,
        email: str = None,
        full_name: str = None,
        requested_role: str = 'developer',
        requested_team_lead_username: str = None,
    ) -> PendingUserRegistration:
        pending = PendingUserRegistration(
            username=username,
            email=email,
            full_name=full_name,
            password_hash=password_hash,
            requested_role=requested_role,
            requested_team_lead_username=requested_team_lead_username,
            status='pending'
        )
        db.add(pending)
        db.commit()
        db.refresh(pending)
        return pending

    @staticmethod
    def get_by_id(db: Session, pending_id: int) -> Optional[PendingUserRegistration]:
        return db.query(PendingUserRegistration).filter(PendingUserRegistration.id == pending_id).first()

    @staticmethod
    def get_by_username(db: Session, username: str, status: str = 'pending') -> Optional[PendingUserRegistration]:
        query = db.query(PendingUserRegistration).filter(PendingUserRegistration.username == username)
        if status:
            query = query.filter(PendingUserRegistration.status == status)
        return query.order_by(desc(PendingUserRegistration.created_at)).first()

    @staticmethod
    def list_requests(db: Session, status: str = 'pending') -> List[PendingUserRegistration]:
        query = db.query(PendingUserRegistration)
        if status:
            query = query.filter(PendingUserRegistration.status == status)
        return query.order_by(desc(PendingUserRegistration.created_at)).all()


# ==========================
# Issue Tracking CRUD
# ==========================

ISSUE_TYPES = {"bug", "feature", "task", "question"}
ISSUE_PRIORITIES = {"critical", "high", "medium", "low"}
ISSUE_STATUSES = {"open", "in_progress", "resolved", "closed"}

_MENTION_RE = re.compile(r"(^|[^A-Za-z0-9_])@([A-Za-z0-9_]{2,50})")
_ISSUE_REF_RE = re.compile(
    r"\b(?P<action>fixes|fix|fixed|closes|close|closed|resolves|resolve|resolved|refs|ref|references|reference)\b"
    r"[^#]{0,40}#(?P<number>\d+)\b",
    re.IGNORECASE
)
_HASH_REF_RE = re.compile(r"(?<![A-Za-z0-9_])#(?P<number>\d+)\b")


def parse_issue_references(text: str) -> List[dict]:
    """Parse 'Fixes #123', 'Closes #123', 'Refs #123'. Returns list of {number:int, link_type:str, close:bool}."""
    if not text:
        return []
    results = []
    for m in _ISSUE_REF_RE.finditer(text):
        action = (m.group("action") or "").lower()
        number = int(m.group("number"))
        close = action.startswith(("fix", "close", "resolve"))
        link_type = "closes" if close else "ref"
        results.append({"number": number, "link_type": link_type, "close": close})
    # If no explicit verb refs exist, allow bare "#123" as ref-only.
    if not results:
        for m in _HASH_REF_RE.finditer(text):
            results.append({"number": int(m.group("number")), "link_type": "ref", "close": False})
    # Deduplicate by (number, link_type) while preserving order.
    seen = set()
    deduped = []
    for r in results:
        key = (r["number"], r["link_type"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def parse_mentions(text: str) -> List[str]:
    if not text:
        return []
    mentions = []
    for m in _MENTION_RE.finditer(text):
        mentions.append(m.group(2))
    # Unique, preserve order
    out = []
    seen = set()
    for u in mentions:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


class NotificationCRUD:
    @staticmethod
    def create_notification(
        db: Session,
        user_id: int,
        notification_type: str,
        title: str = None,
        body: str = None,
        payload: dict = None,
        dedupe_key: str = None,
    ) -> Optional[Notification]:
        # When dedupe_key is not provided, we still allow creation.
        if dedupe_key:
            existing = db.query(Notification).filter(
                Notification.user_id == user_id,
                Notification.dedupe_key == dedupe_key
            ).first()
            if existing:
                return existing

        n = Notification(
            user_id=user_id,
            notification_type=notification_type,
            title=title,
            body=body,
            payload_json=json.dumps(payload or {}, separators=(",", ":")) if payload is not None else None,
            dedupe_key=dedupe_key,
            is_read=False,
        )
        db.add(n)
        db.commit()
        db.refresh(n)
        return n

    @staticmethod
    def list_notifications(db: Session, user_id: int, unread_only: bool = False, limit: int = 50) -> List[Notification]:
        q = db.query(Notification).filter(Notification.user_id == user_id)
        if unread_only:
            q = q.filter(Notification.is_read == False)  # noqa: E712
        return q.order_by(desc(Notification.created_at)).limit(limit).all()

    @staticmethod
    def mark_read(db: Session, user_id: int, notification_ids: List[int]) -> int:
        if not notification_ids:
            return 0
        updated = db.query(Notification).filter(
            Notification.user_id == user_id,
            Notification.id.in_(notification_ids)
        ).update(
            {Notification.is_read: True, Notification.read_at: datetime.utcnow()},
            synchronize_session=False
        )
        db.commit()
        return int(updated or 0)


class IssueEventCRUD:
    @staticmethod
    def add_event(db: Session, issue_id: int, event_type: str, actor_id: int = None, payload: dict = None) -> IssueEvent:
        e = IssueEvent(
            issue_id=issue_id,
            actor_id=actor_id,
            event_type=event_type,
            payload_json=json.dumps(payload or {}, separators=(",", ":")) if payload is not None else None,
        )
        db.add(e)
        db.commit()
        db.refresh(e)
        return e

    @staticmethod
    def list_events(db: Session, issue_id: int, limit: int = 200) -> List[IssueEvent]:
        return db.query(IssueEvent).filter(IssueEvent.issue_id == issue_id).order_by(IssueEvent.created_at.asc()).limit(limit).all()


class MilestoneCRUD:
    @staticmethod
    def create_milestone(db: Session, repo_id: str, title: str, description: str = None, due_date: datetime = None, created_by_id: int = None) -> Milestone:
        m = Milestone(
            repository_id=repo_id,
            title=title,
            description=description,
            due_date=due_date,
            created_by_id=created_by_id,
            is_closed=False
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        return m

    @staticmethod
    def list_milestones(db: Session, repo_id: str, include_closed: bool = True) -> List[Milestone]:
        q = db.query(Milestone).filter(Milestone.repository_id == repo_id)
        if not include_closed:
            q = q.filter(Milestone.is_closed == False)  # noqa: E712
        return q.order_by(desc(Milestone.created_at)).all()

    @staticmethod
    def get_milestone(db: Session, repo_id: str, milestone_id: int) -> Optional[Milestone]:
        return db.query(Milestone).filter(Milestone.repository_id == repo_id, Milestone.id == milestone_id).first()

    @staticmethod
    def close_milestone(db: Session, milestone: Milestone) -> Milestone:
        milestone.is_closed = True
        milestone.closed_at = datetime.utcnow()
        db.commit()
        db.refresh(milestone)
        return milestone


class IssueLabelCRUD:
    @staticmethod
    def upsert_label(db: Session, repo_id: str, name: str, color: str = None) -> IssueLabel:
        existing = db.query(IssueLabel).filter(IssueLabel.repository_id == repo_id, IssueLabel.name == name).first()
        if existing:
            if color is not None:
                existing.color = color
            db.commit()
            db.refresh(existing)
            return existing
        label = IssueLabel(repository_id=repo_id, name=name, color=color)
        db.add(label)
        db.commit()
        db.refresh(label)
        return label

    @staticmethod
    def list_labels(db: Session, repo_id: str) -> List[IssueLabel]:
        return db.query(IssueLabel).filter(IssueLabel.repository_id == repo_id).order_by(IssueLabel.name.asc()).all()


class IssueCRUD:
    @staticmethod
    def _next_issue_number(db: Session, repo_id: str) -> int:
        # SQLite-safe approach: compute max(number)+1. In high concurrency, unique constraint protects;
        # caller should retry on IntegrityError if needed.
        current_max = db.query(Issue.number).filter(Issue.repository_id == repo_id).order_by(Issue.number.desc()).first()
        return (current_max[0] if current_max else 0) + 1

    @staticmethod
    def create_issue(
        db: Session,
        repo_id: str,
        title: str,
        description: str,
        issue_type: str,
        priority: str,
        created_by_id: int,
        assigned_to_id: int = None,
        milestone_id: int = None,
        labels: Optional[List[str]] = None,
        watcher_user_ids: Optional[List[int]] = None,
    ) -> Issue:
        itype = (issue_type or "task").strip().lower()
        if itype not in ISSUE_TYPES:
            itype = "task"
        prio = (priority or "medium").strip().lower()
        if prio not in ISSUE_PRIORITIES:
            prio = "medium"

        number = IssueCRUD._next_issue_number(db, repo_id)
        issue = Issue(
            repository_id=repo_id,
            number=number,
            title=title.strip(),
            description=description,
            issue_type=itype,
            priority=prio,
            status="open",
            created_by_id=created_by_id,
            assigned_to_id=assigned_to_id,
            milestone_id=milestone_id,
        )
        db.add(issue)
        db.commit()
        db.refresh(issue)

        # Default watchers: creator + assignee
        watcher_ids = set(watcher_user_ids or [])
        watcher_ids.add(created_by_id)
        if assigned_to_id:
            watcher_ids.add(assigned_to_id)
        for uid in watcher_ids:
            try:
                db.add(IssueWatcher(issue_id=issue.id, user_id=uid))
            except Exception:
                pass
        db.commit()

        # Labels
        for name in (labels or []):
            lname = (name or "").strip()
            if not lname:
                continue
            label = IssueLabelCRUD.upsert_label(db, repo_id, lname)
            existing_link = db.query(IssueLabelLink).filter(IssueLabelLink.issue_id == issue.id, IssueLabelLink.label_id == label.id).first()
            if not existing_link:
                db.add(IssueLabelLink(issue_id=issue.id, label_id=label.id))
        db.commit()

        IssueEventCRUD.add_event(db, issue.id, "issue_created", actor_id=created_by_id, payload={
            "number": issue.number,
            "title": issue.title,
            "issue_type": issue.issue_type,
            "priority": issue.priority,
        })

        return issue

    @staticmethod
    def get_issue_by_number(db: Session, repo_id: str, number: int) -> Optional[Issue]:
        return db.query(Issue).filter(Issue.repository_id == repo_id, Issue.number == number).first()

    @staticmethod
    def get_issue(db: Session, repo_id: str, issue_id: int) -> Optional[Issue]:
        return db.query(Issue).filter(Issue.repository_id == repo_id, Issue.id == issue_id).first()

    @staticmethod
    def list_issues(
        db: Session,
        repo_id: str,
        status: Optional[str] = None,
        search: Optional[str] = None,
        milestone_id: Optional[int] = None,
        label: Optional[str] = None,
        assignee_username: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        include_total: bool = True,
    ):
        q = db.query(Issue).filter(Issue.repository_id == repo_id)
        if status:
            q = q.filter(Issue.status == status)
        if milestone_id is not None:
            q = q.filter(Issue.milestone_id == milestone_id)
        if search:
            like = f"%{search.strip()}%"
            q = q.filter(or_(Issue.title.ilike(like), Issue.description.ilike(like)))
        if assignee_username:
            u = UserCRUD.get_user_by_username(db, assignee_username)
            if u:
                q = q.filter(Issue.assigned_to_id == u.id)
            else:
                q = q.filter(Issue.assigned_to_id == -1)
        if label:
            q = q.join(IssueLabelLink, IssueLabelLink.issue_id == Issue.id).join(IssueLabel, IssueLabel.id == IssueLabelLink.label_id).filter(
                IssueLabel.repository_id == repo_id,
                IssueLabel.name == label
            )
        total = q.count() if include_total else None
        items = q.order_by(desc(Issue.updated_at)).offset(max(int(offset or 0), 0)).limit(limit).all()
        return items, total

    @staticmethod
    def transition_status(db: Session, issue: Issue, new_status: str, actor_id: int = None, reason: str = None) -> Issue:
        ns = (new_status or "").strip().lower()
        if ns not in ISSUE_STATUSES:
            raise ValueError("Invalid status")

        old = issue.status
        if old == ns:
            return issue

        # Enforce minimal lifecycle semantics.
        allowed = {
            "open": {"in_progress", "resolved", "closed"},
            "in_progress": {"resolved", "closed", "open"},
            "resolved": {"closed", "open", "in_progress"},
            "closed": {"open"},
        }
        if ns not in allowed.get(old, set()):
            raise ValueError(f"Invalid transition {old} -> {ns}")

        issue.status = ns
        now = datetime.utcnow()
        if ns == "resolved":
            issue.resolved_at = now
        if ns == "closed":
            issue.closed_at = now
            UserPermissionCRUD.revoke_issue_scoped_for_issue(db, issue.id)
        if ns == "open":
            # Reopen clears resolution/close timestamps.
            issue.resolved_at = None
            issue.closed_at = None

        db.commit()
        db.refresh(issue)
        IssueEventCRUD.add_event(db, issue.id, "issue_status_changed", actor_id=actor_id, payload={
            "from": old,
            "to": ns,
            "reason": reason
        })
        return issue

    @staticmethod
    def set_assignee(db: Session, issue: Issue, assigned_to_id: Optional[int], actor_id: int = None) -> Issue:
        old = issue.assigned_to_id
        issue.assigned_to_id = assigned_to_id
        db.commit()
        db.refresh(issue)
        IssueEventCRUD.add_event(db, issue.id, "issue_assigned", actor_id=actor_id, payload={"from": old, "to": assigned_to_id})
        if assigned_to_id:
            # ensure watcher
            existing = db.query(IssueWatcher).filter(IssueWatcher.issue_id == issue.id, IssueWatcher.user_id == assigned_to_id).first()
            if not existing:
                db.add(IssueWatcher(issue_id=issue.id, user_id=assigned_to_id))
                db.commit()
        return issue

    @staticmethod
    def add_comment(db: Session, issue: Issue, author_id: int, body: str) -> IssueComment:
        c = IssueComment(issue_id=issue.id, author_id=author_id, body=body)
        db.add(c)
        db.commit()
        db.refresh(c)
        IssueEventCRUD.add_event(db, issue.id, "issue_comment_added", actor_id=author_id, payload={"comment_id": c.id})
        return c


class IssueLinkCRUD:
    @staticmethod
    def link_commit(db: Session, issue_id: int, commit_id: str, link_type: str = "ref") -> None:
        link_type = (link_type or "ref").strip().lower()
        existing = db.query(IssueCommitLink).filter(
            IssueCommitLink.issue_id == issue_id,
            IssueCommitLink.commit_id == commit_id,
            IssueCommitLink.link_type == link_type
        ).first()
        if existing:
            return
        db.add(IssueCommitLink(issue_id=issue_id, commit_id=commit_id, link_type=link_type))
        db.commit()

    @staticmethod
    def link_pull_request(db: Session, issue_id: int, pull_request_id: int, link_type: str = "ref") -> None:
        link_type = (link_type or "ref").strip().lower()
        existing = db.query(IssuePullRequestLink).filter(
            IssuePullRequestLink.issue_id == issue_id,
            IssuePullRequestLink.pull_request_id == pull_request_id,
            IssuePullRequestLink.link_type == link_type
        ).first()
        if existing:
            return
        db.add(IssuePullRequestLink(issue_id=issue_id, pull_request_id=pull_request_id, link_type=link_type))
        db.commit()

    @staticmethod
    def link_branch(db: Session, repo_id: str, issue_id: int, branch_name: str) -> None:
        existing = db.query(IssueBranchLink).filter(
            IssueBranchLink.repository_id == repo_id,
            IssueBranchLink.issue_id == issue_id,
            IssueBranchLink.branch_name == branch_name
        ).first()
        if existing:
            return
        db.add(IssueBranchLink(repository_id=repo_id, issue_id=issue_id, branch_name=branch_name))
        db.commit()

    @staticmethod
    def list_linked_commits(db: Session, issue_id: int) -> List[IssueCommitLink]:
        return db.query(IssueCommitLink).filter(IssueCommitLink.issue_id == issue_id).order_by(desc(IssueCommitLink.created_at)).all()

    @staticmethod
    def list_linked_pull_requests(db: Session, issue_id: int) -> List[IssuePullRequestLink]:
        return db.query(IssuePullRequestLink).filter(IssuePullRequestLink.issue_id == issue_id).order_by(desc(IssuePullRequestLink.created_at)).all()

    @staticmethod
    def list_linked_branches(db: Session, repo_id: str, issue_id: int) -> List[IssueBranchLink]:
        return db.query(IssueBranchLink).filter(
            IssueBranchLink.repository_id == repo_id,
            IssueBranchLink.issue_id == issue_id
        ).order_by(desc(IssueBranchLink.created_at)).all()


# ==========================
# Issue Access Request CRUD
# ==========================

class IssueAccessRequestCRUD:
    @staticmethod
    def create_request(
        db: Session,
        issue_id: int,
        requested_by_id: int,
        requested_user_id: int,
        repo_id: str,
        request_reason: str = None
    ) -> Optional["IssueAccessRequest"]:
        """Create access request when @user is tagged and doesn't own repo"""
        from database.models import IssueAccessRequest
        
        # Check if repo owner
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise ValueError("Repository not found")
        
        if repository.owner_id == requested_user_id:
            return None  # User owns repo, no request needed
        
        # Check if already has access (IMPORTANT: issue-scoped rows must NOT suppress new requests)
        user = db.query(User).filter(User.id == requested_user_id).first()
        if user:
            # Existing commit authors already have content access (and don't need an issue access request)
            if db.query(Commit).filter(Commit.repository_id == repo_id, Commit.author_id == user.id).first():
                return None
            # Only global (non-issue) permissions count as "already has access"
            has_global_perm = db.query(UserPermission).filter(
                UserPermission.repository_id == repo_id,
                UserPermission.user_id == user.id,
                UserPermission.issue_id.is_(None),
            ).first() is not None
            if has_global_perm:
                return None  # Already has access
        
        # Check if pending request already exists
        existing = db.query(IssueAccessRequest).filter(
            IssueAccessRequest.issue_id == issue_id,
            IssueAccessRequest.requested_user_id == requested_user_id,
            IssueAccessRequest.status == "pending"
        ).first()
        
        if existing:
            return existing
        
        # Create new request
        req = IssueAccessRequest(
            issue_id=issue_id,
            repository_id=repo_id,
            requested_by_id=requested_by_id,
            requested_user_id=requested_user_id,
            request_reason=request_reason or "Tagged in issue",
            status="pending"
        )
        db.add(req)
        db.commit()
        db.refresh(req)
        return req
    
    @staticmethod
    def get_request(db: Session, request_id: int) -> Optional["IssueAccessRequest"]:
        """Get access request by ID"""
        from database.models import IssueAccessRequest
        return db.query(IssueAccessRequest).filter(IssueAccessRequest.id == request_id).first()
    
    @staticmethod
    def list_pending_requests(db: Session, repo_id: str = None, status: str = "pending", limit: int = 100) -> List["IssueAccessRequest"]:
        """List access requests (optionally filtered by status/repo)."""
        from database.models import IssueAccessRequest
        q = db.query(IssueAccessRequest)
        if status:
            q = q.filter(IssueAccessRequest.status == status)
        if repo_id:
            q = q.filter(IssueAccessRequest.repository_id == repo_id)
        return q.order_by(desc(IssueAccessRequest.created_at)).limit(limit).all()
    
    @staticmethod
    def list_user_requests(db: Session, user_id: int, status: str = None) -> List["IssueAccessRequest"]:
        """List all access requests for a specific user"""
        from database.models import IssueAccessRequest
        q = db.query(IssueAccessRequest).filter(IssueAccessRequest.requested_user_id == user_id)
        if status:
            q = q.filter(IssueAccessRequest.status == status)
        return q.order_by(desc(IssueAccessRequest.created_at)).all()
    
    @staticmethod
    def approve_request(
        db: Session,
        request_id: int,
        admin_id: int,
        comment: str = None
    ) -> Optional[UserPermission]:
        """Approve access request and grant write permission"""
        from database.models import IssueAccessRequest
        
        req = db.query(IssueAccessRequest).filter(IssueAccessRequest.id == request_id).first()
        if not req or req.status != "pending":
            raise ValueError("Request not found or already reviewed")
        
        # Get users
        requested_user = db.query(User).filter(User.id == req.requested_user_id).first()
        admin = db.query(User).filter(User.id == admin_id).first()
        
        if not requested_user or not admin:
            raise ValueError("User not found")
        
        # Create permission
        perm = UserPermissionCRUD.create_permission(
            db,
            username=requested_user.username,
            repo_id=req.repository_id,
            permission_level="write",
            granted_by_username=admin.username,
            show_in_web_ui=False,
            issue_id=req.issue_id,
        )
        
        # Update request status
        req.status = "approved"
        req.admin_id = admin_id
        req.reviewed_at = datetime.utcnow()
        req.review_comment = comment
        db.commit()
        
        # Log activity
        ActivityCRUD.create_activity(
            db,
            user_id=admin_id,
            repository_id=req.repository_id,
            activity_type="grant_access_to_issue",
            description=f"Granted access to {requested_user.username} for issue #{req.issue_id}"
        )
        
        return perm
    
    @staticmethod
    def deny_request(
        db: Session,
        request_id: int,
        admin_id: int,
        comment: str = None
    ) -> "IssueAccessRequest":
        """Deny access request"""
        from database.models import IssueAccessRequest
        
        req = db.query(IssueAccessRequest).filter(IssueAccessRequest.id == request_id).first()
        if not req or req.status != "pending":
            raise ValueError("Request not found or already reviewed")
        
        admin = db.query(User).filter(User.id == admin_id).first()
        if not admin:
            raise ValueError("Admin user not found")
        
        # Update request status
        req.status = "denied"
        req.admin_id = admin_id
        req.reviewed_at = datetime.utcnow()
        req.review_comment = comment
        db.commit()
        db.refresh(req)
        
        # Log activity
        ActivityCRUD.create_activity(
            db,
            user_id=admin_id,
            repository_id=req.repository_id,
            activity_type="deny_access_request",
            description=f"Denied access to {req.requested_user.username} for issue #{req.issue_id}: {comment or 'No reason provided'}"
        )
        
        return req