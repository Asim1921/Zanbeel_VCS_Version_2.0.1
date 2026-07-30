# server.py

FoxNest Server - Central repository server for the FoxNest version control system with SQL Database

## class `CreateRepositoryRequest`

## class `PushCommitRequest`

## class `RepositoryResponse`

## class `CommitResponse`

## class `CommitsResponse`

## class `RepositoriesResponse`

## class `UpdateRepositoryDetailsRequest`

## class `UpdateRepositoryDetailsResponse`

## class `CreatePermissionRequest`

## class `ReviewCommitRequest`

## async `cors_options_middleware(request, call_next)`

Handle all OPTIONS requests before they reach route handlers

## async `startup_event()`

Initialize database tables

## `repository_to_dict(repo)`

Convert Repository model to dictionary

## `commit_to_dict(commit, include_files)`

Convert Commit model to dictionary

## async `health_check()`

Health check endpoint

## async `download_client()`

Download the latest Fox client

## async `api_root()`

API root endpoint

## async `create_repository(request, db)`

Create a new repository

## async `list_repositories(username, repo_name, db)`

List repositories for a user

## async `list_all_repositories(db)`

List all repositories from all users

## async `get_repository(repo_id, db)`

Get repository information

## async `delete_repository(repo_id, db)`

Delete a repository permanently

## async `archive_repository(repo_id, reason, db)`

Archive a repository (move to archive without deleting)

## async `push_commit(repo_id, request, db)`

Push a commit to repository

## async `pull_commits(repo_id, since_commit, db)`

Pull commits from repository

## async `get_commits(repo_id, full, db)`

Get commit history

## async `get_repository_files(repo_id, include_content, db)`

Get all files in repository from latest commit or pending commit

## async `get_repository_file(repo_id, path, db)`

Get a single file from the latest commit or pending commit

## async `download_repository_file(repo_id, path, db)`

Download a single file from the latest commit or pending commit

## async `create_sample_data(db)`

Create sample repositories for testing (development only)

## async `list_users(db)`

List all users

## async `create_user(request, db)`

Create a new user

## async `update_user(username, request, db)`

Update user information

## async `delete_user(username, db)`

Delete a user

## async `get_recent_activities(limit, db)`

Get recent activities

## async `update_repository_details(repo_id, request, db)`

Update repository G1 coordinator and testing status

## async `upload_instruction_manual(repo_id, file, db)`

Upload instruction manual PDF for a repository

## async `download_instruction_manual(repo_id, db)`

Download instruction manual PDF for a repository

## async `create_permission(request, db)`

Grant a user permission to a repository

## async `update_permission(request, db)`

Update a user's permission level for a repository

## async `get_repository_permissions(repo_id, db)`

Get all permissions for a repository

## async `get_user_permissions(username, db)`

Get all permissions for a user

## async `revoke_permission(username, repo_id, db)`

Revoke a user's permission to a repository

## async `get_all_pending_commits(status, team_lead_username, db)`

Get all pending commits across all repositories, optionally filtered by team lead

## async `get_repository_pending_commits(repo_id, status, db)`

Get pending commits for a specific repository

## async `review_pending_commit(commit_id, request, db)`

Approve or reject a pending commit

## async `get_all_pending_repositories(status, team_lead_username, db)`

Get all pending repository requests, optionally filtered by team lead

## async `review_pending_repository(pending_id, request, db)`

Approve or reject a pending repository

