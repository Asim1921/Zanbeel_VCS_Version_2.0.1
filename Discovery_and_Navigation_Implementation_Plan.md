# Discovery and Navigation — Implementation Plan and Record

**Document status:** Implemented in the working tree, on top of the Integrations work
**Branch:** working tree of `integrations` (Integrations is not committed yet; the two stages can still be committed separately)
**Roadmap workstream:** Discovery and navigation — "Repository, commit and code search; activity audit view; notification surface"
**Gap named in the roadmap:** "Only issues were searchable — no way to find a string across a repository's files or locate a commit by message. Roughly 44% of the API had no user interface: activity, notifications…"
**Week-4 checkpoint:** "Everything the API offers is reachable from the interface."
**Guiding rule:** where a choice exists, do what Git, GitHub and GitLab do.

---

## 1. Starting point

As with the earlier stages, the features existed. What was missing was any check of the paths around them, and the "navigation" half of the workstream.

- **Search:** `/api/search/repositories`, `/commits` and `/code`. Code search covered one repository at a branch tip. There was a Search page and 29 service tests.
- **Activity:** `/api/activities`, an admin-only Activity page, and a ten-row preview on the dashboard.
- **Notifications:** list, unread count, mark read and unread, a bell polling the count every 30 s, and a Notifications page.
- **The web client had no URLs.** Every page was a piece of `useState` in `App.jsx`.

## 2. Problems found

Each was found by reading the code. Most were also reproduced by running this stage's tests against the code as it was before: §6.5 has the results. The 500-for-404 in #13, #15 and #17 were confirmed by reading only.

| # | Problem | Where | Severity |
|---|---|---|---|
| 1 | Any signed-in user got the whole server's activity, including repository names, users and commit subjects from repositories they could not read. The dashboard showed it to every developer | `/api/activities`, Dashboard | High |
| 2 | Code search compiled readers' regular expressions with the stdlib `re` and no time limit, inside an `async` route. One query froze the server: the test for it never finishes on the old code | `services/search.py` | High |
| 3 | No URLs. Nothing could be linked or bookmarked, and Back and refresh dropped you on the dashboard | `App.jsx` | High |
| 4 | Search results led nowhere: no result opened its repository, commit or file | `Search.jsx` | High |
| 5 | The "audit view" fetched at most 500 rows and filtered them in the browser. The API filtered nothing and could not export | `/api/activities`, `Activity.jsx` | High |
| 6 | The activity log missed most changes and described the rest in free text. Branch creation, deletion and rename, queue approvals and rejections, permission, webhook, push-rule, token and key changes, tags, releases, pull-request opening and closing, archives and deletions left no row. Cherry-pick was spelled `cherry-pick` while every other type uses underscores. No row said which commit, branch or credential | ~25 scattered `create_activity` calls | High |
| 7 | Deleting a repository deleted its whole activity history, including any record of who deleted it | `RepositoryCRUD.delete_repository` | High |
| 8 | Code search read up to 4,000 files × 512 KB per query, on the event loop, with no time budget | `search_code` | Medium |
| 9 | No search across repositories, and no "go to file" | — | Medium |
| 10 | A developer was never told their queued commit was approved or rejected unless it referenced an issue, and that is how most commits arrive. Nothing notified on PR @mentions, a closed PR, a failing required check, or access granted from the permissions screen | `review_queue.py`, `pull_requests.py`, `permissions.py` | Medium |
| 11 | Notifications were not actionable: each showed its payload as a dump of ids, with nothing to click | `Notifications.jsx`, `Header.jsx` | Medium |
| 12 | The access-granted notification told every new collaborator to `fox set origin 192.168.15.207:33333`, the LAN address the Critical Bugs report removed from the frontend | `services/notifications.py` | Medium |
| 13 | A permission grant recorded whichever grantor the request body named, so audit attribution could be forged. Revoking a missing permission answered 500 instead of 404 | `permissions.py` | Medium |
| 14 | Commit search read `%` and `_` as wildcards (`fix_typo` matched `fixXtypo`), could not find a commit by id, reported the page size as the total and could not page | `search_commits` | Low |
| 15 | Server times (naive UTC) were read as local time in the activity feed, search results and notifications | web client | Low |
| 16 | No CLI access to search, activity or notifications | `fox.py` | Low |
| 17 | Search ran one or two permission queries per repository on every request | `_visible_repositories` | Low |

## 3. Decisions

### 3.1 One rule for "who can see what"

`readable_repository_ids(db, user)` answers in three set queries what `_user_can_read_repository_contents` answers per repository. It covers owner, privileged role, commit author, any non-issue permission, and manage-level permissions. Search, the activity log, mentions and the index all use it.

Administrators see everything. Everyone else sees the repositories they can read, plus events about their own account. Client addresses are shown to administrators only.

### 3.2 The activity log is written where things happen, once per change

- **Every branch move.** `refs` is the only component that moves a branch. After every change it calls `ref_events.emit`, added in the Integrations stage, which now also calls `audit.record_ref_move`. So push, merge, cherry-pick, revert, rebase, rollback, conflict resolution and branch create, delete and rename are all recorded from any code path, including future ones.
- **One row per change.** Within one request, a route's own description ("merged PR #4") and the branch move it caused become a single row: the route's words, with branch, before, after and operation attached. `AuditContextMiddleware` (pure ASGI) gives every layer access to the request, its credential and its client address.
- **Structured fields.** Rows record target type and id, commit, a JSON payload, the credential (`session`, `token:<name>` or `ssh_key:<title>`) and the client IP. They also keep snapshots of the repository name and the actor's username.
- **History survives deletion.** Deleting a repository detaches its rows (`repository_id` NULL, name kept) instead of deleting them, and records the deletion itself.
- **Read-only.** No API edits or deletes a row. Retention is forever unless `FOXNEST_ACTIVITY_RETENTION_DAYS` is set.

### 3.3 Code search answers from an index of every default branch, as GitHub's does

- **Storage.** An SQLite FTS5 index with the trigram tokenizer holds the text of every repository's default branch, in its own file (`FOXNEST_SEARCH_INDEX_PATH`). It is derived data: delete it and the server rebuilds it at the next start. It works the same with PostgreSQL as the main database.
- **Upkeep.** A single background thread keeps it current: `ref_events` schedules a repository when its default branch moves, as do changing the default branch and creating a fork. Only changed files are read, and a blob shared by forks is stored once.
- **Queries.** A literal query of three or more characters searches every readable repository from the index. A regular expression, another branch, a shorter query or a repository not indexed yet falls back to scanning one repository's files.
- **Bounds on the scan.** The scan uses the `regex` module with a per-line timeout, plus a total time budget (`FOXNEST_SEARCH_SCAN_SECONDS`, default 3 s) and a byte budget (64 MB). The search routes are sync, so they run in the threadpool.
- **Go to file.** `/api/search/paths` matches paths without reading any file contents.

### 3.4 The web client has addresses

React Router 7 (declarative mode, 7.18.4: 6.28 and the rest of the 6.x line carry open-redirect advisories). The URL scheme:

| Address | What it shows |
|---|---|
| `/repositories/:id?branch=&path=&line=&commit=&tab=` | the repository browser at a file, line, commit or tab |
| `/repositories/:id/pulls/:pr` | a pull request |
| `/search?type=code&q=…` | a search |
| `/activity?type=&user=&repo=&since=&until=&q=&page=` | a filtered activity log |
| `/settings?tab=ssh` | a settings tab |
| `/profile/:username` | someone's profile |

Branch names contain slashes, so branch, path and line are query parameters. A link opened while signed out lands on its target after sign-in. Repository and PR pages opened from a link load their repository by id.

### 3.5 Notifications say where to go

Every notification now carries `payload.target`, of kind `pull_request`, `commit`, `issue`, `repository`, `review_queue` or `settings`. The bell, the Notifications page and `fox notifications` use it. Older rows fall back to the ids they carry.

Mentions notify only people who can read the repository, so a mention cannot hand someone the text of a repository they cannot open. A failing *required* check notifies the PR author once per commit and check, so a CI system that reports twice does not notify twice.

## 4. Phases

1. **Visibility and safety (server).** The read-set helper and scoped activity; the regex module with timeouts and time and byte budgets in the threadpool; literal and SHA commit search with true totals and paging.
2. **Audit log (server).** Columns and an idempotent migration (`ensure_activity_columns`); `audit.record` and `record_ref_move`; recording in about twenty routes that wrote nothing; server-side filters; the per-repository feed; the type list; CSV export; retention; history kept on delete.
3. **Code index (server).** `search_index` with startup catch-up, upkeep on branch moves, search across repositories, go to file, and admin status and rebuild.
4. **Notifications (server).** Queue decisions to the author, PR closed, PR @mentions (description, comments, fork PRs), failing required checks, access granted or revoked, `target` everywhere, `FOXNEST_PUBLIC_URL`.
5. **Navigation (web).** The router, the URL scheme, repository and PR pages loaded by id, keyed so moving between repositories starts fresh.
6. **Surfaces (web).**
   - Search with URL state and clickable results; code across repositories; go to file; a header search box with `/` to focus.
   - The Activity page for everyone, with server filters, paging, CSV export and rows that link to what they describe.
   - An Activity tab on every repository.
   - Open buttons on notifications, and local times everywhere.
   - A search-index panel under Operations.
7. **CLI.** `fox search repos|commits|code|files`, `fox activity`, `fox notifications [read]`.
8. **Verification.** HTTP and CLI tests run against the old code too, a live harness, a browser harness, the earlier harnesses rerun, and the full suite.

## 5. Out of scope and known limits

- **The default branch only.** Other branches use the bounded scan, one repository at a time, as on GitHub. Regular expressions also search one repository at a time.
- **Trigram limits.** The index needs at least three characters. Its candidate match folds case; "match case" then filters exactly. Case folding beyond ASCII may miss candidates in some scripts.
- **Index size and first build.** These depend on how much text the default branches hold; the Operations → Search index panel reports both. The first build on a large existing server runs in the background after startup. Until it finishes, search names the repositories not indexed yet.
- **Client addresses** are the direct peer. A reverse proxy's address is recorded unless the proxy passes the client through at the ASGI layer; `X-Forwarded-For` is not trusted.
- **System changes.** `activities.user_id` is NOT NULL, so a change with no actor (a background docs job) is attributed to the repository owner and marked `system`.
- **Static hosting of the web client.** The documented `vite preview` serves `index.html` for every path, so deep links work. Another static server needs the usual single-page fallback (for example nginx `try_files $uri /index.html`).
- **Not built.** Notification preferences, email and push notifications, and real-time updates; the bell still polls every 30 s.
- **Write routes and the event loop.** Unchanged from the Integrations stage: they are still `async def` with synchronous database work. The new search and activity routes are sync and run in the threadpool.
- **The older CLI copies** (`client/fox.py`, `release/fox.py`, the extension's) were not synced.

## 6. Implementation record

### 6.1 API surface

| Method | Path | Change |
|---|---|---|
| GET | `/api/activities` | Scoped to readable repositories, plus the caller's own account events (administrators: all). Filters run on the server: `user`, `repository_id`, `type` (comma-separated), `since`, `until`, `q`, `target_type`, `target_id`, `commit` (prefix). Paged, with a total. Rows carry `label`, `category`, `target_*`, `commit_id`, `payload` (with `ref`), `credential`; `client_ip` for administrators |
| GET | `/api/activities/types` | **New.** Every type with its label and category |
| GET | `/api/activities/export` | **New.** The filtered log as CSV (administrators), formula-injection safe, at most 50,000 rows |
| GET | `/api/repository/{id}/activity` | **New.** One repository's feed, for anyone who can read it |
| GET | `/api/search/code` | `repository_id` optional: without it, every readable repository's default branch from the index. Response adds `source` (`index`/`scan`), `timed_out` and, across repositories, `repositories_searched` and `repositories_not_indexed`. Regular expressions run with timeouts under a time budget |
| GET | `/api/search/commits` | Literal matching, id prefix (7+ hex), a true `total`, and `offset` |
| GET | `/api/search/repositories` | `offset` |
| GET | `/api/search/paths` | **New.** Go to file, across repositories (index) or within one (branch file list) |
| GET | `/api/admin/search-index` | **New.** Index size, counts, queue and last error |
| POST | `/api/admin/search-index/rebuild` | **New.** Drop and rebuild from every default branch |
| POST | `/api/permissions/create`, PUT `/update` | The grantor is the signed-in user; the grantee is notified; recorded |
| DELETE | `/api/permissions/revoke` | 404 when there is nothing to revoke; the user is notified; recorded |
| DELETE | `/api/repository/{id}` | Activity rows are kept (detached); the deletion is recorded; the index drops the repository |
| — | about twenty write routes | Now recorded: PR open (same-repo) and close, queue approve and reject, permission, webhook and push-rule changes, tokens, SSH keys, Revoke all, tags, releases, archive, delete, default branch, users |

### 6.2 Schema and settings

- `activities` gains `target_type`, `target_id`, `commit_id`, `payload_json`, `credential`, `client_ip`, `repository_name` and `actor_username`, with indexes on (created_at), (repository_id, created_at), (user_id, created_at) and (activity_type, created_at). `ensure_activity_columns()` adds them in place at startup; the live harness upgrades a pre-stage database (§6.5).
- The search index is a separate SQLite file, created and filled at startup.
- New settings (in `server/.env.example`): `FOXNEST_PUBLIC_URL`, `FOXNEST_SEARCH_INDEX`, `FOXNEST_SEARCH_INDEX_PATH`, `FOXNEST_SEARCH_SCAN_SECONDS`, `FOXNEST_ACTIVITY_RETENTION_DAYS`.
- New frontend dependency: `react-router-dom` 7.18.4.

### 6.3 CLI (`client/client1/fox.py`)

```text
fox search code parse_config --all          every repository you can read; repo:path:line: text
fox search code 'TODO\(\w+\)' --regex       one repository (the working copy's, or --repo-id)
fox search commits 'fix login'              messages, or a commit id from 7 characters
fox search files server routes              go to file
fox search repos billing
fox activity --type push_commit --since 2026-10-01 [--all | --repo-id ID] [--user U] [--grep W]
fox notifications [--unread]                with where each one points
fox notifications read 12 13 | --all
```

In a working copy, searches and `fox activity` look at that repository; `--all` looks everywhere. Searches exit 1 when nothing matches, as grep does. Every command takes `--json`.

### 6.4 Web

- **Search:** code (across repositories by default), commits, repositories and files. Each result opens what it found: the repository, the commit, or the file with the matching line highlighted and scrolled to. The search lives in the address, and a header box searches from any page (`/` focuses it).
- **Repository browser:** branch, file, line, commit and tab are in the address. Clicking a line number links to it. A file missing on the branch says so. There is a new **Activity** tab.
- **Activity:** for everyone. Filters cover user, type (grouped by category), repository, dates and words, with server paging and CSV export for administrators. Each row shows the credential, branch, before and after, and client address for administrators, and opens what it describes.
- **Notifications:** an **Open** button on the page and in the bell marks the notification read and goes to its target. Times are local.
- **Operations → Search index:** size, counts, queue and **Rebuild**.

### 6.5 Verification

| Suite | What it covers | Result |
|---|---|---|
| `tests/test_discovery_api.py` | Over HTTP: activity scoping (repositories, account events, client address, repository feed); one row per change with branch, commit and credential (session, token, sync routes); every way a branch moves; queue approval and its notification; settings and access changes, and the forged grantor; history kept on repository delete; server filters, paging, type list and export; ReDoS; literal and SHA commit search; true totals; code search across repositories and permissions; the index following the default branch; shared blobs; forks indexed at once; go to file; mentions, PR closed, failing required check once, notification targets, the public address | 27 pass. **Against the code before this stage, all 26 tests that existed then fail**: 25 with wrong results or missing data, and the ReDoS test never finishes (killed after 60 s). The fork test was added afterwards |
| `tests/test_fox_cli_discovery.py` | The real CLI against the real API: grep-style code search across repositories, a regex in one repository, commits, files and repositories, exit 1 on no match, activity for a repository and everywhere, notifications list and read | 6 pass (the commands did not exist before) |
| `tests/test_search.py`, `tests/test_endpoint_auth.py` | Existing tests. The auth test's route parser now also reads sync routes | 33 pass |
| `tools/e2e_discovery.py` | A live server with its own background indexer, as lead, developer and outsider: search across repositories and scoping; 404 not 403; go to file; the index following pushes; another branch scanned; ReDoS stopped while other requests are still answered; the index deleted while the server was down, rebuilt at startup, and rebuilt by an administrator; audit rows with token, address and branch move; queue, rename and delete recorded; filters; CSV export; notifications with targets; the real CLI; upgrade of a pre-stage database | **56/56 checks** |
| `tools/e2e_discovery_ui.py` | Real Chrome, as lead and as developer: a deep link through sign-in; a code match opening the file at its line; reload; Back; commit and file results; `/` search; activity filter in the address, row detail and Open, then Back; a repository's Activity tab by link; notification Open to the PR and to the approved commit; the search-index panel; the developer's narrower view; a link to an unreadable repository | **40/40 checks** |
| Earlier browser harnesses | Rerun after the routing change: merge conflicts, credentials, integrations | 37/37, 41/41, 61/61 |
| Frontend | `eslint` clean on every changed file (the 14 remaining warnings are in files this stage did not touch, or were already there); production build passes | pass |
| Full server suite | Everything in `server/tests`, on the final code | **869 passed, 62 subtests passed, 0 failed** (16 min 52 s) |
