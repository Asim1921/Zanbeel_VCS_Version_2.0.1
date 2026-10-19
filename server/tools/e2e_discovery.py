#!/usr/bin/env python3
"""End-to-end check of Discovery and Navigation against a live server.

    ..\\venv\\Scripts\\python.exe tools/e2e_discovery.py [--keep] [--port N]

Starts a real uvicorn server on a throwaway database, blob store and search index -- the
index is built by the server's own background thread, as in production -- and drives
search, the activity log and notifications over HTTP and through the real fox CLI, as
three users who can see different things. Nothing touches the development database.

Each check prints PASS or FAIL; the run ends with a count and exits non-zero on any
failure, so it can gate a pull request.

Scenarios
  1  Setup
  2  Code search across repositories, from the live index, scoped to what each user reads
  3  The index follows the default branch; a pathological pattern cannot stall the server
  4  The index is disposable: deleted while the server is down, it is rebuilt at startup
  5  The activity log: complete, attributed, scoped, filterable, exportable
  6  Notifications that point somewhere
  7  The fox CLI: search, activity, notifications
  8  Upgrading a database from before the structured activity log
"""

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_merge_conflicts import (  # noqa: E402  (shared harness plumbing)
    Api, Report, Repo, b64, free_port, live_server, remove_tree, server_env,
)
from e2e_credentials import run_cli  # noqa: E402

SERVER_ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "E2e-Passw0rd!long"


def seed(workdir):
    """Users and two repositories: billing (dev may read it) and payroll (dev may not)."""
    script = f"""
import sys
sys.path.insert(0, {str(SERVER_ROOT)!r})
from database.database import SessionLocal
from database.crud import UserCRUD, RepositoryCRUD, UserPermissionCRUD
from app.core.security import hash_password
db = SessionLocal()
for name, role in (("lead", "team_lead"), ("dev", "developer"), ("outsider", "developer"),
                   ("ci", "developer")):
    UserCRUD.create_user(db, username=name, role=role, password_hash=hash_password({PASSWORD!r}),
                         email=name + "@e2e.test")
billing = RepositoryCRUD.create_repository(db, "lead", "billing", "invoices")
payroll = RepositoryCRUD.create_repository(db, "lead", "payroll", "salaries")
for name in ("dev", "ci"):
    UserPermissionCRUD.create_permission(db, name, billing.id, "write", "lead")
print(billing.id, payroll.id)
"""
    out = subprocess.run([sys.executable, "-c", script], cwd=SERVER_ROOT, env=server_env(workdir),
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return out.stdout.strip().splitlines()[-1].split()


def session_of(base, username):
    status, body = Api(base).post("/api/auth/login", {"username": username, "password": PASSWORD})
    return body.get("access_token") if status == 200 else None


def index_env(workdir):
    return {"FOXNEST_SEARCH_INDEX_PATH": str(workdir / "search_index.db")}


def _remove_index(path):
    """Delete the index and its WAL files; Windows can hold them a moment after exit."""
    deadline = time.monotonic() + 10
    while True:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(path) + suffix)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if not path.exists() or time.monotonic() > deadline:
            return
        time.sleep(0.25)


def wait_indexed(admin, expect=None, timeout=60):
    """Wait until nothing is queued (and, given expect, that many repositories are indexed)."""
    deadline = time.monotonic() + timeout
    status = {}
    while time.monotonic() < deadline:
        _s, body = admin.get("/api/admin/search-index")
        status = body.get("index", {}) if isinstance(body, dict) else {}
        idle = not status.get("queued") and not status.get("running")
        if idle and (expect is None or status.get("repositories") == expect):
            return status
        time.sleep(0.2)
    return status


def push(api, repo_id, author, branch, files, message, parents=None):
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


def code(api, **params):
    return api._call("GET", "/api/search/code", params=params)


class Ctx:
    pass


# --- scenarios -------------------------------------------------------------------------

def scenario_code_search(r, c):
    r.section("2  Code search across repositories")
    status = wait_indexed(c.lead, expect=2)
    r.check("the server's indexer picked up both repositories after their first push",
            status.get("repositories") == 2 and status.get("indexed_files", 0) >= 3, status)

    s, body = code(c.lead, q="parse_invoice")
    repos = {row["repository_name"] for row in body.get("results", [])} if s == 200 else set()
    r.check("a lead's search finds the string in every repository", s == 200 and repos == {"billing", "payroll"}, body)
    r.check("...answered from the index", body.get("source") == "index", body.get("source"))
    s, body = code(c.dev, q="parse_invoice")
    r.check("a developer's search shows only the repository they can read",
            s == 200 and {row["repository_name"] for row in body["results"]} == {"billing"}, body)
    s, body = code(c.outsider, q="parse_invoice")
    r.check("someone who can read neither finds nothing (and learns nothing)",
            s == 200 and body["results"] == [] and body["repositories_searched"] == 0, body)
    s, body = code(c.outsider, q="parse_invoice", repository_id=c.payroll)
    r.check("...and naming the repository gets a 404, not a 403", s == 404, body)

    match = next((m for row in code(c.dev, q="parse_invoice")[1]["results"] for m in row["matches"]), {})
    r.check("results carry the line number and the matched span",
            match.get("line") == 1 and match.get("text", "")[match.get("start", 0):match.get("end", 0)] == "parse_invoice",
            match)

    s, body = c.dev._call("GET", "/api/search/paths", params={"q": "invoice.py"})
    r.check("go to file finds a path across repositories",
            s == 200 and [f["path"] for f in body["files"]] == ["src/invoice.py"], body)
    s, body = c.dev._call("GET", "/api/search/commits", params={"q": "100%"})
    r.check("commit search reads % literally", s == 200 and [x["message"] for x in body["commits"]] == ["Bill at 100% rate"], body)
    head = Repo(c.lead, c.billing, "lead").head("main")
    s, body = c.dev._call("GET", "/api/search/commits", params={"q": head[:10]})
    r.check("...and finds a commit by its id", s == 200 and [x["id"] for x in body["commits"]] == [head], body)


def scenario_index_follows(r, c):
    r.section("3  The index follows the default branch; patterns cannot stall the server")
    files = {"src/invoice.py": "def parse_invoice(total):\n    return round(total * 100)  # RoundedCents\n",
             "README.md": "Billing service\n"}
    push(c.lead, c.billing, "lead", "main", files, "Round cents")
    wait_indexed(c.lead)
    s, body = code(c.dev, q="RoundedCents")
    r.check("a push to main is searchable as soon as it is indexed", s == 200 and len(body["results"]) == 1, body)
    s, body = code(c.dev, q="return total * 100")
    r.check("...and the replaced text is gone", s == 200 and body["results"] == [], body)

    Api(c.base, c.lead.token).post(f"/api/repository/{c.billing}/branches", {"name": "feature", "from_branch": "main"})
    push(c.lead, c.billing, "lead", "feature", {**files, "src/tax.py": "VAT_ONLY_ON_FEATURE = 0.2\n"}, "tax")
    wait_indexed(c.lead)
    s, body = code(c.dev, q="VAT_ONLY_ON_FEATURE")
    r.check("another branch is not indexed (GitHub's rule: the default branch)", s == 200 and body["results"] == [], body)
    s, body = code(c.dev, q="VAT_ONLY_ON_FEATURE", repository_id=c.billing, branch="feature")
    r.check("...but searching that branch scans it", s == 200 and body["source"] == "scan" and len(body["results"]) == 1, body)

    push(c.lead, c.billing, "lead", "main", {**files, "big.txt": ("x" * 4000 + "\n") * 40}, "big file")
    box = {}

    def slow():
        started = time.monotonic()
        box["response"] = code(c.dev, q="(x+x+)+y", repository_id=c.billing, regex="true")
        box["seconds"] = time.monotonic() - started

    thread = threading.Thread(target=slow)
    thread.start()
    time.sleep(0.3)
    started = time.monotonic()
    me_status, _ = c.dev.get("/api/auth/me")
    me_seconds = time.monotonic() - started
    thread.join(60)
    s, body = box.get("response", (None, {}))
    r.check("a catastrophic regular expression stops at the time limit and says so",
            s == 200 and body.get("timed_out") is True and box.get("seconds", 99) < 10, (s, box.get("seconds")))
    r.check("...while the server keeps answering other requests", me_status == 200 and me_seconds < 2,
            f"{me_seconds:.2f}s")
    s, body = code(c.dev, q="x+y", regex="true")
    r.check("a regular expression across every repository is refused with the reason", s == 400 and "one repository" in str(body), body)


def scenario_index_rebuilt(r, c, workdir, port):
    r.section("4  The index is disposable")
    path = workdir / "search_index.db"
    _remove_index(path)
    r.check("the index files were deleted while the server was down", not path.exists())
    with live_server(workdir, port, index_env(workdir)) as base:
        lead = Api(base, session_of(base, "lead"))
        dev = Api(base, session_of(base, "dev"))
        status = wait_indexed(lead, expect=2)
        r.check("startup rebuilt it from the object store", status.get("repositories") == 2, status)
        s, body = code(dev, q="RoundedCents")
        r.check("...and searches work again", s == 200 and len(body["results"]) == 1, body)
        s, body = lead.post("/api/admin/search-index/rebuild")
        r.check("an administrator can rebuild it", s == 200 and body.get("queued") == 2, body)
        status = wait_indexed(lead, expect=2)
        r.check("...and it comes back complete", status.get("repositories") == 2, status)
        s, body = dev.post("/api/admin/search-index/rebuild")
        r.check("a developer cannot", s == 403, body)


def scenario_activity(r, c):
    r.section("5  The activity log")
    token = Api(c.base, c.lead.token).post("/api/tokens", {"name": "release-bot", "scopes": ["repo:write"]})[1]["token"]
    bot = Api(c.base, token)
    s, _b, commit_id = push(bot, c.billing, "lead", "main",
                            {"src/invoice.py": "def parse_invoice(total):\n    return total\n", "README.md": "Billing\n"},
                            "From the release bot")
    s, body = c.lead.get("/api/activities", params={"commit": commit_id})
    rows = body.get("activities", []) if s == 200 else []
    r.check("a push is one row, naming the branch and the commit", len(rows) == 1 and rows[0]["target_id"] == "main", rows)
    row = rows[0] if rows else {}
    r.check("...made with the token it names", row.get("credential") == "token:release-bot", row)
    r.check("...from the client's address, shown to administrators", row.get("client_ip") == "127.0.0.1", row)
    r.check("...with the branch move attached", (row.get("payload") or {}).get("ref", {}).get("after") == commit_id, row)

    s, body = c.dev.get("/api/activities", params={"limit": 500})
    text = json.dumps(body)
    r.check("a developer's feed shows nothing from the repository they cannot read",
            s == 200 and "payroll" not in text and "salary" not in text.lower(), body.get("pagination"))
    r.check("...and no client addresses", s == 200 and all("client_ip" not in a for a in body["activities"]))

    status, body = c.dev.post(f"/api/repository/{c.billing}/push", {
        "branch": "main",
        "commit": {"id": hashlib.sha1(b"queued" + str(time.time_ns()).encode()).hexdigest(), "author": "dev",
                   "message": "Developer fix", "parent": Repo(c.lead, c.billing, "lead").head("main"),
                   "files": {"src/invoice.py": b64("def parse_invoice(total):\n    return int(total)\n"),
                             "README.md": b64("Billing\n")}},
    })
    pending = body.get("commit_id")
    c.lead.post(f"/api/pending-commits/{pending}/review", {"action": "approve"})
    c.lead.put(f"/api/repository/{c.billing}/branches/feature/rename", {"new_name": "feature-2"})
    requests.delete(f"{c.base}/api/repository/{c.billing}/branches/feature-2",
                    headers={"Authorization": f"Bearer {c.lead.token}"}, timeout=30)
    s, body = c.lead.get(f"/api/repository/{c.billing}/activity", params={"limit": 200})
    kinds = [a["activity_type"] for a in body.get("activities", [])]
    for kind in ("pending_commit", "approve_commit", "branch_created", "branch_renamed", "branch_deleted", "push_commit"):
        r.check(f"'{kind}' is recorded", kind in kinds, kinds)

    s, body = c.lead.get("/api/activities", params={"type": "approve_commit", "user": "lead"})
    r.check("filters run on the server: by type and user", s == 200 and body["pagination"]["total"] == 1, body.get("pagination"))
    s, body = c.lead.get("/api/activities", params={"until": "2000-01-01"})
    r.check("...and by date", s == 200 and body["pagination"]["total"] == 0, body.get("pagination"))

    response = requests.get(f"{c.base}/api/activities/export", params={"repository_id": c.billing},
                            headers={"Authorization": f"Bearer {c.lead.token}"}, timeout=30)
    rows = list(csv.DictReader(io.StringIO(response.text))) if response.status_code == 200 else []
    r.check("an administrator exports the filtered log as CSV",
            response.status_code == 200 and rows and {"credential", "client_ip"} <= set(rows[0]), response.status_code)
    r.check("a developer cannot", requests.get(f"{c.base}/api/activities/export",
                                               headers={"Authorization": f"Bearer {c.dev.token}"}, timeout=30).status_code == 403)


def scenario_notifications(r, c):
    r.section("6  Notifications that point somewhere")
    s, body = c.dev.get("/api/notifications", params={"limit": 50})
    approved = [n for n in body.get("notifications", []) if n["type"] == "commit_approved"]
    r.check("the developer was told their queued commit was approved", len(approved) == 1, [n["type"] for n in body.get("notifications", [])])
    target = (approved[0]["payload"].get("target") if approved else None) or {}
    r.check("...with a target the client can open", target.get("kind") == "commit" and target.get("repository_id") == c.billing, target)

    Api(c.base, c.lead.token).post(f"/api/repository/{c.billing}/branches", {"name": "rounding", "from_branch": "main"})
    s, body = c.lead.post(f"/api/repository/{c.billing}/pull-requests", {
        "title": "Rounding", "description": "@dev and @outsider: thoughts?", "source_branch": "rounding",
        "target_branch": "main"})
    pr = body.get("pull_request", {}).get("id")
    s, body = c.dev.get("/api/notifications")
    mention = [n for n in body["notifications"] if n["type"] == "pr_mention"]
    r.check("a mention in a pull request reaches someone who can read it",
            mention and mention[0]["payload"]["target"] == {"kind": "pull_request", "repository_id": c.billing, "pr_id": pr}, mention)
    s, body = c.outsider.get("/api/notifications")
    r.check("...and not someone who cannot", not any(n["type"] == "pr_mention" for n in body["notifications"]))

    c.lead.put(f"/api/repository/{c.billing}/branch-policy", {"required_status_checks": ["ci/build"]})
    s, _b, head = push(c.lead, c.billing, "lead", "rounding",
                       {"src/invoice.py": "def parse_invoice(t):\n    return t\n", "README.md": "Billing\n"}, "wip")
    s, body = c.dev.post(f"/api/repository/{c.billing}/pull-requests", {
        "title": "Dev rounding", "description": "", "source_branch": "rounding", "target_branch": "main"})
    dev_pr = body.get("pull_request", {}).get("id")
    for _ in range(2):
        c.ci.post(f"/api/repository/{c.billing}/commits/{head}/statuses",
                  {"context": "ci/build", "state": "failure", "description": "7 tests failed"})
    s, body = c.dev.get("/api/notifications")
    failed = [n for n in body["notifications"] if n["type"] == "check_failed" and n["payload"]["pr_id"] == dev_pr]
    r.check("a failing required check tells the pull request's author, once", len(failed) == 1, failed)

    s, body = c.lead.post("/api/permissions/create", {"username": "outsider", "repo_id": c.billing,
                                                      "permission_level": "read", "granted_by": "dev"})
    s, body = c.outsider.get("/api/notifications")
    r.check("someone given access is told", any(n["type"] == "access_granted" for n in body["notifications"]))


def scenario_cli(r, c, root):
    r.section("7  The fox CLI")
    home, work = root / "cli-home", root / "cli-work"
    home.mkdir()
    work.mkdir()
    env = {"FOXNEST_TOKEN": c.dev.token, "FOXNEST_SERVER": c.base}
    code_, out = run_cli(work, home, ["search", "code", "parse_invoice", "--all"], env=env)
    r.check("fox search code --all prints repo:path:line for what dev can read",
            code_ == 0 and "billing:src/invoice.py:1:" in out and "payroll" not in out, out)
    code_, out = run_cli(work, home, ["search", "code", "NothingLikeThis", "--all"], env=env)
    r.check("...and exits 1 when nothing matches, like grep", code_ == 1 and "Nothing found." in out, out)
    code_, out = run_cli(work, home, ["search", "commits", "Round cents"], env=env)
    r.check("fox search commits", code_ == 0 and "Round cents" in out, out)
    code_, out = run_cli(work, home, ["activity", "--repo-id", c.billing, "--type", "approve_commit"],
                         env=env)
    r.check("fox activity filters on the server", code_ == 0 and "Approved dev's commit" in out, out)
    code_, out = run_cli(work, home, ["notifications", "--unread"], env=env)
    r.check("fox notifications lists them with where they point",
            code_ == 0 and "pull request #" in out and "unread" in out, out)
    code_, out = run_cli(work, home, ["notifications", "read", "--all"], env=env)
    r.check("fox notifications read --all", code_ == 0 and "0 unread" in out, out)


def scenario_upgrade(r, root, port):
    r.section("8  Upgrading a database from before the structured activity log")
    workdir = root / "upgrade"
    workdir.mkdir()
    with live_server(workdir, port, index_env(workdir)) as base:
        billing, _payroll = seed(workdir)
        lead = Api(base, session_of(base, "lead"))
        push(lead, billing, "lead", "main", {"a.txt": "UpgradeMarker\n"}, "first")
    with contextlib.closing(sqlite3.connect(workdir / "e2e.db")) as db:
        db.executescript("""
            CREATE TABLE old_activities (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                repository_id VARCHAR(16), activity_type VARCHAR(50) NOT NULL, description TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO old_activities SELECT id, user_id, repository_id, activity_type, description, created_at
                FROM activities;
            DROP TABLE activities;
            ALTER TABLE old_activities RENAME TO activities;
            -- A database that old is at the 2.0.1 baseline: the patches it lacks are in 0002.
            UPDATE alembic_version SET version_num = '0001_baseline';
        """)
        db.commit()
    _remove_index(workdir / "search_index.db")
    with live_server(workdir, port, index_env(workdir)) as base:
        with contextlib.closing(sqlite3.connect(workdir / "e2e.db")) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(activities)")]
            indexes = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")]
        wanted = {"target_type", "target_id", "commit_id", "payload_json", "credential", "client_ip",
                  "repository_name", "actor_username"}
        r.check("startup adds the activity log's new columns", wanted <= set(columns), columns)
        r.check("...and the indexes it is filtered by",
                {"ix_activities_repository_time", "ix_activities_user_time", "ix_activities_type_time"} <= set(indexes))
        lead = Api(base, session_of(base, "lead"))
        s, body = lead.get("/api/activities")
        r.check("rows from before the upgrade are still listed", s == 200 and body["pagination"]["total"] >= 1, body)
        wait_indexed(lead, expect=2)
        s, body = code(lead, q="UpgradeMarker")
        r.check("an existing server gets a search index on its first start", s == 200 and len(body["results"]) == 1, body)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="zanbeel_e2e_discovery_"))
    workdir = root / "server"
    workdir.mkdir()
    report = Report()
    port = args.port or free_port()
    try:
        report.section("1  Setup")
        c = Ctx()
        with live_server(workdir, port, index_env(workdir)) as base:
            c.base = base
            c.billing, c.payroll = seed(workdir)
            c.lead, c.dev, c.outsider, c.ci = (Api(base, session_of(base, u)) for u in ("lead", "dev", "outsider", "ci"))
            report.check("users and two repositories seeded; everyone signs in",
                         all(a.token for a in (c.lead, c.dev, c.outsider, c.ci)))
            push(c.lead, c.billing, "lead", "main",
                 {"src/invoice.py": "def parse_invoice(total):\n    return total * 100\n", "README.md": "Billing service\n"},
                 "Add invoice parser")
            push(c.lead, c.billing, "lead", "main",
                 {"src/invoice.py": "def parse_invoice(total):\n    return total * 100\n", "README.md": "Billing service\n"},
                 "Bill at 100% rate")
            push(c.lead, c.payroll, "lead", "main", {"src/salary.py": "def parse_invoice():\n    pass  # salary\n"},
                 "Payroll parser")

            scenario_code_search(report, c)
            scenario_index_follows(report, c)
        scenario_index_rebuilt(report, c, workdir, port)
        with live_server(workdir, port, index_env(workdir)) as base:
            c.base = base
            c.lead, c.dev, c.outsider, c.ci = (Api(base, session_of(base, u)) for u in ("lead", "dev", "outsider", "ci"))
            wait_indexed(c.lead, expect=2)
            scenario_activity(report, c)
            scenario_notifications(report, c)
            scenario_cli(report, c, root)
        scenario_upgrade(report, root, free_port())
    except Exception as exc:  # a crash is a failure, not a pass by omission
        import traceback
        traceback.print_exc()
        report.check(f"harness ran to completion ({type(exc).__name__})", False, str(exc))
    finally:
        if args.keep:
            print(f"\nKept: {root}")
        else:
            remove_tree(root)

    sys.exit(0 if report.summary() else 1)


if __name__ == "__main__":
    main()
