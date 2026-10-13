"""The fox CLI's integration commands, driven against the real API in-process.

``fox status-check`` is what a CI job runs; ``fox webhook`` is how a repository owner
wires one up from a terminal. The CLI's HTTP calls are routed to a FastAPI TestClient.
"""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
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
_spec = importlib.util.spec_from_file_location('foxnest_server_cli_integrations', SERVER_FILE)
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

from database.database import Base  # noqa: E402
from database.crud import CommitCRUD, RepositoryCRUD, UserCRUD, UserPermissionCRUD  # noqa: E402
from database.models import Branch  # noqa: E402
from app.services import webhooks as hook_service  # noqa: E402

FOX_PATH = Path(SERVER_ROOT).parent / "client" / "client1" / "fox.py"
_fox_spec = importlib.util.spec_from_file_location("fox_cli_integrations", FOX_PATH)
fox_module = importlib.util.module_from_spec(_fox_spec)
_fox_spec.loader.exec_module(fox_module)
FoxClient = fox_module.FoxClient

BASE = "http://zanbeel.test"
PASSWORD = "Password123!"


class RoutedRequests:
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


class _Receiver(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Receiver.received.append(self.headers.get("X-Zanbeel-Event"))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class CliIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls._db_path = tempfile.mkstemp(prefix="foxnest_cli_integrations_", suffix=".db")
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
            for name, role in (("lead", "team_lead"), ("ci", "developer"), ("dev", "developer")):
                UserCRUD.create_user(db, username=name, role=role, password_hash=password_hash,
                                     email=f"{name}@example.com")
            repo = RepositoryCRUD.create_repository(db, "lead", "app", "cli integrations")
            for name in ("ci", "dev"):
                UserPermissionCRUD.create_permission(db, name, repo.id, "write", "lead")
            cls.repo_id = repo.id
            cls.commit_id = hashlib.sha1(b"cli-integrations-base").hexdigest()
            CommitCRUD.create_commit(db, {
                "id": cls.commit_id, "repository_id": repo.id, "author": "lead", "message": "base",
                "files": {"app.py": base64.b64encode(b"print(1)\n").decode()},
            })
            db.add(Branch(repository_id=repo.id, name="main", head_commit_id=cls.commit_id))
            db.commit()
        finally:
            db.close()

        cls.httpd = HTTPServer(("127.0.0.1", 0), _Receiver)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.receiver = f"http://127.0.0.1:{cls.httpd.server_address[1]}/hook"

        def token(user, scopes):
            session = cls.client.post("/api/auth/login", json={"username": user, "password": PASSWORD}).json()["access_token"]
            return cls.client.post("/api/tokens", headers={"Authorization": f"Bearer {session}"},
                                   json={"name": f"{user}-cli", "scopes": scopes}).json()["token"]

        cls.ci_token = token("ci", ["repo:status"])
        cls.read_token = token("ci", ["repo:read"])
        cls.lead_token = token("lead", ["repo:write"])

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
        _Receiver.received = []
        self.dir = Path(tempfile.mkdtemp(prefix="fox_cli_integrations_"))
        self._cwd = os.getcwd()
        os.chdir(self.dir)
        env = mock.patch.dict(os.environ, {
            "HOME": str(self.dir), "USERPROFILE": str(self.dir), "FOXNEST_SERVER": BASE,
            "FOXNEST_REPO_ID": self.repo_id, "FOXNEST_OUTBOUND_ALLOWED_HOSTS": "127.0.0.1",
        })
        env.start()
        self.addCleanup(env.stop)
        routed = mock.patch.object(fox_module, "requests", RoutedRequests(self.client))
        routed.start()
        self.addCleanup(routed.stop)
        # Events from status reports are not under test here.
        quiet = mock.patch.object(hook_service, "dispatch", return_value=0)
        quiet.start()
        self.addCleanup(quiet.stop)

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_as(self, token, fn, *args, **kwargs):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"FOXNEST_TOKEN": token}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = fn(FoxClient(), *args, **kwargs)
        return result, out.getvalue()

    def set_required(self, checks):
        response = self.client.put(f"/api/repository/{self.repo_id}/branch-policy",
                                   headers={"Authorization": f"Bearer {self.lead_token}"},
                                   json={"required_status_checks": checks})
        self.assertEqual(response.status_code, 200, response.text)

    # --- status-check ---------------------------------------------------------------

    def test_ci_reports_with_a_status_token_and_hears_the_gate(self):
        self.set_required(["ci/build"])
        ok, out = self.run_as(self.ci_token, FoxClient.status_check_report, "ci/build", "success",
                              commit=self.commit_id, url="https://ci.example/run/1")
        self.assertTrue(ok, out)
        self.assertIn(f"Reported ci/build: success on {self.commit_id[:12]}", out)
        self.assertIn("All 1 required check(s) have passed", out)

    def test_a_read_only_token_cannot_report(self):
        ok, out = self.run_as(self.read_token, FoxClient.status_check_report, "ci/build", "success",
                              commit=self.commit_id)
        self.assertFalse(ok)
        self.assertIn("read-only", out)

    def test_show_explains_a_report_that_did_not_count(self):
        self.set_required([{"context": "ci/lint", "reporter": "ci"}])
        self.run_as(self.lead_token, FoxClient.status_check_report, "ci/lint", "success", commit=self.commit_id)
        ok, out = self.run_as(self.lead_token, FoxClient.status_check_show, commit=self.commit_id)
        self.assertTrue(ok, out)
        self.assertIn("ignored success from lead; only ci counts", out)
        self.assertIn("Required checks do not pass yet", out)

    def test_status_check_report_command_exits_non_zero_on_failure(self):
        argv = ["fox", "status-check", "report", "ci/build", "success", "--commit", "0" * 40]
        with mock.patch.dict(os.environ, {"FOXNEST_TOKEN": self.ci_token}), \
                mock.patch.object(fox_module.sys, "argv", argv), self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(io.StringIO()):
                fox_module.main()
        self.assertEqual(ctx.exception.code, 1)

    # --- refusals ------------------------------------------------------------------

    @staticmethod
    def refusal(detail, status=422):
        response = requests.Response()
        response.status_code = status
        response.headers["content-type"] = "application/json"
        response._content = json.dumps({"detail": detail}).encode()
        return response

    def test_a_push_rule_refusal_prints_its_reasons_not_a_dict(self):
        response = self.refusal({
            "code": "PRE_RECEIVE_REJECTED", "hook": "policy",
            "reasons": ["AWS access key in settings.py", "secret file deploy/id_rsa"],
            "message": "Rejected by push rule 'policy': AWS access key in settings.py; secret file deploy/id_rsa"})
        self.assertEqual(FoxClient._response_error_message(response),
                         "Rejected by push rule 'policy': AWS access key in settings.py; secret file deploy/id_rsa")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(FoxClient._print_push_rule_refusal(response, "a" * 40))
        self.assertIn("push rule 'policy' (commit aaaaaaaaaaaa)", out.getvalue())
        self.assertIn("  - secret file deploy/id_rsa", out.getvalue())
        self.assertNotIn("{'code'", out.getvalue())

    def test_a_gate_refusal_lists_its_blockers(self):
        response = self.refusal({"code": "CHECKS_REQUIRED", "message": "Required checks have not passed",
                                 "blockers": ["ci/build has not reported"]}, status=409)
        self.assertEqual(FoxClient._response_error_message(response),
                         "Required checks have not passed; ci/build has not reported")
        self.assertFalse(FoxClient._print_push_rule_refusal(response, "a" * 40))

    # --- webhook --------------------------------------------------------------------

    def test_webhook_add_ping_deliveries_and_redeliver(self):
        ok, out = self.run_as(self.lead_token, FoxClient.webhook_add, self.receiver, events=["push"])
        self.assertTrue(ok, out)
        self.assertIn("Signing secret:", out)
        hook_id = int(out.split("Webhook ", 1)[1].split()[0])

        ok, out = self.run_as(self.lead_token, FoxClient.webhook_list)
        self.assertIn(self.receiver, out)
        self.assertIn("yes", out)

        ok, out = self.run_as(self.lead_token, FoxClient.webhook_ping, hook_id)
        self.assertTrue(ok, out)
        self.assertIn("delivered: 200", out)
        self.assertEqual(_Receiver.received, ["ping"])

        ok, out = self.run_as(self.lead_token, FoxClient.webhook_deliveries, hook_id)
        self.assertTrue(ok, out)
        row = int(out.splitlines()[1].split()[0])
        ok, out = self.run_as(self.lead_token, FoxClient.webhook_redeliver, hook_id, row)
        self.assertTrue(ok, out)
        self.assertEqual(_Receiver.received, ["ping", "ping"])

        ok, out = self.run_as(self.lead_token, FoxClient.webhook_rotate_secret, hook_id)
        self.assertTrue(ok, out)
        self.assertIn("New signing secret", out)
        ok, out = self.run_as(self.lead_token, FoxClient.webhook_remove, hook_id)
        self.assertTrue(ok, out)

    def test_webhook_add_reports_a_refused_destination(self):
        with mock.patch.dict(os.environ, {"FOXNEST_OUTBOUND_ALLOWED_HOSTS": ""}):
            ok, out = self.run_as(self.lead_token, FoxClient.webhook_add, "http://169.254.169.254/x")
        self.assertFalse(ok)
        self.assertIn("link-local", out)


if __name__ == "__main__":
    unittest.main()
