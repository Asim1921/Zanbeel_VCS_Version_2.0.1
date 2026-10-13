#!/usr/bin/env python3
"""End-to-end check of the Integrations stage against a live server.

    ..\\venv\\Scripts\\python.exe tools/e2e_integrations.py [--keep] [--port N]

Starts a real uvicorn server on a throwaway database and blob store, plus a webhook
receiver and an external policy service on 127.0.0.2 -- the one address the server is
allowed to call, so the same server can be shown refusing 127.0.0.1, itself. Drives
signed webhooks, CI status checks and push rules over HTTP and through the real fox
CLI. Nothing touches the development database.

Each check prints PASS or FAIL; the run ends with a count and exits non-zero on any
failure, so it can gate a pull request.

Scenarios
  1  Setup
  2  Signed webhooks: refused destinations, signatures, the delivery log, redelivery
  3  Every way a branch moves is announced, over real HTTP
  4  CI status checks gate every merge; CI reports with a token that cannot push
  5  Push rules on every write, judged on what changes
  6  Upgrading a database from before delivery ids
"""

import argparse
import contextlib
import hashlib
import hmac
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_merge_conflicts import (  # noqa: E402  (shared harness plumbing)
    Api, Report, Repo, b64, code_of, free_port, live_server, remove_tree, server_env,
)
from e2e_credentials import run_cli  # noqa: E402

SERVER_ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "E2e-Passw0rd!long"
RECEIVER_HOST = "127.0.0.2"
SERVER_ENV = {"FOXNEST_OUTBOUND_ALLOWED_HOSTS": RECEIVER_HOST}
SLOW_SECONDS = 4
AWS_KEY_LINE = "aws_key = AKIAIOSFODNN7EXAMPLE\n"
POLICY_SECRET = "policy-secret-for-e2e-0123"
BASE_FILES = {"app.py": "print('hello')\n", "README.md": "readme\n"}


# --- the other end of the wire --------------------------------------------------------

class Receiver:
    """A webhook receiver and an external policy service, as a CI system would run them."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []
        self.flaky_status = 503
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                path = self.path.split("?", 1)[0]
                with receiver.lock:
                    receiver.requests.append({
                        "path": path, "body": body, "at": time.monotonic(),
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                    })
                status, headers, reply = receiver.answer(path, body)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer((RECEIVER_HOST, 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://{RECEIVER_HOST}:{self.httpd.server_address[1]}"

    def url(self, path):
        return self.base + path

    def answer(self, path, body):
        if path == "/slow":
            time.sleep(SLOW_SECONDS)
        if path == "/flaky":
            return self.flaky_status, {}, b"flaky"
        if path == "/redirect":
            return 302, {"Location": self.url("/redirect-target")}, b""
        if path == "/policy":
            message = (json.loads(body).get("commit") or {}).get("message") or ""
            verdict = ({"allow": False, "reason": "release freeze: BLOCKME commits are held"}
                       if "BLOCKME" in message else {"allow": True})
            return 200, {"Content-Type": "application/json"}, json.dumps(verdict).encode()
        return 200, {}, b"ok"

    def received(self, path):
        with self.lock:
            return [r for r in self.requests if r["path"] == path]

    def events(self, path="/hook"):
        out = []
        for request in self.received(path):
            try:
                out.append({**request, "json": json.loads(request["body"])})
            except ValueError:
                pass
        return out

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def signed_by(secret, request):
    """Verify a delivery the way a receiver must: HMAC-SHA256 of the exact body bytes."""
    expected = "sha256=" + hmac.new(secret.encode(), request["body"], hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, request["headers"].get("x-zanbeel-signature-256", ""))


def wait_for(fn, timeout=20.0, interval=0.2):
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(interval)


def event(receiver, kind, since=0, match=lambda data: True, timeout=20.0):
    """The first `kind` event received after index `since` whose data matches."""
    def find():
        for item in receiver.events()[since:]:
            if item["json"].get("event") == kind and match(item["json"].get("data") or {}):
                return item
        return None
    return wait_for(find, timeout)


# --- server-side helpers --------------------------------------------------------------

def seed(workdir):
    """Users and two repositories. There is no public endpoint to create a user without
    an approval flow, so this one step goes to the database; the rest goes over HTTP."""
    script = f"""
import sys
sys.path.insert(0, {str(SERVER_ROOT)!r})
from database.database import SessionLocal
from database.crud import UserCRUD, RepositoryCRUD, UserPermissionCRUD
from app.core.security import hash_password
db = SessionLocal()
for name, role in (("lead", "team_lead"), ("dev", "developer"), ("ci", "developer"),
                   ("contrib", "team_lead")):
    UserCRUD.create_user(db, username=name, role=role, password_hash=hash_password({PASSWORD!r}),
                         email=name + "@e2e.test")
ids = []
for repo_name in ("int-app", "rules-app"):
    repo = RepositoryCRUD.create_repository(db, "lead", repo_name, "integrations e2e")
    for name in ("dev", "ci"):
        UserPermissionCRUD.create_permission(db, name, repo.id, "write", "lead")
    ids.append(repo.id)
print(" ".join(ids))
"""
    out = subprocess.run([sys.executable, "-c", script], cwd=SERVER_ROOT, env=server_env(workdir),
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return out.stdout.strip().splitlines()[-1].split()


def session_of(base, username):
    status, body = Api(base).post("/api/auth/login", {"username": username, "password": PASSWORD})
    return body.get("access_token") if status == 200 else None


def make_token(base, session, name, scopes):
    status, body = Api(base, session).post("/api/tokens", {"name": name, "scopes": scopes})
    return body.get("token") if status == 200 else None


def delete(api, path):
    response = requests.delete(api.base + path, headers={"Authorization": f"Bearer {api.token}"}, timeout=60)
    return response.status_code


def push(api, repo_id, author, branch, files, message, parents=None):
    """A snapshot push that returns the outcome instead of raising, for refusals."""
    if parents is None:
        head = Repo(api, repo_id, author).head(branch)
        parents = [head] if head else []
    commit_id = hashlib.sha1(f"{repo_id}:{branch}:{message}:{time.time_ns()}".encode()).hexdigest()
    status, body = api.post(f"/api/repository/{repo_id}/push", {
        "branch": branch,
        "commit": {"id": commit_id, "author": author, "message": message,
                   "parent": parents[0] if parents else None, "parents": parents,
                   "files": {path: b64(data) for path, data in files.items()},
                   "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")},
    })
    return status, body, commit_id


def timed(fn):
    started = time.monotonic()
    result = fn()
    return result, time.monotonic() - started


def in_background(fn):
    """Run fn on a thread; returns a joiner that yields (result, seconds)."""
    box = {}

    def run():
        box["value"], box["seconds"] = timed(fn)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def join():
        thread.join(120)
        return box.get("value"), box.get("seconds")
    return join


class Ctx:
    """What the scenarios share."""


class Machine:
    """A working directory plus a home directory: one laptop or one CI agent."""

    def __init__(self, root, name, env):
        self.home, self.work = root / f"{name}-home", root / f"{name}-work"
        self.home.mkdir()
        self.work.mkdir()
        self.env = env

    def fox(self, *args, env=None):
        return run_cli(self.work, self.home, list(args), env={**self.env, **(env or {})})

    def write(self, path, text):
        target = self.work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exact bytes: write_text would turn \n into \r\n on Windows, and the commit would
        # then differ from the same text pushed over HTTP.
        target.write_bytes(text.encode("utf-8"))

    def commit(self, message, files):
        for path, text in files.items():
            self.write(path, text)
            self.fox("add", path)
        return self.fox("commit", "-m", message)


# --- scenarios -------------------------------------------------------------------------

def scenario_webhooks(r, c):
    r.section("2  Signed webhooks")
    lead, rx, repo = c.lead, c.rx, c.int_repo

    status, body = lead.post("/api/webhooks", {"url": rx.url("/hook"), "repository_id": repo.id})
    hook, secret = (body.get("webhook") or {}, body.get("secret")) if status == 200 else ({}, None)
    r.check("a webhook created without a secret is given one, shown once",
            status == 200 and body.get("secret_generated") and len(secret or "") >= 32 and hook.get("signed"), body)
    _s, listed = lead.get(f"/api/webhooks?repository_id={repo.id}")
    r.check("...and the secret is not in the listing", bool(secret) and secret not in json.dumps(listed), listed)
    c.hook_id, c.secret = hook.get("id"), secret

    for url, reason in ((f"http://127.0.0.1:{c.port}/api/admin/users", "loopback"),
                        (f"http://[::1]:{c.port}/", "loopback"),
                        ("http://169.254.169.254/latest/meta-data/", "link-local"),
                        ("http://10.20.30.40/hook", "private network")):
        status, body = lead.post("/api/webhooks", {"url": url, "repository_id": repo.id})
        r.check(f"a webhook to {url.split('/')[2]} is refused ({reason})",
                status == 400 and reason in str(body.get("detail")), body)

    before = repo.head("main")
    mark = len(rx.events())
    commit_id = repo.push("main", {**BASE_FILES, "app.py": "print('v2')\n"}, "second version")
    got = event(rx, "push", mark, lambda d: d.get("after") == commit_id)
    r.check("a push is delivered to the receiver", got is not None, rx.events()[mark:])
    if not got:
        return
    data = got["json"]["data"]
    r.check("...signed with the secret: HMAC-SHA256 of the body verifies", signed_by(secret, got))
    r.check("...and a changed body would not verify",
            not signed_by(secret, {**got, "body": got["body"].replace(b"second", b"Second")}))
    r.check("...the delivery id is inside the signed body, and matches the header",
            got["json"].get("delivery_id") == got["headers"].get("x-zanbeel-delivery")
            and got["headers"].get("x-zanbeel-event") == "push" and got["json"].get("delivered_at"), got["headers"])
    r.check("...it says which branch moved, from where to where, and the commit",
            data.get("branch") == "main" and data.get("before") == before
            and (data.get("head_commit") or {}).get("message") == "second version", data)

    def logged():
        _s, body = lead.get(f"/api/webhooks/{c.hook_id}/deliveries?delivery_id={got['json']['delivery_id']}")
        rows = body.get("deliveries", []) if isinstance(body, dict) else []
        return rows if rows and rows[0].get("success") else None
    rows = wait_for(logged, 10)
    r.check("the delivery log records it: event, 200, the same delivery id",
            rows and rows[0]["event"] == "push" and rows[0]["status_code"] == 200, rows)

    # A receiver that takes its time slows neither the push nor anyone else's request.
    status, body = lead.post("/api/webhooks", {"url": rx.url("/slow"), "repository_id": repo.id,
                                               "events": ["push"]})
    slow_id = body.get("webhook", {}).get("id")
    (_s, _b, _id), seconds = timed(lambda: push(lead, repo.id, "lead", "main",
                                                {**BASE_FILES, "app.py": "print('v3')\n"}, "third version"))
    r.check(f"a push returns at once although a receiver takes {SLOW_SECONDS}s", seconds < 2, f"{seconds:.1f}s")
    join = in_background(lambda: lead.post(f"/api/webhooks/{slow_id}/ping"))
    time.sleep(0.5)
    (me_status, _b), me_seconds = timed(lambda: lead.get("/api/auth/me"))
    (status, body), ping_seconds = join()
    r.check("while a ping waits on a slow receiver, the server keeps answering",
            me_status == 200 and me_seconds < 2, f"/api/auth/me took {me_seconds:.1f}s")
    r.check("...and the ping reports the delivery when it lands",
            status == 200 and body.get("delivery", {}).get("success") and ping_seconds >= SLOW_SECONDS - 0.5, body)
    delete(lead, f"/api/webhooks/{slow_id}")

    status, body = lead.post("/api/webhooks", {"url": rx.url("/redirect"), "repository_id": repo.id,
                                               "events": ["release.published"]})
    redirect_id = body.get("webhook", {}).get("id")
    _s, body = lead.post(f"/api/webhooks/{redirect_id}/ping")
    delivery = body.get("delivery") or {}
    r.check("a redirect is not followed: the signed body is not sent on to wherever it points",
            not delivery.get("success") and "not followed" in str(delivery.get("error"))
            and rx.received("/redirect-target") == [], delivery)
    delete(lead, f"/api/webhooks/{redirect_id}")

    status, body = lead.post("/api/webhooks", {"url": rx.url("/flaky"), "repository_id": repo.id,
                                               "events": ["release.published"]})
    flaky_id, flaky_secret = body.get("webhook", {}).get("id"), body.get("secret")
    rx.flaky_status = 503
    _s, body = lead.post(f"/api/webhooks/{flaky_id}/ping")
    failed = body.get("delivery") or {}
    r.check("a delivery the receiver fails is logged as failed, with its status",
            not failed.get("success") and failed.get("status_code") == 503, failed)
    rx.flaky_status = 200
    status, body = lead.post(f"/api/webhooks/{flaky_id}/deliveries/{failed.get('id')}/redeliver")
    again = body.get("delivery") or {}
    last = rx.events("/flaky")[-1]
    r.check("...it can be sent again from the log, and this time it lands",
            status == 200 and again.get("success") and again.get("redelivery_of") == failed.get("delivery_id")
            and again.get("delivery_id") != failed.get("delivery_id"), body)
    r.check("...the redelivery is signed, and says which delivery it repeats",
            signed_by(flaky_secret, last) and last["json"].get("redelivery_of") == failed.get("delivery_id"),
            last["json"])
    delete(lead, f"/api/webhooks/{flaky_id}")

    status, body = lead.post(f"/api/webhooks/{c.hook_id}/rotate-secret")
    new_secret = body.get("secret")
    mark = len(rx.events())
    commit_id = repo.push("main", {**BASE_FILES, "app.py": "print('v4')\n"}, "fourth version")
    got = event(rx, "push", mark, lambda d: d.get("after") == commit_id)
    r.check("after rotating the secret, deliveries verify with the new one and not the old",
            got is not None and signed_by(new_secret, got) and not signed_by(secret, got))
    c.secret = new_secret

    c.lead_cli = Machine(c.root, "lead-cli", {"FOXNEST_TOKEN": c.lead_token, "FOXNEST_SERVER": c.base,
                                              "FOXNEST_REPO_ID": repo.id})
    code, out = c.lead_cli.fox("webhook", "list")
    r.check("fox webhook list shows the hook as signed", code == 0 and rx.url("/hook") in out and " yes " in out, out)
    code, out = c.lead_cli.fox("webhook", "ping", str(c.hook_id))
    r.check("fox webhook ping shows the receiver's answer", code == 0 and "200" in out, out)
    code, out = c.lead_cli.fox("webhook", "deliveries", str(c.hook_id))
    r.check("fox webhook deliveries lists the log", code == 0 and "push" in out and "ok" in out, out)
    code, out = c.lead_cli.fox("webhook", "add", "http://169.254.169.254/latest/")
    r.check("fox webhook add to the metadata service fails, saying why", code != 0 and "link-local" in out, out)


def scenario_announced(r, c):
    r.section("3  Every way a branch moves is announced")
    lead, rx, repo = c.lead, c.rx, c.int_repo
    first = len(rx.events())

    mark = len(rx.events())
    status, body, _cid = push(c.dev, repo.id, "dev", "main", {**BASE_FILES, "app.py": "print('dev')\n"}, "dev work")
    pending_id = body.get("commit_id") if isinstance(body, dict) else None
    r.check("a developer's push is queued for review, and announced as commit.pending",
            status == 200 and body.get("status") == "pending_approval"
            and event(rx, "commit.pending", mark) is not None, body)
    mark = len(rx.events())
    status, body = lead.post(f"/api/pending-commits/{pending_id}/review", {"action": "approve"})
    head = repo.head("main")
    reviewed = event(rx, "commit.reviewed", mark)
    r.check("approving it sends commit.reviewed (it was never sent)",
            status == 200 and reviewed is not None and reviewed["json"]["data"].get("decision") == "approved", body)
    r.check("...and the push it causes (CI never heard about approved commits)",
            event(rx, "push", mark, lambda d: d.get("after") == head and d.get("branch") == "main") is not None)

    mark = len(rx.events())
    repo.branch("feature")
    r.check("creating a branch in the web interface sends branch.created and a push",
            event(rx, "branch.created", mark, lambda d: d.get("branch") == "feature") is not None
            and event(rx, "push", mark, lambda d: d.get("branch") == "feature" and d.get("created")) is not None)

    pr = repo.open_pr("feature")
    mark = len(rx.events())
    feature_head = repo.push("feature", {**BASE_FILES, "feature.txt": "f\n"}, "feature work")
    r.check("pushing to a pull request's branch sends pull_request.updated with the new head",
            event(rx, "pull_request.updated", mark,
                  lambda d: (d.get("pull_request") or {}).get("head") == feature_head) is not None)

    mark = len(rx.events())
    before_merge = repo.head("main")
    status, body = lead.post(repo.url(f"/pull-requests/{pr}/merge"))
    head = repo.head("main")
    r.check("merging the pull request sends pull_request.merged and a push to main",
            status == 200 and event(rx, "pull_request.merged", mark) is not None
            and event(rx, "push", mark, lambda d: d.get("branch") == "main" and d.get("after") == head
                      and d.get("operation") == "merge") is not None, body)

    repo.branch("topic")
    topic_commit = repo.push("topic", {**BASE_FILES, "feature.txt": "f\n", "picked.txt": "p\n"}, "pick me")
    for label, path, payload in (
            ("a cherry-pick", "/cherry-pick", lambda: {"commit_id": topic_commit, "branch": "main"}),
            ("a revert", "/revert", lambda: {"commit_id": repo.head("main"), "branch": "main"}),
            ("a rollback", "/rollback/branch", lambda: {"branch": "main", "target_commit_id": before_merge})):
        mark = len(rx.events())
        status, body = lead.post(repo.url(path), payload())
        moved = repo.head("main")
        r.check(f"{label} onto main is announced as a push",
                status == 200 and event(rx, "push", mark, lambda d: d.get("after") == moved) is not None, body)

    mark = len(rx.events())
    status = delete(lead, repo.url("/branches/topic"))
    r.check("deleting a branch sends branch.deleted",
            status == 200 and event(rx, "branch.deleted", mark, lambda d: d.get("branch") == "topic") is not None)

    received = rx.events()[first:]
    # A new branch starts at an existing commit, so a move is (branch, before, after).
    pushes = [(e["json"]["data"].get("branch"), e["json"]["data"].get("before"), e["json"]["data"].get("after"))
              for e in received if e["json"].get("event") == "push"]
    r.check("every event in this scenario verifies with the current secret",
            received and all(signed_by(c.secret, e) for e in received))
    r.check("...and each branch move was announced once", len(pushes) == len(set(pushes)), pushes)


def scenario_ci(r, c):
    r.section("4  CI status checks gate every merge")
    lead, rx, repo = c.lead, c.rx, c.int_repo
    ci_session = session_of(c.base, "ci")
    status_token = make_token(c.base, ci_session, "ci-runner", ["repo:status"])
    read_token = make_token(c.base, ci_session, "ci-reader", ["repo:read"])
    r.check("the CI account makes a token that can only report statuses", bool(status_token))
    agent = Machine(c.root, "ci-agent", {"FOXNEST_TOKEN": status_token, "FOXNEST_SERVER": c.base,
                                         "FOXNEST_REPO_ID": repo.id})

    status, body = lead.put(repo.url("/branch-policy"),
                            {"required_status_checks": [{"context": "ci/build", "reporter": "ci"}]})
    r.check("the lead requires ci/build, counted only when the ci account reports it", status == 200, body)

    repo.branch("feature-ci")
    head = repo.push("feature-ci", {**BASE_FILES, "feature.txt": "f\n", "ci.txt": "needs ci\n"}, "needs CI")
    pr = repo.open_pr("feature-ci")
    status, body = lead.post(repo.url(f"/pull-requests/{pr}/merge"))
    r.check("the pull request cannot merge before CI has reported", status != 200 and "ci/build" in json.dumps(body), body)

    status, body = c.dev.post(repo.url(f"/commits/{head}/statuses"), {"context": "ci/build", "state": "success"})
    _s, statuses = lead.get(repo.url(f"/commits/{head}/statuses?branch=main"))
    combined = statuses.get("combined", {}) if isinstance(statuses, dict) else {}
    ignored = ((combined.get("required_checks") or [{}])[0].get("ignored_report") or {})
    r.check("a developer's own 'success' is recorded but does not count",
            status == 200 and not combined.get("required_satisfied") and ignored.get("reported_by") == "dev", combined)
    status, body = lead.post(repo.url(f"/pull-requests/{pr}/merge"))
    r.check("...so the merge is still refused, naming who must report", status != 200 and "from ci" in json.dumps(body), body)

    code, out = agent.fox("status-check", "show", head, "--branch", "main")
    r.check("fox status-check show explains the ignored report",
            code == 0 and "ignored success from dev; only ci counts" in out, out)
    code, out = agent.fox("status-check", "report", "ci/build", "pending", "--commit", head,
                          "--url", "https://ci.example/run/1")
    r.check("CI reports 'pending' with fox status-check", code == 0 and "Reported ci/build: pending" in out, out)
    mark = len(rx.events())
    code, out = agent.fox("status-check", "report", "ci/build", "success", "--commit", head,
                          "--url", "https://ci.example/run/1", "--description", "212 tests")
    r.check("...then 'success', and is told the required checks now pass",
            code == 0 and "All 1 required check(s) have passed" in out, out)
    got = event(rx, "status.reported", mark,
                lambda d: (d.get("status") or {}).get("context") == "ci/build"
                and (d.get("status") or {}).get("state") == "success")
    r.check("...the report is announced as status.reported", got is not None)
    status, body = lead.post(repo.url(f"/pull-requests/{pr}/merge"))
    r.check("the pull request merges once CI has passed it", status == 200, body)

    status, _b, _id = push(Api(c.base, status_token), repo.id, "ci", "main", BASE_FILES, "ci pushes")
    r.check("the CI token cannot push", status == 403, _b)
    code, out = agent.fox("status-check", "report", "ci/build", "success", "--commit", head,
                          env={"FOXNEST_TOKEN": read_token})
    r.check("a read-only token cannot report, and fox exits non-zero", code != 0 and "read-only" in out, out)
    code, out = agent.fox("status-check", "report", "ci/build", "success", "--commit", head,
                          "--url", "javascript:alert(1)")
    r.check("a status link must be a web address", code != 0, out)

    # The same checks gate a direct branch merge, together with the branch's own rule.
    status, body = lead.put(repo.url("/branch-protection"), {
        "branch_pattern": "main", "mode": "open", "rules": {"require_status_checks": ["ci/lint"]}})
    r.check("branch protection on main also requires ci/lint", status == 200, body)
    _s, body = lead.get(repo.url("/required-checks?branch=main"))
    r.check("merging into main needs both: one list, from both places",
            sorted(body.get("required_status_checks", [])) == ["ci/build", "ci/lint"], body)
    repo.branch("feature-lint")
    head = repo.push("feature-lint", {**BASE_FILES, "feature.txt": "f\n", "ci.txt": "needs ci\n",
                                      "lint.txt": "l\n"}, "needs lint")
    status, body = lead.post(repo.url("/branches/merge"), {"source_branch": "feature-lint", "target_branch": "main"})
    r.check("a direct branch merge is refused until the checks pass (it used to skip them)",
            status == 409 and code_of(body) == "CHECKS_REQUIRED", body)
    for context in ("ci/build", "ci/lint"):
        agent.fox("status-check", "report", context, "success", "--commit", head)
    status, body = lead.post(repo.url("/branches/merge"), {"source_branch": "feature-lint", "target_branch": "main"})
    r.check("...and goes through once CI has passed both", status == 200, body)


def scenario_push_rules(r, c):
    r.section("5  Push rules on every write, judged on what changes")
    lead, rx, repo = c.lead, c.rx, c.rules_repo
    files = {**BASE_FILES, "generated/schema.sql": "create table t (id int);\n"}

    laptop = Machine(c.root, "laptop", {"FOXNEST_TOKEN": c.lead_token, "FOXNEST_SERVER": c.base})
    laptop.fox("init", "--username", "lead", "--repo-name", "rules-app")
    laptop.fox("set", "origin", c.base)
    config_path = laptop.work / ".fox" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["repo_id"] = repo.id
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    laptop.commit("base", files)
    code, out = laptop.fox("push")
    r.check("the first push from the fox CLI lands", code == 0 and repo.head("main"), out)

    status, body = lead.post("/api/server-hooks", {"name": "repo-policy", "repository_id": repo.id, "config": {
        "block_secrets": True, "protected_paths": ["generated/"], "forbidden_content": ["DO-NOT-SHIP"]}})
    hook_id = body.get("hook", {}).get("id")
    r.check("the lead adds a rule: no secrets, generated/ is protected, no DO-NOT-SHIP", status == 200, body)

    before = repo.head("main")
    laptop.commit("add settings", {"settings.py": AWS_KEY_LINE})
    code, out = laptop.fox("push")
    r.check("fox push of a commit carrying an AWS key is refused, saying which rule and why",
            code != 0 and "Refused by the server's push rule 'repo-policy'" in out and "AWS access key" in out, out)
    r.check("...and nothing reached the branch", repo.head("main") == before)

    status, body, _id = push(lead, repo.id, "lead", "main", {**files, "app.py": "print('edited')\n"}, "edit app")
    r.check("a push that leaves the protected file alone is accepted (it used to be refused)", status == 200, body)
    files["app.py"] = "print('edited')\n"
    without = {k: v for k, v in files.items() if k != "generated/schema.sql"}
    status, body, _id = push(lead, repo.id, "lead", "main", without, "drop the schema")
    r.check("deleting the protected file is refused (it used to pass)",
            status == 422 and "generated/schema.sql" in json.dumps(body), body)
    status, body, _id = push(lead, repo.id, "lead", "main", {**files, "notes.txt": "DO-NOT-SHIP\n"}, "notes")
    r.check("forbidden content is refused", status == 422 and code_of(body) == "PRE_RECEIVE_REJECTED", body)

    # A secret that got in before the rule existed must not come back through a rollback.
    lead.put(f"/api/server-hooks/{hook_id}", {"enabled": False})
    _s, _b, leaked = push(lead, repo.id, "lead", "main", {**files, "secret.txt": AWS_KEY_LINE}, "oops")
    push(lead, repo.id, "lead", "main", files, "remove the leaked key")
    lead.put(f"/api/server-hooks/{hook_id}", {"enabled": True})
    before = repo.head("main")
    status, body = lead.post(repo.url("/rollback/branch"), {"branch": "main", "target_commit_id": leaked})
    r.check("a rollback that would put a removed secret back is refused",
            status == 422 and repo.head("main") == before, body)

    repo.branch("feature")
    push(lead, repo.id, "lead", "feature", {**files, "app.py": "print('feature')\n"}, "feature edit")
    push(lead, repo.id, "lead", "main", {**files, "app.py": "print('main')\n"}, "main edit")
    files["app.py"] = "print('main')\n"
    status, body = lead.post(repo.url("/branches/conflicts"), {"source_branch": "feature", "target_branch": "main"})
    session_id = (body.get("session") or {}).get("id") if isinstance(body, dict) else None
    before = repo.head("main")
    status, body = lead.post(repo.url(f"/branches/conflicts/{session_id}/resolve"),
                             {"resolutions": {"app.py": b64("DO-NOT-SHIP\n")}})
    r.check("a conflict resolution written by hand is checked too", status == 422 and repo.head("main") == before, body)
    status, body = lead.post(repo.url(f"/branches/conflicts/{session_id}/resolve"),
                             {"resolutions": {"app.py": b64("print('main and feature')\n")}})
    r.check("...and a clean resolution goes through", status == 200 and repo.head("main") != before, body)
    files["app.py"] = "print('main and feature')\n"

    contrib = Api(c.base, session_of(c.base, "contrib"))
    status, body = contrib.post(repo.url("/fork"), {"name": "rules-app-fork"})
    fork_id = body.get("repo_id") if isinstance(body, dict) else None
    status, body, _id = push(contrib, fork_id, "contrib", "main", {**files, "config.py": AWS_KEY_LINE}, "fork config")
    r.check("a fork, which has no rules of its own, accepts a commit carrying a key", status == 200, body)
    status, body = contrib.post(repo.url("/pull-requests/from-fork"), {
        "source_repo_id": fork_id, "source_branch": "main", "target_branch": "main", "title": "From the fork"})
    fork_pr = (body.get("pull_request") or {}).get("id") if isinstance(body, dict) else None
    before = repo.head("main")
    status, body = lead.post(repo.url(f"/pull-requests/{fork_pr}/merge"))
    r.check("merging that fork's pull request is refused by upstream's rule (it used to land)",
            status == 422 and "AWS access key" in json.dumps(body) and repo.head("main") == before, body)

    _s, body = lead.post(f"/api/server-hooks/{hook_id}/test", {"files": {"settings.py": AWS_KEY_LINE}})
    r.check("the dry run exercises content rules with a file's text",
            body.get("would_accept") is False and "AWS access key" in json.dumps(body.get("reasons")), body)
    _s, body = lead.post(f"/api/server-hooks/{hook_id}/test", {"deleted": ["generated/schema.sql"]})
    r.check("...and deletions", body.get("would_accept") is False, body)
    _s, body = lead.post(f"/api/server-hooks/{hook_id}/test", {"paths": ["src/a.py"]})
    r.check("...and says when a path was checked without content",
            body.get("would_accept") is True and "src/a.py" in str(body.get("note")), body)

    status, body = lead.post("/api/server-hooks", {"name": "branch-names", "repository_id": repo.id,
                                                   "config": {"branch_name_pattern": "^(feature|fix)/"}})
    names_id = body.get("hook", {}).get("id")
    status, body = lead.post(repo.url("/branches"), {"name": "wip", "from_branch": "main"})
    r.check("a new branch whose name breaks the rule is refused in the web interface", status == 422, body)
    status, body, _id = push(lead, repo.id, "lead", "wip2", files, "new branch", parents=[repo.head("main")])
    r.check("...and by push", status == 422 and "branch name" in json.dumps(body), body)
    status, body = lead.post(repo.url("/branches"), {"name": "feature/login", "from_branch": "main"})
    r.check("a name that follows it is accepted", status == 200, body)
    status, body, _id = push(lead, repo.id, "lead", "main", {**files, "app.py": "print('later')\n"}, "later")
    r.check("existing branches are not affected", status == 200, body)
    files["app.py"] = "print('later')\n"
    delete(lead, f"/api/server-hooks/{names_id}")

    status, body = lead.post("/api/server-hooks", {"name": "policy", "repository_id": repo.id,
                                                   "config": {"external_url": f"http://127.0.0.1:{c.port}/api/x"}})
    r.check("a policy service on this server itself is refused", status == 400 and "loopback" in json.dumps(body), body)
    status, body = lead.post("/api/server-hooks", {"name": "policy", "repository_id": repo.id, "config": {
        "external_url": rx.url("/policy"), "external_secret": POLICY_SECRET}})
    policy = body.get("hook", {}) if status == 200 else {}
    r.check("a signed external policy service is added; its secret is not shown back",
            policy.get("has_external_secret") and "external_secret" not in policy.get("config", {}), body)
    marker = "CONTENT-MARKER-7f3a"
    status, body, _id = push(lead, repo.id, "lead", "main", {**files, "plan.txt": marker + "\n"}, "BLOCKME: ship it")
    r.check("the service's refusal is the push's refusal, with its reason",
            status == 422 and "release freeze" in json.dumps(body), body)
    asked = rx.events("/policy")[-1] if rx.events("/policy") else None
    r.check("...the request was signed with the policy secret",
            asked is not None and signed_by(POLICY_SECRET, asked))
    r.check("...and carried the changed paths but never file contents",
            asked is not None and "plan.txt" in asked["json"]["commit"]["changed_paths"]
            and marker.encode() not in asked["body"], asked and asked["json"])
    status, body, _id = push(lead, repo.id, "lead", "main", {**files, "plan.txt": "fine\n"}, "ordinary change")
    r.check("...and what it allows goes through", status == 200, body)
    files["plan.txt"] = "fine\n"
    delete(lead, f"/api/server-hooks/{policy.get('id')}")

    status, body = lead.post("/api/server-hooks", {"name": "slow-pattern", "repository_id": repo.id,
                                                   "config": {"commit_message_pattern": "(x+x+)+y"}})
    slow_id = body.get("hook", {}).get("id")
    join = in_background(lambda: push(lead, repo.id, "lead", "main", {**files, "app.py": "x\n"}, "x" * 5000))
    time.sleep(0.2)
    (me_status, _b), me_seconds = timed(lambda: lead.get("/api/auth/me"))
    (status, body, _id), seconds = join()
    r.check("a pattern that would run for minutes is stopped, and the push refused",
            status == 422 and "took too long" in json.dumps(body) and seconds < 5, f"{seconds:.1f}s {body}")
    r.check("...while other requests are still answered", me_status == 200 and me_seconds < 3, f"{me_seconds:.1f}s")
    delete(lead, f"/api/server-hooks/{slow_id}")


def scenario_upgrade(r, root, port):
    r.section("6  Upgrading a database from before delivery ids")
    workdir = root / "upgrade"
    workdir.mkdir()
    with live_server(workdir, port, SERVER_ENV) as base:  # creates today's schema
        seed(workdir)
        lead = Api(base, session_of(base, "lead"))
        _s, body = lead.post("/api/webhooks", {"url": "https://hooks.invalid/x"})
        hook_id = body.get("webhook", {}).get("id")
        lead.post(f"/api/webhooks/{hook_id}/ping")   # logs a failed delivery
    # Rebuild the log in its earlier shape, keeping the row.
    with contextlib.closing(sqlite3.connect(workdir / "e2e.db")) as db:
        db.executescript("""
            CREATE TABLE old_deliveries (id INTEGER PRIMARY KEY, webhook_id INTEGER NOT NULL,
                event VARCHAR(60) NOT NULL, payload TEXT NOT NULL, status_code INTEGER,
                response_body TEXT, error TEXT, duration_ms INTEGER, success BOOLEAN,
                attempt INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO old_deliveries SELECT id, webhook_id, event, payload, status_code,
                response_body, error, duration_ms, success, attempt, created_at FROM webhook_deliveries;
            DROP TABLE webhook_deliveries;
            ALTER TABLE old_deliveries RENAME TO webhook_deliveries;
            CREATE INDEX ix_webhook_deliveries_hook_time ON webhook_deliveries (webhook_id, created_at);
            DROP INDEX IF EXISTS ix_commit_statuses_repo_commit;
        """)
        db.commit()

    with live_server(workdir, port, SERVER_ENV) as base:
        with contextlib.closing(sqlite3.connect(workdir / "e2e.db")) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(webhook_deliveries)")]
            indexes = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")]
        r.check("startup adds webhook_deliveries.delivery_guid and redelivery_of",
                "delivery_guid" in columns and "redelivery_of" in columns, columns)
        r.check("...and the indexes the log and the merge gate read by",
                "ix_webhook_deliveries_delivery_guid" in indexes and "ix_commit_statuses_repo_commit" in indexes)
        lead = Api(base, session_of(base, "lead"))
        status, body = lead.get(f"/api/webhooks/{hook_id}/deliveries")
        rows = body.get("deliveries", []) if isinstance(body, dict) else []
        r.check("the delivery logged before the upgrade is still listed, with no id",
                status == 200 and len(rows) == 1 and rows[0]["delivery_id"] is None, body)
        _s, body = lead.post(f"/api/webhooks/{hook_id}/ping")
        r.check("new deliveries get an id", bool((body.get("delivery") or {}).get("delivery_id")), body)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="zanbeel_e2e_integrations_"))
    workdir = root / "server"
    workdir.mkdir()
    report = Report()
    receiver = None
    try:
        report.section("1  Setup")
        receiver = Receiver()
        report.check(f"a receiver is listening on {receiver.base}", True)
        c = Ctx()
        c.root, c.rx, c.port = root, receiver, args.port or free_port()
        with live_server(workdir, c.port, SERVER_ENV) as base:
            c.base = base
            int_id, rules_id = seed(workdir)
            c.lead = Api(base, session_of(base, "lead"))
            c.dev = Api(base, session_of(base, "dev"))
            report.check("users and two repositories seeded; password sign-in works",
                         bool(int_id and rules_id and c.lead.token and c.dev.token))
            c.lead_token = make_token(base, c.lead.token, "lead-cli", ["repo:write"])
            c.int_repo = Repo(c.lead, int_id, "lead")
            c.rules_repo = Repo(c.lead, rules_id, "lead")
            c.int_repo.push("main", BASE_FILES, "base")

            scenario_webhooks(report, c)
            scenario_announced(report, c)
            scenario_ci(report, c)
            scenario_push_rules(report, c)
        scenario_upgrade(report, root, free_port())
    except Exception as exc:  # a crash is a failure, not a pass by omission
        import traceback
        traceback.print_exc()
        report.check(f"harness ran to completion ({type(exc).__name__})", False, str(exc))
    finally:
        if receiver:
            receiver.close()
        if args.keep:
            print(f"\nKept: {root}")
        else:
            remove_tree(root)

    sys.exit(0 if report.summary() else 1)


if __name__ == "__main__":
    main()
