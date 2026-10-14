# Repository Management UI — Implementation Plan and Record

**Document status:** Implemented on this branch
**Branch:** `Repository_Management_UI`, from `main` at `ff07f6e` (Discovery and Navigation merged as PR #8)
**Roadmap workstream:** Repository management UI — "Branch operations, tags and releases, generated documentation in the web client"
**Gap named in the roadmap:** "Roughly 44% of the API had no user interface: activity, notifications, tags, releases, branch management and generated documentation were reachable only from the CLI." Activity and notifications were the Discovery stage. This stage covers the rest.
**Week-4 checkpoint:** "Everything the API offers is reachable from the interface."
**Guiding rule:** where a choice exists, do what Git, GitHub and GitLab do.

---

## 1. Starting point

The web client had screens for these features, but they were only reachable through a dialog behind a "Manage" button on the Repositories page. They could not be linked to, and mostly listed things.

- **Branches:** a list of names and head ids, with rename, delete, set default and move. Creating, merging and publishing lived in a separate "Branch actions" panel.
- **Tags and releases:** lists and create forms. Release notes showed as plain text. Releases could not be edited or deleted, and nothing could be downloaded.
- **Generated documentation:** a file list with raw Markdown in a `<pre>`. The Generate button reported "Documentation generated" as soon as the job was queued. The Repositories page had a second documentation section whose links all answered 401.

The routes behind these screens had never been tested, and neither had the documentation job. That is where the serious problems were.

## 2. Problems found

Each problem was found by reading the code, and most were reproduced against the code as it was before this stage (§6.5). Numbers 4 and 14 surfaced in the live harness and were then traced in the code. Numbers 13 and 16, and the prefix test in 15, were confirmed by reading only.

| # | Problem | Where | Severity |
|---|---|---|---|
| 1 | Generating documentation needed only a sign-in. Anyone could make the server read any repository with the model and commit the result to its default branch. The status route did not check read access either | `routes/docs.py` | High |
| 2 | The documentation commit rebuilt the default branch from every file seen in the last 200 commits, on any branch, and left out binary, large and vendored files. Reproduced: one generation deleted `assets/logo.png` and a hand-written `docs/handwritten.md`, brought back a deleted `old.txt`, and copied `experiment.py` from another branch | `services/docs_jobs.py` | High |
| 3 | That commit bypassed every safeguard: branch protection, push rules, compare-and-swap, the reference audit, the activity log and webhooks. It moved `repository.head_commit_id` but no branch. So the default branch never received it, while everything that reads "the current commit" (new tags and releases, file listings without a branch) pointed at it. Its parent was the newest commit in the repository, on whatever branch. A developer's docs commit also skipped the review queue their pushes go through | `services/docs_jobs.py` | High |
| 4 | Generating project documentation on the server always failed. Since the code moved out of `server.py`, the template path has pointed at `server/app/docs/templates`, which does not exist, so AutoDocs v2 skipped every document as "Missing template". Separately, the document selection reached v2 as keys (`readme`) where it matches names (`README.md`), so any selection, including `fox generate-docs --docs`, wrote only the index | `services/docs_jobs.py` | High |
| 5 | A branch or tag whose name contains a slash (`feature/login`, `release/1.0`) could not be renamed, deleted, made default, moved, frozen or unlocked. The routes stopped at the first slash and answered 404 | `routes/branches.py`, `tags.py`, `protection.py` | High |
| 6 | A branch, tag or release could point at any commit on the server, including another repository's. Anyone who could read this repository could then browse, check out or download those files | `routes/branches.py`, `tags.py`, `releases.py` | High |
| 7 | The web client: branches, tags, releases and docs sat in a dialog with no address. The branch list showed nothing about each branch. Release notes were unrendered and releases could be neither corrected nor downloaded. Documentation showed raw Markdown, and its document selection was sent as a JSON body the server ignored | web client | High |
| 8 | Renaming a branch stranded the open pull requests that used it: they pointed at a branch that no longer existed and could be neither shown nor merged. A protection rule written for exactly that branch stopped applying. Deleting a branch an open pull request used broke the pull request the same way | `routes/branches.py` | Medium |
| 9 | A tag could be deleted out from under its release, leaving the release with no commit. One tag could carry two releases. A release refused after its tag was created left the tag behind | `routes/releases.py`, `tags.py` | Medium |
| 10 | A new tag or release named `repository.head_commit_id`, which a push to any branch moves, not the tip of the default branch. A release's tag could not be aimed at another branch or commit | `routes/tags.py`, `releases.py` | Medium |
| 11 | A duplicate tag, a missing tag and a default-branch change to a missing branch all answered 500. The documentation read routes caught their own 401, 403 and 404 and reported them as 500 | several | Medium |
| 12 | There was no way to download the files at a tag, release, branch or commit | — | Medium |
| 13 | The Repositories page's documentation links were plain links to the API, which carry no token, so every one answered 401 | `Repositories.jsx` | Medium |
| 14 | AutoDocs v2's Word conversion called two `docx_utils` functions that have never existed in this repository. The `AttributeError` was caught, so no Word file was ever produced | `services/docs_jobs.py` | Low |
| 15 | Generated documents were served `inline` with their own media type (`text/html` for HTML) from the API origin, without `nosniff` or a sandbox. The path check was a string-prefix test, and file listings used backslashes on Windows | `routes/docs.py` | Low |
| 16 | Moving a branch from the web could not guard against a concurrent change: the request schema had no `expected_head_commit_id`, so the route's `getattr` always read `None` | `schemas/versioning.py` | Low |
| 17 | The Ollama address and the docs folder were hard-coded. Ollama could not run on another host, and a test server could not have its own documentation | LLM clients, `config.py` | Low |

## 3. Decisions

### 3.1 A commit belongs to a repository if the repository can reach it

Commit rows are shared: `Commit.id` is the whole primary key. So a fork's branches point at the upstream's rows, and merging a fork's pull request brings the fork's rows into the upstream. "Belongs" therefore cannot mean `commit.repository_id == repo`. It means the repository owns the row **or** reaches it from one of its own branches or tags, which is what `git cat-file -e` would answer. `repo_refs.commit_in_repository` applies this rule everywhere a caller names a commit: branch start, branch move, tag, release and archive. A ref may be a branch, a tag, or a full or unambiguous short commit id, in that order (`git rev-parse` order).

### 3.2 Branch operations do what GitHub's do

- **Rename** carries the open pull requests that use the branch (as source or target, forks included) and any protection rule written for exactly that name. A pattern rule (`release/*`) is about names, not this branch, so it stays as written.
- **Delete** is refused while an open pull request uses the branch: 409 `BRANCH_IN_USE`, naming the pull requests. The response carries the branch's last commit, and the web client offers **Restore** from the notice that confirms the deletion.
- **The list** (`?details=true`) shows each branch's last commit, how far it is ahead of and behind the default branch, its protection mode, what that mode permits, whether it is stale (no commit for 90 days), and the open pull requests that use it. Ahead and behind come from one in-memory walk per request over the repository's own parent links, with commits from elsewhere fetched as the walk meets them.
- **Every route that names a branch or tag in its path** uses the `:path` converter.

### 3.3 A release is a tag with notes, and the tag stays

- The tag and the release are written in one transaction. A new release's tag points where `target` says (a branch, tag or commit), or at the default branch's tip.
- One tag carries at most one release, and a tag under a release cannot be deleted (409 `TAG_IN_USE`).
- A release's title and notes can be edited. Its version and tag cannot: they are what people cite.
- Deleting a release keeps its tag, as on GitHub. Deleting the tag afterwards is a separate decision.
- **Latest** is the highest stable semantic version, not the newest row: 1.10.0 beats 1.9.0 and 1.2.1. A version with a `-suffix` is a pre-release.
- **Drafted notes** list the merged pull requests and the other commits since the previous release. The previous release is the newest one whose commit is in the target's history.
- Every release and tag downloads as **Source code (zip)**, with one top-level folder named after the repository and the ref, as GitHub does.
- The separate signed "immutable releases" from the branch-protection stage are unchanged.

### 3.4 Documentation is generated from the default branch and committed only as a push would be

The documents are kept on the server, where the web client reads them. They are also committed under `docs/` on the default branch, which is what `fox generate-docs` pulls afterwards.

- **The source** is the default branch's tip, as it is.
- **The commit** is the source's own file rows (by hash, unread) with the generated paths replaced. It goes through the same steps as every other server-side write: push rules, the protection pre-check, compare-and-swap against the tip, then the reference service, which handles the reference audit, activity log, webhooks and search index. It is authored and signed as the person who asked.
- **Who can land it** follows the `POST /push` rules. A developer's pushes wait for review, the branch policy's scope applies, and only the owner or a team lead pushes directly. When the requester could not have pushed that change, or protection or a push rule refuses it, the documents are kept but not committed, and the reason is recorded and shown. `commit=false` asks for that outright.
- **If the branch moved** during a generation (which takes minutes), the documents land on top of the new tip. They only touch `docs/`, so a long job does not fail at its last step.
- **A manifest** (`.zanbeel-docs.json`, hidden from listings, archives and the commit) records the source commit, the branch, who generated it and when, the model, what was committed or why not. The listing compares it with the default branch to say whether the documents are up to date.
- **Generating** needs write access. Reading needs read access.
- **The template path** now comes from the generator itself, and the selection is converted to names before it reaches AutoDocs v2. Word conversion uses the converter that exists.

### 3.5 The repository page has the tabs

**Branches**, **Releases** (with a **Tags** view) and **Docs** are tabs of the repository page, in the address like the others:

| Address | What it shows |
|---|---|
| `/repositories/:id?tab=branches` | the branch list |
| `/repositories/:id?tab=releases` | releases |
| `/repositories/:id?tab=tags` | tags (a view of the Releases tab) |
| `/repositories/:id?tab=docs&doc=project/README.md` | a generated document |

- The header says how many branches, tags and releases there are, each linking to its tab, and offers the current branch as a ZIP.
- Each panel reads what the caller may do from the server (`permissions`) and what each branch accepts (`protection.permits`). It offers only what will be accepted, and a disabled control says why.
- The old "Manage" dialog keeps webhooks, status checks and push rules, and is now called **Integrations**.

### 3.6 Markdown is rendered and never trusted

Release notes are written by anyone with write access, and documentation by a model from repository content. Both render through `react-markdown` with GitHub-flavoured Markdown:

- Raw HTML is never rendered: there is deliberately no `rehype-raw`.
- Only `http(s)`, `mailto` and relative links survive.
- Relative links move between documents inside the Docs tab instead of navigating the site.

Served directly, a document carries `X-Content-Type-Options: nosniff` and a sandboxing Content-Security-Policy, and HTML is served as text.

## 4. Phases

1. **Branches (server).** `repo_refs` (graph, commit membership, ref resolution, branch overview); `:path` routes; commit checks; rename carrying pull requests and exact rules; delete refusal and restore data; 404s; compare-and-swap on move.
2. **Tags and releases (server).** `release_ops`; atomic release creation with `target`; one release per tag; tag-in-use refusal; edit and delete; latest and pre-release; drafted notes; three webhook events; two activity types.
3. **Source archives (server).** `GET /source.zip?ref=` for anything a ref can name, with checkout permissions.
4. **Documentation (server).** Permissions and status codes; the job rebuilt around the default branch and the server-write path; the manifest and freshness; safe serving; the template, selection and Word fixes; configurable Ollama address, docs folder and template folder.
5. **Web.** Markdown renderer; Branches, Releases/Tags and Docs tabs; header summary and ZIP; the Repositories page's broken docs links; the Integrations dialog; activity rows for tags, releases and docs link to their tabs.
6. **CLI.** `fox generate-docs` says when the documents were kept rather than committed, and why, instead of pulling nothing.
7. **Verification.** HTTP tests also run against the old code, a live harness on the real pipeline with a stand-in model, a browser harness, the earlier harnesses rerun, and the full suite.

## 5. Out of scope and known limits

- **Not built:**
  - Draft releases and release assets other than source code. Releases are published or absent; there is no upload store.
  - Tag protection rules. Tags cannot be moved at all; deleting one needs manage access.
  - Branch compare pages. The list shows ahead and behind; the diff is in a pull request.
- **Docs job state** is in memory, as before. A restart during generation loses the job, though not the documents from the last run, which the manifest describes.
- **The generator** still needs Ollama with the configured model. Without it the job fails and says so. The harnesses use a stand-in that answers as Ollama does.
- **The docs folder** defaults to `/tmp/foxnest_server/docs`, as before. That is not durable across reboots on most systems; set `FOXNEST_DOCS_DIR` in production.
- **A developer's documents** are kept but not committed. They are not queued for review as a push would be; a team lead regenerates to commit them.
- **Ahead/behind** is reported as unknown (`null`) when a branch's history exceeds 50,000 commits.
- **The older CLI copies** (`client/fox.py`, `release/fox.py`, the extension's) were not synced.
- **npm** reports 15 advisories, all in the existing build tooling (vite, rollup, babel, eslint dependencies). None are in the two new packages or their dependencies, and none are fixed here.

## 6. Implementation record

### 6.1 API surface

| Method | Path | Change |
|---|---|---|
| GET | `/api/repository/{id}/branches` | `details=true` adds `head_commit`, `ahead`, `behind`, `compared_to`, `stale`, `protection` (mode, matched patterns, `permits`), `pull_requests`. Always adds `default_branch` and the caller's `permissions` |
| POST | `/api/repository/{id}/branches` | `from_tag`; `from_commit` must belong to the repository (404). No source means the default branch's tip |
| PUT | `/branches/{name:path}/rename` | Slash names; 404 for a missing branch; carries open pull requests and exact-name rules; returns `retargeted_pull_requests` |
| PUT | `/branches/{name:path}/head` | Slash names; the commit must belong to the repository; `expected_head_commit_id` |
| PUT | `/branches/{name:path}/default` | Slash names; 404 for a missing branch, 400 for one with no commits |
| DELETE | `/branches/{name:path}` | Slash names; 409 `BRANCH_IN_USE` while an open pull request uses it; returns the last commit for restoring |
| POST | `/branches/{name:path}/freeze`, `/unlock-requests` | Slash names |
| GET | `/api/repository/{id}/tags` | Each tag's commit summary and release; `permissions` |
| POST | `/api/repository/{id}/tags` | `target` (branch, tag or commit); the commit must belong to the repository; 409 for a duplicate; errors say "Tag name" |
| DELETE | `/tags/{name:path}` | Slash names; 404 when missing; 409 `TAG_IN_USE` under a release; `tag.deleted` webhook |
| GET | `/api/repository/{id}/releases` | `commit`, `prerelease`, `latest`; `permissions` |
| GET | `/api/repository/{id}/releases/notes` | **New.** Notes drafted from the changes since the previous release (`target`, `previous_tag`) |
| POST | `/api/repository/{id}/releases` | `target`; atomic with its tag; 409 for a taken tag or version |
| PATCH | `/api/repository/{id}/releases/{version}` | **New.** Title and notes (write); `release.edited` webhook |
| DELETE | `/api/repository/{id}/releases/{version}` | **New.** Withdraw, keeping the tag (manage); `release.deleted` webhook |
| GET | `/api/repository/{id}/source.zip` | **New.** The files at `ref` (default branch if omitted) as a zip; `X-Zanbeel-Commit` names the commit |
| POST | `/generate-docs`, `/generate-project-docs` | Write access; `commit=false`; real status codes |
| GET | `/docs-status` | Read access; `last_generation`, `up_to_date`, `available_documents`, `permissions`, who asked |
| GET | `/docs`, `/docs/archive.zip`, `/docs/{path}` | 401, 403 and 404 as such; forward-slash paths; the manifest hidden; `nosniff` and a sandboxing CSP |

### 6.2 Schema and settings

- No database changes. The manifest is a file beside the generated documents.
- New webhook events: `tag.deleted`, `release.edited`, `release.deleted`. New activity types: `release_updated`, `release_deleted`. A docs generation and the branch move it causes are one activity row.
- New settings (in `server/.env.example`): `FOXNEST_OLLAMA_URL`, `FOXNEST_DOCS_DIR`, `FOXNEST_DOCS_TEMPLATES_DIR`.
- New frontend dependencies, pinned: `react-markdown` 10.1.0 and `remark-gfm` 4.0.1.

### 6.3 CLI (`client/client1/fox.py`)

`fox generate-docs` reads where the documents went. When they were committed it prints the commit and pulls. When they were kept, it says why ("Changes you push to 'main' go through review…") and points to the Docs tab instead of pulling nothing. Because of fix #4, `--docs readme` now generates the README rather than only the index.

### 6.4 Web

- **Branches tab:**
  - Finding and filtering: a search box and All / Active / Stale.
  - The default branch first, then the others.
  - Each branch: its last commit (linking to the commit), author and age, ahead and behind as bars, protection badges and the open pull requests that use it.
  - Actions: **New branch** from a branch, tag or commit; make default, move, rename and delete through dialogs that say what will happen. **Restore** after a delete.
  - Clicking a branch opens its code.
- **Releases tab:**
  - Each release: rendered notes, Latest and Pre-release badges, tag, commit, author and age, **Source code (zip)**, edit and delete.
  - **Draft a new release:** an existing or new tag aimed at a branch or commit, **Generate notes**, and a write/preview editor.
  - **Tags view:** each tag with its commit, message, release and a zip download; delete is disabled, with the reason, under a release.
- **Docs tab:**
  - Provenance (source commit, branch, who, when, model; committed as which commit, or why not) and an up-to-date badge.
  - **Generate / Regenerate** with a choice of documents and whether to commit, then live per-document progress while the job runs.
  - A file tree and rendered Markdown, with links between documents and the open document in the address.
  - Word files offered as downloads, and everything as a zip.
- **Repository header:** branch, tag and release counts linking to their tabs, and a ZIP of the current branch.
- **Repositories page:**
  - The documentation section opens the Docs tab and downloads with the session; its links had all answered 401.
  - Branches and Releases shortcuts.
  - "Manage" renamed **Integrations**, keeping webhooks, status checks and push rules.

### 6.5 Verification

| Suite | What it covers | Result |
|---|---|---|
| `tests/test_repository_management_api.py` | Over HTTP: slash-named branches and tags; the detailed list (distance, protection, pull requests, permissions); foreign commits refused for branch start, move and tag, while a fork still uses what it shares; rename carrying pull requests and rules; delete refused while in use, then restored; 404 and 409 instead of 500; tags at the default branch, a target or a short id; tag under a release; tag permissions; release target and atomicity; one release per tag; edit and withdraw; latest and pre-release; drafted notes; the source zip at a tag and at main, and its permissions; docs permissions and status codes; the docs commit on main changing nothing but `docs/`, one activity row, freshness; protection, a developer and `commit=false` all keeping without committing; inert serving and the hidden manifest; the generator given its templates and the documents asked for | **26 pass.** Against the code before this stage, **24 fail**, nearly all on behaviour: 404 for slash names, 200 for a foreign commit, 500 for a duplicate tag, the generator shown deleted and other-branch files, the repository head moved past protection and review, the template folder missing. The 2 that pass are deliberate controls (a fork's shared commits, tag permissions) |
| `tools/e2e_repository_management.py` | A live server with the real documentation pipeline (prompts, validation and retries, Word conversion) answered by a stand-in Ollama, as lead, developer and outsider: the branch list, slash rename with pull-request retargeting, delete and restore, foreign commits, stale moves; releases with targets, drafted notes, edits, refusals and latest; source zips byte for byte; docs permissions, a developer kept and not committed, a lead committed on main keeping every file, what the model was shown, one activity row, freshness, protection; the real CLI | **56/56 checks** |
| `tools/e2e_repository_management_ui.py` | Real Chrome, as lead and as developer: a deep link through sign-in; the list's distance, pull request and badges; create, rename, delete and restore in dialogs; a branch in use cannot be deleted, and says why; a branch opens its code, Back; hostile release notes rendered without HTML or a `javascript:` link; drafting, previewing and publishing a release; a zip download; tags and a tag's release; docs rendered, links between documents, Back, a Word file; generating from the browser, committed for the lead and kept for the developer with the reason; no console errors; the server state afterwards | **53/53 checks** |
| Earlier harnesses | Rerun after the tab, dialog and docs changes | _pending_ |
| Frontend | `eslint` clean on every changed file (three warnings remain in `Repositories.jsx`, on lines this stage did not touch); production build passes | pass |
| Full server suite | Everything in `server/tests`, on the final code, run from Git Bash | **895 passed, 62 subtests passed, 0 failed** (26 min 32 s). An earlier run from PowerShell took 3 h and failed one SSH test: `ssh-keygen` found a console there and waited for a passphrase. The old code fails that test the same way under PowerShell, and it passes in 0.6 s from Bash |
