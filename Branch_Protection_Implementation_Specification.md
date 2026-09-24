# Branch Protection and Immutable Release Specification

**Document status:** Implementation draft  
**Target system:** Custom Version Control System (VCS)  
**Primary objective:** Prevent unauthorized or accidental modification of verified branches and releases while supporting controlled future updates.

---

## 1. Purpose

This document defines a branch-protection system for a custom VCS. It covers:

- Completely read-only branches.
- Protected branches that can advance only through a controlled promotion process.
- Immutable release references.
- Permissions, approvals, CI checks, signatures, audit records, and emergency unlocking.
- Server-side validation, concurrency control, APIs, error codes, and testing requirements.

Branch protection preserves a validated version; it does not prove that the version is bug-free. Testing, review, security scanning, and release validation establish confidence in the code. Protection ensures that the validated code cannot be silently replaced afterward.

---

## 2. Core Design Decision

A branch is a named reference to a commit. If the reference is completely locked, it cannot also accept normal updates.

The VCS should therefore provide two complementary mechanisms:

1. **Protected stable branch:** May advance to a new verified commit through a controlled promotion operation.
2. **Immutable release reference:** Permanently identifies a previously verified commit and can never move.

Recommended layout:

```text
feature/*                 Normal development
develop                   Integration branch
release-candidate         Candidate undergoing validation
stable                    Current approved version
releases/v1.0.0           Immutable approved release
releases/v1.1.0           Immutable approved release
```

---

## 3. Terminology

| Term | Meaning |
| --- | --- |
| Reference | A named pointer to a commit object. Branches and release tags are references. |
| Direct push | A user requests that a branch reference move without an approved promotion or merge-request workflow. |
| Force update | A non-fast-forward update that discards or replaces reachable history. |
| Promotion | A privileged operation that advances a protected branch to an exact, previously validated commit. |
| Frozen branch | A branch for which every reference-changing operation is rejected. |
| Immutable release | A permanent signed reference to one exact commit. |
| Attestation | Signed evidence that a named check ran against a particular commit and policy version. |
| Break-glass operation | Exceptional, audited, time-limited bypass used during an emergency. |

---

## 4. Protection Modes

### 4.1 Open

Used for ordinary development.

- Direct pushes may be allowed.
- Normal merges may be allowed.
- Force updates and deletion depend on repository permissions.
- Reviews and checks are optional.

### 4.2 Protected

Used for `stable`, `production`, or other critical branches.

- Human direct pushes are denied.
- Updates must use an approved merge-request or promotion workflow.
- Force updates are denied.
- Deletion and renaming are denied.
- Required checks must pass for the exact target commit.
- Required approvals must apply to the exact target commit.
- Commit or promotion signatures may be required.
- Only authorized identities, preferably a release-service identity, may perform the final reference update.
- Updates should be fast-forward-only unless a stricter policy requires the candidate to have the current protected tip as its direct parent.

### 4.3 Frozen

Used when the branch must remain exactly as it is.

- Direct pushes are denied.
- Merge updates are denied.
- Promotions are denied.
- Force updates are denied.
- Deletion and renaming are denied.
- Automated service updates are denied.
- Policy removal or weakening requires a separately authorized unlock workflow.

### 4.4 Archived

Used for historical branches.

- All Frozen-mode restrictions apply.
- The branch is excluded from normal development views by default.
- It remains readable, cloneable, and auditable.
- Metadata should identify its support status and archival date.

### 4.5 Immutable Release

An immutable release is not a normal writable branch.

- It is created once.
- It can never be moved to another commit.
- It cannot be deleted through ordinary repository operations.
- It should contain a signed release manifest.
- Corrections produce a new release identifier rather than modifying the existing one.

Example: if `releases/v1.2.0` is incorrect, create `releases/v1.2.1`; never move `releases/v1.2.0`.

---

## 5. Recommended Stable-Branch Policy

```json
{
  "schema_version": 1,
  "repository_id": "repo-123",
  "branch_pattern": "stable",
  "mode": "protected",
  "direct_push": "deny",
  "merge": "deny",
  "promotion": "allow",
  "force_update": "deny",
  "delete": "deny",
  "rename": "deny",
  "require_fast_forward": true,
  "require_linear_history": true,
  "require_signed_commits": true,
  "required_approvals": 2,
  "dismiss_approvals_on_new_commit": true,
  "disallow_self_approval": true,
  "required_checks": [
    "unit-tests",
    "integration-tests",
    "security-scan",
    "build-verification"
  ],
  "allowed_updaters": [
    "service:release-service"
  ],
  "unlock_policy": {
    "required_approvals": 2,
    "require_mfa": true,
    "require_reason": true,
    "maximum_duration_minutes": 30,
    "notify_repository_owners": true
  }
}
```

For a completely locked branch, set `mode` to `frozen` and deny promotion as well.

---

## 6. Policy Data Model

Suggested relational representation:

### 6.1 `branch_policies`

| Field | Type | Description |
| --- | --- | --- |
| `id` | UUID | Policy identifier. |
| `repository_id` | UUID | Repository receiving the policy. |
| `branch_pattern` | String | Exact name or supported pattern. |
| `mode` | Enum | `open`, `protected`, `frozen`, or `archived`. |
| `policy_version` | Integer | Increments after every policy change. |
| `rules` | JSON/Object | Normalized protection settings. |
| `created_by` | Principal ID | Policy creator. |
| `created_at` | Timestamp | Creation time. |
| `updated_by` | Principal ID | Last authorized editor. |
| `updated_at` | Timestamp | Last update time. |

### 6.2 `reference_state`

| Field | Type | Description |
| --- | --- | --- |
| `repository_id` | UUID | Repository identifier. |
| `reference_name` | String | Canonical reference name. |
| `object_id` | Hash | Current commit or release-object hash. |
| `generation` | Integer | Monotonically increasing reference version. |
| `reference_type` | Enum | `branch`, `release`, or another supported type. |
| `updated_at` | Timestamp | Last successful update. |

### 6.3 `approvals`

An approval must bind to all security-relevant inputs:

```json
{
  "repository_id": "repo-123",
  "target_branch": "stable",
  "source_commit": "sha256:...",
  "current_target_commit": "sha256:...",
  "policy_version": 14,
  "reviewer_id": "user-377",
  "decision": "approved",
  "created_at": "2026-09-24T14:20:00Z",
  "signature": "..."
}
```

If the candidate commit, target tip, or policy version changes, the approval is stale and must not count.

### 6.4 `check_attestations`

```json
{
  "repository_id": "repo-123",
  "commit": "sha256:...",
  "check_name": "integration-tests",
  "result": "passed",
  "issuer": "service:trusted-ci",
  "workflow_digest": "sha256:...",
  "started_at": "2026-09-24T14:00:00Z",
  "completed_at": "2026-09-24T14:08:00Z",
  "expires_at": "2026-09-25T14:08:00Z",
  "signature": "..."
}
```

Only attestations issued by trusted CI identities should satisfy required checks. A normal repository writer must not be able to create a successful check result.

---

## 7. Permission Model

Define narrow, independent permissions instead of relying only on broad roles such as administrator.

```text
branch.read
branch.create
branch.update
branch.force_update
branch.delete
branch.rename
branch.promote
branch.freeze
branch.unlock
branch.policy.read
branch.policy.write
release.create
release.read
audit.read
break_glass.request
break_glass.approve
```

Recommended rules:

- `branch.update` does not imply `branch.force_update`.
- `branch.policy.write` does not automatically imply `branch.unlock`.
- Repository administration does not automatically bypass protected-branch rules.
- A requester cannot provide all approvals for their own promotion or unlock request.
- Service identities receive only the permissions required for their specific operation.
- Deny rules take precedence over allow rules.

### Policy matching

If multiple policies match one branch, use the most restrictive result for every capability. Do not use a “most permissive policy wins” rule.

Recommended precedence:

1. Exact branch-name policy.
2. Most specific pattern policy.
3. Repository default policy.
4. Organization default policy.

After collecting applicable policies, explicit denial wins. A lower-level repository setting must not weaken an organization-level protection rule unless that override is explicitly supported and authorized.

---

## 8. Authoritative Enforcement Point

All operations that create, update, rename, or delete a reference must pass through one server-side reference service.

Protection must not depend solely on:

- Client-side hooks.
- Disabled UI buttons.
- Command-line warnings.
- Developer behavior.
- Separate validation paths for web, API, SSH, automation, and local protocols.

Every transport must call the same policy-enforcement function.

### Reference-update algorithm

```text
updateReference(request):
    authenticate request.actor
    authorize request.actor for the requested operation

    begin transaction
        current = lockAndReadReference(request.repository, request.reference)
        policy = resolveEffectivePolicy(request.repository, request.reference)

        if current.objectId != request.expectedOldObjectId:
            reject STALE_REFERENCE

        if current.generation != request.expectedGeneration:
            reject STALE_REFERENCE

        decision = evaluatePolicy(
            actor = request.actor,
            operation = request.operation,
            current = current,
            proposedObjectId = request.newObjectId,
            policy = policy,
            evidence = request.evidence
        )

        if decision.denied:
            appendDeniedAuditEvent(request, policy, decision)
            reject decision.errorCode

        verifyObjectExists(request.newObjectId)
        verifyObjectIntegrity(request.newObjectId)

        appendSuccessfulAuditEvent(request, policy, decision)
        compareAndSwapReference(
            expectedObjectId = request.expectedOldObjectId,
            expectedGeneration = request.expectedGeneration,
            newObjectId = request.newObjectId
        )
    commit transaction
```

The reference update and its successful audit event should commit atomically. If atomic storage across both is impossible, use a transactional outbox or write-ahead event that can be reconciled deterministically.

---

## 9. Operation Decision Matrix

| Operation | Open | Protected | Frozen | Immutable release |
| --- | ---: | ---: | ---: | ---: |
| Read/clone | Allow | Allow | Allow | Allow |
| Direct push | Policy | Deny | Deny | Deny |
| Approved merge | Policy | Optional | Deny | Deny |
| Certified promotion | N/A | Allow after checks | Deny | N/A |
| Force update | Policy | Deny | Deny | Deny |
| Delete | Policy | Deny | Deny | Deny |
| Rename | Policy | Deny | Deny | Deny |
| Weaken policy | Authorized | Unlock workflow | Unlock workflow | Deny |
| Archive | Authorized | Authorized workflow | Authorized workflow | N/A |

---

## 10. Promotion Workflow

Recommended command:

```bash
vcs promote release-candidate --to stable
```

The client submits a request but does not decide whether the promotion is valid.

### Validation sequence

1. Resolve the source name to an exact candidate commit hash.
2. Read the current target commit and target generation.
3. Resolve the effective target policy and record its version.
4. Confirm the actor may request a promotion.
5. Confirm the final updater is the authorized release service.
6. Confirm the candidate is a descendant of the current target.
7. Confirm linear-history or direct-parent requirements.
8. Verify signatures on all commits introduced by the promotion, if required.
9. Verify required approvals for the exact candidate, target tip, and policy version.
10. Verify required check attestations for the exact candidate commit.
11. Confirm attestations came from trusted issuers and have not expired.
12. Re-read the target and policy inside the update transaction.
13. Atomically advance the target using compare-and-swap.
14. Write the audit event.
15. Optionally create an immutable release reference and signed release manifest.

### Important anti-race requirement

Never approve only a branch name such as `release-candidate`. Approve its resolved commit hash. Branch names can move after approval; commit hashes should not.

---

## 11. Frozen-Branch Behavior

When a branch is frozen, reject any operation that changes its reference state:

```text
if effectivePolicy.mode == FROZEN:
    if operation in [UPDATE, MERGE, PROMOTE, FORCE_UPDATE, DELETE, RENAME]:
        reject BRANCH_FROZEN
```

The server should return a clear response:

```json
{
  "error": "BRANCH_FROZEN",
  "message": "Branch 'stable-v1' is frozen and cannot be modified.",
  "branch": "stable-v1",
  "current_commit": "sha256:...",
  "policy_id": "policy-456",
  "policy_version": 14
}
```

Reading, cloning, fetching, comparing, and creating a new branch from a frozen branch remain allowed unless another policy denies them.

---

## 12. Immutable Release Manifest

Example:

```json
{
  "schema_version": 1,
  "release": "v1.4.0",
  "repository_id": "repo-123",
  "commit": "sha256:8dc17a...",
  "tree_hash": "sha256:36ba91...",
  "parent_release": "v1.3.0",
  "created_at": "2026-09-24T14:30:00Z",
  "created_by": "service:release-service",
  "approved_by": ["user-142", "user-377"],
  "checks": [
    "unit-tests",
    "integration-tests",
    "security-scan",
    "build-verification"
  ],
  "policy_id": "policy-456",
  "policy_version": 14,
  "artifact_digests": {
    "linux-amd64": "sha256:...",
    "source-archive": "sha256:..."
  },
  "signature": "..."
}
```

The signature should cover the canonical serialization of all fields except the signature field itself.

---

## 13. Unlock and Break-Glass Workflow

Unlocking a branch is a security-sensitive operation, not an ordinary policy edit.

### Required controls

- Dedicated `branch.unlock` or `break_glass.request` permission.
- Strong reauthentication or MFA.
- Mandatory reason and ticket/incident identifier.
- Approval from at least two authorized people.
- Requester cannot approve their own request.
- Optional waiting period before activation.
- Short expiration time, such as 30 minutes.
- Explicit scope: repository, branch, allowed operations, and requester.
- Notification to repository owners and security administrators.
- Automatic expiration and refreezing.
- Full audit trail for request, approvals, activation, use, and expiration.

Example:

```bash
vcs branch unlock stable \
  --reason "Critical production vulnerability CVE-XXXX" \
  --ticket "INC-2041" \
  --allow promote \
  --expires-in 30m
```

An emergency grant should be represented as a separate, expiring authorization record. Avoid rewriting or deleting the original policy during the emergency.

---

## 14. Audit Log

Record both successful and denied security-relevant operations.

### Events to record

- Policy created, changed, or deleted.
- Branch frozen, archived, or unlocked.
- Direct push rejected.
- Force update rejected.
- Promotion requested, approved, rejected, or completed.
- Approval created, withdrawn, invalidated, or expired.
- Check attestation accepted or rejected.
- Release created.
- Break-glass request created, approved, activated, used, expired, or revoked.
- Reference deletion or rename attempts.

### Suggested event structure

```json
{
  "event_id": "evt-789",
  "event_type": "branch.promotion.completed",
  "repository_id": "repo-123",
  "reference": "stable",
  "actor": "service:release-service",
  "requester": "user-142",
  "old_object_id": "sha256:old...",
  "new_object_id": "sha256:new...",
  "old_generation": 21,
  "new_generation": 22,
  "policy_id": "policy-456",
  "policy_version": 14,
  "request_id": "req-abc",
  "source_ip": "192.0.2.10",
  "occurred_at": "2026-09-24T14:30:00Z",
  "previous_event_hash": "sha256:...",
  "event_hash": "sha256:..."
}
```

Use append-only storage, restricted write access, retention rules, and hash chaining or another tamper-evident mechanism. Export critical audit records to a separate security boundary so repository administrators cannot silently erase them.

---

## 15. API Suggestions

### Create or update policy

```http
PUT /repositories/{repositoryId}/branch-policies/{policyId}
If-Match: "policy-version-13"
```

### Freeze branch

```http
POST /repositories/{repositoryId}/branches/{branch}/freeze
```

### Request promotion

```http
POST /repositories/{repositoryId}/promotions
```

```json
{
  "source_commit": "sha256:...",
  "target_branch": "stable",
  "expected_target_commit": "sha256:...",
  "expected_target_generation": 21,
  "expected_policy_version": 14
}
```

### Create immutable release

```http
POST /repositories/{repositoryId}/releases
```

### Request emergency unlock

```http
POST /repositories/{repositoryId}/branches/{branch}/unlock-requests
```

Use idempotency keys for mutation requests so client retries cannot create duplicate promotions, releases, or unlock requests.

---

## 16. Error Codes

| Code | Meaning |
| --- | --- |
| `BRANCH_PROTECTED` | Requested operation is disallowed by branch protection. |
| `BRANCH_FROZEN` | Every reference-changing operation is denied. |
| `IMMUTABLE_REFERENCE` | An immutable release cannot be moved or deleted. |
| `DIRECT_PUSH_DENIED` | The target requires promotion or review workflow. |
| `FORCE_UPDATE_DENIED` | Non-fast-forward update is forbidden. |
| `STALE_REFERENCE` | Expected old commit or generation no longer matches. |
| `STALE_POLICY` | Policy changed after validation began. |
| `STALE_APPROVAL` | Approval does not match the current candidate, target, or policy. |
| `INSUFFICIENT_APPROVALS` | Required independent approvals are missing. |
| `REQUIRED_CHECK_MISSING` | A required check has no valid attestation. |
| `REQUIRED_CHECK_FAILED` | A required check failed. |
| `UNTRUSTED_ATTESTATION` | Check result came from an unauthorized issuer. |
| `SIGNATURE_REQUIRED` | Required commit, promotion, or manifest signature is missing. |
| `SIGNATURE_INVALID` | Cryptographic verification failed. |
| `NON_FAST_FORWARD` | Proposed target is not a descendant of the current target. |
| `UNLOCK_REQUIRED` | The operation requires an approved temporary unlock. |
| `UNLOCK_EXPIRED` | Temporary authorization is no longer valid. |

---

## 17. Security Requirements

### 17.1 Server-side enforcement

Apply protection to every protocol and mutation path, including web UI, REST/RPC API, SSH push, background jobs, mirroring, imports, and administrative tools.

### 17.2 Canonical reference names

- Normalize and validate names before matching policies.
- Reject ambiguous encodings and path traversal forms.
- Define case-sensitivity explicitly.
- Prevent branch/tag namespace confusion.
- If branches and release tags may share display names, apply equivalent protection to both namespaces or expose the distinction clearly.

### 17.3 Cryptographic integrity

- Use collision-resistant object hashes.
- Validate objects before making them reachable from a protected reference.
- Support signed commits or signed promotion records.
- Sign immutable release manifests.
- Store trusted public keys separately from repository content.
- Support key rotation and revocation without rewriting historical releases.

### 17.4 CI trust boundary

- Bind checks to the exact commit.
- Accept results only from trusted CI identities.
- Include workflow/configuration digests in attestations.
- Prevent untrusted pull-request code from obtaining protected signing credentials.
- Expire old attestations when appropriate.

### 17.5 Denial and inheritance

- Deny by default for protected operations.
- Explicit denial wins when policies overlap.
- Child/repository policy must not silently weaken an organization policy.
- Policy edits require optimistic concurrency control.

### 17.6 Backup and recovery

- Back up repository objects, reference state, policies, signatures, and audit logs.
- Maintain off-system or immutable backups for critical releases.
- Test restoration regularly.
- Restoration must not bypass protection silently; record recovery operations in the audit trail.

---

## 18. Concurrency and Transaction Requirements

Reference updates must use compare-and-swap semantics:

```text
CAS(reference, expectedHash, expectedGeneration, newHash)
```

Reject the operation if either expected value differs from current state.

For a relational store, use a transaction and row lock or a versioned conditional update. For a distributed key-value or object store, use its conditional-write version/ETag mechanism. Do not implement correctness using only a process-local mutex when multiple servers can update the repository.

Before commit, revalidate:

- Current target hash and generation.
- Effective policy version.
- Approval validity.
- Required check attestations.
- Unlock grant status and expiration.

This closes time-of-check/time-of-use gaps.

---

## 19. Recommended Command-Line Interface

```bash
# View effective protection
vcs branch policy show stable

# Protect a branch
vcs branch protect stable --policy stable-policy.json

# Completely freeze a branch
vcs branch freeze stable-v1 \
  --reason "Verified production baseline"

# Request promotion of an exact commit
vcs promote sha256:abc123... --to stable

# Create an immutable release
vcs release create v1.4.0 \
  --commit sha256:abc123... \
  --sign

# Request an emergency, time-limited unlock
vcs branch unlock stable \
  --reason "Critical production vulnerability" \
  --ticket INC-2041 \
  --expires-in 30m

# Inspect audit history
vcs audit branch stable
```

The CLI should display the effective policy and server rejection reason, but the server remains authoritative.

---

## 20. Test Plan

### 20.1 Unit tests

- Every operation against every protection mode.
- Exact-name and wildcard matching.
- Deny-overrides-allow behavior.
- Fast-forward and non-fast-forward detection.
- Approval invalidation after candidate change.
- Approval invalidation after target change.
- Approval invalidation after policy change.
- Check attestation issuer, commit, expiry, and signature validation.
- Self-approval restrictions.
- Unlock expiry and scope validation.
- Canonical branch-name handling.

### 20.2 Integration tests

- Direct update through each supported transport.
- Two simultaneous promotions using the same expected target.
- Promotion during a policy update.
- Promotion during unlock expiration.
- Service restart during reference update.
- Audit-write failure during reference update.
- Repository import containing a protected reference.
- Mirror attempting to force-update a protected branch.
- Backup restoration of immutable releases and policy state.

### 20.3 Required negative tests

The following attempts must fail:

- Administrator directly pushes to a protected branch without explicit bypass policy.
- Release service promotes an unapproved commit.
- Approval is reused after the candidate branch moves.
- A repository writer forges a passing check.
- A force update is disguised as a normal push.
- Frozen branch is deleted or renamed through an alternate API.
- Policy is weakened without the required unlock workflow.
- Expired emergency authorization is reused.
- Existing immutable release name is recreated with another commit.
- Case or encoding variation bypasses branch-name matching.

### 20.4 Property and fuzz tests

- A frozen reference never changes under any generated operation sequence.
- An immutable release name maps to no more than one object for its lifetime.
- A successful protected update always has matching approvals and checks.
- Reference generation increases monotonically.
- Concurrent successful updates never lose another committed update.
- Malformed reference names cannot escape their namespace.

---

## 21. Implementation Phases

### Phase 1: Minimum viable protection

- Add `open`, `protected`, and `frozen` modes.
- Centralize reference updates on the server.
- Deny direct push, force update, deletion, and rename as configured.
- Add atomic compare-and-swap reference updates.
- Add structured audit events.
- Add effective-policy inspection command/API.

### Phase 2: Controlled promotion

- Add exact-commit promotion requests.
- Add required CI checks.
- Add independent approvals.
- Invalidate approvals after relevant state changes.
- Restrict final updates to a release-service identity.

### Phase 3: Immutable releases

- Add create-once release references.
- Add signed release manifests.
- Add artifact digests and verification commands.
- Add immutable/off-system audit and backup storage.

### Phase 4: Enterprise controls

- Add organization-level policies.
- Add two-person and MFA-protected unlock workflow.
- Add time-limited break-glass authorization.
- Add signed CI attestations and key rotation.
- Add compliance exports, retention policies, and recovery validation.

---

## 22. MVP Acceptance Criteria

The initial feature is ready when all of the following are true:

- [ ] Every reference mutation uses the same server-side enforcement service.
- [ ] A frozen branch cannot be pushed, merged, promoted, force-updated, deleted, or renamed.
- [ ] A protected branch cannot be directly pushed or force-updated.
- [ ] A protected branch can advance only through an authorized exact-commit promotion.
- [ ] Stale concurrent updates fail instead of overwriting another update.
- [ ] Required checks are bound to the candidate commit hash.
- [ ] Approvals become stale when the candidate, target, or policy changes.
- [ ] Policy changes are versioned and audited.
- [ ] Immutable release references cannot move or be recreated.
- [ ] Security-relevant success and rejection events are audited.
- [ ] An emergency unlock is explicit, scoped, approved, time-limited, and audited.
- [ ] Automated negative and concurrency tests cover alternate mutation paths.

---

## 23. Recommended Final Architecture

```text
Client / Web UI / SSH / API / Automation
                    |
                    v
          Authentication Service
                    |
                    v
         Reference Update Service
          |         |          |
          v         v          v
     Policy      Evidence     Object
     Engine      Verifier     Store
          |         |
          +----+----+
               |
               v
      Atomic Reference Store
               |
               v
      Tamper-Evident Audit Log
```

The Reference Update Service is the only component permitted to change reference state. The policy engine decides whether the operation is allowed; the evidence verifier validates approvals, checks, signatures, and temporary authorizations; the reference store applies an atomic compare-and-swap; and the audit system records the result.

---

## 24. Reference Material

- [GitHub: Available rules for rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
- [GitLab: Protected branches](https://docs.gitlab.com/user/project/repository/branches/protected/)
- [Git: `update-ref` documentation](https://git-scm.com/docs/git-update-ref.html)

These references are useful comparisons, but this specification intentionally recommends deny-overrides-allow policy evaluation and an explicit immutable-release concept for a security-focused custom VCS.
