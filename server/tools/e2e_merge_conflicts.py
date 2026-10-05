#!/usr/bin/env python3
"""End-to-end check of merge conflict resolution against a live server.

    ..\\venv\\Scripts\\python.exe tools/e2e_merge_conflicts.py [--keep] [--port N]

Starts a real uvicorn server on a throwaway database and blob store, signs users in with
real passwords, and drives every merge-conflict path over HTTP -- the same requests the
web client and the fox CLI make -- then drives the real fox CLI against the same server.
Nothing touches the development database.

Each check prints PASS or FAIL; the run ends with a count and exits non-zero on any
failure, so it can gate a pull request.

Scenarios
  1  Sign-in and setup
  2  Line-level merge: edits to different parts of one file merge cleanly
  3  Pull request conflicts, GitHub's model: resolve on the PR branch, re-review, merge
  4  A session goes stale when a branch moves
  5  A protected source branch refuses a browser resolution
  6  Permissions: a read-only user can look but not resolve
  7  Direct branch merge: resolve onto the target, review rules still apply
  8  Pull requests from a fork: the fork's branch is what merges; conflicts resolve in the fork
  9  The fox CLI against the live server: pr resolve, merge --resolve, local merge
 10  Upgrading an existing database: the migration adds the new columns
"""

import argparse
import base64
import contextlib
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
CLI = REPO_ROOT / "client" / "client1" / "fox.py"
PASSWORD = "E2e-Passw0rd!long"

URDU_BASE = "سلام\nخوش آمدید\n"
URDU_MAIN = "سلام دنیا\nخوش آمدید\n"
URDU_FEATURE = "سلام دوستو\nخوش آمدید\n"
URDU_RESOLVED = "سلام دنیا اور دوستو\nخوش آمدید ✓\n"
PNG_BASE = b"\x89PNG\r\n\x1a\n\x00base-bytes\xff"
PNG_MAIN = b"\x89PNG\r\n\x1a\n\x00main-bytes\xfe"
PNG_FEATURE = b"\x89PNG\r\n\x1a\n\x00feature-bytes\xfd"


def app_lines(**overrides):
    """A ten-line file; overrides replace numbered lines."""
    lines = [f"line {n}" for n in range(1, 11)]
    for key, value in overrides.items():
        lines[int(key[1:]) - 1] = value
    return "\n".join(lines) + "\n"


def b64(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


# --- reporting ---------------------------------------------------------------------

class Report:
    def __init__(self):
        self.passed = 0
        self.failed = []
        self.scenario = ""

    def section(self, title):
        self.scenario = title
        print(f"\n{title}")

    def check(self, name, ok, detail=""):
        if ok:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append(f"{self.scenario}: {name}")
            print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))
        return ok

    def summary(self):
        total = self.passed + len(self.failed)
        print(f"\n{self.passed}/{total} checks passed")
        for name in self.failed:
            print(f"  failed: {name}")
        return not self.failed


# --- the live server ---------------------------------------------------------------

def remove_tree(path):
    """Delete a work directory; Windows can hold the database file a moment after exit."""
    for _ in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not Path(path).exists():
            return
        time.sleep(0.5)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def server_env(workdir):
    return dict(
        os.environ,
        DATABASE_URL="sqlite:///" + (workdir / "e2e.db").as_posix(),
        FOXNEST_AUTH_SECRET=hashlib.sha256(b"e2e-auth").hexdigest() * 2,
        FOXNEST_PASSWORD_SETUP_KEY=hashlib.sha256(b"e2e-setup").hexdigest() * 2,
        FOXNEST_BLOB_DIR=str(workdir / "blobs"),
        PYTHONIOENCODING="utf-8",
    )


@contextlib.contextmanager
def live_server(workdir, port, extra_env=None):
    log = open(workdir / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=SERVER_ROOT, env={**server_env(workdir), **(extra_env or {})},
        stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early; see {workdir / 'server.log'}")
            try:
                if requests.get(f"{base}/docs", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("server did not start within 90s")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def seed(workdir):
    """Users and one repository, written the way an administrator's setup would.

    There is no public endpoint to create a user without an approval flow, so this one
    step goes to the database; everything after it goes through HTTP.
    """
    script = f"""
import sys
sys.path.insert(0, {str(SERVER_ROOT)!r})
from database.database import SessionLocal
from database.crud import UserCRUD, RepositoryCRUD, UserPermissionCRUD
from app.core.security import hash_password
db = SessionLocal()
for name, role in (("lead", "team_lead"), ("alice", "team_lead"),
                   ("contrib", "team_lead"), ("reader", "developer")):
    UserCRUD.create_user(db, username=name, role=role, password_hash=hash_password({PASSWORD!r}),
                         email=name + "@e2e.test")
repo = RepositoryCRUD.create_repository(db, "lead", "e2e-app", "merge conflict e2e")
UserPermissionCRUD.create_permission(db, "reader", repo.id, "read", "lead")
print(repo.id)
"""
    out = subprocess.run([sys.executable, "-c", script], cwd=SERVER_ROOT, env=server_env(workdir),
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return out.stdout.strip().splitlines()[-1]


# --- HTTP ----------------------------------------------------------------------------

class Api:
    def __init__(self, base, token=None):
        self.base = base
        self.token = token

    def _call(self, method, path, **kwargs):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = requests.request(method, self.base + path, headers=headers, timeout=120, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = response.content
        return response.status_code, body

    def get(self, path, **kw):
        return self._call("GET", path, **kw)

    def post(self, path, body=None):
        return self._call("POST", path, json=body if body is not None else {})

    def put(self, path, body=None):
        return self._call("PUT", path, json=body if body is not None else {})

    def raw(self, route, **params):
        headers = {"Authorization": f"Bearer {self.token}"}
        return requests.get(self.base + route, headers=headers, params=params, timeout=60)


def login(base, username):
    status, body = Api(base).post("/api/auth/login", {"username": username, "password": PASSWORD})
    return status, (body.get("access_token") if isinstance(body, dict) else None)


class Repo:
    """One repository, driven by one signed-in user."""

    def __init__(self, api, repo_id, author):
        self.api, self.id, self.author = api, repo_id, author

    def url(self, suffix=""):
        return f"/api/repository/{self.id}{suffix}"

    def head(self, branch):
        _status, body = self.api.get(self.url("/branches"))
        for row in body.get("branches", []):
            if row["name"] == branch:
                return row["head_commit_id"]
        return None

    def push(self, branch, files, message, parents=None):
        """Push a full snapshot to a branch, as `fox push` does."""
        parents = parents if parents is not None else [p for p in [self.head(branch)] if p]
        commit_id = hashlib.sha1(f"{self.id}:{branch}:{message}:{time.time_ns()}".encode()).hexdigest()
        status, body = self.api.post(self.url("/push"), {
            "branch": branch,
            "commit": {
                "id": commit_id, "author": self.author, "message": message,
                "parent": parents[0] if parents else None, "parents": parents,
                "files": {path: b64(data) for path, data in files.items()},
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            },
        })
        if status != 200:
            raise RuntimeError(f"push to {branch} failed: {status} {body}")
        return commit_id

    def branch(self, name, from_branch="main"):
        status, body = self.api.post(self.url("/branches"), {"name": name, "from_branch": from_branch})
        if status != 200:
            raise RuntimeError(f"create branch {name}: {status} {body}")

    def file(self, branch, path):
        """Exact bytes of a file on a branch, or None when it does not exist."""
        response = self.api.raw(self.url("/file/download"), path=path, branch=branch)
        return response.content if response.status_code == 200 else None

    def open_pr(self, source, target="main", title=None):
        status, body = self.api.post(self.url("/pull-requests"), {
            "title": title or f"{source} into {target}", "description": "e2e",
            "source_branch": source, "target_branch": target,
        })
        if status != 200:
            raise RuntimeError(f"open PR {source}: {status} {body}")
        return body["pull_request"]["id"]


def commit_parents(workdir, commit_id):
    # closing(): a sqlite3 connection used as a context manager commits but stays open,
    # which keeps the database file locked on Windows and the work directory undeletable.
    with contextlib.closing(sqlite3.connect(workdir / "e2e.db")) as db:
        rows = db.execute(
            "SELECT parent_commit_id FROM commit_parents WHERE commit_id = ? ORDER BY parent_order",
            (commit_id,),
        ).fetchall()
    return [row[0] for row in rows]


def code_of(body):
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    return body.get("code") or (detail.get("code") if isinstance(detail, dict) else None)


# --- scenarios -------------------------------------------------------------------------

def scenario_line_merge(r, repo):
    r.section("2  Line-level merge: edits to different parts of one file")
    repo.branch("topic-clean")
    repo.push("topic-clean", {"app.py": app_lines(l10="line 10 from topic"), "README.md": "readme\n"},
              "topic edits the end")
    repo.push("main", {"app.py": app_lines(l1="line 1 from main"), "README.md": "readme\n"},
              "main edits the start")
    pr = repo.open_pr("topic-clean")
    _s, state = repo.api.get(repo.url(f"/pull-requests/{pr}/mergeability"))
    r.check("mergeability reports no conflict", state.get("state") == "clean", state)
    status, body = repo.api.post(repo.url(f"/pull-requests/{pr}/merge"),
                                 {"expected_head_commit_id": state.get("target_head_commit_id")})
    r.check("the pull request merges", status == 200 and body.get("status") == "merged", body)
    r.check("both edits are in the merged file",
            repo.file("main", "app.py") == app_lines(l1="line 1 from main", l10="line 10 from topic").encode())


def scenario_pull_request(r, repo, alice_repo, workdir):
    r.section("3  Pull request conflicts, GitHub's model")
    base_files = {
        "app.py": app_lines(), "notes.txt": "notes\n", "logo.png": PNG_BASE, "README.md": "readme\n",
    }
    repo.push("main", base_files, "base for conflicts")
    repo.branch("feature")
    repo.push("main", {"app.py": app_lines(l5="line 5 main"), "logo.png": PNG_MAIN,
                       "README.md": "readme\n", "i18n/ur.txt": URDU_MAIN}, "main side")
    feature_head = repo.push("feature", {
        "app.py": app_lines(l5="line 5 feature", l9="line 9 feature"), "notes.txt": "notes edited\n",
        "logo.png": PNG_FEATURE, "README.md": "readme\n", "i18n/ur.txt": URDU_FEATURE,
    }, "feature side")

    status, _ = repo.api.put(repo.url("/branch-protection"),
                             {"branch_pattern": "main", "mode": "open", "rules": {"required_approvals": 1}})
    _s, policy = repo.api.get(repo.url("/branch-protection"))
    r.check("main requires one approval", status == 200
            and policy.get("branches", {}).get("main", {}).get("requires_review") is True, policy)

    pr = repo.open_pr("feature", title="Feature with conflicts")
    status, body = repo.api.post(repo.url(f"/pull-requests/{pr}/merge"), {})
    r.check("before any review, Merge reports the conflict first (as GitHub does)",
            status == 409 and body.get("code") == "MERGE_CONFLICT", body)
    r.check("...and still lists the missing approval",
            any("approval" in b for b in body.get("review_blockers", [])), body)
    status, _ = alice_repo.api.post(repo.url(f"/pull-requests/{pr}/reviews"), {"state": "approved"})
    r.check("a reviewer approves", status == 200)

    _s, state = repo.api.get(repo.url(f"/pull-requests/{pr}/mergeability"))
    r.check("mergeability lists the four conflicting files",
            state.get("state") == "conflicts"
            and state.get("conflicts") == ["app.py", "i18n/ur.txt", "logo.png", "notes.txt"], state)
    r.check("the author is offered the resolver", state.get("can_resolve") is True)

    main_before = repo.head("main")
    status, body = repo.api.post(repo.url(f"/pull-requests/{pr}/merge"),
                                 {"expected_head_commit_id": main_before})
    r.check("Merge refuses with MERGE_CONFLICT and a resolve_url",
            status == 409 and body.get("code") == "MERGE_CONFLICT" and body.get("resolve_url"), body)

    status, bundle = repo.api.post(repo.url(f"/pull-requests/{pr}/conflicts"))
    session = bundle.get("session", {}) if isinstance(bundle, dict) else {}
    files = {f["path"]: f for f in bundle.get("files", [])} if isinstance(bundle, dict) else {}
    r.check("a session opens and will commit to the PR's branch",
            status == 200 and session.get("lands_on") == "source"
            and session.get("commit_branch") == "feature", session)
    r.check("each file is classified",
            {p: f["kind"] for p, f in files.items()} == {
                "app.py": "content", "i18n/ur.txt": "add_add", "logo.png": "binary",
                "notes.txt": "modify_delete"}, {p: f.get("kind") for p, f in files.items()})
    suggested = base64.b64decode(files["app.py"]["suggested_b64"]).decode()
    r.check("markers name the branches and show the base",
            "<<<<<<< main\nline 5 main\n||||||| base\nline 5\n=======\nline 5 feature\n>>>>>>> feature\n"
            in suggested, suggested)
    r.check("only the overlapping line conflicts; line 9 merged cleanly",
            files["app.py"]["conflict_count"] == 1 and "line 9 feature\n" in suggested
            and suggested.count("<<<<<<<") == 1)
    r.check("Urdu text arrives byte-exact",
            base64.b64decode(files["i18n/ur.txt"]["ours_b64"]).decode() == URDU_MAIN)
    r.check("notes.txt reports which side deleted it", files["notes.txt"]["deleted_in"] == "target")

    sid = session["id"]
    resolved_app = app_lines(l5="line 5 main and feature", l9="line 9 feature")
    status, saved = repo.api.put(repo.url(f"/pull-requests/{pr}/conflicts/{sid}/files"),
                                 {"path": "app.py", "content_b64": b64(resolved_app)})
    r.check("a draft is saved for one file", status == 200 and saved.get("unresolved") == 3, saved)
    _s, again = repo.api.post(repo.url(f"/pull-requests/{pr}/conflicts"))
    again_app = next(f for f in again["files"] if f["path"] == "app.py")
    r.check("reopening resumes the same session with the draft",
            again["session"]["id"] == sid and again_app["resolved"]
            and base64.b64decode(again_app["resolved_b64"]).decode() == resolved_app)

    repo.api.put(repo.url(f"/pull-requests/{pr}/conflicts/{sid}/files"),
                 {"path": "notes.txt", "delete": True})
    repo.api.put(repo.url(f"/pull-requests/{pr}/conflicts/{sid}/files"),
                 {"path": "logo.png", "content_b64": files["logo.png"]["theirs_b64"]})

    status, body = repo.api.post(repo.url(f"/pull-requests/{pr}/conflicts/{sid}/resolve"),
                                 {"resolutions": {"i18n/ur.txt": files["i18n/ur.txt"]["suggested_b64"]}})
    r.check("leaving markers in is refused (UNRESOLVED_MARKERS)",
            status == 409 and body.get("code") == "UNRESOLVED_MARKERS"
            and body.get("paths") == ["i18n/ur.txt"], body)
    r.check("...and nothing was written", repo.head("feature") == feature_head)

    repo.api.put(repo.url(f"/pull-requests/{pr}/conflicts/{sid}/files"),
                 {"path": "i18n/ur.txt", "content_b64": b64(URDU_RESOLVED)})
    status, body = repo.api.post(repo.url(f"/pull-requests/{pr}/conflicts/{sid}/resolve"),
                                 {"expected_head_commit_id": main_before})
    r.check("the head guard checks the branch written (feature), not main",
            status == 409 and body.get("code") == "HEAD_MISMATCH", body)

    status, result = repo.api.post(repo.url(f"/pull-requests/{pr}/conflicts/{sid}/resolve"),
                                   {"expected_head_commit_id": feature_head})
    r.check("Commit merge succeeds", status == 200 and result.get("status") == "resolved", result)
    r.check("the resolution is on feature; main is untouched",
            repo.head("feature") == result.get("merge_commit_id") and repo.head("main") == main_before)
    r.check("the merge commit's first parent is feature",
            commit_parents(workdir, result.get("merge_commit_id")) == [feature_head, main_before])
    r.check("feature holds exactly the decisions",
            repo.file("feature", "app.py") == resolved_app.encode()
            and repo.file("feature", "logo.png") == PNG_FEATURE
            and repo.file("feature", "i18n/ur.txt") == URDU_RESOLVED.encode()
            and repo.file("feature", "notes.txt") is None)

    _s, reviews = repo.api.get(repo.url(f"/pull-requests/{pr}/reviews"))
    r.check("the earlier approval is now stale",
            len(reviews.get("stale_approvals", [])) == 1 and reviews.get("can_merge") is False, reviews)
    _s, state = repo.api.get(repo.url(f"/pull-requests/{pr}/mergeability"))
    r.check("the PR now merges cleanly", state.get("state") == "clean", state)
    status, body = repo.api.post(repo.url(f"/pull-requests/{pr}/merge"),
                                 {"expected_head_commit_id": main_before})
    r.check("Merge waits for re-approval (REVIEW_REQUIRED)",
            status == 409 and code_of(body) == "REVIEW_REQUIRED", body)

    alice_repo.api.post(repo.url(f"/pull-requests/{pr}/reviews"), {"state": "approved"})
    status, body = repo.api.post(repo.url(f"/pull-requests/{pr}/merge"),
                                 {"expected_head_commit_id": main_before})
    r.check("after re-approval the PR merges", status == 200 and body.get("status") == "merged", body)
    r.check("main now holds the resolved content",
            repo.file("main", "app.py") == resolved_app.encode()
            and repo.file("main", "i18n/ur.txt") == URDU_RESOLVED.encode()
            and repo.file("main", "notes.txt") is None)
    _s, pr_body = repo.api.get(repo.url(f"/pull-requests/{pr}"))
    r.check("the PR is marked merged", pr_body.get("pull_request", {}).get("status") == "merged")


def conflicted_branch(repo, name, line, message):
    """A branch off main and a main commit that both change one app.py line."""
    current = repo.file("main", "app.py").decode()
    repo.branch(name)
    lines = current.splitlines(keepends=True)
    theirs = lines.copy()
    theirs[line - 1] = f"{name} line {line}\n"
    head = repo.push(name, {"app.py": "".join(theirs), "README.md": "readme\n"}, f"{name}: {message}")
    ours = lines.copy()
    ours[line - 1] = f"main line {line} before {name}\n"
    snapshot = {"app.py": "".join(ours), "README.md": "readme\n"}
    for extra in ("i18n/ur.txt", "logo.png"):
        content = repo.file("main", extra)
        if content is not None:
            snapshot[extra] = content
    repo.push("main", snapshot, f"main: conflicts with {name}")
    return head


def scenario_stale(r, repo):
    r.section("4  A session goes stale when a branch moves")
    conflicted_branch(repo, "stale-topic", 2, "edit")
    pr = repo.open_pr("stale-topic")
    _s, bundle = repo.api.post(repo.url(f"/pull-requests/{pr}/conflicts"))
    sid = bundle["session"]["id"]
    repo.push("stale-topic", {"app.py": "moved on\n", "README.md": "readme\n"}, "moves on")
    status, body = repo.api.put(repo.url(f"/pull-requests/{pr}/conflicts/{sid}/files"),
                                {"path": "app.py", "content_b64": b64("x\n")})
    r.check("saving into it is refused (SESSION_STALE)",
            status == 409 and body.get("code") == "SESSION_STALE", body)
    _s, bundle = repo.api.get(repo.url(f"/pull-requests/{pr}/conflicts/{sid}"))
    r.check("the session reports itself stale", bundle["session"]["status"] == "stale")
    _s, fresh = repo.api.post(repo.url(f"/pull-requests/{pr}/conflicts"))
    r.check("reopening computes a fresh session",
            isinstance(fresh, dict) and fresh.get("session", {}).get("id") not in (None, sid), fresh)
    return pr


def scenario_protected_source(r, repo):
    r.section("5  A protected source branch refuses a browser resolution")
    head = conflicted_branch(repo, "locked-topic", 3, "edit")
    repo.api.put(repo.url("/branch-protection"),
                 {"branch_pattern": "locked-topic", "mode": "open", "rules": {"required_approvals": 1}})
    pr = repo.open_pr("locked-topic")
    _s, bundle = repo.api.post(repo.url(f"/pull-requests/{pr}/conflicts"))
    sid = bundle["session"]["id"]
    status, body = repo.api.post(repo.url(f"/pull-requests/{pr}/conflicts/{sid}/resolve"),
                                 {"resolutions": {"app.py": b64("resolved\n")}})
    r.check("resolving is refused with REVIEW_REQUIRED", status == 409 and body.get("code") == "REVIEW_REQUIRED", body)
    r.check("the branch did not move", repo.head("locked-topic") == head)


def scenario_permissions(r, repo, reader_api, pr):
    r.section("6  Permissions: a read-only user can look but not resolve")
    _s, state = reader_api.get(repo.url(f"/pull-requests/{pr}/mergeability"))
    r.check("the reader sees the conflicts", state.get("state") == "conflicts", state)
    r.check("...but is not offered the resolver", state.get("can_resolve") is False)
    status, body = reader_api.post(repo.url(f"/pull-requests/{pr}/conflicts"))
    r.check("opening a session is refused (403)", status == 403 and "cannot push" in str(body), body)
    _s, sessions = repo.api.get(repo.url(f"/pull-requests/{pr}/conflicts"))
    sid = sessions["sessions"][0]["id"]
    status, _ = reader_api.get(repo.url(f"/pull-requests/{pr}/conflicts/{sid}"))
    r.check("the reader can still read an open session", status == 200)
    status, _ = reader_api.put(repo.url(f"/pull-requests/{pr}/conflicts/{sid}/files"),
                               {"path": "app.py", "content_b64": b64("x\n")})
    r.check("...but cannot save into it", status == 403)


def scenario_branch_merge(r, repo, workdir):
    r.section("7  Direct branch merge (no pull request)")
    repo.branch("dev-a")
    repo.branch("dev-b")
    a_head = repo.push("dev-a", {"app.py": app_lines(l4="dev-a line 4"), "README.md": "readme\n",
                                 "shared.cfg": "a=1\n"}, "dev-a work")
    b_head = repo.push("dev-b", {"app.py": app_lines(l4="dev-b line 4"), "README.md": "readme\n",
                                 "shared.cfg": "b=1\n"}, "dev-b work")
    status, body = repo.api.post(repo.url("/branches/merge"), {"source_branch": "dev-b", "target_branch": "dev-a"})
    r.check("the merge refuses with MERGE_CONFLICT and a resolve_url",
            status == 409 and code_of(body) == "MERGE_CONFLICT"
            and "resolve_url" in json.dumps(body), body)

    status, bundle = repo.api.post(repo.url("/branches/conflicts"),
                                   {"source_branch": "dev-b", "target_branch": "dev-a"})
    sid = bundle["session"]["id"]
    r.check("a branch session opens and will commit to the target",
            status == 200 and bundle["session"]["commit_branch"] == "dev-a"
            and bundle["session"]["pull_request_id"] is None, bundle.get("session"))
    _s, listed = repo.api.get(repo.url("/branches/conflicts"))
    r.check("it is listed for resuming", any(s["id"] == sid for s in listed.get("sessions", [])))

    status, body = repo.api.post(repo.url(f"/branches/conflicts/{sid}/resolve"), {})
    r.check("an incomplete resolution is refused, in the shared error shape",
            status == 400 and body.get("code") == "INCOMPLETE" and isinstance(body.get("detail"), str)
            and body.get("success") is False, body)

    repo.api.put(repo.url(f"/branches/conflicts/{sid}/files"),
                 {"path": "shared.cfg", "content_b64": b64("a=1\nb=1\n")})
    status, result = repo.api.post(repo.url(f"/branches/conflicts/{sid}/resolve"),
                                   {"resolutions": {"app.py": b64(app_lines(l4="dev-a and dev-b line 4"))}})
    r.check("resolving completes the merge onto dev-a",
            status == 200 and result.get("status") == "merged" and repo.head("dev-a") == result.get("merge_commit_id"),
            result)
    r.check("parents are dev-a then dev-b", commit_parents(workdir, result.get("merge_commit_id")) == [a_head, b_head])
    r.check("dev-a holds both decisions",
            repo.file("dev-a", "shared.cfg") == b"a=1\nb=1\n"
            and repo.file("dev-a", "app.py") == app_lines(l4="dev-a and dev-b line 4").encode())

    # Into main, which requires review, a branch merge is refused even after resolving.
    conflicted_branch(repo, "hotfix", 6, "edit")
    _s, bundle = repo.api.post(repo.url("/branches/conflicts"), {"source_branch": "hotfix", "target_branch": "main"})
    sid = bundle["session"]["id"]
    status, body = repo.api.post(repo.url(f"/branches/conflicts/{sid}/resolve"),
                                 {"resolutions": {"app.py": b64("hotfix resolved\n")}})
    r.check("resolving a branch merge into review-required main is refused",
            status == 409 and body.get("code") == "REVIEW_REQUIRED", body)


def scenario_fork(r, base, repo, lead_api, alice_api, contrib_api, reader_api):
    r.section("8  Pull requests from a fork")
    status, body = contrib_api.post(repo.url("/fork"), {"name": "e2e-app-fork"})
    fork_id = body.get("repo_id") if isinstance(body, dict) else None
    r.check("the contributor forks the repository", status == 200 and bool(fork_id), body)
    fork = Repo(contrib_api, fork_id, "contrib")

    upstream_main = repo.head("main")
    snapshot = {path: repo.file("main", path) for path in ("app.py", "README.md", "i18n/ur.txt", "logo.png")}
    snapshot = {p: c for p, c in snapshot.items() if c is not None}
    fork_head = fork.push("main", {**snapshot, "CONTRIBUTING.md": "from the fork\n"}, "fork contribution")

    status, body = contrib_api.post(repo.url("/pull-requests/from-fork"), {
        "source_repo_id": fork_id, "source_branch": "main", "target_branch": "main",
        "title": "Contribution from a fork"})
    pr = body.get("pull_request", {}).get("id") if isinstance(body, dict) else None
    r.check("a cross-repository PR opens", status == 200 and pr, body)

    _s, state = lead_api.get(repo.url(f"/pull-requests/{pr}/mergeability"))
    r.check("mergeability reads the fork's branch, not upstream main",
            state.get("source_head_commit_id") == fork_head and state.get("state") == "clean", state)
    alice_api.post(repo.url(f"/pull-requests/{pr}/reviews"), {"state": "approved"})
    _s, reviews = lead_api.get(repo.url(f"/pull-requests/{pr}/reviews"))
    r.check("the approval is pinned to the fork's head",
            [a.get("commit_id") for a in reviews.get("approvals", [])] == [fork_head], reviews)
    status, body = lead_api.post(repo.url(f"/pull-requests/{pr}/merge"), {"expected_head_commit_id": upstream_main})
    r.check("the fork PR merges", status == 200 and body.get("status") == "merged", body)
    r.check("the fork's work landed upstream (it used to be dropped)",
            repo.file("main", "CONTRIBUTING.md") == b"from the fork\n")

    # A conflicting fork PR is resolved in the fork, by someone who can push there.
    current = repo.file("main", "app.py").decode().splitlines(keepends=True)
    fork_side = current.copy()
    fork_side[7] = "fork line 8\n"
    fork_snapshot = {**{p: repo.file("main", p) for p in ("README.md", "CONTRIBUTING.md")},
                     "app.py": "".join(fork_side)}
    fork_snapshot = {p: c for p, c in fork_snapshot.items() if c is not None}
    fork_head = fork.push("main", fork_snapshot, "fork edits line 8",
                          parents=[repo.head("main")])
    main_side = current.copy()
    main_side[7] = "upstream line 8\n"
    repo.push("main", {**{p: repo.file("main", p) for p in ("README.md", "CONTRIBUTING.md")},
                       "app.py": "".join(main_side)}, "upstream edits line 8")
    _s, body = contrib_api.post(repo.url("/pull-requests/from-fork"), {
        "source_repo_id": fork_id, "source_branch": "main", "target_branch": "main",
        "title": "Conflicting fork change"})
    pr2 = body["pull_request"]["id"]

    status, body = reader_api.post(repo.url(f"/pull-requests/{pr2}/conflicts"))
    r.check("someone without access to the fork is told the author must resolve it",
            status == 403 and "fork" in str(body), body)
    status, bundle = contrib_api.post(repo.url(f"/pull-requests/{pr2}/conflicts"))
    r.check("the fork's owner opens the session; it commits to the fork",
            status == 200 and bundle["session"]["commit_repository_id"] == fork_id, bundle)
    sid = bundle["session"]["id"]
    resolved = current.copy()
    resolved[7] = "upstream and fork line 8\n"
    upstream_before = repo.head("main")
    status, result = contrib_api.post(repo.url(f"/pull-requests/{pr2}/conflicts/{sid}/resolve"),
                                      {"resolutions": {"app.py": b64("".join(resolved))}})
    r.check("the resolution is committed to the fork's branch",
            status == 200 and result.get("commit_repository_id") == fork_id
            and fork.head("main") == result.get("merge_commit_id"), result)
    r.check("upstream main is untouched", repo.head("main") == upstream_before)
    _s, state = lead_api.get(repo.url(f"/pull-requests/{pr2}/mergeability"))
    r.check("the fork PR now merges cleanly", state.get("state") == "clean", state)


def run_cli(workdir, home, args, check_ok=True):
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, str(CLI), *args], cwd=workdir, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=300)
    return result.returncode, result.stdout + result.stderr


def scenario_cli(r, base, repo, root):
    r.section("9  The fox CLI against the live server")
    home = root / "cli-home"
    work = root / "cli-work"
    home.mkdir()
    work.mkdir()

    run_cli(work, home, ["init", "--username", "lead", "--repo-name", "e2e-app"])
    run_cli(work, home, ["set", "origin", base])
    config_path = work / ".fox" / "config.json"
    config = json.loads(config_path.read_text())
    config["repo_id"] = repo.id
    config_path.write_text(json.dumps(config, indent=2))
    code, out = run_cli(work, home, ["login", "--username", "lead", "--password", PASSWORD])
    r.check("fox login signs in against the live server", code == 0 and
            (home / ".foxnest" / "credentials.json").exists(), out)

    head = conflicted_branch(repo, "cli-topic", 7, "edit")
    pr = repo.open_pr("cli-topic")
    code, out = run_cli(work, home, ["pr", "merge", str(pr)])
    r.check("fox pr merge reports the conflict first, with the command to resolve it",
            f"fox pr resolve {pr}" in out and "conflicts" in out, out)
    r.check("...and still mentions the missing approval", "approval" in out, out)
    code, out = run_cli(work, home, ["pr", "resolve", str(pr)])
    resolved_files = list((work / ".fox" / "merge-conflicts").rglob("app.py.resolved"))
    r.check("fox pr resolve writes the files to edit", code == 0 and len(resolved_files) == 1, out)
    resolved_file = resolved_files[0]
    r.check(".resolved starts as the labelled merge", b"<<<<<<< main" in resolved_file.read_bytes())

    code, out = run_cli(work, home, ["pr", "resolve", str(pr), "--submit"])
    r.check("--submit with markers left in is refused before sending", code != 0 and "markers" in out, out)
    r.check("...and cli-topic did not move", repo.head("cli-topic") == head)

    final = repo.file("main", "app.py").decode().replace("main line 7 before cli-topic", "resolved in the cli")
    resolved_file.write_bytes(final.encode())
    code, out = run_cli(work, home, ["pr", "resolve", str(pr)])
    r.check("re-running never overwrites the edited file", resolved_file.read_bytes() == final.encode())
    code, out = run_cli(work, home, ["pr", "resolve", str(pr), "--submit"])
    r.check("fox pr resolve --submit commits to the PR's branch",
            code == 0 and "Committed the resolution to 'cli-topic'" in out, out)
    r.check("the server branch holds exactly the edited file", repo.file("cli-topic", "app.py") == final.encode())

    repo.branch("cli-a")
    repo.branch("cli-b")
    repo.push("cli-a", {"app.py": app_lines(l2="cli-a"), "README.md": "readme\n"}, "cli-a")
    repo.push("cli-b", {"app.py": app_lines(l2="cli-b"), "README.md": "readme\n"}, "cli-b")
    code, out = run_cli(work, home, ["merge", "cli-b", "--resolve", "--into", "cli-a"])
    branch_files = list((work / ".fox" / "merge-conflicts").glob("branch_session_*/app.py.resolved"))
    r.check("fox merge --resolve opens a server branch session", code == 0 and branch_files, out)
    if branch_files:
        branch_files[0].write_bytes(app_lines(l2="cli-a and cli-b").encode())
    code, out = run_cli(work, home, ["merge", "cli-b", "--resolve", "--into", "cli-a", "--submit"])
    r.check("--submit merges cli-b into cli-a on the server",
            code == 0 and repo.file("cli-a", "app.py") == app_lines(l2="cli-a and cli-b").encode(), out)

    # A purely local merge in a fresh repository.
    local = root / "cli-local"
    local.mkdir()
    run_cli(local, home, ["init", "--username", "lead", "--repo-name", "local"])
    (local / "m.py").write_bytes(b"a = 1\nb = 2\nc = 3\n")
    run_cli(local, home, ["add", "m.py"])
    run_cli(local, home, ["commit", "-m", "base"])
    run_cli(local, home, ["branch", "create", "topic"])
    (local / "m.py").write_bytes(b"a = 'main'\nb = 2\nc = 3\n")
    run_cli(local, home, ["add", "m.py"])
    run_cli(local, home, ["commit", "-m", "main"])
    run_cli(local, home, ["checkout", "topic"])
    (local / "m.py").write_bytes(b"a = 'topic'\nb = 2\nc = 'topic'\n")
    run_cli(local, home, ["add", "m.py"])
    run_cli(local, home, ["commit", "-m", "topic"])
    run_cli(local, home, ["checkout", "main"])
    code, out = run_cli(local, home, ["merge", "topic"])
    text = (local / "m.py").read_text(encoding="utf-8")
    r.check("a local conflicted merge stops with labelled markers",
            code != 0 and "<<<<<<< main" in text and "c = 'topic'" in text, out + text)
    code, out = run_cli(local, home, ["status"])
    r.check("fox status lists the unmerged path", "both modified:   m.py" in out, out)
    (local / "m.py").write_bytes(b"a = 'both'\nb = 2\nc = 'topic'\n")
    code, out = run_cli(local, home, ["merge", "--continue"])
    commits = json.loads((local / ".fox" / "commits.json").read_text())
    r.check("fox merge --continue records a two-parent merge commit",
            code == 0 and len(commits[-1].get("parents", [])) == 2, out)


def scenario_migration(r, root):
    r.section("10  Upgrading an existing database")
    db_path = root / "legacy.db"
    script = f"""
import sys, sqlite3
sys.path.insert(0, {str(SERVER_ROOT)!r})
from app.db import migrations as m
m.ensure_merge_conflict_tables()          # the original table shapes
con = sqlite3.connect({str(db_path)!r})
con.execute("INSERT INTO merge_conflict_sessions (id, repository_id, pull_request_id, source_branch, target_branch, base_commit_id, source_head_commit_id, target_head_commit_id, status, created_by_id) VALUES (1, 'r', 1, 's', 't', 'b', 'sh', 'th', 'resolved', 1)")
con.execute("INSERT INTO merge_conflict_files (session_id, file_path, resolved_file_hash, is_binary) VALUES (1, 'a.txt', 'abc', 0)")
con.commit(); con.close()
m.ensure_merge_conflict_branch_sessions()
m.ensure_merge_conflict_resolution_columns()
m.ensure_merge_conflict_resolution_columns()   # idempotent
con = sqlite3.connect({str(db_path)!r})
s = [r[1] for r in con.execute("PRAGMA table_info(merge_conflict_sessions)")]
f = [r[1] for r in con.execute("PRAGMA table_info(merge_conflict_files)")]
nullable = [r for r in con.execute("PRAGMA table_info(merge_conflict_sessions)") if r[1] == 'pull_request_id'][0][3] == 0
legacy = con.execute("SELECT resolution FROM merge_conflict_files").fetchone()[0]
print(all(c in s for c in ('source_repository_id', 'result_commit_id', 'resolved_by_id', 'resolved_at')),
      all(c in f for c in ('conflict_kind', 'resolution', 'resolved_by_id', 'resolved_at')),
      nullable, legacy)
"""
    env = dict(os.environ, DATABASE_URL="sqlite:///" + db_path.as_posix(),
               FOXNEST_AUTH_SECRET="z" * 64, FOXNEST_PASSWORD_SETUP_KEY="w" * 64)
    out = subprocess.run([sys.executable, "-c", script], cwd=SERVER_ROOT, env=env,
                         capture_output=True, text=True, encoding="utf-8")
    last = (out.stdout.strip().splitlines() or [""])[-1].split()
    r.check("session columns are added", last[:1] == ["True"], out.stdout + out.stderr)
    r.check("file columns are added", last[1:2] == ["True"])
    r.check("pull_request_id is nullable after the rebuild", last[2:3] == ["True"])
    r.check("a legacy resolved row is marked resolution='content'", last[3:4] == ["content"])


# --- main ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="foxnest_e2e_merge_"))
    report = Report()
    try:
        port = args.port or free_port()
        with live_server(root, port) as base:
            report.section("1  Sign-in and setup")
            repo_id = seed(root)
            tokens = {}
            for name in ("lead", "alice", "contrib", "reader"):
                status, tokens[name] = login(base, name)
                report.check(f"{name} signs in", status == 200 and tokens[name])
            lead_api = Api(base, tokens["lead"])
            alice_api = Api(base, tokens["alice"])
            contrib_api = Api(base, tokens["contrib"])
            reader_api = Api(base, tokens["reader"])
            repo = Repo(lead_api, repo_id, "lead")
            repo.push("main", {"app.py": app_lines(), "README.md": "readme\n"}, "initial", parents=[])
            report.check("the first push lands on main", repo.head("main") is not None)

            scenario_line_merge(report, repo)
            scenario_pull_request(report, repo, Repo(alice_api, repo_id, "alice"), root)
            stale_pr = scenario_stale(report, repo)
            scenario_protected_source(report, repo)
            scenario_permissions(report, repo, reader_api, stale_pr)
            scenario_branch_merge(report, repo, root)
            scenario_fork(report, base, repo, lead_api, alice_api, contrib_api, reader_api)
            scenario_cli(report, base, repo, root)
        scenario_migration(report, root)
    except Exception as exc:  # a crash mid-scenario is a failure, not a pass
        import traceback
        traceback.print_exc()
        report.check("the run completed without an unexpected error", False, repr(exc))
    finally:
        if args.keep:
            print(f"\nkept: {root}")
        else:
            remove_tree(root)

    sys.exit(0 if report.summary() else 1)


if __name__ == "__main__":
    main()
