# Issue Access Request API - Quick Reference

## Endpoints Summary

### For Repository Owners/Admins

#### 1. GET `/api/access-requests/pending`
List all pending access requests for their repositories.

**Query Parameters**
- `repo_id` (optional): Filter by specific repository

**Response**
```json
{
  "success": true,
  "access_requests": [
    {
      "id": 1,
      "issue_number": 42,
      "issue_title": "Fix bug",
      "repository_id": "repo123",
      "repository_name": "backend",
      "requested_user": "john_dev",
      "requested_by": "admin",
      "status": "pending",
      "reason": "Tagged in issue #42",
      "created_at": "2026-04-16T10:30:00"
    }
  ]
}
```

#### 2. POST `/api/access-requests/{request_id}/approve`
Approve an access request and grant write permission.

**Request Body**
```json
{
  "comment": "Approved"
}
```

**Response**
```json
{
  "success": true,
  "message": "Access granted to @john_dev",
  "permission": {
    "user": "john_dev",
    "repository": "backend",
    "permission_level": "write",
    "granted_at": "2026-04-16T10:32:00"
  }
}
```

User receives notification with CLI setup guide for immediate access.

#### 3. POST `/api/access-requests/{request_id}/deny`
Deny an access request.

**Request Body**
```json
{
  "comment": "Already working on this"
}
```

**Response**
```json
{
  "success": true,
  "message": "Access request denied",
  "request_status": {
    "user": "john_dev",
    "repository": "backend",
    "status": "denied",
    "reason": "Already working on this",
    "denied_at": "2026-04-16T10:35:00"
  }
}
```

---

### For Regular Users

#### 1. GET `/api/access-requests/my-requests`
List all access requests for the current user.

**Query Parameters**
- `status` (optional): 'pending', 'approved', or 'denied'

**Response**
```json
{
  "success": true,
  "access_requests": [
    {
      "id": 1,
      "issue_number": 42,
      "issue_title": "Fix bug",
      "repository_id": "repo123",
      "repository_name": "backend",
      "status": "approved",
      "created_at": "2026-04-16T10:30:00",
      "reviewed_at": "2026-04-16T10:32:00",
      "admin_comment": "Approved"
    }
  ]
}
```

---

## Workflow Example

### Step 1: Create Issue with @mention
```bash
curl -X POST http://localhost:5000/api/repository/abc123/issues \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{
    "title": "Implement login feature",
    "description": "@john_dev please implement this feature",
    "issue_type": "feature",
    "priority": "high"
  }'
```

**Automatic Actions:**
- Access request created for john_dev
- Admin notified: "john_dev needs access for issue #42"
- john_dev notified: "Access request pending for repository X"

### Step 2: Admin Approves
```bash
curl -X POST http://localhost:5000/api/access-requests/15/approve \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{"comment": "Go ahead"}'
```

**Result:**
- john_dev gets write permission
- john_dev receives CLI setup notification:
```
✅ REPOSITORY ACCESS GRANTED

QUICK START:
$ fox clone admin/backend
$ cd backend
$ fox config user.name "John Dev"  
$ fox pull
$ fox add src/login.py
$ fox commit -m "Feature: login (Fixes #42)"
$ fox push
```

### Step 3: User Works
```bash
$ fox clone admin/backend
$ fox config user.name "John Dev"
$ fox pull
$ fox branch feature/issue-42
$ fox add src/login.py
$ fox commit -m "Implement login (Fixes #42)"
$ fox push
```

**Automatic Result:**
- Issue #42 automatically closes when commit references "Fixes #42"

---

## Error Responses

### 403 Forbidden - Not Repository Owner
```json
{
  "detail": "Only repository owner can approve access requests"
}
```

### 404 Not Found
```json
{
  "detail": "Access request not found"
}
```

### 400 Bad Request - Already Reviewed
```json
{
  "detail": "Request already approved"
}
```

---

## Key Features

✓ **Automatic Triggering** - Create request by mentioning @user in issue  
✓ **One-Click Approval** - Admin approves, user gets access instantly  
✓ **CLI Ready** - Notification includes all setup commands  
✓ **Audit Trail** - All actions logged with timestamps  
✓ **Smart Filtering** - Skips repo owners and already-authorized users  
✓ **Secure** - Only repo owner can approve/deny  

---

## Status Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 400 | Bad request (invalid status, already reviewed) |
| 403 | Forbidden (not repo owner) |
| 404 | Not found (request doesn't exist) |
| 500 | Server error (check logs) |

---

## Testing Commands

### Create test issue
```bash
TOKEN=$( curl -X POST http://localhost:5000/api/auth/login \
  -d '{"username":"admin","password":"pass"}' \
  | jq -r '.access_token' )

curl -X POST http://localhost:5000/api/repository/test123/issues \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"title":"Test","description":"@testuser please fix","issue_type":"bug"}'
```

### List pending requests
```bash
curl -X GET http://localhost:5000/api/access-requests/pending \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### Approve request
```bash
curl -X POST http://localhost:5000/api/access-requests/1/approve \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -d '{}'
```

---

**Last Updated:** April 16, 2026  
**Version:** 1.0
