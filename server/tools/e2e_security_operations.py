#!/usr/bin/env python3
"""End-to-end check of Security and Operations against a live server.

    ..\\venv\\Scripts\\python.exe tools/e2e_security_operations.py [--keep] [--port N]

Starts a real uvicorn server on a throwaway database, blob store, backup folder and
docs folder. Drives sign-in limits with real concurrent requests (addresses simulated
with X-Forwarded-For, which the server is told to trust), takes and test-restores
backups over HTTP, restores one for real with tools/backup.py while the server is
stopped and starts the server on the result, upgrades databases from before Alembic,
refuses one it cannot serve, and pages a 210-commit history over HTTP and through the
real fox CLI. Nothing touches the development database.

Each check prints PASS or FAIL; the run ends with a count and exits non-zero on any
failure, so it can gate a pull request.

Scenarios
  1  Setup
  2  Sign-in: concurrent guesses, an address limit, an account lock, the same answer for everyone
  3  Backups over HTTP: a job, verified by reading back, linked, restore-drilled
  4  A real restore: the server stopped, tools/backup.py --restore, the server started on it
  5  Upgrades: a database from before Alembic, data that breaks a rule, a newer database
  6  Paging: a 210-commit history over HTTP and through fox pull / log / push
  7  The CLI: fox admin, and the older CLI bundled with the VS Code extension
"""

import argparse
import contextlib
import hashlib
import json
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_merge_conflicts import (  # noqa: E402  (shared harness plumbing)
    Api, Report, b64, free_port, live_server, remove_tree, server_env,
)
from e2e_credentials import OLD_CLI, run_cli  # noqa: E402

SERVER_ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "E2e-Passw0rd!long"
LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
HEAD = "0003_security_and_operations"


def ops_env(workdir):
    return {
        "FOXNEST_TRUST_PROXY": "1",
        "FOXNEST_BACKUP_DIR": str(workdir / "backups"),
        "FOXNEST_DOCS_DIR": str(workdir / "docs"),
        # The startup log is read while the server runs.
        "PYTHONUNBUFFERED": "1",
    }


def full_env(workdir):
    return {**server_env(workdir), **ops_env(workdir)}


def seed(workdir):
    script = f"""
import sys
sys.path.insert(0, {str(SERVER_ROOT)!r})
from database.database import SessionLocal
from database.crud import UserCRUD, RepositoryCRUD, UserPermissionCRUD
from app.core.security import hash_password
db = SessionLocal()
for name, role in (("admin", "admin"), ("lead", "team_lead"), ("dev", "developer")):
    UserCRUD.create_user(db, username=name, role=role, password_hash=hash_password({PASSWORD!r}),
                         email=name + "@e2e.test")
repo = RepositoryCRUD.create_repository(db, "lead", "ops-app", "security and operations e2e")
UserPermissionCRUD.create_permission(db, "dev", repo.id, "write", "lead")
print(repo.id)
"""
    out = subprocess.run([sys.executable, "-c", script], cwd=SERVER_ROOT, env=full_env(workdir),
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return out.stdout.strip().splitlines()[-1]


def login(base, username, password=PASSWORD, ip="192.0.2.1"):
    started = time.perf_counter()
    response = requests.post(f"{base}/api/auth/login", json={"username": username, "password": password},
                             headers={"X-Forwarded-For": ip}, timeout=60)
    return response, time.perf_counter() - started


def session(base, username, ip="192.0.2.250"):
    response, _ = login(base, username, ip=ip)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def push(api, repo_id, files, message, branch="main"):
    status, body = api.get(f"/api/repository/{repo_id}/branches")
    head = next((b["head_commit_id"] for b in (body or {}).get("branches", []) if b["name"] == branch), None)
    commit_id = hashlib.sha1(f"{repo_id}{message}{time.time_ns()}".encode()).hexdigest()
    status, body = api.post(f"/api/repository/{repo_id}/push", {
        "branch": branch,
        "commit": {"id": commit_id, "author": "lead", "message": message, "parent": head,
                   "parents": [head] if head else [],
                   "files": {path: b64(data) for path, data in files.items()}},
    })
    assert status == 200, body
    return commit_id


def main_head(api, repo_id):
    status, body = api.get(f"/api/repository/{repo_id}/branches")
    return next((b["head_commit_id"] for b in (body or {}).get("branches", []) if b["name"] == "main"), None)


def wait_backup(api, backup_id, field="status", seconds=300):
    deadline = time.time() + seconds
    while time.time() < deadline:
        _s, body = api.get(f"/api/admin/backups/{backup_id}")
        row = (body or {}).get("backup") or {}
        if row.get(field) not in ("running", None):
            return row
        time.sleep(0.5)
    return {}


def tool(workdir, *args):
    out = subprocess.run([sys.executable, str(SERVER_ROOT / "tools" / "backup.py"), *args], cwd=SERVER_ROOT,
                         env=full_env(workdir), capture_output=True, text=True, encoding="utf-8", timeout=900)
    return out.returncode, out.stdout + out.stderr


def db_query(workdir, sql, *params):
    with contextlib.closing(sqlite3.connect(workdir / "e2e.db")) as db:
        return db.execute(sql, params).fetchall()


def db_exec(workdir, script):
    with contextlib.closing(sqlite3.connect(workdir / "e2e.db")) as db:
        db.executescript(script)
        db.commit()


def server_log(workdir):
    try:
        return (workdir / "server.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def refuses_to_start(workdir, port):
    try:
        with live_server(workdir, port, ops_env(workdir)):
            return False
    except RuntimeError:
        return True


# --- scenarios -------------------------------------------------------------------------

def scenario_sign_in(r, base, workdir):
    r.section("2  Sign-in: concurrent guesses, an address limit, an account lock, one answer")

    def guess(_):
        response, _t = login(base, "dev", "wrong-password", ip="198.51.100.7")
        return response.status_code, response.headers.get("Retry-After"), response.json().get("detail", "")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(guess, range(20)))
    reached = sum(1 for s, _ra, _d in results if s == 401)
    refused = [(s, ra, d) for s, ra, d in results if s == 429]
    r.check("20 concurrent wrong passwords: at most 5 reach the password check", reached <= 5,
            [s for s, _ra, _d in results])
    r.check("...and the rest are refused with 429", reached + len(refused) == 20, results)
    r.check("...each saying how long to wait, in Retry-After and in words",
            refused and all(ra and int(ra) > 0 and "Try again in" in d for _s, ra, d in refused), refused[:2])

    response, _t = login(base, "dev", ip="198.51.100.7")
    r.check("the right password from that address is refused while it is throttled",
            response.status_code == 429, response.text)
    response, _t = login(base, "dev", ip="198.51.100.8")
    r.check("...but the owner signs in from their own address", response.status_code == 200, response.text)

    unknown = [login(base, f"nobody{i}", "wrong-password", ip=f"203.0.113.{i}") for i in range(4)]
    wrong = [login(base, "dev", "wrong-password", ip=f"203.0.113.{10 + i}") for i in range(4)]
    r.check("an unknown username and a wrong password get the same answer",
            {x.status_code for x, _ in unknown + wrong} == {401}
            and len({x.json()["detail"] for x, _ in unknown + wrong}) == 1)
    slow_unknown = statistics.median(t for _, t in unknown)
    slow_wrong = statistics.median(t for _, t in wrong)
    r.check("...and take about as long (no timing oracle for which names exist)",
            0.5 < slow_unknown / slow_wrong < 2.0, f"unknown {slow_unknown:.3f}s, wrong {slow_wrong:.3f}s")

    for i in range(20):
        login(base, "lead", "wrong-password", ip=f"100.64.0.{i}")
    response, _t = login(base, "lead", ip="100.64.1.1")
    r.check("guessing from twenty addresses locks the account itself",
            response.status_code == 429 and "locked" in response.json()["detail"], response.text)
    admin = Api(base, session(base, "admin"))
    _s, body = admin.get("/api/admin/security/lockouts")
    lock = next((x for x in body.get("locked_accounts", []) if x["username"] == "lead"), None)
    r.check("the lockout view shows it, with its lock count", lock is not None and lock["lock_count"] == 1, body)
    status, body = admin.post("/api/admin/security/unlock", {"username": "lead"})
    r.check("an administrator unlocks it", status == 200 and body.get("was_locked"), body)
    response, _t = login(base, "lead", ip="100.64.1.1")
    r.check("...and the owner signs in", response.status_code == 200, response.text)
    lead = Api(base, response.json()["access_token"])
    _s, body = lead.get("/api/notifications")
    r.check("the owner was told their account was locked",
            any(n.get("type") == "account_locked" or n.get("notification_type") == "account_locked"
                for n in body.get("notifications", [])), body)
    _s, body = admin.get("/api/activities", params={"type": "account_locked,account_unlocked"})
    kinds = {row.get("activity_type") for row in body.get("activities", [])}
    r.check("the lock and the unlock are in the activity log", {"account_locked", "account_unlocked"} <= kinds, kinds)

    home, work = workdir / "cli-home", workdir / "cli-work"
    home.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    (home / ".foxnest").mkdir(exist_ok=True)
    (home / ".foxnest" / "credentials.json").write_text(json.dumps({"username": "dev", "origin_url": base}))
    outputs = [run_cli(work, home, ["login", "--username", "dev", "--password", "not-it"],
                       env={"FOXNEST_SERVER": base}) for _ in range(6)]
    code, out = outputs[-1]
    r.check("fox login, throttled, says how long to wait", code != 0 and "Try again in" in out, out[-400:])
    admin.post("/api/admin/security/unlock", {"ip_address": "127.0.0.1"})


def scenario_backups(r, base, workdir, repo_id):
    r.section("3  Backups over HTTP: a job, verified by reading back, linked, restore-drilled")
    admin = Api(base, session(base, "admin"))
    lead = Api(base, session(base, "lead"))
    push(lead, repo_id, {"src/app.py": "print('backed up')\n", "assets/logo.png": bytes(range(256)) * 8},
         "before the backup")
    (workdir / "docs" / repo_id).mkdir(parents=True, exist_ok=True)
    (workdir / "docs" / repo_id / "README.md").write_text("# Generated\n", encoding="utf-8")

    status, body = admin.post("/api/admin/backups", {"label": "e2e-first"})
    first = body.get("backup") or {}
    r.check("a backup starts as a job (202, running)", status == 202 and first.get("status") == "running", body)
    first = wait_backup(admin, first.get("id"))
    r.check("...and finishes verified", first.get("status") == "complete" and first.get("verified"), first)
    manifest_path = Path(first.get("path", "")) / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    r.check("its manifest records the checksum, integrity, schema and documents",
            manifest.get("format") == 2 and manifest["database"]["integrity"] == "ok"
            and manifest["schema_revision"] == HEAD and manifest["docs"]["count"] == 1, manifest.get("database"))

    status, body = admin.post("/api/admin/backups?wait=true", {"label": "e2e-second", "test_restore": True})
    second = body.get("backup") or {}
    r.check("the next backup links every unchanged blob instead of copying it",
            second.get("linked_blobs") == first.get("blob_count") and second.get("verified"), second)
    drill = second.get("restore_test_report") or {}
    r.check("...and its restore drill passes: restored, upgraded, read back",
            second.get("restore_test_status") == "passed" and drill.get("schema_healthy")
            and drill.get("head_files_checked", 0) >= 2, drill)
    _s, body = admin.get("/api/admin/backups")
    summary = body.get("summary") or {}
    r.check("the summary shows a fresh verified backup and the drill",
            summary.get("stale") is False and (summary.get("last_restore_test") or {}).get("status") == "passed",
            summary)
    code, out = tool(workdir, "--verify", str(second["id"]))
    r.check("tools/backup.py --verify re-reads it while the server runs",
            code == 0 and re.search(r"verified\s+: yes", out), out[-600:])
    return second, main_head(lead, repo_id)


def scenario_restore(r, port, workdir, repo_id, backup, head_at_backup):
    r.section("4  A real restore: the server stopped, tools/backup.py --restore, started on it")
    with live_server(workdir, port, ops_env(workdir)) as base:
        lead = Api(base, session(base, "lead"))
        after = push(lead, repo_id, {"src/app.py": "print('after the backup')\n"}, "after the backup")
    code, out = tool(workdir, "--restore", str(backup["id"]), "--yes")
    r.check("tools/backup.py --restore restores with the server stopped", code == 0, out[-800:])
    moved = list(workdir.glob("e2e.db.replaced-*"))
    r.check("...moving the database it replaced aside, not deleting it", len(moved) == 1, out[-400:])
    with live_server(workdir, port, ops_env(workdir)) as base:
        lead = Api(base, session(base, "lead"))
        r.check("the server starts on the restored database at the backup's head",
                main_head(lead, repo_id) == head_at_backup, main_head(lead, repo_id))
        status, _ = lead.get(f"/api/repository/{repo_id}/commits/{after}/changes")
        r.check("...the commit made after the backup is gone", status == 404, status)
        response = lead.raw(f"/api/repository/{repo_id}/file", path="assets/logo.png", branch="main")
        content = (response.json().get("file") or {}).get("content") if response.status_code == 200 else None
        r.check("...and a binary file from before reads back byte for byte (its blob was restored)",
                content == b64(bytes(range(256)) * 8), response.text[:300])
        admin = Api(base, session(base, "admin"))
        _s, body = admin.get("/api/admin/schema")
        r.check("...with a healthy schema", body["schema"]["healthy"], body["schema"])


def scenario_upgrades(r, port, workdir):
    r.section("5  Upgrades: a database from before Alembic, data that breaks a rule, a newer one")
    db_exec(workdir, """
        DROP TABLE alembic_version;
        DROP TABLE schema_migration_runs;
        CREATE TABLE old_attempts (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL,
            ip_address VARCHAR(64), success BOOLEAN NOT NULL DEFAULT 0, user_agent VARCHAR(300),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO old_attempts SELECT id, username, ip_address, success, user_agent, created_at FROM login_attempts;
        DROP TABLE login_attempts;
        ALTER TABLE old_attempts RENAME TO login_attempts;
    """)
    snapshots_before = set((workdir / "backups" / "pre-upgrade").glob("*.sqlite3"))
    with live_server(workdir, port, ops_env(workdir)) as base:
        log = server_log(workdir)
        r.check("a database from before Alembic is upgraded at startup",
                "no revision recorded" in log and HEAD in log, log[-800:])
        columns = [row[1] for row in db_query(workdir, "PRAGMA table_info(login_attempts)")]
        r.check("...its old table gains the new column", "kind" in columns, columns)
        snapshots = set((workdir / "backups" / "pre-upgrade").glob("*.sqlite3")) - snapshots_before
        r.check("...after a copy of it was taken", len(snapshots) == 1, snapshots)
        admin = Api(base, session(base, "admin"))
        _s, body = admin.get("/api/admin/schema")
        runs = body.get("migrations") or []
        r.check("...and the upgrade is on the record",
                body["schema"]["healthy"] and runs and runs[0]["from_revision"] is None
                and runs[0]["snapshot_path"], runs[:1])

    repo = db_query(workdir, "SELECT id FROM repositories LIMIT 1")[0][0]
    commit = db_query(workdir, "SELECT id FROM commits LIMIT 1")[0][0]
    db_exec(workdir, f"""
        DROP INDEX ix_immutable_releases_repo_name;
        INSERT INTO immutable_releases (repository_id, name, commit_id) VALUES ('{repo}', 'v9', '{commit}');
        INSERT INTO immutable_releases (repository_id, name, commit_id) VALUES ('{repo}', 'v9', '{commit}');
        UPDATE alembic_version SET version_num = '0002_startup_patches';
    """)
    r.check("data that breaks a uniqueness rule stops the server starting", refuses_to_start(workdir, port))
    log = server_log(workdir)
    r.check("...and the log names the rule", "SCHEMA DRIFT" in log and "missing unique rule" in log, log[-800:])
    db_exec(workdir, "DELETE FROM immutable_releases WHERE rowid NOT IN "
                     "(SELECT min(rowid) FROM immutable_releases GROUP BY repository_id, name);"
                     "UPDATE alembic_version SET version_num = '0002_startup_patches';")
    with live_server(workdir, port, ops_env(workdir)):
        indexes = [row[0] for row in db_query(workdir, "SELECT name FROM sqlite_master WHERE type='index'")]
        r.check("once the duplicate is resolved it starts and builds the rule",
                "ix_immutable_releases_repo_name" in indexes)

    db_exec(workdir, "UPDATE alembic_version SET version_num = '0099_from_a_newer_server';")
    r.check("a database upgraded by a newer server is refused", refuses_to_start(workdir, port))
    r.check("...saying so", "newer version" in server_log(workdir), server_log(workdir)[-600:])
    db_exec(workdir, f"UPDATE alembic_version SET version_num = '{HEAD}';")


def scenario_paging(r, base, workdir, repo_id):
    r.section("6  Paging: a 210-commit history over HTTP and through fox pull / log / push")
    lead = Api(base, session(base, "lead"))
    _s, body = lead.get(f"/api/repository/{repo_id}/commits", params={"limit": 1})
    already = body["pagination"]["total"]
    for i in range(210 - already):
        push(lead, repo_id, {"log.txt": f"line {i}\n"}, f"commit {i}")
    response = lead.raw(f"/api/repository/{repo_id}/commits")
    body = response.json()
    r.check("a default page is 200 of 210, and says there is more",
            body["pagination"]["total"] == 210 and len(body["commits"]) == 200 and body["pagination"]["has_more"],
            body["pagination"])
    r.check("...in X-Total-Count and a Link header too",
            response.headers.get("X-Total-Count") == "210" and LINK_NEXT.search(response.headers.get("Link", "")),
            dict(response.headers))
    seen = [c["id"] for c in body["commits"]]
    nxt = LINK_NEXT.search(response.headers["Link"]).group(1)
    rest = requests.get(base + nxt, headers={"Authorization": f"Bearer {lead.token}"}, timeout=60).json()
    seen += [c["id"] for c in rest["commits"]]
    r.check("following Link: next gives the other 10, none twice", len(seen) == 210 == len(set(seen)), len(seen))

    home, work = workdir / "pager-home", workdir / "pager-work"
    home.mkdir()
    work.mkdir()
    env = {"FOXNEST_TOKEN": lead.token, "FOXNEST_SERVER": base}
    run_cli(work, home, ["init", "--username", "lead", "--repo-name", "ops-app"], env=env)
    run_cli(work, home, ["set", "origin", base], env=env)
    config_path = work / ".fox" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["repo_id"] = repo_id
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    code, out = run_cli(work, home, ["pull"], env=env)
    r.check("fox pull fetches the history", code == 0, out[-400:])
    code, out = run_cli(work, home, ["log"], env=env)
    lines = [line for line in out.splitlines() if line.startswith("Commit: ")]
    r.check("fox log shows all 210 commits, not the first page of 200", len(lines) == 210, f"{len(lines)} commits")
    (work / "new.txt").write_text("one more\n", encoding="utf-8")
    run_cli(work, home, ["add", "new.txt"], env=env)
    run_cli(work, home, ["commit", "-m", "one more"], env=env)
    code, out = run_cli(work, home, ["push"], env=env)
    _s, body = lead.get(f"/api/repository/{repo_id}/commits", params={"limit": 1})
    r.check("fox push sends only the new commit (the server had all the others)",
            code == 0 and body["pagination"]["total"] == 211, out[-600:])


def scenario_cli(r, base, workdir):
    r.section("7  The CLI: fox admin, and the older CLI bundled with the VS Code extension")
    admin_token = session(base, "admin")
    home, work = workdir / "admin-home", workdir / "admin-work"
    home.mkdir()
    work.mkdir()
    env = {"FOXNEST_TOKEN": admin_token, "FOXNEST_SERVER": base}
    code, out = run_cli(work, home, ["admin", "backup", "--label", "from-cli", "--drill"], env=env)
    r.check("fox admin backup --drill takes, verifies and test-restores a backup",
            code == 0 and "verified: yes" in out and "drill: passed" in out, out[-600:])
    code, out = run_cli(work, home, ["admin", "backups"], env=env)
    r.check("fox admin backups lists it", code == 0 and "from-cli" in out, out[-400:])
    code, out = run_cli(work, home, ["admin", "schema"], env=env)
    r.check("fox admin schema reports the revision", code == 0 and HEAD in out, out[-400:])
    code, out = run_cli(work, home, ["admin", "lockouts"], env=env)
    r.check("fox admin lockouts reads the policy", code == 0 and "Policy:" in out, out[-400:])

    old_home = workdir / "old-home"
    old_home.mkdir()
    (old_home / ".foxnest").mkdir()
    (old_home / ".foxnest" / "credentials.json").write_text(json.dumps({
        "username": "admin", "origin_url": base, "access_token": admin_token, "auth_method": "session"}))
    for command in ("lockouts", "backups", "schema"):
        code, out = run_cli(work, old_home, ["admin", command], env={"FOXNEST_SERVER": base}, cli=OLD_CLI)
        r.check(f"the older CLI's 'admin {command}' still works", code == 0, out[-400:])


# --- main ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="zanbeel_e2e_secops_"))
    workdir = root / "server"
    workdir.mkdir()
    (workdir / "docs").mkdir()
    report = Report()
    port = args.port or free_port()
    try:
        with live_server(workdir, port, ops_env(workdir)) as base:
            report.section("1  Setup")
            repo_id = seed(workdir)
            report.check("server started on a throwaway database at the current revision",
                         db_query(workdir, "SELECT version_num FROM alembic_version")[0][0] == HEAD)
            scenario_sign_in(report, base, workdir)
            backup, head = scenario_backups(report, base, workdir, repo_id)
        scenario_restore(report, port, workdir, repo_id, backup, head)
        scenario_upgrades(report, port, workdir)
        with live_server(workdir, port, ops_env(workdir)) as base:
            scenario_paging(report, base, workdir, repo_id)
            scenario_cli(report, base, workdir)
    finally:
        if args.keep:
            print(f"\nkept: {root}")
        else:
            remove_tree(root)
    sys.exit(0 if report.summary() else 1)


if __name__ == "__main__":
    main()
