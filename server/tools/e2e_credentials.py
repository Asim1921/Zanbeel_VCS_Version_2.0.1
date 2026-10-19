#!/usr/bin/env python3
"""End-to-end check of scoped access tokens and SSH-key sign-in against a live server.

    ..\\venv\\Scripts\\python.exe tools/e2e_credentials.py [--keep] [--port N]

Starts a real uvicorn server on a throwaway database and blob store, then drives the
real fox CLI as a subprocess -- each "machine" with its own home directory -- using
keys made by the real ssh-keygen. Nothing touches the development database.

Each check prints PASS or FAIL; the run ends with a count and exits non-zero on any
failure, so it can gate a pull request.

Scenarios
  1  Setup
  2  fox login: the password is exchanged for a machine token, and pushes use it
  3  A read-only token on a build agent, handed in through FOXNEST_TOKEN
  4  Revocation: one token, and logout
  5  SSH keys made by ssh-keygen: register, sign in, push, renew, scope, remove
  6  A token cannot turn itself into more than it is
  7  An administrator revokes everything one person holds
  8  The older CLI bundled with the VS Code extension still signs in with a key
  9  Upgrading a database from before key scopes
"""

import argparse
import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_merge_conflicts import (  # noqa: E402  (shared harness plumbing)
    Api, Report, free_port, live_server, remove_tree, server_env,
)

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
CLI = REPO_ROOT / "client" / "client1" / "fox.py"
OLD_CLI = REPO_ROOT / "vscode-extension" / "cli" / "fox.py"
PASSWORD = "E2e-Passw0rd!long"
HOST = socket.gethostname()


def find_ssh_keygen():
    import shutil
    found = shutil.which("ssh-keygen")
    if found:
        return found
    bundled = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh-keygen.exe"
    return str(bundled) if bundled.exists() else None


KEYGEN = find_ssh_keygen()


def seed(workdir):
    """Users and one repository. There is no public endpoint to create a user without
    an approval flow, so this one step goes to the database; the rest goes over HTTP."""
    script = f"""
import sys
sys.path.insert(0, {str(SERVER_ROOT)!r})
from database.database import SessionLocal
from database.crud import UserCRUD, RepositoryCRUD
from app.core.security import hash_password
db = SessionLocal()
for name, role in (("lead", "team_lead"), ("dev", "developer")):
    UserCRUD.create_user(db, username=name, role=role, password_hash=hash_password({PASSWORD!r}),
                         email=name + "@e2e.test")
repo = RepositoryCRUD.create_repository(db, "dev", "cred-app", "credentials e2e")
print(repo.id)
"""
    out = subprocess.run([sys.executable, "-c", script], cwd=SERVER_ROOT, env=server_env(workdir),
                         capture_output=True, text=True, encoding="utf-8")
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return out.stdout.strip().splitlines()[-1]


def run_cli(cwd, home, args, stdin="", env=None, cli=CLI):
    """One fox invocation on one 'machine' (its own home directory)."""
    e = dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONIOENCODING="utf-8")
    e.pop("FOXNEST_TOKEN", None)
    e.pop("FOXNEST_SERVER", None)
    e.update(env or {})
    result = subprocess.run([sys.executable, str(cli), *args], cwd=cwd, env=e, input=stdin,
                            capture_output=True, text=True, encoding="utf-8", timeout=300)
    return result.returncode, result.stdout + result.stderr


def creds(home):
    path = home / ".foxnest" / "credentials.json"
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def keygen(path, kind="ed25519", bits=None):
    args = [KEYGEN, "-q", "-t", kind, "-N", "", "-f", str(path), "-C", f"{path.name}@e2e"]
    if bits:
        args[2:2] = ["-b", str(bits)]
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL, capture_output=True)
    return path


def token_in(output):
    return next((w for w in output.split() if w.startswith("fxp_")), None)


def me(base, token):
    return requests.get(f"{base}/api/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=30)


def notifications(workdir, username):
    with contextlib.closing(sqlite3.connect(workdir / "e2e.db")) as db:
        rows = db.execute(
            "SELECT n.notification_type FROM notifications n JOIN users u ON u.id = n.user_id "
            "WHERE u.username = ?", (username,)).fetchall()
    return [row[0] for row in rows]


class Machine:
    """A working copy plus a home directory: what one laptop or build agent has."""

    def __init__(self, root, name, base, repo_id, env=None, workdir=None):
        self.workdir = workdir  # the server's directory, for checking what it stored
        self.home = root / f"{name}-home"
        self.work = root / f"{name}-work"
        self.home.mkdir()
        self.work.mkdir()
        self.base, self.repo_id, self.env = base, repo_id, env or {}

    def fox(self, *args, stdin="", env=None, cli=CLI):
        return run_cli(self.work, self.home, list(args), stdin=stdin, env={**self.env, **(env or {})}, cli=cli)

    def init(self, username):
        self.fox("init", "--username", username, "--repo-name", "cred-app")
        self.fox("set", "origin", self.base)
        config_path = self.work / ".fox" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["repo_id"] = self.repo_id
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    def commit(self, name, text):
        (self.work / name).write_text(text, encoding="utf-8")
        self.fox("add", name)
        return self.fox("commit", "-m", f"{name}: {text.strip()}")

    def push(self):
        return self.fox("push")


def server_head(base, session, repo_id, branch="main"):
    status, body = Api(base, session).get(f"/api/repository/{repo_id}/branches")
    for row in (body or {}).get("branches", []) if status == 200 else []:
        if row["name"] == branch:
            return row["head_commit_id"]
    return None


def local_head(machine):
    commits = json.loads((machine.work / ".fox" / "commits.json").read_text(encoding="utf-8"))
    return commits[-1]["id"] if commits else None


def server_has_commit(workdir, commit_id):
    """Did the server accept this commit? A developer's push lands in the review queue
    (pending_commits) rather than on the branch, so either table counts."""
    with contextlib.closing(sqlite3.connect(workdir / "e2e.db")) as db:
        for table in ("commits", "pending_commits"):
            if db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (commit_id,)).fetchone():
                return True
    return False


def session_of(base, username):
    status, body = Api(base).post("/api/auth/login", {"username": username, "password": PASSWORD})
    return body.get("access_token") if status == 200 else None


# --- scenarios -------------------------------------------------------------------------

def scenario_login(r, base, laptop, dev_session):
    r.section("2  fox login: the password is exchanged for a machine token")
    laptop.init("dev")
    code, out = laptop.fox("login", "--username", "dev", "--password", PASSWORD)
    saved = creds(laptop.home)
    r.check("fox login succeeds", code == 0, out)
    r.check("...and saves an access token, not a session",
            saved.get("auth_method") == "token" and str(saved.get("access_token", "")).startswith("fxp_"), saved)
    r.check("...named after this machine", saved.get("token_name") == f"fox CLI on {HOST}", saved)
    r.check("...with repo:write and a 90-day expiry",
            saved.get("scopes") == ["repo:write"] and bool(saved.get("expires_at")), saved)
    r.check("the password is not in the credentials file",
            PASSWORD not in (laptop.home / ".foxnest" / "credentials.json").read_text(encoding="utf-8"))
    status, body = Api(base, dev_session).get("/api/tokens")
    r.check("the token is listed under the account's access tokens",
            any(t["name"] == f"fox CLI on {HOST}" for t in body.get("tokens", [])), body)

    code, out = laptop.fox("whoami")
    r.check("fox whoami names the token and its scopes",
            code == 0 and f"access token 'fox CLI on {HOST}'" in out and "repo:write" in out, out)

    laptop.commit("README.md", "hello from the laptop\n")
    code, out = laptop.push()
    r.check("fox push with the machine token is accepted by the server",
            code == 0 and server_has_commit(laptop.workdir, local_head(laptop)), out)

    first = saved["access_token"]
    code, out = laptop.fox("login", "--username", "dev", "--password", PASSWORD)
    r.check("logging in again replaces the machine token", code == 0 and creds(laptop.home)["access_token"] != first, out)
    r.check("...and the old one stops working", me(base, first).status_code == 401)


def scenario_build_agent(r, base, laptop, agent):
    r.section("3  A read-only token on a build agent, through FOXNEST_TOKEN")
    code, out = laptop.fox("token", "create", "ci-read", "--scope", "repo:read",
                           "--expires-in-days", "7", stdin=PASSWORD + "\n")
    token = token_in(out)
    r.check("fox token create asks for the password (piped on stdin) and prints the token once",
            code == 0 and "needs your password" in out and token is not None, out)
    r.check("...without replacing the laptop's own credential",
            creds(laptop.home)["access_token"] != token)

    code, out = laptop.fox("token", "create", "nope", stdin="wrong-password\n")
    r.check("a wrong password creates nothing", code != 0 and token_in(out) is None, out)

    # What a CI job is given: the token from its secret store and the server address.
    agent.env = {"FOXNEST_TOKEN": token, "FOXNEST_SERVER": base}
    code, out = agent.fox("whoami")
    r.check("the agent is signed in from FOXNEST_TOKEN + FOXNEST_SERVER alone",
            code == 0 and "access token 'ci-read'" in out and "Source:     FOXNEST_TOKEN" in out, out)
    r.check("...and nothing was written to its home directory", not (agent.home / ".foxnest").exists())

    status, body = Api(base, token).get(f"/api/repository/{agent.repo_id}/branches")
    r.check("the read-only token can read the repository", status == 200, body)

    agent.init("dev")
    agent.commit("agent.txt", "should not land\n")
    code, out = agent.push()
    r.check("a push with the read-only token is refused, and says why",
            "read-only" in out and not server_has_commit(agent.workdir, local_head(agent)), out)
    return token


def scenario_revocation(r, base, laptop, agent, ci_token):
    r.section("4  Revocation: one token, and logout")
    token_id = me(base, ci_token).json()["credential"]["token_id"]
    code, out = laptop.fox("token", "revoke", str(token_id), stdin=PASSWORD + "\n")
    r.check("fox token revoke (another token: asks for the password)", code == 0 and "revoked" in out, out)
    code, out = agent.fox("whoami")
    r.check("the agent is locked out at once", code != 0 and "rejected" in out, out)
    code, out = laptop.fox("whoami")
    r.check("...while the laptop is not", code == 0, out)

    machine_token = creds(laptop.home)["access_token"]
    code, out = laptop.fox("logout")
    r.check("fox logout revokes the machine token on the server",
            code == 0 and "Revoked this machine's token" in out and me(base, machine_token).status_code == 401, out)
    r.check("...and forgets it locally", "access_token" not in creds(laptop.home))


def scenario_ssh(r, base, laptop, root, dev_session, workdir):
    r.section("5  SSH keys made by ssh-keygen")
    if not KEYGEN:
        r.check("ssh-keygen is installed", False, "install OpenSSH to run this scenario")
        return
    keygen(laptop.home / ".ssh" / "id_ed25519")
    laptop.fox("login", "--username", "dev", "--password", PASSWORD)

    code, out = laptop.fox("ssh", "add", "laptop", stdin=PASSWORD + "\n")
    r.check("fox ssh add registers ~/.ssh/id_ed25519.pub (asks for the password)",
            code == 0 and "fingerprint: SHA256:" in out and "repo:write" in out, out)
    r.check("the owner is notified of the new key", "credential_ssh_key_added" in notifications(workdir, "dev"))

    code, out = laptop.fox("logout")
    code, out = laptop.fox("login", "--ssh")
    saved = creds(laptop.home)
    r.check("fox login --ssh signs in with no password",
            code == 0 and saved.get("auth_method") == "ssh" and str(saved.get("access_token", "")).startswith("fxp_"), out)
    code, out = laptop.fox("whoami")
    r.check("fox whoami says it is an SSH sign-in with key 'laptop'",
            "SSH sign-in with key 'laptop'" in out and "renewed automatically" in out, out)

    laptop.commit("ssh.txt", "pushed over an SSH sign-in\n")
    code, out = laptop.push()
    r.check("fox push works on the SSH sign-in",
            code == 0 and server_has_commit(workdir, local_head(laptop)), out)

    old = saved["access_token"]
    saved["expires_at"] = (datetime.now(timezone.utc).replace(tzinfo=None)
                         - timedelta(minutes=1)).isoformat()
    (laptop.home / ".foxnest" / "credentials.json").write_text(json.dumps(saved), encoding="utf-8")
    code, out = laptop.fox("whoami")
    renewed = creds(laptop.home)["access_token"]
    r.check("an expired sign-in renews itself by signing again with the key",
            code == 0 and "Renewed SSH sign-in" in out and renewed != old and me(base, renewed).status_code == 200, out)

    status, body = Api(base, dev_session).get("/api/tokens")
    r.check("SSH sign-ins are not listed as personal tokens",
            not any(t["name"].startswith("SSH sign-in") for t in body.get("tokens", [])), body)

    # A read-only RSA key, signed with RSA by ssh-keygen's own key file.
    rsa_key = keygen(root / "keys" / "deploy_rsa", "rsa", 3072)
    code, out = laptop.fox("logout")
    laptop.fox("login", "--username", "dev", "--password", PASSWORD)
    code, out = laptop.fox("ssh", "add", "deploy", "--key", str(rsa_key) + ".pub",
                           "--scope", "repo:read", stdin=PASSWORD + "\n")
    r.check("a second, read-only RSA key is registered", code == 0 and "repo:read" in out, out)
    code, out = laptop.fox("login", "--ssh", "--key", str(rsa_key))
    r.check("signing in with the RSA key works", code == 0 and creds(laptop.home).get("scopes") == ["repo:read"], out)
    laptop.commit("deploy.txt", "a deploy key must not push\n")
    code, out = laptop.push()
    r.check("...and that sign-in cannot push", "read-only" in out, out)

    code, out = laptop.fox("ssh", "list")
    r.check("fox ssh list shows both keys with their scopes",
            code == 0 and "laptop" in out and "deploy" in out and "repo:read" in out, out)

    rsa_token = creds(laptop.home)["access_token"]
    rsa_key_id = creds(laptop.home)["ssh_key_id"]
    code, out = laptop.fox("ssh", "remove", str(rsa_key_id), stdin=PASSWORD + "\n")
    r.check("fox ssh remove (asks for the password)", code == 0 and "removed" in out, out)
    r.check("...ends the sign-in it made, at once", me(base, rsa_token).status_code == 401)
    code, out = laptop.fox("login", "--ssh", "--key", str(rsa_key))
    r.check("...and the key can no longer sign in", code != 0, out)


def scenario_escalation(r, base, dev_session):
    r.section("6  A token cannot turn itself into more than it is")
    status, body = Api(base, dev_session).post("/api/tokens", {"name": "probe", "scopes": ["repo:write"]})
    token = body.get("token")
    api = Api(base, token)
    status, body = api.post("/api/tokens", {"name": "child"})
    r.check("it cannot mint another token", status == 403, body)
    status, body = api.post("/api/ssh-keys", {"title": "x", "public_key": "ssh-ed25519 AAAA"})
    r.check("it cannot register an SSH key", status == 403, body)
    status, body = api.put("/api/profile", {"email": "attacker@evil.test"})
    r.check("it cannot change the account's email", status == 403, body)
    status, body = api.post("/api/auth/change-password",
                            {"current_password": PASSWORD, "new_password": "Taken-over-123"})
    r.check("it cannot change the password", status == 403, body)

    lead_session = session_of(base, "lead")
    status, body = Api(base, lead_session).post("/api/tokens", {"name": "lead-ci", "scopes": ["repo:write"]})
    lead_write = Api(base, body.get("token"))
    status, body = lead_write.post("/api/webhooks", {"url": "https://exfil.example/hook"})
    r.check("a team lead's repo:write token cannot create a server-wide webhook", status == 403, body)
    status, body = lead_write.put("/api/users/dev", {"full_name": "Renamed"})
    r.check("...or edit another user's account", status == 403, body)
    status, body = Api(base, lead_session).post("/api/webhooks", {"url": "https://hooks.example/ok"})
    r.check("the same lead signed in with a password can", status == 200, body)


def scenario_admin(r, base, laptop, workdir):
    r.section("7  An administrator revokes everything one person holds")
    # --key: `fox login --ssh` reuses the last key, and the last one was just removed.
    code, out = laptop.fox("login", "--ssh", "--key", str(laptop.home / ".ssh" / "id_ed25519"))
    r.check("the laptop signs in with its remaining key", code == 0, out)
    ssh_token = creds(laptop.home)["access_token"]
    dev_session = session_of(base, "dev")
    status, body = Api(base, dev_session).post("/api/tokens", {"name": "spare", "scopes": ["repo:read"]})
    spare = body.get("token")

    lead = Api(base, session_of(base, "lead"))
    status, body = lead.get("/api/admin/credentials")
    entry = next((u for u in body.get("users", []) if u["user"]["username"] == "dev"), None) if status == 200 else None
    r.check("the admin view lists dev's tokens and key", entry is not None and entry["ssh_keys"] and entry["tokens"], body)
    r.check("...and shows no secret", spare not in json.dumps(body) and "token_hash" not in json.dumps(body))

    status, body = lead.post("/api/admin/users/dev/credentials/revoke")
    r.check("Revoke all succeeds", status == 200 and body.get("removed_ssh_keys") == 1, body)
    r.check("every token dev held stops working",
            me(base, spare).status_code == 401 and me(base, ssh_token).status_code == 401)
    code, out = laptop.fox("whoami")
    r.check("the laptop's SSH sign-in cannot renew (the key is gone)", code != 0, out)
    r.check("dev is told", "credentials_revoked" in notifications(workdir, "dev"))
    r.check("dev can still sign in with the password", session_of(base, "dev") is not None)


def scenario_old_cli(r, base, root):
    r.section("8  The older CLI bundled with the VS Code extension still signs in")
    if not (KEYGEN and OLD_CLI.exists()):
        r.check("prerequisites present", False, "needs ssh-keygen and vscode-extension/cli/fox.py")
        return
    old = Machine(root, "old-cli", base, None)
    key = keygen(old.home / ".ssh" / "id_ed25519")
    session = session_of(base, "dev")
    Api(base, session).post("/api/ssh-keys", {"title": "old-cli",
                                              "public_key": Path(str(key) + ".pub").read_text().strip()})
    old.fox("set", "origin", base, "--global", cli=OLD_CLI)
    cfg_dir = old.home / ".foxnest"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "credentials.json").write_text(json.dumps({"username": "dev", "origin_url": base}))
    code, out = old.fox("ssh", "login", "--username", "dev", "--key", str(key), cli=OLD_CLI)
    token = creds(old.home).get("access_token")
    r.check("its raw-signature sign-in is still accepted", code == 0 and token and me(base, token).status_code == 200, out)
    r.check("...and yields a scoped, key-bound token, not a full session",
            token and me(base, token).json()["credential"]["type"] == "ssh_key")


def scenario_upgrade(r, root, port):
    r.section("9  Upgrading a database from before key scopes")
    if not KEYGEN:
        r.check("ssh-keygen is installed", False)
        return
    workdir = root / "upgrade"
    workdir.mkdir()
    with live_server(workdir, port):  # creates today's schema
        seed(workdir)
    machine = Machine(root, "upgrade", None, None)
    key = keygen(machine.home / ".ssh" / "id_ed25519")
    pub = Path(str(key) + ".pub").read_text().strip()
    fingerprint = subprocess.run([KEYGEN, "-lf", str(key) + ".pub"], capture_output=True,
                                 text=True).stdout.split()[1]
    # Rebuild both tables in their pre-scope shape, with a key already registered.
    with contextlib.closing(sqlite3.connect(workdir / "e2e.db")) as db:
        db.executescript("""
            DROP TABLE IF EXISTS access_tokens;
            DROP TABLE IF EXISTS ssh_keys;
            CREATE TABLE access_tokens (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                name VARCHAR(100) NOT NULL, prefix VARCHAR(16) NOT NULL,
                token_hash VARCHAR(64) NOT NULL UNIQUE, scopes VARCHAR(200) NOT NULL DEFAULT 'repo:read',
                expires_at TIMESTAMP, last_used_at TIMESTAMP, revoked_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE ssh_keys (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                title VARCHAR(100) NOT NULL, key_type VARCHAR(30) NOT NULL, public_key TEXT NOT NULL,
                fingerprint VARCHAR(80) NOT NULL UNIQUE, last_used_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            -- A database that old is at the 2.0.1 baseline: the patches it lacks are in 0002.
            UPDATE alembic_version SET version_num = '0001_baseline';
        """)
        dev_id = db.execute("SELECT id FROM users WHERE username = 'dev'").fetchone()[0]
        db.execute("INSERT INTO ssh_keys (user_id, title, key_type, public_key, fingerprint) "
                   "VALUES (?, 'pre-upgrade', 'ssh-ed25519', ?, ?)", (dev_id, pub, fingerprint))
        db.commit()

    with live_server(workdir, port) as base:
        with contextlib.closing(sqlite3.connect(workdir / "e2e.db")) as db:
            key_cols = [row[1] for row in db.execute("PRAGMA table_info(ssh_keys)")]
            token_cols = [row[1] for row in db.execute("PRAGMA table_info(access_tokens)")]
            scopes = db.execute("SELECT scopes FROM ssh_keys").fetchone()[0]
        r.check("startup adds ssh_keys.scopes and access_tokens.ssh_key_id",
                "scopes" in key_cols and "ssh_key_id" in token_cols, (key_cols, token_cols))
        r.check("the existing key gets repo:write", scopes == "repo:write", scopes)
        cfg_dir = machine.home / ".foxnest"
        cfg_dir.mkdir()
        (cfg_dir / "credentials.json").write_text(json.dumps({"username": "dev", "origin_url": base}))
        code, out = run_cli(machine.work, machine.home, ["login", "--ssh"])
        r.check("the pre-upgrade key signs in after the upgrade",
                code == 0 and creds(machine.home).get("scopes") == ["repo:write"], out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="zanbeel_e2e_credentials_"))
    workdir = root / "server"
    workdir.mkdir()
    report = Report()
    try:
        report.section("1  Setup")
        with live_server(workdir, args.port or free_port()) as base:
            # The server creates the schema on startup; seed only once it has.
            repo_id = seed(workdir)
            report.check("users and a repository seeded", bool(repo_id))
            dev_session = session_of(base, "dev")
            report.check("password sign-in works", dev_session is not None)
            laptop = Machine(root, "laptop", base, repo_id, workdir=workdir)
            agent = Machine(root, "agent", base, repo_id, workdir=workdir)

            scenario_login(report, base, laptop, dev_session)
            ci_token = scenario_build_agent(report, base, laptop, agent)
            scenario_revocation(report, base, laptop, agent, ci_token)
            scenario_ssh(report, base, laptop, root, dev_session, workdir)
            scenario_escalation(report, base, dev_session)
            scenario_admin(report, base, laptop, workdir)
            scenario_old_cli(report, base, root)
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
