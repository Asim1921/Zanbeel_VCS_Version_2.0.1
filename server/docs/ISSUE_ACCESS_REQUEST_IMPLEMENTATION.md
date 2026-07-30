# Issue Access Request Feature - Implementation Complete

## 📋 Summary

The **Issue Access Request & Approval Workflow** feature has been successfully implemented in FoxNest. This feature enables secure, controlled repository access through an admin-approval system triggered when users are tagged in issues.

## ✅ What Was Implemented

### 1. **Database Layer**
- ✓ New `IssueAccessRequest` model in `server/database/models.py`
  - Tracks repository access requests with pending/approved/denied status
  - Indexes for fast queries on repository and status
  - Foreign keys to Issue, Repository, User (requested_by, requested_user, admin)

### 2. **CRUD Operations** (`server/database/crud.py`)
- ✓ `IssueAccessRequestCRUD` class with methods:
  - `create_request()` - Initiate access request  
  - `get_request()` - Fetch single request
  - `list_pending_requests()` - List by repository and status
  - `list_user_requests()` - List by user
  - `approve_request()` - Grant permission and create UserPermission
  - `deny_request()` - Reject request and log

### 3. **Server Logic** (`server/server.py`)
- ✓ Modified issue creation endpoint to:
  - Parse @mentions from issue description
  - Create access requests for non-repo-owner users
  - Skip repo owners and already-permitted users
  - Send notifications to admin and mentioned users

- ✓ Helper function `_handle_issue_access_requests()`:
  - Parses mentions using existing `parse_mentions()` utility
  - Creates access requests for eligible users
  - Generates appropriate notifications

### 4. **API Endpoints** (3 new endpoints)

| Endpoint | Method | Purpose | Auth |
|----------|--------|---------|------|
| `/api/access-requests/pending` | GET | List pending requests (with optional repo_id filter) | Repo owner |
| `/api/access-requests/my-requests` | GET | List requests for current user (with status filter) | Any user |
| `/api/access-requests/{id}/approve` | POST | Approve request and grant write permission | Repo owner |
| `/api/access-requests/{id}/deny` | POST | Deny request and notify user | Repo owner |

### 5. **Notification System**
- ✓ 4 notification types implemented:
  - `issue_access_request_created` - Alerts repo admin
  - `issue_access_request_pending` - Informs tagged user
  - `issue_access_request_approved` - Includes CLI setup guide
  - `issue_access_request_denied` - Explanation message

### 6. **Testing Suite**
- ✓ Comprehensive test file: `server/tests/test_issue_access_request_api.py`
  - Tests issue creation with @mentions
  - Tests multiple mentions creating multiple requests
  - Tests approval workflow and permission granting
  - Tests denial workflow
  - Tests listing permissions for admins and users
  - Tests permission boundaries (non-admin authorization)
  - Tests that repo owners don't create requests

---

## 🔄 Complete Workflow

```
ADMIN/REPO OWNER creates issue with @dev_user mention
        ↓
System detects @dev_user, checks ownership
        ↓
dev_user doesn't own repo? → Proceed
        ↓
Check if dev_user already has permission? → No
        ↓
CREATE access request in database
        ↓
NOTIFY repo owner → "dev_user needs access for issue #42"
NOTIFY dev_user → "Access request pending for repository X"
        ↓
Admin reviews in dashboard & clicks Approve/Deny
        ↓
IF APPROVED:
  ├─ Grant write permission to repository
  ├─ Create Notification with CLI setup:
  │  • $ fox clone owner/repo_name
  │  • $ fox config user.name ""
  │  • $ fox pull
  │  • $ fox add <files>
  │  • $ fox commit -m "fixes #42"
  │  • $ fox push
  │  └─ dev_user can immediately clone and work
  │
  IF DENIED:
  └─ Create "Access Denied" notification with reason
```

---

## 🛠 How to Deploy

### Pre-Deployment
1. Backup production database
2. Review code changes:
   - `server/database/models.py` - IssueAccessRequest class
   - `server/database/crud.py` - IssueAccessRequestCRUD class
   - `server/server.py` - Issue creation logic + API endpoints

### Migration Strategy (Zero Downtime)
```bash
# Step 1: Database migration
# Since IssueAccessRequest is a NEW table, no data migration needed
# Simply run initialization after deployment

# Step 2: Python dependencies check (no new packages needed)

# Step 3: Restart server
systemctl restart foxnest-backend
```

### Rollback Plan (if needed)
- IssueAccessRequest table can be safely dropped without affecting existing data
- The feature is completely isolated and optional
- Existing permission system remains unchanged

---

## 📡 API Usage Examples

### 1. Create Issue with @mention (triggers access request)
```bash
curl -X POST http://localhost:5000/api/repository/repo123/issues \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Fix authentication bug",
    "description": "@john_dev please implement this",
    "issue_type": "bug",
    "priority": "high"
  }'
```
**Response:**
```json
{
  "success": true,
  "issue": {
    "number": 42,
    "title": "Fix authentication bug",
    "status": "open"
  }
}
```

### 2. List Pending Requests (Admin)
```bash
curl -X GET "http://localhost:5000/api/access-requests/pending?repo_id=repo123" \
  -H "Authorization: Bearer <admin_token>"
```

**Response:**
```json
{
  "success": true,
  "access_requests": [
    {
      "id": 15,
      "issue_number": 42,
      "issue_title": "Fix authentication bug",
      "repository_id": "repo123",
      "repository_name": "backend-service",
      "requested_user": "john_dev",
      "requested_by": "admin_user",
      "status": "pending",
      "reason": "Tagged in issue #42: Fix authentication bug",
      "created_at": "2026-04-16T10:30:00"
    }
  ]
}
```

### 3. Approve Access Request
```bash
curl -X POST http://localhost:5000/api/access-requests/15/approve \
  -H "Authorization: Bearer <admin_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "comment": "Approved for Q2 release"
  }'
```

**Response:**
```json
{
  "success": true,
  "message": "Access granted to @john_dev",
  "permission": {
    "user": "john_dev",
    "repository": "backend-service",
    "permission_level": "write",
    "granted_at": "2026-04-16T10:32:00"
  }
}
```

Notification sent to john_dev includes CLI setup:
```
✅ REPOSITORY ACCESS GRANTED

Repository: backend-service
Owner: @admin_user

QUICK START - FoxNest CLI
1️⃣  CLONE: $ fox clone admin_user/backend-service
2️⃣  ENTER: $ cd backend-service
3️⃣  CONFIG: $ fox config user.name "John Dev"
4️⃣  PULL: $ fox pull
5️⃣  BRANCH: $ fox branch feature/issue-42
6️⃣  COMMIT: $ fox add src/auth.py && fox commit -m "Fix: auth (Fixes #42)"
7️⃣  PUSH: $ fox push
```

### 4. List My Requests (User)
```bash
curl -X GET http://localhost:5000/api/access-requests/my-requests \
  -H "Authorization: Bearer <user_token>"
```

---

## 🔐 Security Features

1. **Authorization Checks**
   - Only repo owner can approve/deny requests
   - Only request requester can view their pending requests
   - No permission escalation possible

2. **Automatic Safeguards**
   - Repo owners auto-skipped (no request needed)
   - Duplicate requests prevented (unique constraint)
   - Existing permissions recognized (no double grant)
   - Inactive users ignored

3. **Audit Trail**
   - All approvals/denials logged in Activity table
   - Timestamps track when actions occur
   - Admin comment stored with decision
   - Full request history preserved

---

## 📊 Status Transitions

### Access Request Status Flow
```
PENDING ──[Admin Approves]──→ APPROVED ──[Permission Created]──→ (User gains access)
   ↓
   └──[Admin Denies]──→ DENIED ──[User notified]──→ (Can request again)
```

### Issue Status (Independent)
- Not affected by access request status
- Continues as: open → in_progress → resolved → closed
- Access request is transparent to issue lifecycle

---

## 🚀 Features & Benefits

| Feature | Benefit |
|---------|---------|
| Automatic request generation | No manual setup needed |
| One-click approval | Fast access provisioning |
| CLI setup guide in notification | Users ready to work immediately |
| Mention-based triggering | Seamless workflow integration |
| Repository owner control | Prevents unauthorized access |
| Audit trail | Compliance and security |
| Repo name consistency | GUI name = CLI name |

---

## 📋 Database Schema

### `issue_access_requests` Table
```sql
CREATE TABLE issue_access_requests (
    id INTEGER PRIMARY KEY,
    issue_id INTEGER FOREIGN KEY → issues.id,
    repository_id TEXT FOREIGN KEY → repositories.id,
    requested_by_id INTEGER FOREIGN KEY → users.id,
    requested_user_id INTEGER FOREIGN KEY → users.id,
    status TEXT ('pending', 'approved', 'denied'),
    admin_id INTEGER FOREIGN KEY → users.id,
    reviewed_at DATETIME,
    review_comment TEXT,
    request_reason TEXT,
    created_at DATETIME,
    updated_at DATETIME,
    
    UNIQUE (issue_id, requested_user_id),
    INDEX (repository_id, status),
    INDEX (requested_user_id, status),
    INDEX (created_at DESC)
);
```

---

## ✅ Deployment Checklist

- [ ] Code reviewed by team
- [ ] Database backup created
- [ ] Syntax validated (Python 3.8+)
- [ ] Import statements verified
- [ ] Test suite reviewed
- [ ] Notification templates approved
- [ ] API endpoints documented
- [ ] Authorization logic validated
- [ ] Rollback procedure documented
- [ ] Monitoring setup for errors
- [ ] User documentation prepared
- [ ] Server restarted successfully
- [ ] End-to-end workflow tested
- [ ] Admin notifications working
- [ ] User notifications working
- [ ] CLI setup guide displaying correctly

---

## 📞 Support & Troubleshooting

### Issue: Access request not created
- Check if user is repo owner (should skip)
- Check if user already has permission (should skip)
- Verify mentions are using `@username` format
- Check user account is active (is_active=true)

### Issue: Notification not received
- Verify user is active in system
- Check notification type is correct
- Ensure database transaction committed
- Verify user notification preferences

### Issue: Permission not appearing after approval
- Verify admin user is repo owner
- Check UserPermission record created
- Ensure transaction was committed
- Verify permission_level set to 'write'

---

## 🎯 Phase 2 Recommendations (Future)

1. **Email Notifications** - Queue notifications for email delivery
2. **Auto-expire Access** - Add expiry_date field for temporary access
3. **Scope-based Permissions** - Branch-specific or directory-specific access
4. **Approval Templates** - Pre-configured approval workflows
5. **Bulk Access Management** - Grant access to multiple users at once
6. **Access Analytics** - Dashboard showing trends and patterns

---

## 📁 Files Modified

```
server/database/models.py
  ├─ Added IssueAccessRequest class (45 lines)

server/database/crud.py
  ├─ Added IssueAccessRequestCRUD class (165 lines)
  ├─ Updated imports

server/server.py
  ├─ Modified create_issue() endpoint
  ├─ Added _handle_issue_access_requests() function
  ├─ Added 4 new API endpoints (210 lines)
  ├─ Added _notify_user_access_approved() helper
  ├─ Updated imports

server/tests/test_issue_access_request_api.py  
  └─ NEW comprehensive test suite (350 lines)
```

---

**Implementation Date:** April 16, 2026
**Status:** ✅ COMPLETE - Ready for deployment
**Backward Compatible:** ✅ YES
**Data Migration Required:** ❌ NO  
**New Dependencies:** ❌ NO
