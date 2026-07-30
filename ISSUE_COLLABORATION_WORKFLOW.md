# FoxNest Issue Collaboration & Access Control Workflow

## Current System Overview

Your FoxNest has a **role-based permission system** that allows developers to work on issues while maintaining access control.

### User Roles

| Role | Description | Permissions |
|------|-------------|------------|
| **developer** | Standard developer with limited access | Can create issues, comment, write to repository after first commit |
| **team_lead** | Senior developer/manager role | Can manage repositories, approve access, override access controls |
| **admin** | System administrator | Full access to all repositories and system functions |

### Repository Access Levels

| Level | Scope | Permission Level |
|-------|-------|-----------------|
| **Owner** | User who created repo | Can do everything (manage, write, read) |
| **team_lead** | Assigned role | Can manage repository & grant permissions |
| **admin** | System role | Can manage repository & grant permissions |
| **write** | Granted permission | Can push commits, create branches, create issues/comments |
| **read** | Default access | Can view repository and issues |

---

## Complete Issue Workflow

### **STEP 1: Create Issue & Assign Developer**

```
Project Lead/Owner creates issue and assigns to developer
├─ Frontend: Create issue form
├─ API: POST /api/repository/{repo_id}/issues
│  └─ Requires: "write" scope access to repository
├─ Backend: IssueCRUD.create_issue()
│  └─ Starts workflow
└─ TRIGGER: Notifications sent (if mentioned or assigned)
   ├─ @mentions → Notification to mentioned users
   └─ assigned_to → Notification to assignee
```

**Example Flow:**
```
1. Project Lead (bilal8244) creates issue "Fix login bug"
2. Assigns to developer (Div8244)
3. Div8244 receives NOTIFICATION: "Assigned to issue #1: Fix login bug"
```

---

### **STEP 2: Developer Gets Repository Access**

**If developer already has write access:**
- ✅ Can immediately start working (already in UserPermissions table)
- ✅ Can clone/pull repository
- ✅ Can push commits

**If developer needs to be granted access:**

The system checks: `can_write_repository(db, actor, repository)`

```python
def can_write_repository(db, actor, repository):
    # Check 1: Is actor the owner?
    if actor.id == repository.owner_id:
        return True  # Owner has full access
    
    # Check 2: Is actor a team_lead or admin?
    if actor.role in ['team_lead', 'admin']:
        return True  # Privileged role has full access
    
    # Check 3: Has actor already committed to this repo?
    if db.query(Commit).filter(
        Commit.repository_id == repository.id,
        Commit.author_id == actor.id
    ).first():
        return True  # Already contributed, can continue
    
    # Check 4: Does actor have explicit 'write' permission?
    if UserPermissionCRUD.has_permission(db, actor.username, repo.id, 'write'):
        return True  # Explicit write permission granted
    
    return False  # No access
```

**The key insight:** After a developer makes their **FIRST COMMIT**, they automatically get write access to continue!

---

### **STEP 3: Developer Works on Issue**

Once developer has write access:

```
Developer works locally:
├─ clone: git clone repo
├─ create branch: git branch issue-1
├─ make changes: edit files
├─ commit: git commit -m "Fix login bug" 
│  (Can link to issue with "Fixes #1")
└─ push: git push
   └─ API: POST /api/repository/{repo_id}/push
      └─ Requires: "write" scope
         └─ System checks: can_write_repository()
            └─ ✅ ALLOWED (has made 1st commit or has permission)
```

**Link issue to commit:**
- Commit message: `"Fix login bug - Fixes #1"`
- System auto-links issue to commit
- Issue status updates → "in_progress" or similar

---

### **STEP 4: Create Pull Request**

```
Developer creates PR:
├─ API: POST /api/repository/{repo_id}/pull-request
├─ Requires: "write" scope
├─ Creates link between issue and PR
└─ Notifies: Repository owner/team_lead
```

---

### **STEP 5: Approval & Merging**

```
Team Lead/Owner reviews and merges:
├─ Requires: "manage" scope (owner/team_lead/admin only)
├─ API: POST /api/repository/{repo_id}/pull-request/{pr_id}/merge
└─ Result: Commits merged to main branch
   └─ Issue status → "resolved"
```

---

## Access Control Matrix

```
┌─────────────────┬──────────┬─────────┬──────┐
│ Action          │ Developer│ Lead/TL │ Owner│
├─────────────────┼──────────┼─────────┼──────┤
│ View Issue      │    ✅    │   ✅    │  ✅  │
│ Create Issue    │    ✅    │   ✅    │  ✅  │
│ Comment Issue   │    ✅    │   ✅    │  ✅  │
│ Create Branch   │    ✅*   │   ✅    │  ✅  │
│ Push Commit     │    ✅*   │   ✅    │  ✅  │
│ Create PR       │    ✅*   │   ✅    │  ✅  │
│ Merge PR        │    ❌    │   ✅    │  ✅  │
│ Manage Repo     │    ❌    │   ✅    │  ✅  │
│ Grant Access    │    ❌    │   ✅    │  ✅  │
│ Delete Repo     │    ❌    │   ✅    │  ✅  │
└─────────────────┴──────────┴─────────┴──────┘
* After first commit or explicit permission granted
```

---

## Recommended Implementation Workflow

### **Option A: Auto-Grant (Current - Most Flexible)**

```
1. Project Lead creates issue and assigns developer
2. Developer automatically gets access AFTER first commit
   ├─ No approval process needed
   ├─ Developer can immediately start working (if repo already has write permission)
   └─ System tracks and grants on first commit
```

**Best for:** Fast-moving teams, hackathons, agile workflows

---

### **Option B: Explicit Approval (More Controlled - Recommended)**

```
1. Project Lead creates issue and assigns developer
2. ✅ System AUTOMATICALLY grants "write" permission to assignee
   └─ developer gets UserPermission(username, repo_id, 'write')
3. Developer gets notification with:
   ├─ Issue details
   ├─ Repository clone URL
   ├─ Access granted ✅
   └─ Ready to start!
4. Developer clones and works
5. After finishing, creates PR
6. Lead reviews and merges (requires "manage" permission)
```

---

## What You Need to Add

To implement **Option B (Recommended)**, add this to the issue creation:

```python
@app.post("/api/repository/{repo_id}/issues")
async def create_issue(...):
    # ... existing code ...
    
    # NEW: Auto-grant write access to assignee
    if assigned_to_id:
        existing_perm = UserPermissionCRUD.has_permission(
            db, assignee.username, repo_id, 'write'
        )
        if not existing_perm:
            # Grant write permission
            UserPermissionCRUD.grant_permission(
                db,
                assignee.username,
                repo_id,
                'write',
                granted_by_id=current_user.id
            )
            
            # Notify with "access granted" message
            NotificationCRUD.create_notification(
                db,
                user_id=assigned_to_id,
                notification_type="access_granted",
                title=f"Write access granted to {repository.name}",
                body=f"You can now work on issue #{issue.number}",
                payload={
                    "repository_id": repo_id,
                    "repository_name": repository.name,
                    "clone_url": f"<clone_url>"
                }
            )
```

---

## Testing the Complete Flow

### **Test Case:**

1. **User A (team_lead):**
   - Creates repository "test-repo"
   - Creates issue "Implement feature X"
   - Assigns to **User B (developer)**

2. **User B (developer):**
   - ✅ Receives notification
   - ✅ Has write access granted
   - ✅ Clones repo
   - ✅ Creates branch issue-1
   - ✅ Makes commits
   - ✅ Pushes to server
   - ✅ Creates PR

3. **User A (team_lead):**
   - ✅ Reviews PR
   - ✅ Approves
   - ❌ Waits for User A to merge (only Lead/Owner can)

---

## Summary

| Aspect | Implementation |
|--------|-----------------|
| Auth | JWT token based |
| Role verification | Before every write operation |
| Auto-access | After first commit OR explicit grant |
| Issue assignment | Grants "write" permission automatically |
| Notifications | When assigned, mentioned, or permissions change |
| PR review | Only Lead/Owner/Admin can merge |

The flow is: **Assign → Notify → Auto-Grant Access → Developer Works → Submit PR → Lead Merges**

