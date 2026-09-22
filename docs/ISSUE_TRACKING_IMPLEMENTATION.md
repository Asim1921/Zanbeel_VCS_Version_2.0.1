# FoxNest Issue Tracking Implementation Summary

## Overview
FoxNest has a complete, integrated issue tracking system with full support for:
- Issue creation, updates, and lifecycle management
- Comments and notifications
- Integration with commits, pull requests, and branches
- Milestones and labels
- Issue watchers and events
- Automatic linking based on commit messages (e.g., "Fixes #123")

---

## 1. Backend API Endpoints

### Base URL Structure
All endpoints are prefixed with `/api/repository/{repo_id}`

### Issue Management Endpoints

#### List Issues
- **Endpoint**: `GET /api/repository/{repo_id}/issues`
- **Query Parameters**:
  - `status` (optional): Filter by status (open, in_progress, resolved, closed)
  - `search` (optional): Search in title/description
  - `label` (optional): Filter by label name
  - `milestone_id` (optional): Filter by milestone
  - `assignee` (optional): Filter by assignee username
  - `offset` (optional, default: 0): Pagination offset
  - `limit` (optional, default: 50, max: 200): Pagination limit
- **Response**: `{ success: bool, issues: [Issue], total: int }`

#### Create Issue
- **Endpoint**: `POST /api/repository/{repo_id}/issues`
- **Request Body**:
  ```json
  {
    "title": "string (required)",
    "description": "string (optional)",
    "issue_type": "string (bug|feature|task|question, default: task)",
    "priority": "string (critical|high|medium|low, default: medium)",
    "assigned_to": "string (username, optional)",
    "milestone_id": "int (optional)",
    "labels": "array of strings (optional)"
  }
  ```
- **Features**:
  - Automatically adds creator and assignee as watchers
  - Parses @mentions from description and notifies users
  - Creates issue event record
- **Response**: `{ success: bool, issue: Issue }`

#### Get Issue Details
- **Endpoint**: `GET /api/repository/{repo_id}/issues/{issue_number}`
- **Response**: `{ success: bool, issue: IssueDetail }`
- **Details Included**:
  - Issue metadata (title, description, type, priority, status)
  - Assignee and watchers
  - Comments with authors
  - All labels
  - Linked commits, branches, and pull requests
  - Event history

#### Update Issue
- **Endpoint**: `PUT /api/repository/{repo_id}/issues/{issue_number}`
- **Request Body** (all optional):
  ```json
  {
    "status": "string (open|in_progress|resolved|closed)",
    "title": "string",
    "description": "string",
    "priority": "string",
    "issue_type": "string",
    "assigned_to": "string",
    "milestone_id": "int"
  }
  ```
- **Features**:
  - Status transitions are validated (enforces lifecycle)
  - Automatic timestamp updates (resolved_at, closed_at)
  - Creates status change event
  - Notifies assignee on change
- **Response**: `{ success: bool, issue: IssueDetail }`

### Comment Management

#### Add Comment
- **Endpoint**: `POST /api/repository/{repo_id}/issues/{issue_number}/comments`
- **Request Body**:
  ```json
  {
    "body": "string (required)"
  }
  ```
- **Features**:
  - Parses @mentions and notifies mentioned users
  - Notifies issue creator and assignee
  - Comment supports markdown formatting
- **Response**: `{ success: bool, issue: IssueDetail }`

### Watch Management

#### Watch/Unwatch Issue
- **Endpoint**: `POST /api/repository/{repo_id}/issues/{issue_number}/watch`
- **Request Body**:
  ```json
  {
    "watch": "bool (true to watch, false to unwatch)"
  }
  ```
- **Response**: `{ success: bool }`

### Label Management

#### List Labels
- **Endpoint**: `GET /api/repository/{repo_id}/issue-labels`
- **Response**: `{ success: bool, labels: [{id, name, color}, ...] }`

#### Create/Update Label
- **Endpoint**: `POST /api/repository/{repo_id}/issue-labels`
- **Request Body**:
  ```json
  {
    "name": "string (required)",
    "color": "string (hex color, optional)"
  }
  ```
- **Response**: `{ success: bool, label: {id, name, color} }`

### Milestone Management

#### List Milestones
- **Endpoint**: `GET /api/repository/{repo_id}/milestones`
- **Query Parameters**:
  - `include_closed` (optional, default: true): Include closed milestones
- **Response**: 
  ```json
  {
    "success": bool,
    "milestones": [
      {
        "id": int,
        "title": string,
        "description": string,
        "due_date": ISO datetime,
        "is_closed": bool
      }
    ]
  }
  ```

#### Create Milestone
- **Endpoint**: `POST /api/repository/{repo_id}/milestones`
- **Request Body**:
  ```json
  {
    "title": "string (required)",
    "description": "string (optional)",
    "due_date": "ISO datetime (optional)"
  }
  ```
- **Response**: `{ success: bool, milestone: Milestone }`

---

## 2. Database Models & Schema

### Core Issue Model
**Table**: `issues`
```sql
CREATE TABLE issues (
  id INTEGER PRIMARY KEY,
  repository_id VARCHAR(16) NOT NULL,
  number INTEGER NOT NULL,                -- Per-repository issue number (e.g., #123)
  title VARCHAR(300) NOT NULL,
  description TEXT,
  issue_type VARCHAR(20) DEFAULT 'task',  -- bug|feature|task|question
  priority VARCHAR(20) DEFAULT 'medium',  -- critical|high|medium|low
  status VARCHAR(20) DEFAULT 'open',      -- open|in_progress|resolved|closed
  created_by_id INTEGER NOT NULL FOREIGN KEY(users.id),
  assigned_to_id INTEGER FOREIGN KEY(users.id),
  milestone_id INTEGER FOREIGN KEY(milestones.id),
  created_at DATETIME DEFAULT NOW(),
  updated_at DATETIME DEFAULT NOW(),
  resolved_at DATETIME,                   -- When moved to 'resolved'
  closed_at DATETIME,                     -- When moved to 'closed'
  UNIQUE(repository_id, number),
  INDEX(repository_id, status),
  INDEX(repository_id, priority),
  INDEX(repository_id, issue_type)
);
```

### Comments
**Table**: `issue_comments`
```sql
CREATE TABLE issue_comments (
  id INTEGER PRIMARY KEY,
  issue_id INTEGER NOT NULL FOREIGN KEY(issues.id),
  author_id INTEGER NOT NULL FOREIGN KEY(users.id),
  body TEXT NOT NULL,
  created_at DATETIME DEFAULT NOW(),
  updated_at DATETIME DEFAULT NOW(),
  INDEX(issue_id)
);
```

### Labels
**Table**: `issue_labels`
```sql
CREATE TABLE issue_labels (
  id INTEGER PRIMARY KEY,
  repository_id VARCHAR(16) NOT NULL FOREIGN KEY(repositories.id),
  name VARCHAR(50) NOT NULL,
  color VARCHAR(7),                       -- Hex color (e.g., #ff00aa)
  created_at DATETIME DEFAULT NOW(),
  UNIQUE(repository_id, name),
  INDEX(repository_id)
);
```

**Table**: `issue_label_links` (Junction table)
```sql
CREATE TABLE issue_label_links (
  id INTEGER PRIMARY KEY,
  issue_id INTEGER NOT NULL FOREIGN KEY(issues.id),
  label_id INTEGER NOT NULL FOREIGN KEY(issue_labels.id),
  UNIQUE(issue_id, label_id)
);
```

### Watchers
**Table**: `issue_watchers`
```sql
CREATE TABLE issue_watchers (
  id INTEGER PRIMARY KEY,
  issue_id INTEGER NOT NULL FOREIGN KEY(issues.id),
  user_id INTEGER NOT NULL FOREIGN KEY(users.id),
  created_at DATETIME DEFAULT NOW(),
  UNIQUE(issue_id, user_id),
  INDEX(user_id)
);
```

### Milestones
**Table**: `milestones`
```sql
CREATE TABLE milestones (
  id INTEGER PRIMARY KEY,
  repository_id VARCHAR(16) NOT NULL FOREIGN KEY(repositories.id),
  title VARCHAR(200) NOT NULL,
  description TEXT,
  due_date DATETIME,
  is_closed BOOLEAN DEFAULT FALSE,
  closed_at DATETIME,
  created_by_id INTEGER FOREIGN KEY(users.id),
  created_at DATETIME DEFAULT NOW(),
  updated_at DATETIME DEFAULT NOW(),
  INDEX(repository_id, is_closed)
);
```

### Issue Linking (to commits, PRs, branches)

**Table**: `issue_commit_links`
```sql
CREATE TABLE issue_commit_links (
  id INTEGER PRIMARY KEY,
  issue_id INTEGER NOT NULL FOREIGN KEY(issues.id),
  commit_id VARCHAR(40) NOT NULL FOREIGN KEY(commits.id),
  link_type VARCHAR(20) DEFAULT 'ref',   -- ref|fixes|closes
  created_at DATETIME DEFAULT NOW(),
  UNIQUE(issue_id, commit_id, link_type)
);
```

**Table**: `issue_pull_request_links`
```sql
CREATE TABLE issue_pull_request_links (
  id INTEGER PRIMARY KEY,
  issue_id INTEGER NOT NULL FOREIGN KEY(issues.id),
  pull_request_id INTEGER NOT NULL FOREIGN KEY(pull_requests.id),
  link_type VARCHAR(20) DEFAULT 'ref',   -- ref|fixes|closes
  created_at DATETIME DEFAULT NOW(),
  UNIQUE(issue_id, pull_request_id, link_type)
);
```

**Table**: `issue_branch_links`
```sql
CREATE TABLE issue_branch_links (
  id INTEGER PRIMARY KEY,
  issue_id INTEGER NOT NULL FOREIGN KEY(issues.id),
  repository_id VARCHAR(16) NOT NULL FOREIGN KEY(repositories.id),
  branch_name VARCHAR(100) NOT NULL,
  created_at DATETIME DEFAULT NOW(),
  UNIQUE(repository_id, branch_name, issue_id),
  INDEX(repository_id, branch_name)
);
```

### Events
**Table**: `issue_events`
```sql
CREATE TABLE issue_events (
  id INTEGER PRIMARY KEY,
  issue_id INTEGER NOT NULL FOREIGN KEY(issues.id),
  actor_id INTEGER FOREIGN KEY(users.id),
  event_type VARCHAR(50) NOT NULL,       -- created|comment|status_changed|assigned|labeled|etc
  payload_json TEXT,                     -- JSON details of the event
  created_at DATETIME DEFAULT NOW(),
  INDEX(issue_id, created_at)
);
```

### Notifications
**Table**: `notifications`
```sql
CREATE TABLE notifications (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL FOREIGN KEY(users.id),
  notification_type VARCHAR(50) NOT NULL, -- mention|issue_status|issue_comment|issue_assigned|etc
  title VARCHAR(200),
  body TEXT,
  payload_json TEXT,
  dedupe_key VARCHAR(200),               -- Optional idempotency key
  is_read BOOLEAN DEFAULT FALSE,
  read_at DATETIME,
  created_at DATETIME DEFAULT NOW(),
  INDEX(user_id, is_read),
  UNIQUE(user_id, dedupe_key)
);
```

---

## 3. Frontend Components & UI

### Main Component
**File**: [foxnestFrontend/src/components/IssuesModal.jsx](foxnestFrontend/src/components/IssuesModal.jsx)

#### Tabs
1. **Issues Tab**: View and manage project issues
2. **Milestones Tab**: Create and manage milestones

#### Key Features in UI

**Create Issue Form**:
- Title (required)
- Description (supports @mentions and issue references)
- Type selector (bug, feature, task, question)
- Priority selector (critical, high, medium, low)
- Assignee input (username)
- Milestone selector
- Labels input (comma-separated)

**Issues List View**:
- Displays all issues with filters
- Status filter dropdown (open, in_progress, resolved, closed, all)
- Search input (searches title and description)
- Sortable by priority and update time
- Shows: issue number, title, priority, type, assignee, status, comment count
- Click to view details

**Issue Detail View**:
- Full issue information
- Status transition buttons (validate lifecycle)
- Comments section with add comment form
- Linked commits, branches, and PRs
- Watchers list

**Milestones Management**:
- Create new milestone form (title, description, due date)
- List all milestones with status

#### UI Elements & Icons
- `FiX`: Close modal
- `FiRefreshCw`: Refresh data
- `FiPlus`: Create new items
- `FiLoader`: Loading indicator
- `FiMessageSquare`: Comment count
- `FiTag`: Labels/tags
- `FiFlag`: Priority
- `FiUser`: Assignee
- `FiHash`: Issue number
- `FiCheckCircle`: Status indicator
- `Badge`: Status badges with color variants

#### Status Display
- **OPEN**: Success variant (green)
- **IN PROGRESS**: Info variant (blue)
- **RESOLVED**: Default variant
- **CLOSED**: Default variant

---

## 4. CRUD Operations (Backend)

### File
[server/database/crud.py](server/database/crud.py)

#### IssueCRUD Class

**create_issue**
```python
IssueCRUD.create_issue(
  db, repo_id, title, description, issue_type, priority, created_by_id,
  assigned_to_id=None, milestone_id=None, labels=None, watcher_user_ids=None
) -> Issue
```
- Auto-generates per-repository issue number
- Validates issue_type and priority
- Creates initial watchers (creator + assignee)
- Applies labels
- Creates "issue_created" event

**get_issue_by_number**
```python
IssueCRUD.get_issue_by_number(db, repo_id, number) -> Optional[Issue]
```

**list_issues**
```python
IssueCRUD.list_issues(
  db, repo_id, status=None, search=None, milestone_id=None, label=None,
  assignee_username=None, limit=100, offset=0, include_total=True
) -> (List[Issue], total_count)
```
- Supports complex filtering
- Returns total count when requested
- Sorted by updated_at descending

**transition_status**
```python
IssueCRUD.transition_status(db, issue, new_status, actor_id=None, reason=None) -> Issue
```
- Validates status transitions (enforces lifecycle)
- Allowed transitions:
  - `open` → `in_progress|resolved|closed`
  - `in_progress` → `resolved|closed|open`
  - `resolved` → `closed|open|in_progress`
  - `closed` → `open`
- Sets/clears timestamps (resolved_at, closed_at)
- Creates status_changed event

**set_assignee**
```python
IssueCRUD.set_assignee(db, issue, assigned_to_id, actor_id=None) -> Issue
```
- Updates assignee
- Adds assignee to watchers
- Creates issue_assigned event

**add_comment**
```python
IssueCRUD.add_comment(db, issue, author_id, body) -> IssueComment
```
- Creates comment record

#### MilestoneCRUD Class

**create_milestone**
```python
MilestoneCRUD.create_milestone(db, repo_id, title, description=None, due_date=None, created_by_id=None) -> Milestone
```

**list_milestones**
```python
MilestoneCRUD.list_milestones(db, repo_id, include_closed=True) -> List[Milestone]
```

**close_milestone**
```python
MilestoneCRUD.close_milestone(db, milestone) -> Milestone
```

#### IssueLabelCRUD Class

**upsert_label**
```python
IssueLabelCRUD.upsert_label(db, repo_id, name, color=None) -> IssueLabel
```
- Creates or updates label
- Updates color if exists

**list_labels**
```python
IssueLabelCRUD.list_labels(db, repo_id) -> List[IssueLabel]
```

#### IssueLinkCRUD Class

**link_commit**
```python
IssueLinkCRUD.link_commit(db, issue_id, commit_id, link_type='ref') -> None
```

**link_pull_request**
```python
IssueLinkCRUD.link_pull_request(db, issue_id, pr_id, link_type='ref') -> None
```

**link_branch**
```python
IssueLinkCRUD.link_branch(db, repo_id, issue_id, branch_name) -> None
```

#### IssueEventCRUD Class

**add_event**
```python
IssueEventCRUD.add_event(db, issue_id, event_type, actor_id=None, payload=None) -> IssueEvent
```

#### NotificationCRUD Class

**create_notification**
```python
NotificationCRUD.create_notification(
  db, user_id, notification_type, title=None, body=None, 
  payload=None, dedupe_key=None
) -> Optional[Notification]
```
- Idempotent with dedupe_key

---

## 5. Issue Reference Parsing & Linking

### File
[server/database/crud.py](server/database/crud.py)

### parse_issue_references(text: str)
Extracts issue references from commit messages and PR descriptions.

**Patterns Matched**:
- `Fixes #123` / `Fix #123` / `Fixed #123`
- `Closes #123` / `Close #123` / `Closed #123`
- `Resolves #123` / `Resolve #123` / `Resolved #123`
- `Refs #123` / `Ref #123` / `References #123` / `Reference #123`
- Bare `#123` (treated as `ref`)

**Returns**: List of dicts with `{number, link_type, close}`
```python
[
  {"number": 123, "link_type": "closes", "close": True},
  {"number": 124, "link_type": "ref", "close": False}
]
```

**Automatic Linking Logic**:
When a commit is pushed to the default branch:
1. Issue references are parsed from commit message
2. Commit is linked to issue with appropriate link_type
3. If `link_type` is "closes", issue status → "closed"
4. Branch is linked to issue

When PR is merged:
1. Both PR title and description are parsed
2. If "closes" reference found and on default branch, issue → "closed"
3. Merge commit also linked to issue

### parse_mentions(text: str)
Extracts @username mentions from text.

**Pattern**: `@[A-Za-z0-9_]{2,50}`

**Returns**: List of unique usernames

**Notification Logic**:
- When @username appears in issue description or comment
- Creates "issue_mention" notification for that user
- Skips inactive users

---

## 6. Tests

### File
[server/tests/test_issue_tracking_api.py](server/tests/test_issue_tracking_api.py)

### Test Cases

#### test_create_and_get_issue
- Creates issue with title, description, type, priority
- Verifies issue created with status="open"
- Fetches issue detail and validates all fields

#### test_commit_fixes_issue_on_default_branch
- Creates an issue
- Creates commit with message "Fixes #{issue_number}"
- Verifies issue status automatically → "closed"
- Demonstrates issue linking via commit message

#### test_pr_merge_auto_closes_issue
- Creates issue
- Creates PR branch and new commit
- Creates PR with title referencing issue
- Merges PR
- Verifies issue status → "closed"
- Verifies PR linked to issue

### Setup
- In-memory SQLite database per test class
- Database seeding with test user, repository, and commits
- Custom dependency override for database session

---

## 7. Key Implementation Details & Flow

### Issue Lifecycle States
```
open
  ├→ in_progress
  │   ├→ resolved
  │   │   └→ closed
  │   └→ closed
  └→ resolved
      └→ closed
```

### Auto-Close Mechanisms
1. **Commit Message** (on default branch):
   - Triggers when "Fixes/Closes/Resolves #123" appears
   - Issue → "closed"

2. **PR Merge** (on default branch):
   - Triggers when PR title/description contains "Fixes/Closes/Resolves #123"
   - Issue → "closed"

### Notification Types
- `issue_mention`: User mentioned with @
- `issue_status`: Status changed
- `issue_comment`: New comment on watched issue
- `issue_assigned`: User assigned to issue

### Event Types Tracked
- `issue_created`: Initial creation
- `issue_status_changed`: Status transition (includes from/to)
- `issue_assigned`: Assignee change (includes from/to)
- `issue_comment`: Comment added
- `issue_labeled`: Label applied
- etc.

### Default Watchers
When issue created:
- Creator automatically added as watcher
- Assigned user automatically added as watcher
- Any mentioned user gets notification

### Frontend API Methods (utils/api.js)

```javascript
// Issues
api.listIssues(repoId, params)
api.createIssue(repoId, payload)
api.getIssue(repoId, issueNumber)
api.updateIssue(repoId, issueNumber, payload)
api.addIssueComment(repoId, issueNumber, {body})
api.watchIssue(repoId, issueNumber, watch)

// Labels
api.listIssueLabels(repoId)
api.upsertIssueLabel(repoId, {name, color})

// Milestones
api.listMilestones(repoId, includeClosed)
api.createMilestone(repoId, {title, description, due_date})
```

---

## 8. File Locations Summary

| Component | Location |
|-----------|----------|
| **API Endpoints** | [server/server.py](server/server.py#L3114) (lines 3114-3500+) |
| **Database Models** | [server/database/models.py](server/database/models.py#L332) (Milestone, Issue, IssueComment, IssueLabel, IssueWatcher, IssueEvent, Notification, etc.) |
| **CRUD Operations** | [server/database/crud.py](server/database/crud.py#L1129) (IssueCRUD, MilestoneCRUD, IssueLabelCRUD, IssueLinkCRUD, IssueEventCRUD, NotificationCRUD) |
| **Helper Functions** | [server/database/crud.py](server/database/crud.py#L1142) (parse_issue_references, parse_mentions) |
| **Frontend Component** | [foxnestFrontend/src/components/IssuesModal.jsx](foxnestFrontend/src/components/IssuesModal.jsx) |
| **Frontend API** | [foxnestFrontend/src/utils/api.js](foxnestFrontend/src/utils/api.js#L217) (lines 217+) |
| **Tests** | [server/tests/test_issue_tracking_api.py](server/tests/test_issue_tracking_api.py) |

---

## 9. Constants & Enumerations

**Issue Types**:
- `bug`, `feature`, `task`, `question`

**Priority Levels**:
- `critical`, `high`, `medium`, `low`

**Statuses**:
- `open`, `in_progress`, `resolved`, `closed`

**Link Types**:
- `ref`: Simple reference
- `fixes` / `closes`: Auto-close trigger

**Notification Types**:
- `issue_mention`, `issue_status`, `issue_comment`, `issue_assigned`, etc.

**Event Types**:
- `issue_created`, `issue_status_changed`, `issue_assigned`, `issue_comment`, etc.

---

## 10. Access Control

All issue tracking endpoints require:
- Valid authentication token
- Repository access level (read for viewing, write for creating/updating)
- Enforced via `require_repository_access()` middleware

---

## Summary

FoxNest's issue tracking system is a **production-ready, fully integrated** implementation featuring:
- ✅ Complete CRUD operations for issues, comments, labels, milestones
- ✅ Automatic issue linking to commits and PRs
- ✅ Smart auto-close on commit/PR merge messages
- ✅ @mention notifications and watchers
- ✅ Event history and audit trail
- ✅ Lifecycle state management with validation
- ✅ Responsive React UI with real-time filtering
- ✅ Comprehensive test coverage
- ✅ Full database schema with proper relationships and indexes
