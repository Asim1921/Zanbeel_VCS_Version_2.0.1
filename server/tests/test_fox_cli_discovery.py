"""The fox CLI's discovery commands, driven against the real API in-process.

``fox search``, ``fox activity`` and ``fox notifications`` did not exist: search, the
activity log and notifications were reachable from the web client only.
"""
import base64
import contextlib
import hashlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
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
_spec = importlib.util.spec_from_file_location('foxnest_server_cli_discovery', SERVER_FILE)
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

from database.database import Base  # noqa: E402
from database.crud import RepositoryCRUD, UserCRUD, UserPermissionCRUD  # noqa: E402
from database.models import Branch  # noqa: E402
from app.services import search_index  # noqa: E402
from app.services import webhooks as hook_service  # noqa: E402

FOX_PATH = Path(SERVER_ROOT).parent / "client" / "client1" / "fox.py"
_fox_spec = importlib.util.spec_from_file_location("fox_cli_discovery", FOX_PATH)
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


class CliDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls._db_path = tempfile.mkstemp(prefix="foxnest_cli_discovery_", suffix=".db")
        os.close(fd)
        cls._index_path = cls._db_path + ".index"
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
        search_index.configure(path=cls._index_path, session_factory=cls.Session, background=False)

        db = cls.Session()
        try:
            password_hash = server_module.hash_password(PASSWORD)
            for name, role in (("lead", "team_lead"), ("dev", "developer")):
                UserCRUD.create_user(db, username=name, role=role, password_hash=password_hash,
                                     email=f"{name}@example.com")
            shared = RepositoryCRUD.create_repository(db, "lead", "billing", "cli discovery")
            UserPermissionCRUD.create_permission(db, "dev", shared.id, "write", "lead")
            hidden = RepositoryCRUD.create_repository(db, "lead", "payroll", "not for dev")
            cls.shared, cls.hidden = shared.id, hidden.id
        finally:
            db.close()

        def session(user):
            return cls.client.post("/api/auth/login", json={"username": user, "password": PASSWORD}).json()["access_token"]

        cls.lead_session, cls.dev_session = session("lead"), session("dev")
        with mock.patch.object(hook_service, "dispatch", return_value=0):
            cls.push(cls.shared, {"src/invoice.py": "def parse_invoice(total):\n    return total * 100  # cents\n",
                                  "README.md": "Billing service\n"}, "Add invoice parser")
            cls.push(cls.hidden, {"src/salary.py": "def parse_invoice():\n    pass\n"}, "Payroll copy")

    @classmethod
    def push(cls, repo_id, files, message):
        db = cls.Session()
        try:
            row = db.query(Branch).filter(Branch.repository_id == repo_id, Branch.name == "main").first()
            parent = row.head_commit_id if row else None
        finally:
            db.close()
        commit_id = hashlib.sha1(f"{repo_id}{message}{time.time_ns()}".encode()).hexdigest()
        response = cls.client.post(f"/api/repository/{repo_id}/push",
                                   headers={"Authorization": f"Bearer {cls.lead_session}"}, json={
            "branch": "main",
            "commit": {"id": commit_id, "author": "lead", "message": message, "parent": parent,
                       "parents": [parent] if parent else [],
                       "files": {p: base64.b64encode(t.encode()).decode() for p, t in files.items()}},
        })
        assert response.status_code == 200, response.text
        return commit_id

    @classmethod
    def tearDownClass(cls):
        search_index.disable()
        server_module.app.dependency_overrides.clear()
        server_module.app.router.on_startup = cls._startup
        cls.client.close()
        cls.engine.dispose()
        for path in (cls._db_path, cls._index_path):
            try:
                os.remove(path)
            except OSError:
                pass

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fox_cli_discovery_"))
        self._cwd = os.getcwd()
        os.chdir(self.dir)
        env = mock.patch.dict(os.environ, {"HOME": str(self.dir), "USERPROFILE": str(self.dir),
                                           "FOXNEST_SERVER": BASE})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FOXNEST_REPO_ID", None)
        routed = mock.patch.object(fox_module, "requests", RoutedRequests(self.client))
        routed.start()
        self.addCleanup(routed.stop)
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

    def test_code_search_everywhere_prints_grep_style_lines_for_readable_repositories(self):
        ok, out = self.run_as(self.dev_session, FoxClient.search, "code", "parse_invoice", everywhere=True)
        self.assertTrue(ok, out)
        self.assertIn("billing:src/invoice.py:1: def parse_invoice(total):", out)
        self.assertNotIn("payroll", out)

    def test_a_regex_in_one_repository(self):
        with mock.patch.dict(os.environ, {"FOXNEST_REPO_ID": self.shared}):
            ok, out = self.run_as(self.dev_session, FoxClient.search, "code", r"\* \d+", regex=True)
        self.assertTrue(ok, out)
        self.assertIn("src/invoice.py:2:", out)

    def test_commits_files_and_repositories(self):
        ok, out = self.run_as(self.dev_session, FoxClient.search, "commits", "invoice parser")
        self.assertTrue(ok, out)
        self.assertIn("Add invoice parser", out)
        ok, out = self.run_as(self.dev_session, FoxClient.search, "files", "invoice", everywhere=True)
        self.assertIn("billing:src/invoice.py", out)
        ok, out = self.run_as(self.dev_session, FoxClient.search, "repos", "bill")
        self.assertIn("billing", out)
        self.assertNotIn("payroll", out)

    def test_nothing_found_exits_non_zero_like_grep(self):
        ok, out = self.run_as(self.dev_session, FoxClient.search, "code", "NoSuchStringAnywhere", everywhere=True)
        self.assertFalse(ok)
        self.assertIn("Nothing found.", out)
        argv = ["fox", "search", "commits", "NoSuchStringAnywhere"]
        with mock.patch.dict(os.environ, {"FOXNEST_TOKEN": self.dev_session}), \
                mock.patch.object(fox_module.sys, "argv", argv), self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(io.StringIO()):
                fox_module.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_activity_for_a_repository_and_everywhere(self):
        with mock.patch.dict(os.environ, {"FOXNEST_REPO_ID": self.shared}):
            ok, out = self.run_as(self.dev_session, FoxClient.activity)
        self.assertTrue(ok, out)
        self.assertIn("Pushed", out)
        self.assertIn("Add invoice parser", out)
        ok, out = self.run_as(self.dev_session, FoxClient.activity, everywhere=True)
        self.assertNotIn("Payroll copy", out)
        ok, out = self.run_as(self.lead_session, FoxClient.activity, everywhere=True, kind="push_commit")
        self.assertIn("payroll: ", out)

    def test_notifications_list_and_read(self):
        # Opening a pull request that mentions dev gives dev something to read.
        self.client.post(f"/api/repository/{self.shared}/branches",
                         headers={"Authorization": f"Bearer {self.lead_session}"},
                         json={"name": f"topic-{time.time_ns()}", "from_branch": "main"})
        branches = self.client.get(f"/api/repository/{self.shared}/branches",
                                   headers={"Authorization": f"Bearer {self.lead_session}"}).json()["branches"]
        topic = next(b["name"] for b in branches if b["name"].startswith("topic-"))
        self.client.post(f"/api/repository/{self.shared}/pull-requests",
                         headers={"Authorization": f"Bearer {self.lead_session}"},
                         json={"title": "Invoice rounding", "description": "@dev can you check?",
                               "source_branch": topic, "target_branch": "main"})
        ok, out = self.run_as(self.dev_session, FoxClient.notifications_list, unread=True)
        self.assertTrue(ok, out)
        self.assertIn("mentioned you", out)
        self.assertIn("billing · pull request #", out)
        ok, out = self.run_as(self.dev_session, FoxClient.notifications_read, everything=True)
        self.assertTrue(ok, out)
        self.assertIn("0 unread", out)


if __name__ == "__main__":
    unittest.main()
