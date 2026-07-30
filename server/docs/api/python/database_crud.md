# database/crud.py

## class `UserCRUD`

### `create_user(db, username, email, full_name, role, team_lead_id)`

Create a new user

### `get_user_by_username(db, username)`

Get user by username

### `get_user_by_id(db, user_id)`

Get user by ID

### `get_or_create_user(db, username, email, full_name)`

Get existing user or create new one

## class `RepositoryCRUD`

### `generate_repo_id(username, repo_name)`

Generate unique repository ID

### `create_repository(db, username, repo_name, description)`

Create a new repository

### `get_repository(db, repo_id)`

Get repository by ID

### `get_repositories_by_user(db, username)`

Get all repositories for a user

### `get_all_repositories(db)`

Get all repositories

### `archive_repository(db, repo_id, reason)`

Archive a repository

### `unarchive_repository(db, repo_id)`

Unarchive a repository (move back to active repositories)

### `delete_repository(db, repo_id)`

Delete a repository and all associated data

### `calculate_repository_size(db, repo_id)`

Calculate total repository size based on all unique files in the repository

### `update_repository_size(db, repo_id)`

Update repository size based on current head commit

### `update_repository_details(db, repo_id, g1_coordinator, tested, instruction_manual_path, instruction_manual_filename)`

Update repository G1 coordinator, testing status, and instruction manual

## class `CommitCRUD`

### `create_commit(db, commit_data)`

Create a new commit

### `create_commit_from_file_hashes(db, commit_data, file_entries)`

Create a new commit using existing file hashes (no base64 payloads).

### `get_commit(db, commit_id)`

Get commit by ID

### `get_commits_by_repository(db, repo_id, limit)`

Get commits for a repository

## class `FileObjectCRUD`

### `calculate_file_hash(content)`

Calculate SHA-1 hash of file content

### `store_file_object(db, content, mime_type)`

Store a file object

### `store_file_and_create_commit_file(db, commit_id, file_path, file_content)`

Store file content and create commit file entry

## class `BranchCRUD`

### `create_branch(db, repo_id, branch_name, head_commit_id, is_default)`

Create a new branch

### `get_branches_by_repository(db, repo_id)`

Get all branches for a repository

## class `ActivityCRUD`

### `create_activity(db, user_id, activity_type, description, repository_id)`

Create an activity record

### `get_recent_activities(db, limit)`

Get recent activities

## class `PendingCommitCRUD`

### `create_pending_commit(db, commit_data)`

Create a new pending commit awaiting approval

### `get_pending_commit(db, commit_id)`

Get pending commit by ID

### `get_pending_commits_by_repository(db, repo_id, status)`

Get pending commits for a repository

### `get_all_pending_commits(db, status)`

Get all pending commits across all repositories

### `approve_pending_commit(db, commit_id, reviewer_username, comment)`

Approve a pending commit and convert it to a real commit

### `reject_pending_commit(db, commit_id, reviewer_username, comment)`

Reject a pending commit

## class `UserPermissionCRUD`

### `create_permission(db, username, repo_id, permission_level, granted_by_username)`

Grant a user permission to a repository

### `get_user_permission(db, username, repo_id)`

Get a user's permission for a repository

### `get_repository_permissions(db, repo_id)`

Get all permissions for a repository

### `get_user_permissions(db, username)`

Get all permissions for a user

### `revoke_permission(db, username, repo_id)`

Revoke a user's permission to a repository

### `has_permission(db, username, repo_id, required_level)`

Check if a user has permission to access a repository

## class `PendingRepositoryCRUD`

### `create_pending_repository(db, repo_name, description, requested_by_username, owner_username)`

Create a new pending repository awaiting approval

### `get_pending_repository(db, pending_id)`

Get pending repository by ID

### `get_all_pending_repositories(db, status)`

Get all pending repositories

### `get_pending_repositories_by_team_lead(db, team_lead_username, status)`

Get pending repositories for a specific team lead

### `approve_pending_repository(db, pending_id, reviewer_username, comment)`

Approve a pending repository and create it

### `reject_pending_repository(db, pending_id, reviewer_username, comment)`

Reject a pending repository

