"""Integrations over HTTP: signed webhooks, CI status checks as a gate, push rules.

The three features existed; the paths around them did not hold. Each test here pins one
of the gaps that was open:

* webhooks could be pointed at this server, the LAN or the metadata service and the
  reply read back; redirects were followed with the signature attached; ping froze the
  server; unsigned hooks were the default; and no ``push`` event was sent when a queued
  commit was approved or a branch moved any way other than a team lead's push;
* the PR merge gate read only the repository-wide required checks and a branch merge
  only the per-branch ones; statuses were read by commit id across repositories; and
  any writer, the pull request's author included, could report a required check green;
* push rules ran on ``fox push`` only, looked at the whole snapshot (so deleting a
  protected file passed), and ran owner-written regexes with no time limit.
"""
import base64
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

SERVER_FILE = os.path.join(SERVER_ROOT, 'server.py')
_spec = importlib.util.spec_from_file_location('foxnest_server_integrations', SERVER_FILE)
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

from database.database import Base  # noqa: E402
from database.crud import RepositoryCRUD, UserCRUD, UserPermissionCRUD  # noqa: E402
from database.models import (  # noqa: E402
    AccountLock, Branch, Commit, CommitStatus, LoginAttempt, PendingCommit, Repository,
    ServerHook, Webhook, WebhookDelivery,
)
from app.services import webhooks as hook_service  # noqa: E402

PASSWORD = "Password123!"
ALLOW_LOOPBACK = {"FOXNEST_OUTBOUND_ALLOWED_HOSTS": "127.0.0.1"}
SECRET_FILE_TEXT = "aws_key = AKIAIOSFODNN7EXAMPLE\n"


def b64(text):
    data = text.encode("utf-8") if isinstance(text, str) else text
    return base64.b64encode(data).decode("ascii")


class _Receiver(BaseHTTPRequestHandler):
    """A webhook receiver: records requests, answers as configured."""

    received = []
    status = 200
    location = None

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Receiver.received.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(_Receiver.status)
        if _Receiver.location:
            self.send_header("Location", _Receiver.location)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class IntegrationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls._db_path = tempfile.mkstemp(prefix="foxnest_integrations_", suffix=".db")
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
            for name, role in (("lead", "team_lead"), ("dev", "developer"),
                               ("ci", "developer"), ("other", "developer")):
                UserCRUD.create_user(db, username=name, role=role, password_hash=password_hash,
                                     email=f"{name}@example.com")
        finally:
            db.close()

        cls.httpd = HTTPServer(("127.0.0.1", 0), _Receiver)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.receiver = f"http://127.0.0.1:{cls.httpd.server_address[1]}/hook"
        cls._counter = 0
        cls._sessions = {}

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server_module.app.dependency_overrides.clear()
        server_module.app.router.on_startup = cls._startup
        cls.client.close()
        cls.engine.dispose()
        try:
            os.remove(cls._db_path)
        except OSError:
            pass

    def setUp(self):
        _Receiver.received, _Receiver.status, _Receiver.location = [], 200, None
        db = self.Session()
        db.query(LoginAttempt).delete()
        db.query(AccountLock).delete()
        db.commit()
        db.close()
        self.events = []
        # Webhook events are captured rather than delivered: delivery runs on worker
        # threads with their own sessions, and is tested in test_webhooks.py.
        patcher = mock.patch.object(hook_service, "dispatch", side_effect=self._capture)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _capture(self, db, event, repository_id, payload, background=True):
        self.events.append((event, repository_id, payload))
        return 1

    # --- helpers -----------------------------------------------------------------

    @classmethod
    def session(cls, username):
        if username not in cls._sessions:
            response = cls.client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
            assert response.status_code == 200, response.text
            cls._sessions[username] = response.json()["access_token"]
        return cls._sessions[username]

    def h(self, who="lead"):
        token = who if who.startswith("fxp_") else self.session(who)
        return {"Authorization": f"Bearer {token}"}

    def new_repo(self, files=None):
        """A repository owned by lead, dev and ci with write access, main at one commit."""
        type(self)._counter += 1
        db = self.Session()
        try:
            repo = RepositoryCRUD.create_repository(db, "lead", f"app{self._counter}", "test")
            for name in ("dev", "ci"):
                UserPermissionCRUD.create_permission(db, name, repo.id, "write", "lead")
            repo_id = repo.id
        finally:
            db.close()
        self.push(repo_id, "main", files or {"app.py": "print('hello')\n", "README.md": "readme\n"}, "base")
        return repo_id

    def head(self, repo_id, branch):
        db = self.Session()
        try:
            row = db.query(Branch).filter(Branch.repository_id == repo_id, Branch.name == branch).first()
            return row.head_commit_id if row else None
        finally:
            db.close()

    def push(self, repo_id, branch, files, message, who="lead", parent_branch=None, expect=200):
        parent = self.head(repo_id, branch) or (self.head(repo_id, parent_branch) if parent_branch else None)
        commit_id = hashlib.sha1(f"{repo_id}{branch}{message}{time.time_ns()}".encode()).hexdigest()
        response = self.client.post(f"/api/repository/{repo_id}/push", headers=self.h(who), json={
            "branch": branch,
            "commit": {"id": commit_id, "author": who if not who.startswith("fxp_") else "lead",
                       "message": message, "parent": parent, "parents": [parent] if parent else [],
                       "files": {path: b64(text) for path, text in files.items()}},
        })
        if expect is not None:
            self.assertEqual(response.status_code, expect, response.text)
        return response, commit_id

    def store_commit(self, repo_id, files, parent=None, branch=None):
        """A commit written straight to the database: content no push rule ever saw,
        as a fork's commit or one pushed before the rule existed would be."""
        from database.crud import CommitCRUD
        commit_id = hashlib.sha1(f"stored{time.time_ns()}".encode()).hexdigest()
        db = self.Session()
        try:
            CommitCRUD.create_commit(db, {
                "id": commit_id, "repository_id": repo_id, "author": "lead", "message": "stored",
                "parent": parent, "parents": [parent] if parent else [],
                "files": {path: b64(text) for path, text in files.items()},
            })
            if branch:
                row = db.query(Branch).filter(Branch.repository_id == repo_id, Branch.name == branch).first()
                if row:
                    row.head_commit_id = commit_id
                else:
                    db.add(Branch(repository_id=repo_id, name=branch, head_commit_id=commit_id))
                db.commit()
        finally:
            db.close()
        return commit_id

    def add_hook(self, repo_id, config, name="rules"):
        response = self.client.post("/api/server-hooks", headers=self.h(), json={
            "name": name, "repository_id": repo_id, "config": config})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["hook"]

    def open_pr(self, repo_id, source, target="main"):
        response = self.client.post(f"/api/repository/{repo_id}/pull-requests", headers=self.h(), json={
            "title": f"{source} into {target}", "description": "", "source_branch": source,
            "target_branch": target})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["pull_request"]["id"]

    def report(self, repo_id, commit_id, context, state, who="ci", url=None):
        body = {"context": context, "state": state}
        if url:
            body["target_url"] = url
        return self.client.post(f"/api/repository/{repo_id}/commits/{commit_id}/statuses",
                                headers=self.h(who), json=body)

    def events_of(self, kind):
        return [payload for event, _repo, payload in self.events if event == kind]

    # --- webhooks: destinations ---------------------------------------------------------

    def test_hooks_cannot_point_at_this_server_the_lan_or_the_metadata_service(self):
        repo = self.new_repo()
        for url, reason in (("http://127.0.0.1:33333/api/admin/gc", "loopback"),
                            ("http://localhost:8080/x", "loopback"),
                            ("http://169.254.169.254/latest/meta-data/", "link-local"),
                            ("http://10.1.2.3/hook", "private network"),
                            ("http://[::1]:9000/hook", "loopback")):
            with self.subTest(url=url):
                response = self.client.post("/api/webhooks", headers=self.h(),
                                            json={"url": url, "repository_id": repo})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn(reason, response.json()["detail"])

    def test_an_administrator_can_allow_private_networks_but_not_loopback(self):
        repo = self.new_repo()
        with mock.patch.dict(os.environ, {"FOXNEST_OUTBOUND_ALLOW_PRIVATE": "1"}):
            ok = self.client.post("/api/webhooks", headers=self.h(),
                                  json={"url": "http://10.1.2.3/hook", "repository_id": repo})
            self.assertEqual(ok.status_code, 200, ok.text)
            refused = self.client.post("/api/webhooks", headers=self.h(),
                                       json={"url": "http://127.0.0.1/hook", "repository_id": repo})
            self.assertEqual(refused.status_code, 400)

    # --- webhooks: signing, delivery ids, redirects, redelivery -------------------------

    def create_hook(self, repo, **extra):
        with mock.patch.dict(os.environ, ALLOW_LOOPBACK):
            response = self.client.post("/api/webhooks", headers=self.h(),
                                        json={"url": self.receiver, "repository_id": repo, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_every_hook_is_signed_and_its_secret_is_shown_once(self):
        repo = self.new_repo()
        created = self.create_hook(repo)
        self.assertTrue(created["secret_generated"])
        self.assertGreaterEqual(len(created["secret"]), 32)
        self.assertTrue(created["webhook"]["signed"])
        listed = self.client.get(f"/api/webhooks?repository_id={repo}", headers=self.h()).json()
        self.assertNotIn(created["secret"], json.dumps(listed))

        short = self.client.post("/api/webhooks", headers=self.h(),
                                 json={"url": self.receiver, "repository_id": repo, "secret": "short"})
        self.assertEqual(short.status_code, 400)
        cleared = self.client.put(f"/api/webhooks/{created['webhook']['id']}", headers=self.h(),
                                  json={"secret": ""})
        self.assertEqual(cleared.status_code, 400)
        self.assertIn("always signed", cleared.json()["detail"])

    def test_ping_is_signed_over_a_body_that_carries_its_delivery_id(self):
        repo = self.new_repo()
        created = self.create_hook(repo)
        with mock.patch.dict(os.environ, ALLOW_LOOPBACK):
            response = self.client.post(f"/api/webhooks/{created['webhook']['id']}/ping", headers=self.h())
        self.assertEqual(response.status_code, 200, response.text)
        delivery = response.json()["delivery"]
        self.assertTrue(delivery["success"], delivery)
        request = _Receiver.received[-1]
        body = json.loads(request["body"])
        self.assertTrue(hook_service.verify_signature(
            created["secret"], request["body"], request["headers"]["X-Zanbeel-Signature-256"]))
        self.assertEqual(body["delivery_id"], request["headers"]["X-Zanbeel-Delivery"])
        self.assertEqual(body["delivery_id"], delivery["delivery_id"])
        self.assertTrue(body["delivered_at"])

    def test_rotating_the_secret_changes_the_signature(self):
        repo = self.new_repo()
        created = self.create_hook(repo)
        rotated = self.client.post(f"/api/webhooks/{created['webhook']['id']}/rotate-secret",
                                   headers=self.h()).json()
        self.assertNotEqual(rotated["secret"], created["secret"])
        with mock.patch.dict(os.environ, ALLOW_LOOPBACK):
            self.client.post(f"/api/webhooks/{created['webhook']['id']}/ping", headers=self.h())
        request = _Receiver.received[-1]
        signature = request["headers"]["X-Zanbeel-Signature-256"]
        self.assertTrue(hook_service.verify_signature(rotated["secret"], request["body"], signature))
        self.assertFalse(hook_service.verify_signature(created["secret"], request["body"], signature))

    def test_redirects_are_not_followed(self):
        repo = self.new_repo()
        created = self.create_hook(repo)
        _Receiver.status, _Receiver.location = 302, self.receiver + "/elsewhere"
        with mock.patch.dict(os.environ, ALLOW_LOOPBACK):
            delivery = self.client.post(f"/api/webhooks/{created['webhook']['id']}/ping",
                                        headers=self.h()).json()["delivery"]
        self.assertFalse(delivery["success"])
        self.assertIn("not followed", delivery["error"])
        self.assertEqual(len(_Receiver.received), 1)   # only the original request arrived

    def test_a_destination_is_checked_again_at_delivery_time(self):
        repo = self.new_repo()
        created = self.create_hook(repo)
        # Allowed when saved; not any more when sent.
        delivery = self.client.post(f"/api/webhooks/{created['webhook']['id']}/ping",
                                    headers=self.h()).json()["delivery"]
        self.assertFalse(delivery["success"])
        self.assertIn("Not sent", delivery["error"])
        self.assertEqual(_Receiver.received, [])

    def test_a_logged_delivery_can_be_sent_again(self):
        repo = self.new_repo()
        created = self.create_hook(repo)
        hook_id = created["webhook"]["id"]
        _Receiver.status = 503
        with mock.patch.dict(os.environ, ALLOW_LOOPBACK):
            failed = self.client.post(f"/api/webhooks/{hook_id}/ping", headers=self.h()).json()["delivery"]
            _Receiver.status = 200
            response = self.client.post(f"/api/webhooks/{hook_id}/deliveries/{failed['id']}/redeliver",
                                        headers=self.h())
        self.assertEqual(response.status_code, 200, response.text)
        again = response.json()["delivery"]
        self.assertTrue(again["success"])
        self.assertNotEqual(again["delivery_id"], failed["delivery_id"])
        self.assertEqual(again["redelivery_of"], failed["delivery_id"])
        body = json.loads(_Receiver.received[-1]["body"])
        self.assertEqual(body["redelivery_of"], failed["delivery_id"])
        logged = self.client.get(f"/api/webhooks/{hook_id}/deliveries?delivery_id={failed['delivery_id']}",
                                 headers=self.h()).json()["deliveries"]
        self.assertEqual([d["id"] for d in logged], [failed["id"]])

    # --- webhooks: every way a branch moves is announced ---------------------------------

    def test_a_push_is_announced_once(self):
        repo = self.new_repo()
        self.events = []
        _, commit_id = self.push(repo, "main", {"app.py": "v2\n"}, "second")
        pushes = self.events_of("push")
        self.assertEqual(len(pushes), 1)
        self.assertEqual((pushes[0]["branch"], pushes[0]["after"], pushes[0]["created"]),
                         ("main", commit_id, False))
        self.assertEqual(pushes[0]["head_commit"]["message"], "second")

    def test_approving_a_queued_commit_is_announced(self):
        """Most commits here arrive this way, and CI never heard about them."""
        repo = self.new_repo()
        response, _ = self.push(repo, "main", {"app.py": "dev change\n"}, "dev work", who="dev")
        pending_id = response.json()["commit_id"]
        self.events = []
        review = self.client.post(f"/api/pending-commits/{pending_id}/review", headers=self.h(),
                                  json={"action": "approve"})
        self.assertEqual(review.status_code, 200, review.text)
        self.assertEqual(self.events_of("commit.reviewed")[0]["decision"], "approved")
        pushes = self.events_of("push")
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0]["after"], self.head(repo, "main"))

    def test_cherry_pick_and_rollback_are_announced(self):
        repo = self.new_repo()
        base = self.head(repo, "main")
        self.client.post(f"/api/repository/{repo}/branches", headers=self.h(),
                         json={"name": "topic", "from_branch": "main"})
        _, topic_commit = self.push(repo, "topic", {"app.py": "print('hello')\n", "README.md": "readme\n",
                                                    "new.txt": "x\n"}, "add new")
        self.events = []
        picked = self.client.post(f"/api/repository/{repo}/cherry-pick", headers=self.h(),
                                  json={"commit_id": topic_commit, "branch": "main"})
        self.assertEqual(picked.status_code, 200, picked.text)
        self.assertEqual([p["branch"] for p in self.events_of("push")], ["main"])
        self.events = []
        rolled = self.client.post(f"/api/repository/{repo}/rollback/branch", headers=self.h(),
                                  json={"branch": "main", "target_commit_id": base})
        self.assertEqual(rolled.status_code, 200, rolled.text)
        self.assertEqual(len(self.events_of("push")), 1)

    def test_branches_created_through_the_api_are_recorded_and_announced(self):
        repo = self.new_repo()
        self.events = []
        created = self.client.post(f"/api/repository/{repo}/branches", headers=self.h(),
                                   json={"name": "topic-x", "from_branch": "main"})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(self.events_of("branch.created")[0]["branch"], "topic-x")
        self.assertTrue(self.events_of("push")[0]["created"])
        self.events = []
        deleted = self.client.delete(f"/api/repository/{repo}/branches/topic-x", headers=self.h())
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.events_of("branch.deleted")[0]["branch"], "topic-x")

    def test_moving_a_pull_requests_source_announces_the_new_head(self):
        repo = self.new_repo()
        self.client.post(f"/api/repository/{repo}/branches", headers=self.h(),
                         json={"name": "feature", "from_branch": "main"})
        pr = self.open_pr(repo, "feature")
        self.events = []
        _, commit_id = self.push(repo, "feature", {"app.py": "feature\n"}, "more work")
        updated = self.events_of("pull_request.updated")
        self.assertEqual((updated[0]["pull_request"]["id"], updated[0]["pull_request"]["head"]),
                         (pr, commit_id))
        self.events = []
        self.client.post(f"/api/repository/{repo}/pull-requests/{pr}/close", headers=self.h())
        self.assertEqual(self.events_of("pull_request.closed")[0]["pull_request"]["id"], pr)

    def test_a_pull_request_merge_announces_the_push_to_its_target(self):
        repo = self.new_repo()
        self.client.post(f"/api/repository/{repo}/branches", headers=self.h(),
                         json={"name": "feature", "from_branch": "main"})
        self.push(repo, "feature", {"app.py": "print('hello')\n", "README.md": "readme\n", "f.txt": "f\n"}, "f")
        pr = self.open_pr(repo, "feature")
        self.events = []
        merged = self.client.post(f"/api/repository/{repo}/pull-requests/{pr}/merge", headers=self.h())
        self.assertEqual(merged.status_code, 200, merged.text)
        self.assertEqual(self.events_of("push")[0]["branch"], "main")
        self.assertEqual(self.events_of("push")[0]["operation"], "merge")
        self.assertEqual(len(self.events_of("pull_request.merged")), 1)

    # --- status checks: one set of required checks, on every merge path -----------------

    def feature_with_change(self, repo):
        self.client.post(f"/api/repository/{repo}/branches", headers=self.h(),
                         json={"name": "feature", "from_branch": "main"})
        _, commit_id = self.push(repo, "feature", {"app.py": "changed\n", "README.md": "readme\n"}, "change")
        return commit_id

    def test_repository_wide_checks_now_gate_a_branch_merge(self):
        """`fox merge` used to land unchecked code past the checks the web screen sets."""
        repo = self.new_repo()
        head = self.feature_with_change(repo)
        policy = self.client.put(f"/api/repository/{repo}/branch-policy", headers=self.h(),
                                 json={"required_status_checks": ["ci/build"]})
        self.assertEqual(policy.status_code, 200, policy.text)
        refused = self.client.post(f"/api/repository/{repo}/branches/merge", headers=self.h(),
                                   json={"source_branch": "feature", "target_branch": "main"})
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["detail"]["code"], "CHECKS_REQUIRED")
        self.assertEqual(self.report(repo, head, "ci/build", "success").status_code, 200)
        merged = self.client.post(f"/api/repository/{repo}/branches/merge", headers=self.h(),
                                  json={"source_branch": "feature", "target_branch": "main"})
        self.assertEqual(merged.status_code, 200, merged.text)

    def test_per_branch_checks_now_gate_a_pull_request_merge(self):
        repo = self.new_repo()
        head = self.feature_with_change(repo)
        rule = self.client.put(f"/api/repository/{repo}/branch-protection", headers=self.h(), json={
            "branch_pattern": "main", "mode": "open", "rules": {"require_status_checks": ["ci/lint"]}})
        self.assertEqual(rule.status_code, 200, rule.text)
        pr = self.open_pr(repo, "feature")
        refused = self.client.post(f"/api/repository/{repo}/pull-requests/{pr}/merge", headers=self.h())
        self.assertNotEqual(refused.status_code, 200)
        self.assertIn("ci/lint", refused.text)
        self.report(repo, head, "ci/lint", "success")
        merged = self.client.post(f"/api/repository/{repo}/pull-requests/{pr}/merge", headers=self.h())
        self.assertEqual(merged.status_code, 200, merged.text)

    def test_required_checks_endpoint_combines_both_sources(self):
        repo = self.new_repo()
        self.client.put(f"/api/repository/{repo}/branch-policy", headers=self.h(),
                        json={"required_status_checks": ["ci/build"]})
        self.client.put(f"/api/repository/{repo}/branch-protection", headers=self.h(), json={
            "branch_pattern": "main", "mode": "open", "rules": {"require_status_checks": ["ci/lint"]}})
        body = self.client.get(f"/api/repository/{repo}/required-checks?branch=main", headers=self.h()).json()
        self.assertEqual(sorted(body["required_status_checks"]), ["ci/build", "ci/lint"])

    def test_a_pinned_reporter_is_the_only_one_who_counts(self):
        """Otherwise the author of a pull request can report its own check green."""
        repo = self.new_repo()
        head = self.feature_with_change(repo)
        policy = self.client.put(f"/api/repository/{repo}/branch-policy", headers=self.h(), json={
            "required_status_checks": [{"context": "ci/build", "reporter": "ci"}]})
        self.assertEqual(policy.status_code, 200, policy.text)
        pr = self.open_pr(repo, "feature")
        self.assertEqual(self.report(repo, head, "ci/build", "success", who="dev").status_code, 200)
        summary = self.client.get(f"/api/repository/{repo}/commits/{head}/statuses?branch=main",
                                  headers=self.h()).json()["combined"]
        self.assertFalse(summary["required_satisfied"])
        self.assertEqual(summary["required_checks"][0]["ignored_report"]["reported_by"], "dev")
        refused = self.client.post(f"/api/repository/{repo}/pull-requests/{pr}/merge", headers=self.h())
        self.assertIn("from ci", refused.text)
        self.report(repo, head, "ci/build", "success", who="ci")
        merged = self.client.post(f"/api/repository/{repo}/pull-requests/{pr}/merge", headers=self.h())
        self.assertEqual(merged.status_code, 200, merged.text)

    def test_a_status_cannot_be_reported_through_another_repository(self):
        repo, elsewhere = self.new_repo(), self.new_repo()
        theirs = self.head(elsewhere, "main")
        response = self.report(repo, theirs, "ci/build", "success")
        self.assertEqual(response.status_code, 404)
        self.assertIn("not part of this repository", response.json()["detail"])

    def test_statuses_count_only_in_the_repository_they_were_reported_to(self):
        repo, elsewhere = self.new_repo(), self.new_repo()
        head = self.head(repo, "main")
        db = self.Session()
        db.add(CommitStatus(repository_id=elsewhere, commit_id=head, context="ci/build", state="success"))
        db.commit()
        db.close()
        self.client.put(f"/api/repository/{repo}/branch-policy", headers=self.h(),
                        json={"required_status_checks": ["ci/build"]})
        summary = self.client.get(f"/api/repository/{repo}/commits/{head}/statuses",
                                  headers=self.h()).json()["combined"]
        self.assertEqual(summary["missing_required"], ["ci/build"])

    def test_a_status_link_must_be_a_web_address(self):
        repo = self.new_repo()
        head = self.head(repo, "main")
        response = self.report(repo, head, "ci/build", "success", url="javascript:alert(1)")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.report(repo, head, "ci/build", "success", url="https://ci.example/1").status_code, 200)

    def test_a_repo_status_token_reports_but_cannot_push(self):
        repo = self.new_repo()
        head = self.head(repo, "main")
        created = self.client.post("/api/tokens", headers=self.h("ci"),
                                   json={"name": "ci-runner", "scopes": ["repo:status"]})
        self.assertEqual(created.status_code, 200, created.text)
        token = created.json()["token"]
        self.assertEqual(self.report(repo, head, "ci/build", "success", who=token).status_code, 200)
        self.assertEqual(self.client.get(f"/api/repository/{repo}/branches", headers=self.h(token)).status_code, 200)
        response, _ = self.push(repo, "main", {"app.py": "sneaky\n"}, "sneaky", who=token, expect=None)
        self.assertEqual(response.status_code, 403)
        read_only = self.client.post("/api/tokens", headers=self.h("ci"),
                                     json={"name": "reader", "scopes": ["repo:read"]}).json()["token"]
        self.assertEqual(self.report(repo, head, "ci/build", "success", who=read_only).status_code, 403)

    # --- push rules: every write, judged on what changes ---------------------------------

    def test_a_pull_request_cannot_carry_content_the_rules_forbid(self):
        """A fork's commits were only ever checked against the fork's own rules."""
        repo = self.new_repo()
        base = self.head(repo, "main")
        self.store_commit(repo, {"app.py": "print('hello')\n", "README.md": "readme\n",
                                 "config.py": SECRET_FILE_TEXT}, parent=base, branch="feature")
        self.add_hook(repo, {"block_secrets": True})
        pr = self.open_pr(repo, "feature")
        refused = self.client.post(f"/api/repository/{repo}/pull-requests/{pr}/merge", headers=self.h())
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertEqual(refused.json()["detail"]["code"], "PRE_RECEIVE_REJECTED")
        self.assertIn("AWS access key", refused.text)
        self.assertEqual(self.head(repo, "main"), base)

    def test_a_branch_merge_is_checked_too(self):
        repo = self.new_repo()
        base = self.head(repo, "main")
        self.store_commit(repo, {"app.py": "print('hello')\n", "README.md": "readme\n",
                                 "deploy/id_rsa": "key\n"}, parent=base, branch="feature")
        self.add_hook(repo, {"block_secrets": True})
        refused = self.client.post(f"/api/repository/{repo}/branches/merge", headers=self.h(),
                                   json={"source_branch": "feature", "target_branch": "main"})
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertIn("secret file", refused.text)
        self.assertEqual(self.head(repo, "main"), base)

    def test_a_rollback_cannot_put_back_a_removed_secret(self):
        repo = self.new_repo()
        leaked = self.store_commit(repo, {"app.py": "print('hello')\n", "secret.txt": SECRET_FILE_TEXT},
                                   parent=self.head(repo, "main"), branch="main")
        self.push(repo, "main", {"app.py": "print('hello')\n"}, "remove the leaked key")
        self.add_hook(repo, {"block_secrets": True})
        refused = self.client.post(f"/api/repository/{repo}/rollback/branch", headers=self.h(),
                                   json={"branch": "main", "target_commit_id": leaked})
        self.assertEqual(refused.status_code, 422, refused.text)

    def test_a_conflict_resolution_is_checked(self):
        """Its conflicted files are written by hand: new content no push ever saw."""
        repo = self.new_repo()
        self.client.post(f"/api/repository/{repo}/branches", headers=self.h(),
                         json={"name": "feature", "from_branch": "main"})
        self.push(repo, "feature", {"app.py": "feature\n", "README.md": "readme\n"}, "feature edit")
        self.push(repo, "main", {"app.py": "main\n", "README.md": "readme\n"}, "main edit")
        self.add_hook(repo, {"forbidden_content": ["DO-NOT-SHIP"]})
        session = self.client.post(f"/api/repository/{repo}/branches/conflicts", headers=self.h(),
                                   json={"source_branch": "feature", "target_branch": "main"})
        self.assertEqual(session.status_code, 200, session.text)
        session_id = session.json()["session"]["id"]
        before = self.head(repo, "main")
        refused = self.client.post(
            f"/api/repository/{repo}/branches/conflicts/{session_id}/resolve", headers=self.h(),
            json={"resolutions": {"app.py": b64("DO-NOT-SHIP\n")}})
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertEqual(self.head(repo, "main"), before)

    def test_an_untouched_protected_file_no_longer_blocks_every_push(self):
        repo = self.new_repo({"app.py": "a\n", "generated/schema.sql": "create table x;\n"})
        self.add_hook(repo, {"protected_paths": ["generated/"]})
        self.push(repo, "main", {"app.py": "b\n", "generated/schema.sql": "create table x;\n"}, "edit app")

    def test_deleting_a_protected_file_is_refused(self):
        """A deleted file is not in the snapshot, so the rule never saw it go."""
        repo = self.new_repo({"app.py": "a\n", "generated/schema.sql": "create table x;\n"})
        self.add_hook(repo, {"protected_paths": ["generated/"]})
        response, _ = self.push(repo, "main", {"app.py": "a\n"}, "drop the schema", expect=422)
        self.assertIn("generated/schema.sql", response.text)

    def test_a_pathological_pattern_cannot_stall_the_server(self):
        repo = self.new_repo()
        self.add_hook(repo, {"commit_message_pattern": "(x+x+)+y"})
        started = time.monotonic()
        response, _ = self.push(repo, "main", {"app.py": "z\n"}, "x" * 5000, expect=422)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("took too long", response.text)

    def test_secrets_are_caught_in_files_and_by_name(self):
        repo = self.new_repo()
        self.add_hook(repo, {"block_secrets": True})
        for files, expect in (({"settings.py": SECRET_FILE_TEXT}, "AWS access key"),
                              ({"keys/id_ed25519": "x\n"}, "secret file"),
                              ({"ci.txt": "token=fxp_" + "a" * 43 + "\n"}, "Zanbeel access token")):
            with self.subTest(expect=expect):
                response, _ = self.push(repo, "main", {"app.py": "print('hello')\n", **files}, "leak", expect=422)
                self.assertIn(expect, response.text)
        self.push(repo, "main", {"app.py": "print('fine')\n", "README.md": "readme\n"}, "clean")

    def test_new_branch_names_follow_the_rule(self):
        repo = self.new_repo()
        self.add_hook(repo, {"branch_name_pattern": r"^(feature|fix)/"})
        refused = self.client.post(f"/api/repository/{repo}/branches", headers=self.h(),
                                   json={"name": "wip", "from_branch": "main"})
        self.assertEqual(refused.status_code, 422, refused.text)
        response, _ = self.push(repo, "wip2", {"app.py": "x\n"}, "new branch", parent_branch="main", expect=422)
        self.assertIn("branch name", response.text)
        ok = self.client.post(f"/api/repository/{repo}/branches", headers=self.h(),
                              json={"name": "feature/login", "from_branch": "main"})
        self.assertEqual(ok.status_code, 200, ok.text)
        # Existing branches are not renamed by a new rule: pushing to main still works.
        self.push(repo, "main", {"app.py": "y\n"}, "still fine")

    def test_post_receive_hooks_are_refused_with_a_pointer_to_webhooks(self):
        repo = self.new_repo()
        response = self.client.post("/api/server-hooks", headers=self.h(), json={
            "name": "notify", "repository_id": repo, "hook_type": "post-receive", "config": {}})
        self.assertEqual(response.status_code, 400)
        self.assertIn("webhook", response.json()["detail"])

    def test_a_policy_service_cannot_be_on_loopback_and_its_secret_is_kept(self):
        repo = self.new_repo()
        refused = self.client.post("/api/server-hooks", headers=self.h(), json={
            "name": "policy", "repository_id": repo, "config": {"external_url": "http://127.0.0.1:9/p"}})
        self.assertEqual(refused.status_code, 400)
        with mock.patch.dict(os.environ, ALLOW_LOOPBACK):
            hook = self.add_hook(repo, {"external_url": self.receiver, "external_secret": "s" * 20})
            self.assertTrue(hook["has_external_secret"])
            self.assertNotIn("external_secret", hook["config"])
            updated = self.client.put(f"/api/server-hooks/{hook['id']}", headers=self.h(), json={
                "config": {"external_url": self.receiver, "commit_message_min_length": 3}})
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertTrue(updated.json()["hook"]["has_external_secret"])

    def test_the_dry_run_exercises_content_rules(self):
        repo = self.new_repo()
        hook = self.add_hook(repo, {"block_secrets": True})
        result = self.client.post(f"/api/server-hooks/{hook['id']}/test", headers=self.h(), json={
            "files": {"settings.py": SECRET_FILE_TEXT}}).json()
        self.assertFalse(result["would_accept"])
        self.assertIn("AWS access key", result["reasons"][0])


if __name__ == "__main__":
    unittest.main()
