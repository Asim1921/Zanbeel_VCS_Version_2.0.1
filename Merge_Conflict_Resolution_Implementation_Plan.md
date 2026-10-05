# Merge Conflict Resolution — Implementation Plan

**Document status:** Approved for implementation
**Branch:** `Merge_Conflict_Resolution` (from `main` after PR #5)
**Roadmap workstream:** Merge conflict resolution — "server-side resolution sessions; branch protection enforced on push and merge"
**Gap named in the roadmap:** "A conflicted merge reported the conflict and stopped, with no way to resolve it."
**Guiding rule:** where a choice exists, do what Git and GitHub do.

---

## 1. Starting point

Commit `3782aea` and the Branch Protection work already provide the skeleton:

- Resolution sessions for pull requests and for direct branch merges (`merge_conflict_sessions`, `merge_conflict_files`).
- Staleness detection (a session records both branch heads and refuses to resolve if either moved), abort, reuse of an open session.
- A web resolver dialog, `fox pr resolve` and `fox merge --resolve`.
- The merge gate and branch protection checks on the resolve path.

The branch-protection half of the workstream is therefore done. What remains is that the resolution feature is not yet trustworthy or usable.

## 2. Problems found in the current code

| # | Problem | Where | Severity |
|---|---|---|---|
| 1 | `fox pr resolve --submit` rewrites every `.resolved` file from the server's suggestion before reading it, so the user's edits are discarded and the conflict markers are what land | `client/client1/fox.py` `pr_resolve` | Critical |
| 2 | Pull requests from forks look up their source branch in the upstream repository. A fork PR either cannot merge, or — when upstream has a branch with the same name — merges that branch instead, marks the PR merged, and the fork's work never lands. Affects merge, review pinning, code owners, comments and conflict sessions | `pull_requests.py`, `pr_reviews.py`, `codeowners.py`, `pr_comments.py`, `merge_conflicts.py` | Critical |
| 3 | Resolved content skips review: the resolution commit is written straight to the target, the PR branch does not move, so existing approvals still count for content no reviewer saw | `merge_conflicts.resolve` | Critical |
| 4 | The web resolver decodes with `atob`/`btoa`, corrupting any non-ASCII text it shows or saves | `MergeConflictResolverModal.jsx` | High |
| 5 | Merging is whole-file: two edits anywhere in one file conflict, even in different functions. Shared by PR merge, branch merge, cherry-pick, revert and rebase | `app/services/merge.py` | High |
| 6 | A PR merged through the resolver skips the webhook, merge notification and linked-issue sync that a normal merge performs | `merge_conflicts.resolve` | High |
| 7 | The PR resolver dialog never loads: it is given `repoId` but reads `repo` | `PullRequestsModal.jsx` | High |
| 8 | The PR detail page sends the *source* head as `expected_head_commit_id`; the server compares it with the *target* head, so the merge is refused | `PullRequestDetail.jsx` | High |
| 9 | Progress is not saved: resolutions only reach the server at the final submit | `merge_conflicts.py` | Medium |
| 10 | A modify/delete conflict cannot be resolved by deleting the file — "use ours" writes an empty file | service + dialog | Medium |
| 11 | Nothing stops a resolution that still contains `<<<<<<<` markers | `merge_conflicts.resolve` | Medium |
| 12 | The dialog ignores `stale_reason`; a user finds out only on submit | dialog | Medium |
| 13 | A PR does not show it has conflicts until someone presses Merge; open branch sessions cannot be found in the UI | PR screens, branch panel | Medium |
| 14 | Local `fox merge`: a clean merge writes files but never commits; a conflicted one says "commit the merge" but nothing records the merge, so the commit gets one parent and the next merge hits the same conflicts | `fox.py merge_branch` | Medium |
| 15 | `fox pr merge` looks for a `session_id` the server never returns, so a conflict prints a list and no way forward | `fox.py merge_pull_request` | Medium |
| 16 | PR and branch resolve endpoints return errors in different shapes | `pull_requests.py`, `branches.py` | Low |

## 3. Decisions

### 3.1 Resolved PR conflicts are committed to the PR's own branch (GitHub model)

GitHub's "Resolve conflicts" button commits a merge of the base branch **into the PR's head branch** ("Merge branch 'main' into feature"). The PR then merges normally. We do the same:

- A PR session's resolution is a commit on the **source** branch with parents `[source head, target head]`.
- The source branch moves, so approvals pinned to the old head go **stale** through the existing rule in `pr_reviews` — reviewers approve the resolved content before it can land.
- The PR then merges through the one normal merge path, which is now clean, so the webhook, notifications and issue sync always run (fixes #3 and #6 by construction).
- Permission is **write access to the repository that holds the source branch** — the PR author can resolve their own PR, as on GitHub. For a fork PR that means the fork; an upstream maintainer without access to the fork gets a clear message that the author must resolve it.
- The source branch's own protection rules apply to that commit.

**Branch merges without a PR** keep landing on the target, as `git merge` + push would. They already pass through `merge_gate`, so a branch that requires review refuses them and points at a pull request.

### 3.2 Local merge follows Git

`fox merge` records the merge in progress (`.fox/MERGE_HEAD`, `MERGE_MSG`, conflicted paths), `fox status` lists unmerged paths, and `fox merge --continue` / `fox merge --abort` finish or undo it. `fox commit` refuses while a merge is unfinished and points at `--continue`.

## 4. Phases

### Phase 1 — Stop the data loss (#1, #2, #4, #6, #7, #8, #15)

- `app/services/pr_source.py`: one place that answers "where does this PR's source branch live". Used by PR merge, reviews, code owners, comments, files and conflict sessions.
- Sessions record `source_repository_id`.
- Web: UTF-8 via `TextEncoder`/`TextDecoder`; binary content is never decoded as text.
- CLI: one resolve routine for PRs and branch merges that never overwrites an existing `.resolved` file, checks staleness and reports the server's errors.
- Fix the dialog's props and the detail page's head check.

### Phase 2 — Line-level three-way merge (#5)

- Replace the whole-file merge with a diff3 merge in `app/services/merge.py`:
  - edits to different regions of a file merge cleanly; overlapping or adjacent edits conflict (as in Git);
  - identical changes on both sides are taken once;
  - line endings and the presence of a final newline are preserved byte for byte.
- Markers name the branches and show the common ancestor (Git's `diff3` style):

  ```text
  <<<<<<< main
  target version
  ||||||| base
  common ancestor
  =======
  source version
  >>>>>>> feature
  ```

- Each conflicted file is classified: `content`, `add_add`, `modify_delete`, `binary`.
- The session bundle returns each conflicting region so clients can offer per-region choices.
- The CLI's local merge uses the same algorithm, so local and server agree.
- Very large inputs fall back to a whole-file conflict rather than spending unbounded time.

### Phase 3 — Sessions people can work in (#3, #9, #10, #11, #13, #16)

- Draft resolutions saved per file (`PUT …/conflicts/{session}/files`): content, delete, or clear. Progress survives closing the dialog and can be shared.
- "Delete this file" is a first-class resolution.
- Finalising is refused while any resolved text still contains a complete conflict-marker block, unless the caller passes `allow_markers: true`.
- One error shape for every conflict endpoint: `{success: false, detail, code, ...}`.
- `GET …/pull-requests/{id}/mergeability`: whether the PR merges cleanly, the conflicting paths, the open session if any. Computed by a dry run and cached per (source head, target head).
- PR sessions are listable (`GET …/pull-requests/{id}/conflicts`) so an interrupted resolution can be resumed.

### Phase 4 — Web UI (#12, #13)

- Resolver, modelled on GitHub's conflict editor:
  - file list with resolved/unresolved state and an overall count;
  - editable result with each conflict block offering *Accept {target}*, *Accept {source}*, *Accept both*;
  - target / base / source panes for reference;
  - *Mark as resolved* per file (disabled while markers remain), saved to the server;
  - *Keep file* / *Delete file* for modify/delete conflicts; side choice for binary files;
  - stale-session banner with *Recompute*;
  - *Commit merge* enabled once every file is resolved, with copy that says where the commit goes.
- PR list and PR detail show "This branch has conflicts that must be resolved" with a *Resolve conflicts* button, and "approvals will need renewing" after a resolution.
- Branch panel lists open branch-merge sessions with *Resume*.

### Phase 5 — CLI

- Local merge: diff3 algorithm, `MERGE_HEAD` state, `--continue`, `--abort`, unmerged paths in `fox status`, `fox commit` guard; merge commits are written as full snapshots with both parents.
- Server resolution: `fox pr resolve <id>` opens or resumes the session itself; `--submit`, `--abort`, `--status`; `fox merge <branch> --resolve` shares the same routine.
- `fox pr merge` on conflict says exactly which command resolves it.
- Changes only in `client/client1/fox.py` (the current CLI). The older copies in `client/`, `release/` and `vscode-extension/` are not touched.

### Phase 6 — Tests and verification

- Merge engine: separate edits, overlapping edits, adjacent edits, identical edits, add/add, modify/delete, CRLF, missing final newline, binary, labels.
- Fork PR whose branch shares the target's name merges the fork's content.
- PR resolution lands on the source branch, approvals go stale, the PR then merges with webhook/notification/issue sync.
- Drafts save and resume; delete resolution; marker refusal and override; stale session; mergeability.
- CLI: `pr resolve --submit` keeps edits; local merge `--continue` creates a two-parent commit; `--abort` restores the tree.
- Frontend: `npm run verify:conflicts` checks the UTF-8 round trip and conflict-block parsing; production build passes.
- Full server suite.

## 5. Out of scope

- Rename-aware merging (a file renamed on one side and edited on the other still conflicts).
- Structural merging of binary formats.
- Octopus (more-than-two-parent) merges.
- Syncing the stale CLI copies.

## 6. Implementation record

All six phases are implemented on this branch.

### 6.1 Additional defects found and fixed during implementation

| # | Problem | Fix |
|---|---|---|
| 17 | `fox commit` built each snapshot on the *last commit in `commits.json`* rather than HEAD, so after committing on one branch, a commit on another carried the first branch's files — corrupting the trees every later merge compares | `commit()` builds on HEAD |
| 18 | A commit snapshot keyed files by content hash, so two files with identical content (two empty `__init__.py`) collapsed into one | Merge snapshots disambiguate the key (`<hash>~n`); readers strip the suffix |
| 19 | `GET /pull-requests/{id}` did not return `created_by`, so the detail page never recognised the author | Field added, with `source_repository_id` |
| 20 | Merging a PR that both conflicts and lacks approval reported only the missing approval; reviewers then approved, the merge hit the conflict, and resolving it made their approval stale | Merge reports `MERGE_CONFLICT` first (as GitHub does), with `review_blockers` listing what review still needs; the CLI prints both |
| 21 | After closing the resolver with decisions saved, the PR still offered "Resolve conflicts" instead of "Continue resolving" | Both PR screens refresh mergeability when the resolver closes |

### 6.2 API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/repository/{repo}/pull-requests/{id}/mergeability` | Clean / conflicts / up to date, conflicting paths, open session, whether the caller can resolve |
| GET | `/api/repository/{repo}/pull-requests/{id}/conflicts` | Open sessions for the PR |
| POST | `/api/repository/{repo}/pull-requests/{id}/conflicts` | Open or resume a session (write access to the source branch's repository) |
| GET | `…/conflicts/{session}` | Bundle: three sides, suggested merge, per-region hunks, saved decisions |
| PUT | `…/conflicts/{session}/files` | Save one file's decision: `content_b64`, `delete` or `clear` |
| POST | `…/conflicts/{session}/resolve` | Commit the resolution (PR → source branch; branch merge → target). Body optional: `resolutions` (null = delete), `expected_head_commit_id`, `allow_markers` |
| POST | `…/conflicts/{session}/abort` | Discard the session |
| PUT | `/api/repository/{repo}/branches/conflicts/{session}/files` | Same as above for a branch-merge session |

Every conflict endpoint refuses in one shape: `{success: false, detail, message, code, …}`. Codes: `NO_CONFLICTS`, `SESSION_STALE`, `SESSION_CLOSED`, `INCOMPLETE`, `NOT_CONFLICTED`, `UNRESOLVED_MARKERS`, `HEAD_MISMATCH`, `REVIEW_REQUIRED`, `PULL_REQUEST_CLOSED`, `INVALID_CONTENT`.

### 6.3 Schema

`merge_conflict_sessions`: `source_repository_id`, `result_commit_id`, `resolved_by_id`, `resolved_at`.
`merge_conflict_files`: `conflict_kind`, `resolution`, `resolved_by_id`, `resolved_at`.
Added in place by `ensure_merge_conflict_resolution_columns()` (runs after the session-table rebuild); verified on a copy of the development database, idempotent, schema check healthy.

### 6.4 CLI

```text
fox merge feature                 line-level merge; fast-forward when possible
fox merge --continue              two-parent commit once conflicts are fixed
fox merge --abort                 restore the work tree
fox status                        lists unmerged paths during a merge
fox pr resolve 7                  write .base/.ours/.theirs/.resolved (never overwrites .resolved)
fox pr resolve 7 --status         what is decided, locally and on the server
fox pr resolve 7 --submit [--delete PATH] [--allow-markers]
fox pr resolve 7 --abort
fox merge feature --resolve …     the same, for a server-side branch merge
```

### 6.5 Verification

- `tests/test_merge_engine.py` — 23 engine cases (separate, overlapping, adjacent, identical edits; insertions; CRLF; final newline; non-ASCII; modify/delete; binary; marker detection).
- `tests/test_merge_resolution_workflow.py` — 23 cases: drafts, delete, markers, stale approvals, protected source branch, head guard, mergeability, fork PRs.
- `tests/test_merge_resolution_api.py` — 5 cases: the full GitHub-style flow over HTTP including the webhook on the final merge, the shared error shape, the permission refusal, a fork PR merge, and the real CLI driven against the real API.
- `tests/test_fox_cli_merge.py` — 14 cases: local merge (clean, conflicted, continue, abort, fast-forward, modify/delete, protection of uncommitted work, status, commit-on-HEAD) and `pr resolve` keeping edits.
- `tests/test_merge_conflicts.py` — 20 existing session tests, updated to the source-branch model.
- `npm run verify:conflicts` — 29 checks (UTF-8 round trip incl. Urdu/emoji/BOM, block parsing against the server's exact output); production build passes.
- Full server suite: **701 passed, 0 failed** (22 min), plus the CLI-against-API contract test run separately (passed). Cherry-pick, revert, rebase, branch-merge, fork and review tests all pass unchanged on the new merge engine.
- Real CLI smoke run: a conflicted `fox merge` merged the non-overlapping edit cleanly, wrote labelled diff3 markers for the overlapping one, listed it under "Unmerged paths", refused a plain commit and a `--continue` with markers left, made the two-parent commit once fixed, and reported "Already up to date" on the next merge.

### 6.6 End-to-end scenarios (live server, real browser)

Two harnesses run the whole feature the way users meet it. Neither touches the development database: each starts its own server on a throwaway database and blob store, on free ports, and cleans up afterwards. Both exit non-zero on any failure.

**API + CLI** — from `server/`: `..\venv\Scripts\python.exe tools/e2e_merge_conflicts.py` — **84 checks**

| # | Scenario | What is proven |
|---|---|---|
| 1 | Sign-in and setup | Real password sign-in for four users; pushes over HTTP |
| 2 | Line-level merge | Edits to different parts of one file report no conflict and merge with both edits |
| 3 | PR conflicts, GitHub model | Conflict reported before review; four conflict kinds classified; labelled diff3 markers; only the overlapping line conflicts; Urdu byte-exact; drafts saved and resumed; marker refusal; head guard on the branch written; commit lands on `feature` with `feature` as first parent; main untouched; approval goes stale; merge waits for re-approval; then merges with the resolved content |
| 4 | Stale session | Saving after the branch moves is refused; reopening computes a fresh session |
| 5 | Protected source branch | Resolution refused with `REVIEW_REQUIRED`; branch unchanged |
| 6 | Permissions | A read-only user sees conflicts, is not offered the resolver, cannot open or save, can read |
| 7 | Branch merge | `MERGE_CONFLICT` with `resolve_url`; session listed; shared error shape; add/add + content resolved onto the target with correct parents; review-required target refuses |
| 8 | Fork PRs | Mergeability and approval pinned to the fork's head; the fork's work lands upstream; a conflicting fork PR is resolved in the fork by its owner, refused for others |
| 9 | fox CLI on the live server | `login`; `pr merge` points at `pr resolve` and still lists the missing approval; `pr resolve` writes files, refuses markers, never overwrites edits, `--submit` commits exactly the edited file; `merge --resolve` end to end; local `merge` / `status` / `--continue` |
| 10 | Upgrading a database | New columns added to the original table shapes; `pull_request_id` nullable; legacy resolved rows marked; idempotent |

**Browser** — from `server/`: `..\venv\Scripts\python.exe tools/e2e_merge_conflicts_ui.py` — **37 checks**
(needs Chrome and, once, `cd foxnestFrontend && npm i --no-save playwright-core`; `--headed` to watch, `--keep` to keep screenshots)

Signs in through the login page, opens the repository and its pull requests (conflict badge and *Resolve conflicts* on the row), opens the PR page (*This branch has conflicts that must be resolved*, Merge disabled), resolves `app.py` with *Accept both* (Mark as resolved disabled while markers remain), edits an Urdu file (shown and saved without corruption), deletes a modify/delete file, closes and reopens (*Continue resolving*, decisions kept), commits, and sees the stale-approval state with Merge waiting. No browser console errors. The server is then checked byte for byte against what was clicked.
