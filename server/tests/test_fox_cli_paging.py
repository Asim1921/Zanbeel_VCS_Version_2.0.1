"""The fox CLI follows paged lists, driven against the real API in-process.

The server has returned commits 200 at a time since 2.0.1, and the CLI never asked for
the next page: a clone of a longer history kept only the newest 200 commits (their
parents pointing at nothing), `fox log` stopped at 200, and `fox push` compared local
history with the newest 200 on the server, so older commits looked new.
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
_spec = importlib.util.spec_from_file_location('foxnest_server_cli_paging', SERVER_FILE)
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

from database.database import Base  # noqa: E402
from database.crud import BranchCRUD, CommitCRUD, RepositoryCRUD, UserCRUD  # noqa: E402
from app.services import blob_store  # noqa: E402

FOX_PATH = Path(SERVER_ROOT).parent / "client" / "client1" / "fox.py"
_fox_spec = importlib.util.spec_from_file_location("fox_cli_paging", FOX_PATH)
fox_module = importlib.util.module_from_spec(_fox_spec)
_fox_spec.loader.exec_module(fox_module)
FoxClient = fox_module.FoxClient

BASE = "http://zanbeel.test"
PASSWORD = "Password123!"
COMMITS = 210  # more than one page of 200


class RoutedRequests:
    """`requests` for the CLI, answered by the app in-process; counts the GETs."""

    exceptions = requests.exceptions
    Response = requests.Response

    def __init__(self, client):
        self.client = client
        self.gets = []

    def _call(self, method, url, headers=None, json=None, params=None, timeout=None, **_):
        assert url.startswith(BASE), url
        if method == "GET":
            self.gets.append((url, dict(params or {})))
        return self.client.request(method, url[len(BASE):], headers=headers, json=json, params=params)

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def put(self, url, **kw):
        return self._call("PUT", url, **kw)

    def delete(self, url, **kw):
        return self._call("DELETE", url, **kw)


class CliPagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp(prefix="fox_cli_paging_"))
        cls.engine = create_engine("sqlite:///" + (cls._tmp / "app.db").as_posix())
        cls.Session = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)
        cls._store = mock.patch.object(blob_store, "store", blob_store.BlobStore(cls._tmp / "blobs"))
        cls._store.start()
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
            UserCRUD.create_user(db, username="lead", role="team_lead", email="lead@example.com",
                                 password_hash=server_module.hash_password(PASSWORD))
            repo = RepositoryCRUD.create_repository(db, "lead", "history", "long history")
            cls.repo = repo.id
            parent = None
            for n in range(COMMITS):
                commit_id = hashlib.sha1(f"history-{n}".encode()).hexdigest()
                CommitCRUD.create_commit(db, {
                    "id": commit_id, "repository_id": repo.id, "author": "lead",
                    "message": f"commit {n}", "parent": parent, "parents": [parent] if parent else [],
                    "files": {"log.txt": base64.b64encode(f"line {n}\n".encode()).decode()},
                })
                parent = commit_id
            cls.tip = parent
            if BranchCRUD.get_branch(db, repo.id, "main"):
                BranchCRUD.update_branch_head(db, repo.id, "main", parent)
            else:
                BranchCRUD.create_branch(db, repo.id, "main", parent, is_default=True)
        finally:
            db.close()
        cls.token = cls.client.post("/api/auth/login",
                                    json={"username": "lead", "password": PASSWORD}).json()["access_token"]

    @classmethod
    def tearDownClass(cls):
        cls._store.stop()
        server_module.app.dependency_overrides.clear()
        server_module.app.router.on_startup = cls._startup
        cls.client.close()
        cls.engine.dispose()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fox_cli_paging_work_"))
        self._cwd = os.getcwd()
        os.chdir(self.dir)
        env = mock.patch.dict(os.environ, {"HOME": str(self.dir), "USERPROFILE": str(self.dir),
                                           "FOXNEST_SERVER": BASE, "FOXNEST_TOKEN": self.token})
        env.start()
        self.addCleanup(env.stop)
        self.routed = RoutedRequests(self.client)
        patcher = mock.patch.object(fox_module, "requests", self.routed)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def quietly(self, fn, *args, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = fn(*args, **kwargs)
        return result, out.getvalue()

    def linked_client(self):
        client = FoxClient()
        self.quietly(client.init, username="lead", repo_name="history")
        config = json.loads((self.dir / ".fox" / "config.json").read_text(encoding="utf-8"))
        config.update({"repo_id": self.repo, "server_url": BASE, "origin_url": BASE})
        (self.dir / ".fox" / "config.json").write_text(json.dumps(config), encoding="utf-8")
        return FoxClient(), config

    def test_every_page_is_followed_and_merged(self):
        response = FoxClient()._authed_get_all(f"{BASE}/api/repository/{self.repo}/commits", "commits",
                                               page_size=50)
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(body["commits"]), COMMITS)
        self.assertEqual(len({c["id"] for c in body["commits"]}), COMMITS)
        self.assertFalse(body["pagination"]["has_more"])
        self.assertEqual([p["offset"] for _, p in self.routed.gets], [0, 50, 100, 150, 200])

    def test_a_server_that_does_not_page_answers_once(self):
        response = FoxClient()._authed_get_all(f"{BASE}/api/repository/{self.repo}/branch-policy", "rules")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.routed.gets), 1)

    def test_a_failing_page_is_returned_as_it_is(self):
        response = FoxClient()._authed_get_all(f"{BASE}/api/repository/nope/commits", "commits")
        self.assertEqual(response.status_code, 404)

    def test_a_clone_keeps_the_whole_history(self):
        """The clone path asked once for commits with their files: it got 200 of 210."""
        client, config = self.linked_client()
        ok, out = self.quietly(client.pull_existing_repository, config)
        self.assertTrue(ok, out)
        commits = json.loads((self.dir / ".fox" / "commits.json").read_text(encoding="utf-8"))
        self.assertEqual(len(commits), COMMITS, out)
        self.assertEqual(commits[-1]["id"], self.tip)
        ids = {c["id"] for c in commits}
        orphans = [c for c in commits if c.get("parent") and c["parent"] not in ids]
        self.assertEqual(orphans, [], "a commit whose parent the clone does not have")
        pages = [p for url, p in self.routed.gets if url.endswith("/commits")]
        self.assertEqual(len(pages), 2, "full commits come 200 to a page")

    def test_log_shows_the_whole_history_not_the_first_page(self):
        client, _ = self.linked_client()
        from app.core import pagination
        # A server that hands out 200 to a page, as it does when no limit is asked for.
        with mock.patch.object(pagination, "MAX_PAGE_SIZE", 200):
            from app.api.routes import commits as commit_routes
            with mock.patch.object(commit_routes, "MAX_PAGE_SIZE", 200):
                (commits_by_id, head), out = self.quietly(client._get_server_commits_for_log)
        self.assertEqual(len(commits_by_id or {}), COMMITS, out)
        self.assertEqual(head, self.tip)
        self.assertEqual(len([u for u, _ in self.routed.gets if u.endswith("/commits")]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
