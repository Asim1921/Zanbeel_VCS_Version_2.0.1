"""Scoped, revocable credentials over HTTP: where a token's limits actually hold.

The token and key services were already tested on their own. What was not tested was
the path around them, and that is where the holes were:

* a token could mint another token (outliving its own expiry and revocation),
  register an SSH key and trade it for an unscoped session, or change the account's
  email and take the account through a password reset;
* an admin's repo:write token could configure server-wide webhooks or edit other
  people's accounts, because those routes checked the role and never the scope;
* an SSH sign-in returned a session with the account's full powers, which nothing
  could revoke -- removing the key did not end it.
"""
import base64
import importlib.util
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

SERVER_FILE = os.path.join(SERVER_ROOT, 'server.py')
_spec = importlib.util.spec_from_file_location('foxnest_server_credentials', SERVER_FILE)
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa  # noqa: E402

from database.database import Base  # noqa: E402
from database.crud import UserCRUD  # noqa: E402
from database.models import (  # noqa: E402
    AccessToken, AccountLock, LoginAttempt, Notification, SSHChallenge, SSHKey, User,
)
from app.db import migrations  # noqa: E402

FOX_PATH = Path(SERVER_ROOT).parent / "client" / "client1" / "fox.py"
_fox_spec = importlib.util.spec_from_file_location("fox_cli_for_credentials_api", FOX_PATH)
fox = importlib.util.module_from_spec(_fox_spec)
_fox_spec.loader.exec_module(fox)

PASSWORD = "Password123!"
CREDENTIAL_HEADER = "x-zanbeel-credential-required"


def ed25519_key():
    private = ed25519.Ed25519PrivateKey.generate()
    line = private.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    return private, line


class CredentialApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls._db_path = tempfile.mkstemp(prefix="foxnest_credentials_", suffix=".db")
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
            for name, role in (("lead", "team_lead"), ("dev", "developer"), ("other", "developer")):
                UserCRUD.create_user(db, username=name, role=role, password_hash=password_hash,
                                     email=f"{name}@example.com")
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
        # Every test starts from the same accounts. Without the password and lockout
        # reset, one test that (wrongly) manages to change a password would lock that
        # account for every test after it, burying the real failure under 429s.
        db = self.Session()
        try:
            db.query(AccessToken).delete()
            db.query(SSHKey).delete()
            db.query(SSHChallenge).delete()
            db.query(Notification).delete()
            db.query(LoginAttempt).delete()
            db.query(AccountLock).delete()
            password_hash = server_module.hash_password(PASSWORD)
            for user in db.query(User).all():
                user.role = {"lead": "team_lead"}.get(user.username, "developer")
                user.email = f"{user.username}@example.com"
                user.is_active = True
                user.password_hash = password_hash
            db.commit()
        finally:
            db.close()
        self._sessions = {}

    # --- helpers -----------------------------------------------------------------

    @staticmethod
    def h(token):
        return {"Authorization": f"Bearer {token}"}

    def session(self, username):
        if username not in self._sessions:
            response = self.client.post("/api/auth/login",
                                        json={"username": username, "password": PASSWORD})
            self.assertEqual(response.status_code, 200, response.text)
            self._sessions[username] = response.json()["access_token"]
        return self._sessions[username]

    def token(self, username, scopes, name="t", days=None):
        body = {"name": name, "scopes": scopes}
        if days:
            body["expires_in_days"] = days
        response = self.client.post("/api/tokens", json=body, headers=self.h(self.session(username)))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"], response.json()["access_token"]["id"]

    def add_key(self, username, line, scopes=None, title="laptop"):
        body = {"title": title, "public_key": line}
        if scopes:
            body["scopes"] = scopes
        response = self.client.post("/api/ssh-keys", json=body, headers=self.h(self.session(username)))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["key"]

    def ssh_sign_in(self, username, private, namespace=fox.SSHSIG_NAMESPACE, legacy=False):
        nonce = self.client.post("/api/auth/ssh/challenge", json={"username": username}).json()["nonce"]
        if legacy:
            signature = base64.b64encode(private.sign(nonce.encode())).decode()
        else:
            signature = fox.sshsig_sign_with_key(private, nonce.encode(), namespace=namespace)
        return self.client.post("/api/auth/ssh/verify", json={"nonce": nonce, "signature": signature})

    def assert_needs_password(self, response):
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.headers.get(CREDENTIAL_HEADER), "password")
        self.assertIn("password sign-in", response.json()["detail"])

    # --- a token cannot manage credentials or identity ------------------------------

    def test_a_token_cannot_create_a_token(self):
        write, _ = self.token("dev", ["repo:write"])
        self.assert_needs_password(
            self.client.post("/api/tokens", json={"name": "child", "scopes": ["repo:write"]},
                             headers=self.h(write)))

    def test_an_admin_token_cannot_create_a_token_either(self):
        """Even the strongest token: a credential must not be able to mint its successor."""
        admin, _ = self.token("lead", ["admin"])
        self.assert_needs_password(
            self.client.post("/api/tokens", json={"name": "child"}, headers=self.h(admin)))

    def test_a_token_cannot_register_or_remove_an_ssh_key(self):
        """Otherwise: add your own key, sign in with it, and walk away with a fresh credential."""
        write, _ = self.token("dev", ["repo:write"])
        _, line = ed25519_key()
        self.assert_needs_password(
            self.client.post("/api/ssh-keys", json={"title": "x", "public_key": line},
                             headers=self.h(write)))
        key = self.add_key("dev", line)
        self.assert_needs_password(self.client.delete(f"/api/ssh-keys/{key['id']}", headers=self.h(write)))

    def test_a_token_cannot_change_the_password(self):
        write, _ = self.token("dev", ["repo:write"])
        self.assert_needs_password(self.client.post(
            "/api/auth/change-password",
            json={"current_password": PASSWORD, "new_password": "another-password"},
            headers=self.h(write)))

    def test_a_token_cannot_change_the_email(self):
        """The email is where password resets go."""
        write, _ = self.token("dev", ["repo:write"])
        self.assert_needs_password(self.client.put(
            "/api/profile", json={"email": "attacker@example.com"}, headers=self.h(write)))
        self.assert_needs_password(self.client.put(
            "/api/users/dev", json={"email": "attacker@example.com"}, headers=self.h(write)))
        # Unchanged email, other fields: fine from a token.
        response = self.client.put("/api/profile", json={"email": "dev@example.com", "bio": "hi"},
                                   headers=self.h(write))
        self.assertEqual(response.status_code, 200, response.text)
        # And a password session may change it.
        response = self.client.put("/api/profile", json={"email": "dev2@example.com"},
                                   headers=self.h(self.session("dev")))
        self.assertEqual(response.status_code, 200, response.text)

    def test_an_ssh_sign_in_cannot_manage_credentials(self):
        private, line = ed25519_key()
        self.add_key("dev", line)
        ssh_token = self.ssh_sign_in("dev", private).json()["access_token"]
        self.assert_needs_password(
            self.client.post("/api/tokens", json={"name": "x"}, headers=self.h(ssh_token)))

    def test_any_token_can_revoke_itself(self):
        """`fox logout` with a read-only token must still revoke it."""
        read, _ = self.token("dev", ["repo:read"])
        response = self.client.delete("/api/tokens/current", headers=self.h(read))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["access_token"]["revoked"])
        self.assertEqual(self.client.get("/api/auth/me", headers=self.h(read)).status_code, 401)

    def test_a_password_session_has_no_current_token(self):
        response = self.client.delete("/api/tokens/current", headers=self.h(self.session("dev")))
        self.assertEqual(response.status_code, 400)

    def test_revoking_another_token_needs_a_password(self):
        """A stolen token must not be able to lock its owner's other machines out."""
        first, first_id = self.token("dev", ["repo:write"], name="first")
        second, second_id = self.token("dev", ["repo:write"], name="second")
        self.assert_needs_password(self.client.delete(f"/api/tokens/{second_id}", headers=self.h(first)))
        self.assertEqual(self.client.get("/api/auth/me", headers=self.h(second)).status_code, 200)
        # A token may revoke itself by id, and a session may revoke any.
        self.assertEqual(self.client.delete(f"/api/tokens/{first_id}", headers=self.h(first)).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/tokens/{second_id}",
                                            headers=self.h(self.session("dev"))).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me", headers=self.h(second)).status_code, 401)

    def test_another_users_token_id_is_not_found(self):
        _, lead_token_id = self.token("lead", ["repo:read"])
        response = self.client.delete(f"/api/tokens/{lead_token_id}", headers=self.h(self.session("dev")))
        self.assertEqual(response.status_code, 404)

    # --- scopes ----------------------------------------------------------------------

    def test_a_read_only_token_cannot_write(self):
        read, _ = self.token("dev", ["repo:read"])
        response = self.client.post("/api/repository/create",
                                    json={"username": "dev", "repo_name": "nope"}, headers=self.h(read))
        self.assertEqual(response.status_code, 403)
        self.assertIn("read-only", response.json()["detail"])
        self.assertEqual(self.client.get("/api/tokens", headers=self.h(read)).status_code, 200)

    def test_a_developer_cannot_grant_admin(self):
        response = self.client.post("/api/tokens", json={"name": "x", "scopes": ["admin"]},
                                    headers=self.h(self.session("dev")))
        self.assertEqual(response.status_code, 400)
        _, line = ed25519_key()
        response = self.client.post("/api/ssh-keys",
                                    json={"title": "x", "public_key": line, "scopes": ["admin"]},
                                    headers=self.h(self.session("dev")))
        self.assertEqual(response.status_code, 400)

    def test_global_webhooks_need_the_admin_scope(self):
        """A team lead's repo:write token is a repository credential, not an admin one."""
        write, _ = self.token("lead", ["repo:write"])
        admin, _ = self.token("lead", ["repo:write", "admin"])
        hook = {"url": "https://hooks.example.com/zanbeel"}
        response = self.client.post("/api/webhooks", json=hook, headers=self.h(write))
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.client.get("/api/webhooks", headers=self.h(write)).status_code, 403)
        self.assertEqual(self.client.post("/api/webhooks", json=hook, headers=self.h(admin)).status_code, 200)
        self.assertEqual(self.client.post("/api/webhooks", json=hook,
                                          headers=self.h(self.session("lead"))).status_code, 200)

    def test_global_server_hooks_need_the_admin_scope(self):
        write, _ = self.token("lead", ["repo:write"])
        admin, _ = self.token("lead", ["admin"])
        self.assertEqual(self.client.get("/api/server-hooks", headers=self.h(write)).status_code, 403)
        self.assertEqual(self.client.get("/api/server-hooks", headers=self.h(admin)).status_code, 200)

    def test_editing_another_user_needs_the_admin_scope(self):
        write, _ = self.token("lead", ["repo:write"])
        admin, _ = self.token("lead", ["admin"])
        self.assertEqual(self.client.put("/api/users/dev", json={"full_name": "X"},
                                         headers=self.h(write)).status_code, 403)
        self.assertEqual(self.client.put("/api/users/dev", json={"full_name": "X"},
                                         headers=self.h(admin)).status_code, 200)
        # Changing someone's email stays a password-session act even for an admin token.
        self.assert_needs_password(self.client.put("/api/users/dev", json={"email": "x@example.com"},
                                                   headers=self.h(admin)))

    def test_creating_a_repository_for_someone_else_needs_the_admin_scope(self):
        write, _ = self.token("lead", ["repo:write"])
        admin, _ = self.token("lead", ["repo:write", "admin"])
        body = {"username": "dev", "repo_name": "for-dev"}
        self.assertEqual(self.client.post("/api/repository/create", json=body,
                                          headers=self.h(write)).status_code, 403)
        self.assertNotEqual(self.client.post("/api/repository/create", json=body,
                                             headers=self.h(admin)).status_code, 403)

    def test_admin_endpoints_need_the_admin_scope(self):
        write, _ = self.token("lead", ["repo:write"])
        self.assertEqual(self.client.get("/api/admin/credentials", headers=self.h(write)).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/credentials",
                                         headers=self.h(self.session("dev"))).status_code, 403)

    # --- /api/auth/me says what you are signed in with ----------------------------------

    def test_me_describes_the_credential(self):
        me = self.client.get("/api/auth/me", headers=self.h(self.session("dev"))).json()
        self.assertEqual(me["credential"]["type"], "session")

        read, token_id = self.token("dev", ["repo:read"], name="ci-runner", days=7)
        cred = self.client.get("/api/auth/me", headers=self.h(read)).json()["credential"]
        self.assertEqual(cred["type"], "access_token")
        self.assertEqual((cred["name"], cred["token_id"], cred["scopes"]), ("ci-runner", token_id, ["repo:read"]))
        self.assertIsNotNone(cred["expires_at"])

        private, line = ed25519_key()
        key = self.add_key("dev", line, title="work-laptop")
        ssh_token = self.ssh_sign_in("dev", private).json()["access_token"]
        cred = self.client.get("/api/auth/me", headers=self.h(ssh_token)).json()["credential"]
        self.assertEqual(cred["type"], "ssh_key")
        self.assertEqual(cred["ssh_key"]["fingerprint"], key["fingerprint"])

    # --- SSH sign-in --------------------------------------------------------------------

    def test_ssh_sign_in_returns_a_short_lived_token_scoped_like_the_key(self):
        private, line = ed25519_key()
        key = self.add_key("dev", line, scopes=["repo:read"])
        response = self.ssh_sign_in("dev", private)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertTrue(data["access_token"].startswith("fxp_"))
        self.assertEqual(data["scopes"], ["repo:read"])
        self.assertEqual(data["signature_format"], "sshsig")
        expires = datetime.fromisoformat(data["expires_at"])
        self.assertLess(abs((expires - datetime.utcnow()) - timedelta(hours=8)), timedelta(minutes=5))

        token = data["access_token"]
        self.assertEqual(self.client.get("/api/tokens", headers=self.h(token)).status_code, 200)
        refused = self.client.post("/api/repository/create", json={"username": "dev", "repo_name": "x"},
                                   headers=self.h(token))
        self.assertEqual(refused.status_code, 403)
        self.assertIn("SSH key is read-only", refused.json()["detail"])

        # It is not listed among personal tokens, and the key shows one live sign-in.
        tokens = self.client.get("/api/tokens", headers=self.h(self.session("dev"))).json()["tokens"]
        self.assertEqual(tokens, [])
        keys = self.client.get("/api/ssh-keys", headers=self.h(self.session("dev"))).json()["keys"]
        self.assertEqual((keys[0]["id"], keys[0]["active_sessions"], keys[0]["scopes"]),
                         (key["id"], 1, ["repo:read"]))

    def test_keys_default_to_repo_write(self):
        private, line = ed25519_key()
        self.assertEqual(self.add_key("dev", line)["scopes"], ["repo:write"])
        self.assertEqual(self.ssh_sign_in("dev", private).json()["scopes"], ["repo:write"])

    def test_removing_the_key_ends_its_sign_ins_at_once(self):
        private, line = ed25519_key()
        key = self.add_key("dev", line)
        token = self.ssh_sign_in("dev", private).json()["access_token"]
        self.assertEqual(self.client.get("/api/auth/me", headers=self.h(token)).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/ssh-keys/{key['id']}",
                                            headers=self.h(self.session("dev"))).status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me", headers=self.h(token)).status_code, 401)
        self.assertEqual(self.ssh_sign_in("dev", private).status_code, 401)

    def test_another_namespace_does_not_sign_in(self):
        """A signature the key made for git, say, is not a Zanbeel sign-in."""
        private, line = ed25519_key()
        self.add_key("dev", line)
        self.assertEqual(self.ssh_sign_in("dev", private, namespace="git").status_code, 401)

    def test_one_users_key_cannot_sign_in_as_another(self):
        private, line = ed25519_key()
        self.add_key("dev", line)
        self.assertEqual(self.ssh_sign_in("other", private).status_code, 401)

    def test_the_legacy_raw_signature_still_works(self):
        """The CLI bundled with the VS Code extension still sends raw signatures."""
        private, line = ed25519_key()
        self.add_key("dev", line, scopes=["repo:read"])
        response = self.ssh_sign_in("dev", private, legacy=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["signature_format"], "legacy")
        self.assertEqual(response.json()["scopes"], ["repo:read"])

    def test_a_demoted_lead_loses_admin_from_an_old_key(self):
        private, line = ed25519_key()
        self.add_key("lead", line, scopes=["repo:write", "admin"])
        self.assertIn("admin", self.ssh_sign_in("lead", private).json()["scopes"])
        db = self.Session()
        db.query(User).filter(User.username == "lead").update({"role": "developer"})
        db.commit()
        db.close()
        self.assertNotIn("admin", self.ssh_sign_in("lead", private).json()["scopes"])

    def test_spent_sign_ins_are_cleaned_up(self):
        private, line = ed25519_key()
        key = self.add_key("dev", line)
        self.ssh_sign_in("dev", private)
        db = self.Session()
        db.query(AccessToken).filter(AccessToken.ssh_key_id == key["id"]).update(
            {"expires_at": datetime.utcnow() - timedelta(minutes=1)})
        db.commit()
        self.ssh_sign_in("dev", private)
        rows = db.query(AccessToken).filter(AccessToken.ssh_key_id == key["id"]).all()
        db.close()
        self.assertEqual(len(rows), 1)

    def test_outstanding_challenges_are_capped(self):
        """The challenge endpoint is unauthenticated; it must not fill the table."""
        private, line = ed25519_key()
        self.add_key("dev", line)
        nonces = [self.client.post("/api/auth/ssh/challenge", json={"username": "dev"}).json()["nonce"]
                  for _ in range(9)]
        db = self.Session()
        self.assertEqual(db.query(SSHChallenge).filter(SSHChallenge.username == "dev").count(), 5)
        db.close()
        signature = fox.sshsig_sign_with_key(private, nonces[-1].encode())
        response = self.client.post("/api/auth/ssh/verify", json={"nonce": nonces[-1], "signature": signature})
        self.assertEqual(response.status_code, 200)

    def test_weak_rsa_keys_are_refused(self):
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        line = weak.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        response = self.client.post("/api/ssh-keys", json={"title": "old", "public_key": line},
                                    headers=self.h(self.session("dev")))
        self.assertEqual(response.status_code, 400)
        self.assertIn("2048", response.json()["detail"])

    # --- administrators -----------------------------------------------------------------

    def test_an_admin_sees_who_holds_what_but_no_secrets(self):
        plaintext, _ = self.token("dev", ["repo:read"], name="ci")
        private, line = ed25519_key()
        self.add_key("dev", line)
        self.ssh_sign_in("dev", private)

        response = self.client.get("/api/admin/credentials", headers=self.h(self.session("lead")))
        self.assertEqual(response.status_code, 200, response.text)
        entry = next(u for u in response.json()["users"] if u["user"]["username"] == "dev")
        self.assertEqual([t["name"] for t in entry["tokens"]], ["ci"])
        self.assertEqual(entry["ssh_keys"][0]["active_sessions"], 1)
        self.assertNotIn(plaintext, response.text)
        self.assertNotIn("token_hash", response.text)
        self.assertNotIn("other", [u["user"]["username"] for u in response.json()["users"]])

    def test_an_admin_can_revoke_everything_one_user_holds(self):
        """A stolen laptop: cut off every credential without disabling the account."""
        plaintext, _ = self.token("dev", ["repo:write"])
        private, line = ed25519_key()
        self.add_key("dev", line)
        ssh_token = self.ssh_sign_in("dev", private).json()["access_token"]

        response = self.client.post("/api/admin/users/dev/credentials/revoke",
                                    headers=self.h(self.session("lead")))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["revoked_tokens"], response.json()["removed_ssh_keys"]), (1, 1))
        for token in (plaintext, ssh_token):
            self.assertEqual(self.client.get("/api/auth/me", headers=self.h(token)).status_code, 401)
        self.assertEqual(self.ssh_sign_in("dev", private).status_code, 401)
        # The account itself still works with the password.
        self._sessions.pop("dev", None)
        self.assertEqual(self.client.get("/api/auth/me", headers=self.h(self.session("dev"))).status_code, 200)

        db = self.Session()
        kinds = [n.notification_type for n in db.query(Notification).filter(Notification.user_id ==
                 db.query(User).filter(User.username == "dev").first().id)]
        db.close()
        self.assertIn("credentials_revoked", kinds)

    # --- notifications ------------------------------------------------------------------

    def test_the_owner_hears_about_every_new_credential(self):
        self.token("dev", ["repo:read"], name="ci")
        _, line = ed25519_key()
        self.add_key("dev", line, title="new-laptop")
        db = self.Session()
        dev = db.query(User).filter(User.username == "dev").first()
        rows = {n.notification_type: n.title for n in db.query(Notification).filter(Notification.user_id == dev.id)}
        db.close()
        self.assertEqual(rows.get("credential_token_created"), "New access token: ci")
        self.assertEqual(rows.get("credential_ssh_key_added"), "New SSH key: new-laptop")


class CredentialMigrationTests(unittest.TestCase):
    """A database from before key scopes upgrades in place, keeping its keys usable."""

    def test_old_tables_gain_the_new_columns(self):
        fd, path = tempfile.mkstemp(prefix="foxnest_cred_migration_", suffix=".db")
        os.close(fd)
        engine = create_engine(f"sqlite:///{path}")
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "CREATE TABLE ssh_keys (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                    "title VARCHAR(100) NOT NULL, key_type VARCHAR(30) NOT NULL, public_key TEXT NOT NULL, "
                    "fingerprint VARCHAR(80) NOT NULL UNIQUE, last_used_at TIMESTAMP, created_at TIMESTAMP)"))
                conn.execute(text(
                    "CREATE TABLE access_tokens (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                    "name VARCHAR(100) NOT NULL, prefix VARCHAR(16) NOT NULL, token_hash VARCHAR(64) NOT NULL, "
                    "scopes VARCHAR(200) NOT NULL, expires_at TIMESTAMP, last_used_at TIMESTAMP, "
                    "revoked_at TIMESTAMP, created_at TIMESTAMP)"))
                conn.execute(text(
                    "INSERT INTO ssh_keys (user_id, title, key_type, public_key, fingerprint) "
                    "VALUES (1, 'old', 'ssh-ed25519', 'ssh-ed25519 AAAA', 'SHA256:old')"))
            with mock.patch.object(migrations, "engine", engine):
                migrations.ensure_credential_columns()
                migrations.ensure_credential_columns()  # idempotent
            self.assertIn("scopes", [c["name"] for c in inspect(engine).get_columns("ssh_keys")])
            self.assertIn("ssh_key_id", [c["name"] for c in inspect(engine).get_columns("access_tokens")])
            with engine.connect() as conn:
                self.assertEqual(conn.execute(text("SELECT scopes FROM ssh_keys")).scalar(), "repo:write")
        finally:
            engine.dispose()
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
