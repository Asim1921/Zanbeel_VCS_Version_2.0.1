"""The fox CLI's credentials, driven against the real API in-process.

The CLI's HTTP calls are routed to a FastAPI TestClient, so these exercise the actual
client code against the actual server code -- login, tokens, SSH keys, renewal --
without a network or a running server.

What they pin down:
* `fox login` sends the password once and saves a machine-named access token, never the
  password and never a session; logging in again retires the previous token;
* `fox logout` revokes the token on the server, not just on disk;
* creating credentials asks for the password even when signed in with a token;
* an SSH sign-in renews itself with the key when it runs out, and stops when the key
  is removed;
* a 401 on a PUT no longer crashes the CLI (it called a method that did not exist).
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import requests
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

SERVER_FILE = os.path.join(SERVER_ROOT, 'server.py')
_spec = importlib.util.spec_from_file_location('foxnest_server_cli_credentials', SERVER_FILE)
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402

from database.database import Base  # noqa: E402
from database.crud import UserCRUD  # noqa: E402
from database.models import (  # noqa: E402
    AccessToken, AccountLock, LoginAttempt, Notification, SSHChallenge, SSHKey,
)

FOX_PATH = Path(SERVER_ROOT).parent / "client" / "client1" / "fox.py"
_fox_spec = importlib.util.spec_from_file_location("fox_cli_credentials", FOX_PATH)
fox_module = importlib.util.module_from_spec(_fox_spec)
_fox_spec.loader.exec_module(fox_module)
FoxClient = fox_module.FoxClient

BASE = "http://zanbeel.test"
PASSWORD = "Password123!"


class RoutedRequests:
    """Stands in for the `requests` module inside fox.py, answering from a TestClient."""

    exceptions = requests.exceptions
    Response = requests.Response

    def __init__(self, client):
        self.client = client

    def _call(self, method, url, headers=None, json=None, params=None, timeout=None, **_):
        assert url.startswith(BASE), url
        return self.client.request(method, url[len(BASE):], headers=headers, json=json, params=params)

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def put(self, url, **kw):
        return self._call("PUT", url, **kw)

    def delete(self, url, **kw):
        return self._call("DELETE", url, **kw)


class CliCredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls._db_path = tempfile.mkstemp(prefix="foxnest_cli_credentials_", suffix=".db")
        os.close(fd)
        cls.engine = create_engine(f"sqlite:///{cls._db_path}")
        cls.Session = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)
        cls._startup = list(server_module.app.router.on_startup)
        server_module.app.router.on_startup = []

        def override_get_db():
            db = cls.Session()
            try:
                yield db
            finally:
                db.close()

        server_module.app.dependency_overrides[server_module.get_db] = override_get_db
        cls.client = TestClient(server_module.app)
        db = cls.Session()
        try:
            password_hash = server_module.hash_password(PASSWORD)
            UserCRUD.create_user(db, username="dev", role="developer", password_hash=password_hash,
                                 email="dev@example.com")
            UserCRUD.create_user(db, username="lead", role="team_lead", password_hash=password_hash,
                                 email="lead@example.com")
        finally:
            db.close()

    @classmethod
    def tearDownClass(cls):
        server_module.app.dependency_overrides.clear()
        server_module.app.router.on_startup = cls._startup
        cls.client.close()
        cls.engine.dispose()
        try:
            os.remove(cls._db_path)
        except OSError:
            pass

    def setUp(self):
        db = self.Session()
        for model in (AccessToken, SSHKey, SSHChallenge, Notification, LoginAttempt, AccountLock):
            db.query(model).delete()
        db.commit()
        db.close()

        self.dir = Path(tempfile.mkdtemp(prefix="fox_cli_cred_"))
        self.home = self.dir / "home"
        self.home.mkdir()
        self._cwd = os.getcwd()
        os.chdir(self.dir)
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home), "FOXNEST_SERVER": BASE}
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        os.environ.pop("FOXNEST_TOKEN", None)
        self._requests = mock.patch.object(fox_module, "requests", RoutedRequests(self.client))
        self._requests.start()

    def tearDown(self):
        self._requests.stop()
        self._env.stop()
        os.chdir(self._cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    # --- helpers -----------------------------------------------------------------

    def run_quiet(self, fn, *args, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = fn(*args, **kwargs)
        return result, out.getvalue()

    def creds(self):
        path = self.home / ".foxnest" / "credentials.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def me(self, token):
        return self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

    def login(self, username="dev", **kwargs):
        ok, out = self.run_quiet(FoxClient().login, username=username, password=PASSWORD, **kwargs)
        self.assertTrue(ok, out)
        return out

    def with_password(self, password=PASSWORD):
        return mock.patch.object(FoxClient, "_read_secret", lambda self, prompt: password)

    def write_ssh_key(self):
        private = ed25519.Ed25519PrivateKey.generate()
        ssh_dir = self.home / ".ssh"
        ssh_dir.mkdir(exist_ok=True)
        (ssh_dir / "id_ed25519").write_bytes(private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
        (ssh_dir / "id_ed25519.pub").write_text(private.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode() + " dev@laptop\n")
        return private

    def ssh_signed_in(self):
        self.write_ssh_key()
        self.login()
        with self.with_password():
            ok, out = self.run_quiet(FoxClient().ssh_key_add, "laptop")
        self.assertTrue(ok, out)
        ok, out = self.run_quiet(FoxClient().ssh_login)
        self.assertTrue(ok, out)
        return out

    # --- login / logout ---------------------------------------------------------------

    def test_login_saves_a_machine_token_and_never_the_password(self):
        out = self.login()
        creds = self.creds()
        self.assertEqual(creds["auth_method"], "token")
        self.assertTrue(creds["access_token"].startswith("fxp_"))
        self.assertEqual(creds["token_name"], f"fox CLI on {socket.gethostname()}")
        self.assertEqual(creds["scopes"], ["repo:write"])
        self.assertIsNotNone(creds["expires_at"])
        self.assertNotIn(PASSWORD, json.dumps(creds))
        self.assertIn("Your password was not saved", out)
        cred = self.me(creds["access_token"]).json()["credential"]
        self.assertEqual((cred["type"], cred["token_id"]), ("access_token", creds["token_id"]))

    def test_a_team_leads_machine_token_includes_admin(self):
        self.login("lead")
        self.assertEqual(self.creds()["scopes"], ["repo:write", "admin"])

    def test_login_scopes_and_expiry_can_be_narrowed(self):
        self.login("lead", scopes=["repo:read"], expires_in_days=0)
        creds = self.creds()
        self.assertEqual(creds["scopes"], ["repo:read"])
        self.assertNotIn("expires_at", creds)

    def test_logging_in_again_retires_the_previous_machine_token(self):
        self.login()
        first = self.creds()["access_token"]
        self.login()
        second = self.creds()["access_token"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.me(first).status_code, 401)
        self.assertEqual(self.me(second).status_code, 200)

    def test_a_wrong_password_saves_nothing(self):
        ok, out = self.run_quiet(FoxClient().login, username="dev", password="wrong")
        self.assertFalse(ok)
        self.assertIn("Invalid username or password", out)
        self.assertEqual(self.creds(), {})

    def test_logout_revokes_the_token_on_the_server(self):
        self.login()
        token = self.creds()["access_token"]
        ok, out = self.run_quiet(FoxClient().logout)
        self.assertTrue(ok, out)
        self.assertIn("Revoked this machine's token", out)
        self.assertEqual(self.me(token).status_code, 401)
        self.assertNotIn("access_token", self.creds())
        self.assertEqual(self.creds().get("username"), "dev")

    def test_whoami_names_the_credential(self):
        self.login()
        ok, out = self.run_quiet(FoxClient().whoami)
        self.assertTrue(ok, out)
        self.assertIn("dev (developer)", out)
        self.assertIn(f"access token 'fox CLI on {socket.gethostname()}'", out)
        self.assertIn("repo:write", out)

    def test_whoami_reports_a_revoked_token(self):
        self.login()
        token_id = self.creds()["token_id"]
        db = self.Session()
        db.query(AccessToken).filter(AccessToken.id == token_id).update({"revoked_at": datetime.utcnow()})
        db.commit()
        db.close()
        ok, out = self.run_quiet(FoxClient().whoami)
        self.assertFalse(ok)
        self.assertIn("rejected the saved credential", out)

    # --- tokens -------------------------------------------------------------------------

    def test_token_create_needs_the_password_and_leaves_the_machine_token_alone(self):
        self.login()
        machine = self.creds()["access_token"]
        with self.with_password():
            ok, out = self.run_quiet(FoxClient().token_create, "ci", scopes=["repo:read"], expires_in_days=7)
        self.assertTrue(ok, out)
        self.assertIn("needs your password", out)
        created = next(line.strip() for line in out.splitlines() if line.strip().startswith("fxp_"))
        self.assertEqual(self.me(created).json()["credential"]["scopes"], ["repo:read"])
        self.assertEqual(self.creds()["access_token"], machine)

        with self.with_password("wrong"):
            ok, out = self.run_quiet(FoxClient().token_create, "ci2")
        self.assertFalse(ok)

    def test_token_use_checks_the_token_before_saving_it(self):
        self.login()
        with self.with_password():
            _, out = self.run_quiet(FoxClient().token_create, "ci", scopes=["repo:read"])
        created = next(line.strip() for line in out.splitlines() if line.strip().startswith("fxp_"))
        ok, out = self.run_quiet(FoxClient().token_use, created)
        self.assertTrue(ok, out)
        self.assertEqual((self.creds()["access_token"], self.creds()["token_name"]), (created, "ci"))
        ok, _ = self.run_quiet(FoxClient().token_use, "fxp_not-a-real-token")
        self.assertFalse(ok)
        self.assertEqual(self.creds()["access_token"], created)

    def test_token_list_marks_this_machine(self):
        self.login()
        ok, out = self.run_quiet(FoxClient().token_list)
        self.assertTrue(ok, out)
        self.assertIn("(this machine)", out)

    def test_revoking_this_machines_token_signs_it_out(self):
        self.login()
        token, token_id = self.creds()["access_token"], self.creds()["token_id"]
        ok, out = self.run_quiet(FoxClient().token_revoke, token_id)
        self.assertTrue(ok, out)
        self.assertEqual(self.me(token).status_code, 401)
        self.assertNotIn("access_token", self.creds())

    def test_a_read_only_machine_can_revoke_its_own_token(self):
        """Revoking by id is a write; the CLI must use /current for its own token."""
        self.login(scopes=["repo:read"])
        token, token_id = self.creds()["access_token"], self.creds()["token_id"]
        ok, out = self.run_quiet(FoxClient().token_revoke, token_id)
        self.assertTrue(ok, out)
        self.assertEqual(self.me(token).status_code, 401)

    def test_revoking_another_token_asks_for_the_password(self):
        self.login()
        with self.with_password():
            _, out = self.run_quiet(FoxClient().token_create, "old-ci")
        created = next(line.strip() for line in out.splitlines() if line.strip().startswith("fxp_"))
        token_id = self.me(created).json()["credential"]["token_id"]
        with self.with_password():
            ok, out = self.run_quiet(FoxClient().token_revoke, token_id)
        self.assertTrue(ok, out)
        self.assertIn("needs your password", out)
        self.assertEqual(self.me(created).status_code, 401)

    def test_foxnest_token_overrides_the_saved_credential(self):
        self.login()
        with self.with_password():
            _, out = self.run_quiet(FoxClient().token_create, "ci", scopes=["repo:read"])
        created = next(line.strip() for line in out.splitlines() if line.strip().startswith("fxp_"))
        with mock.patch.dict(os.environ, {"FOXNEST_TOKEN": created}):
            self.assertEqual(FoxClient().get_auth_headers(), {"Authorization": f"Bearer {created}"})
            ok, out = self.run_quiet(FoxClient().whoami)
        self.assertTrue(ok, out)
        self.assertIn("access token 'ci'", out)
        self.assertIn("Source:     FOXNEST_TOKEN", out)

    # --- SSH keys -------------------------------------------------------------------------

    def test_ssh_add_and_login(self):
        out = self.ssh_signed_in()
        self.assertIn("Signed in as dev with id_ed25519", out)
        creds = self.creds()
        self.assertEqual(creds["auth_method"], "ssh")
        self.assertEqual(Path(creds["ssh_key_path"]).name, "id_ed25519")
        cred = self.me(creds["access_token"]).json()["credential"]
        self.assertEqual(cred["type"], "ssh_key")
        ok, out = self.run_quiet(FoxClient().ssh_key_list)
        self.assertTrue(ok, out)
        self.assertIn("(this machine)", out)

    def test_an_expired_ssh_sign_in_renews_itself(self):
        self.ssh_signed_in()
        old = self.creds()["access_token"]
        creds = self.creds()
        creds["expires_at"] = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
        (self.home / ".foxnest" / "credentials.json").write_text(json.dumps(creds), encoding="utf-8")

        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            headers = FoxClient().get_auth_headers()
        new = headers["Authorization"].split(" ", 1)[1]
        self.assertNotEqual(new, old)
        self.assertIn("Renewed SSH sign-in", out.getvalue())
        self.assertEqual(self.me(new).status_code, 200)

    def test_a_rejected_ssh_sign_in_signs_again_on_401(self):
        self.ssh_signed_in()
        old = self.creds()["access_token"]
        db = self.Session()
        db.query(AccessToken).filter(AccessToken.ssh_key_id.isnot(None)).delete()
        db.commit()
        db.close()
        response, _ = self.run_quiet(FoxClient()._authed_get, f"{BASE}/api/auth/me")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self.creds()["access_token"], old)

    def test_removing_the_key_ends_the_sign_in_and_its_renewal(self):
        self.ssh_signed_in()
        key_id = self.creds()["ssh_key_id"]
        token = self.creds()["access_token"]
        with self.with_password():
            ok, out = self.run_quiet(FoxClient().ssh_key_remove, key_id)
        self.assertTrue(ok, out)
        self.assertIn("This machine was signed in with that key", out)
        self.assertEqual(self.me(token).status_code, 401)
        ok, _ = self.run_quiet(FoxClient().ssh_login)
        self.assertFalse(ok)

    def test_ssh_add_needs_the_password_even_when_signed_in(self):
        self.write_ssh_key()
        self.login()
        with self.with_password("wrong"):
            ok, out = self.run_quiet(FoxClient().ssh_key_add, "laptop")
        self.assertFalse(ok)
        db = self.Session()
        self.assertEqual(db.query(SSHKey).count(), 0)
        db.close()

    # --- plumbing ------------------------------------------------------------------------

    def test_a_401_on_put_no_longer_crashes(self):
        """_authed_put called self.prompt_login_and_retry(), which never existed."""
        self.login()
        db = self.Session()
        db.query(AccessToken).update({"revoked_at": datetime.utcnow()})
        db.commit()
        db.close()
        response, out = self.run_quiet(FoxClient()._authed_put, f"{BASE}/api/profile", json={"bio": "x"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("was rejected", out)

    def test_the_password_is_read_from_stdin_when_not_a_terminal(self):
        fake_stdin = io.StringIO("piped-secret\n")
        with mock.patch.object(fox_module.sys, "stdin", fake_stdin):
            self.assertEqual(FoxClient()._read_secret("Password: "), "piped-secret")

    def test_the_login_command_exits_non_zero_on_failure(self):
        argv = ["fox", "login", "--username", "dev", "--password", "wrong"]
        with mock.patch.object(fox_module.sys, "argv", argv), self.assertRaises(SystemExit) as ctx:
            self.run_quiet(fox_module.main)
        self.assertEqual(ctx.exception.code, 1)

    def test_the_whoami_command(self):
        self.login()
        with mock.patch.object(fox_module.sys, "argv", ["fox", "whoami"]):
            _, out = self.run_quiet(fox_module.main)
        self.assertIn("User:       dev", out)


if __name__ == "__main__":
    unittest.main()
