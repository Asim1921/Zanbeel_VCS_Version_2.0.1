"""Discovery and navigation over HTTP: search, the activity log, notifications.

Each test pins a gap that was open:

* the activity feed returned every row on the server to anyone signed in, could not be
  filtered, missed most ways a branch moves, said nothing about which commit or
  credential, and was erased with the repository it described;
* code search compiled readers' regular expressions with no time limit, read up to two
  gigabytes per query, and could not search more than one repository; commit search read
  ``%`` and ``_`` as wildcards and could not find a commit by its id;
* a developer was never told their queued commit was approved or rejected, @mentions in
  pull requests and closed pull requests notified nobody, a failing required check told
  the author nothing, and notifications carried nothing a client could link to.
"""
import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
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
_spec = importlib.util.spec_from_file_location('foxnest_server_discovery', SERVER_FILE)
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

from database.database import Base  # noqa: E402
from database.crud import RepositoryCRUD, UserCRUD, UserPermissionCRUD  # noqa: E402
from database.models import (  # noqa: E402
    AccountLock, Activity, Branch, LoginAttempt, Notification, Repository,
)
from app.services import webhooks as hook_service  # noqa: E402
try:
    from app.services import search_index  # noqa: E402
except ImportError:  # the code before this work had no index; the other tests still run
    search_index = None

PASSWORD = "Password123!"


def b64(text):
    data = text.encode("utf-8") if isinstance(text, str) else text
    return base64.b64encode(data).decode("ascii")


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls._db_path = tempfile.mkstemp(prefix="foxnest_discovery_", suffix=".db")
        os.close(fd)
        cls._index_dir = tempfile.mkdtemp(prefix="foxnest_index_")
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
                               ("outsider", "developer"), ("ci", "developer")):
                UserCRUD.create_user(db, username=name, role=role, password_hash=password_hash,
                                     email=f"{name}@example.com")
        finally:
            db.close()
        cls._counter = 0
        cls._sessions = {}

    @classmethod
    def tearDownClass(cls):
        if search_index is not None:
            search_index.disable()
        server_module.app.dependency_overrides.clear()
        server_module.app.router.on_startup = cls._startup
        cls.client.close()
        cls.engine.dispose()
        for path in (cls._db_path,):
            try:
                os.remove(path)
            except OSError:
                pass

    def setUp(self):
        db = self.Session()
        db.query(LoginAttempt).delete()
        db.query(AccountLock).delete()
        db.commit()
        db.close()
        type(self)._counter += 1
        if search_index is not None:
            search_index.configure(path=os.path.join(self._index_dir, f"index{self._counter}.db"),
                                   session_factory=self.Session, background=False)
        quiet = mock.patch.object(hook_service, "dispatch", return_value=0)
        quiet.start()
        self.addCleanup(quiet.stop)

    # --- helpers -----------------------------------------------------------------------

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

    def new_repo(self, files=None, readers=("dev",), name=None):
        type(self)._counter += 1
        db = self.Session()
        try:
            repo = RepositoryCRUD.create_repository(db, "lead", name or f"app{self._counter}", "test")
            for user in readers:
                UserPermissionCRUD.create_permission(db, user, repo.id, "write", "lead")
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

    def activities(self, who="lead", **params):
        response = self.client.get("/api/activities", headers=self.h(who), params={"limit": 500, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["activities"]

    def rows(self, repo_id):
        db = self.Session()
        try:
            return db.query(Activity).filter(Activity.repository_id == repo_id).order_by(Activity.id).all()
        finally:
            db.close()

    def notifications(self, who):
        response = self.client.get("/api/notifications", headers=self.h(who), params={"limit": 200})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["notifications"]

    def open_pr(self, repo_id, source, target="main", description="", who="lead"):
        response = self.client.post(f"/api/repository/{repo_id}/pull-requests", headers=self.h(who), json={
            "title": f"{source} into {target}", "description": description,
            "source_branch": source, "target_branch": target})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["pull_request"]["id"]

    def branch(self, repo_id, name, who="lead"):
        response = self.client.post(f"/api/repository/{repo_id}/branches", headers=self.h(who),
                                    json={"name": name, "from_branch": "main"})
        self.assertEqual(response.status_code, 200, response.text)

    # --- the activity log: who may see what ----------------------------------------------

    def test_activity_is_limited_to_repositories_the_reader_can_open(self):
        """Every row used to go to anyone signed in, the dashboard included."""
        shared = self.new_repo(readers=("dev",))
        private = self.new_repo(readers=(), name=f"secret-plans-{self._counter}")
        self.push(private, "main", {"app.py": "v2\n"}, "Acquire competitor next quarter")
        seen = self.activities("dev")
        self.assertTrue(any(a["repository_id"] == shared for a in seen))
        self.assertFalse(any(a["repository_id"] == private for a in seen))
        self.assertNotIn("Acquire competitor", json.dumps(seen))
        self.assertEqual(self.activities("outsider", repository_id=private), [])
        self.assertTrue(any(a["repository_id"] == private for a in self.activities("lead")))

    def test_account_events_are_visible_to_their_owner_and_administrators(self):
        created = self.client.post("/api/tokens", headers=self.h("dev"), json={"name": "dev-laptop"})
        self.assertEqual(created.status_code, 200, created.text)
        mine = [a for a in self.activities("dev") if a["activity_type"] == "token_created"]
        self.assertTrue(any("dev-laptop" in a["description"] for a in mine))
        self.assertFalse(any("dev-laptop" in (a["description"] or "") for a in self.activities("outsider")))
        self.assertTrue(any("dev-laptop" in (a["description"] or "") for a in self.activities("lead")))

    def test_the_client_address_is_shown_to_administrators_only(self):
        self.new_repo()
        self.assertIn("client_ip", self.activities("lead")[0])
        self.assertNotIn("client_ip", self.activities("dev")[0])

    def test_a_repository_feed_needs_read_access(self):
        repo = self.new_repo(readers=("dev",))
        self.assertEqual(self.client.get(f"/api/repository/{repo}/activity",
                                         headers=self.h("outsider")).status_code, 403)
        body = self.client.get(f"/api/repository/{repo}/activity", headers=self.h("dev")).json()
        self.assertTrue(body["activities"])
        self.assertTrue(all(a["repository_id"] == repo for a in body["activities"]))

    # --- the activity log: what is recorded ----------------------------------------------

    def test_a_push_is_one_row_naming_its_branch_commit_and_credential(self):
        repo = self.new_repo()
        before = self.head(repo, "main")
        _, commit_id = self.push(repo, "main", {"app.py": "v2\n"}, "second version")
        rows = [r for r in self.rows(repo) if r.commit_id == commit_id]
        self.assertEqual(len(rows), 1, [(r.activity_type, r.description) for r in rows])
        row = rows[0]
        self.assertEqual((row.activity_type, row.target_type, row.target_id), ("push_commit", "branch", "main"))
        ref = json.loads(row.payload_json)["ref"]
        self.assertEqual((ref["before"], ref["after"]), (before, commit_id))
        self.assertEqual(row.credential, "session")
        self.assertTrue(row.client_ip)

    def test_a_token_push_records_which_token(self):
        repo = self.new_repo()
        token = self.client.post("/api/tokens", headers=self.h("lead"),
                                 json={"name": "build-agent", "scopes": ["repo:write"]}).json()["token"]
        _, commit_id = self.push(repo, "main", {"app.py": "from ci\n"}, "from ci", who=token)
        row = [r for r in self.rows(repo) if r.commit_id == commit_id][0]
        self.assertEqual(row.credential, "token:build-agent")

    def test_every_way_a_branch_moves_is_recorded(self):
        repo = self.new_repo()
        self.branch(repo, "topic")
        _, picked = self.push(repo, "topic", {"app.py": "print('hello')\n", "README.md": "readme\n",
                                              "new.txt": "x\n"}, "add new")
        base = self.head(repo, "main")
        self.assertEqual(self.client.post(f"/api/repository/{repo}/cherry-pick", headers=self.h(),
                                          json={"commit_id": picked, "branch": "main"}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/repository/{repo}/revert", headers=self.h(),
                                          json={"commit_id": self.head(repo, "main"), "branch": "main"}).status_code, 200)
        self.assertEqual(self.client.put(f"/api/repository/{repo}/branches/topic/rename", headers=self.h(),
                                         json={"new_name": "topic-2"}).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/repository/{repo}/branches/topic-2",
                                            headers=self.h()).status_code, 200)
        self.push(repo, "main", {"app.py": "something else\n"}, "something else")
        rolled = self.client.post(f"/api/repository/{repo}/rollback/branch", headers=self.h(),
                                  json={"branch": "main", "target_commit_id": base})
        self.assertEqual(rolled.status_code, 200, rolled.text)
        kinds = [r.activity_type for r in self.rows(repo)]
        for expected in ("branch_created", "cherry_pick", "revert", "branch_renamed", "branch_deleted",
                         "rollback_branch"):
            self.assertIn(expected, kinds)
        renamed = [r for r in self.rows(repo) if r.activity_type == "branch_renamed"][0]
        self.assertEqual(json.loads(renamed.payload_json)["ref"]["renamed_from"], "topic")
        picked_row = [r for r in self.rows(repo) if r.activity_type == "cherry_pick"][0]
        self.assertEqual(json.loads(picked_row.payload_json)["ref"]["branch"], "main")
        # One row per change: the cherry-pick is not also a separate "branch_updated".
        self.assertNotIn("branch_updated", kinds)
        self.assertEqual(len(kinds), len(self.rows(repo)))

    def test_approving_a_queued_commit_is_recorded_and_the_author_is_told(self):
        repo = self.new_repo()
        response, _ = self.push(repo, "main", {"app.py": "dev change\n"}, "Dev work", who="dev")
        pending_id = response.json()["commit_id"]
        review = self.client.post(f"/api/pending-commits/{pending_id}/review", headers=self.h(),
                                  json={"action": "approve", "comment": "looks good"})
        self.assertEqual(review.status_code, 200, review.text)
        rows = [r for r in self.rows(repo) if r.activity_type == "approve_commit"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0].payload_json)["ref"]["after"], self.head(repo, "main"))
        told = [n for n in self.notifications("dev") if n["type"] == "commit_approved"]
        self.assertEqual(len(told), 1)
        self.assertEqual(told[0]["payload"]["target"],
                         {"kind": "commit", "repository_id": repo, "commit_id": self.head(repo, "main")})

        response, _ = self.push(repo, "main", {"app.py": "bad\n"}, "Bad idea", who="dev")
        self.client.post(f"/api/pending-commits/{response.json()['commit_id']}/review", headers=self.h(),
                         json={"action": "reject", "comment": "no"})
        self.assertTrue(any(r.activity_type == "reject_commit" for r in self.rows(repo)))
        self.assertTrue(any(n["type"] == "commit_rejected" for n in self.notifications("dev")))

    def test_settings_and_access_changes_are_recorded(self):
        repo = self.new_repo(readers=())
        with mock.patch.dict(os.environ, {"FOXNEST_OUTBOUND_ALLOWED_HOSTS": "hooks.example"}):
            hook = self.client.post("/api/webhooks", headers=self.h(),
                                    json={"url": "https://hooks.example/x", "repository_id": repo}).json()
        self.client.delete(f"/api/webhooks/{hook['webhook']['id']}", headers=self.h())
        self.client.post("/api/server-hooks", headers=self.h(),
                         json={"name": "rules", "repository_id": repo, "config": {"block_secrets": True}})
        granted = self.client.post("/api/permissions/create", headers=self.h(), json={
            "username": "dev", "repo_id": repo, "permission_level": "write", "granted_by": "outsider"})
        self.assertEqual(granted.status_code, 200, granted.text)
        self.client.post(f"/api/repository/{repo}/tags", headers=self.h(), json={"name": "v1"})
        kinds = {r.activity_type for r in self.rows(repo)}
        self.assertTrue({"webhook_created", "webhook_deleted", "push_rule_created", "grant_permission",
                         "tag_created"} <= kinds, kinds)
        grant = [r for r in self.rows(repo) if r.activity_type == "grant_permission"][0]
        self.assertEqual(grant.actor_username, "lead")
        # Webhook creation is a sync route, run on a worker thread: the request's
        # credential must still reach the log.
        created = [r for r in self.rows(repo) if r.activity_type == "webhook_created"][0]
        self.assertEqual(created.credential, "session")
        # The grantor is the signed-in user, not a name in the request body.
        db = self.Session()
        try:
            from database.models import UserPermission, User
            dev = db.query(User).filter(User.username == "dev").first()
            row = db.query(UserPermission).filter(UserPermission.repository_id == repo,
                                                  UserPermission.user_id == dev.id).first()
            self.assertEqual(row.granted_by.username, "lead")
        finally:
            db.close()
        self.assertTrue(any(n["type"] == "access_granted" for n in self.notifications("dev")))
        revoked = self.client.delete("/api/permissions/revoke", headers=self.h(),
                                     params={"username": "dev", "repo_id": repo})
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(self.client.delete("/api/permissions/revoke", headers=self.h(),
                                            params={"username": "dev", "repo_id": repo}).status_code, 404)
        self.assertTrue(any(n["type"] == "access_revoked" for n in self.notifications("dev")))

    def test_deleting_a_repository_keeps_its_history(self):
        repo = self.new_repo(name=f"doomed-{self._counter}")
        self.assertTrue(self.rows(repo))
        self.assertEqual(self.client.delete(f"/api/repository/{repo}", headers=self.h()).status_code, 200)
        db = self.Session()
        try:
            kept = db.query(Activity).filter(Activity.repository_name.like("doomed-%"),
                                             Activity.repository_id.is_(None)).all()
        finally:
            db.close()
        self.assertTrue(any(r.activity_type == "push_commit" for r in kept))
        deletion = [a for a in self.activities("lead", type="delete_repository")
                    if (a["payload"] or {}).get("repository_id") == repo]
        self.assertEqual(len(deletion), 1)

    # --- the activity log: filters, paging, export ---------------------------------------

    def test_filters_and_paging_happen_on_the_server(self):
        repo = self.new_repo()
        for n in range(3):
            self.push(repo, "main", {"app.py": f"v{n}\n"}, f"change {n}")
        body = self.client.get("/api/activities", headers=self.h(), params={
            "repository_id": repo, "type": "push_commit", "limit": 2}).json()
        self.assertEqual(body["pagination"]["total"], 4)
        self.assertTrue(body["pagination"]["has_more"])
        self.assertEqual(len(body["activities"]), 2)
        self.assertEqual(len(self.activities("lead", repository_id=repo, q="change 1")), 1)
        self.assertEqual(self.activities("lead", repository_id=repo, user="dev"), [])
        self.assertEqual(self.activities("lead", repository_id=repo, until="2000-01-01"), [])
        self.assertEqual(self.client.get("/api/activities", headers=self.h(),
                                         params={"since": "yesterday"}).status_code, 400)
        commit = self.head(repo, "main")
        self.assertEqual(len(self.activities("lead", commit=commit[:10])), 1)

    def test_the_type_list_matches_what_the_server_writes(self):
        types = {t["type"]: t for t in self.client.get("/api/activities/types", headers=self.h()).json()["types"]}
        for name in ("push_commit", "branch_created", "cherry_pick", "approve_commit", "webhook_created"):
            self.assertIn(name, types)
        self.assertNotIn("cherry-pick", types)

    def test_export_is_for_administrators_and_cannot_inject_formulas(self):
        repo = self.new_repo()
        self.push(repo, "main", {"app.py": "x\n"}, "=HYPERLINK(\"http://evil\")")
        self.assertEqual(self.client.get("/api/activities/export", headers=self.h("dev")).status_code, 403)
        response = self.client.get("/api/activities/export", headers=self.h(), params={"repository_id": repo})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["content-type"])
        rows = list(csv.DictReader(io.StringIO(response.text)))
        self.assertTrue(rows)
        self.assertFalse(any((row["description"] or "").startswith("=") for row in rows))

    # --- search --------------------------------------------------------------------------

    def code(self, who="lead", **params):
        return self.client.get("/api/search/code", headers=self.h(who), params=params)

    def test_a_pathological_regex_cannot_stall_the_server(self):
        repo = self.new_repo({"big.txt": ("x" * 5000 + "\n") * 20})
        started = time.monotonic()
        response = self.code(repository_id=repo, q="(x+x+)+y", regex="true")
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["timed_out"])

    def test_commit_search_reads_the_query_literally_and_finds_ids(self):
        repo = self.new_repo()
        self.push(repo, "main", {"a.txt": "1\n"}, "100% done")
        self.push(repo, "main", {"a.txt": "2\n"}, "fix_typo in docs")
        self.push(repo, "main", {"a.txt": "3\n"}, "fixXtypo elsewhere")
        found = self.client.get("/api/search/commits", headers=self.h(),
                                params={"q": "fix_typo", "repository_id": repo}).json()
        self.assertEqual([c["message"] for c in found["commits"]], ["fix_typo in docs"])
        found = self.client.get("/api/search/commits", headers=self.h(),
                                params={"q": "100%", "repository_id": repo}).json()
        self.assertEqual([c["message"] for c in found["commits"]], ["100% done"])
        head = self.head(repo, "main")
        by_id = self.client.get("/api/search/commits", headers=self.h(), params={"q": head[:9]}).json()
        self.assertEqual([c["id"] for c in by_id["commits"]], [head])

    def test_commit_search_reports_a_true_total_and_pages(self):
        repo = self.new_repo()
        for n in range(5):
            self.push(repo, "main", {"a.txt": f"{n}\n"}, f"pageable change {n}")
        first = self.client.get("/api/search/commits", headers=self.h(),
                                params={"q": "pageable", "limit": 2}).json()
        self.assertEqual((first["total"], len(first["commits"]), first["truncated"]), (5, 2, True))
        last = self.client.get("/api/search/commits", headers=self.h(),
                               params={"q": "pageable", "limit": 2, "offset": 4}).json()
        self.assertEqual((len(last["commits"]), last["truncated"]), (1, False))

    def test_code_search_covers_every_readable_repository(self):
        marker = f"ZanbeelMarker{self._counter}"
        shared = self.new_repo({"src/one.py": f"value = '{marker}'\n"}, readers=("dev",))
        private = self.new_repo({"src/two.py": f"other = '{marker}'\n"}, readers=())
        everyone = self.code(q=marker).json()
        self.assertEqual(everyone["source"], "index")
        self.assertEqual({r["repository_id"] for r in everyone["results"]}, {shared, private})
        dev = self.code("dev", q=marker).json()
        self.assertEqual({r["repository_id"] for r in dev["results"]}, {shared})
        self.assertEqual(dev["results"][0]["matches"][0]["line"], 1)

    def test_the_index_follows_the_default_branch(self):
        repo = self.new_repo({"app.py": "OldContentMarker\n"})
        self.push(repo, "main", {"app.py": "NewContentMarker\n"}, "replace")
        self.assertEqual(self.code(q="OldContentMarker").json()["results"], [])
        self.assertEqual(len(self.code(q="NewContentMarker").json()["results"]), 1)
        self.branch(repo, "feature")
        self.push(repo, "feature", {"app.py": "FeatureOnlyMarker\n"}, "feature work")
        self.assertEqual(self.code(q="FeatureOnlyMarker").json()["results"], [])
        on_branch = self.code(q="FeatureOnlyMarker", repository_id=repo, branch="feature").json()
        self.assertEqual((on_branch["source"], len(on_branch["results"])), ("scan", 1))

    def test_shared_content_is_indexed_once_and_a_deleted_repository_leaves(self):
        text = f"SharedBlob{self._counter}\n"
        first = self.new_repo({"same.txt": text})
        self.new_repo({"same.txt": text})
        self.assertEqual(len(self.code(q=text.strip()).json()["results"]), 2)
        self.assertEqual(search_index.status()["unique_blobs"],
                         len({row for row in search_index._connection().execute("SELECT hash FROM blobs")}))
        self.client.delete(f"/api/repository/{first}", headers=self.h())
        self.assertEqual(len(self.code(q=text.strip()).json()["results"]), 1)

    def test_a_new_fork_is_searchable_at_once(self):
        """A fork's branches are copied rather than moved, so nothing else would index it."""
        marker = f"ForkedMarker{self._counter}"
        source = self.new_repo({"lib.py": f"{marker} = 1\n"})
        forked = self.client.post(f"/api/repository/{source}/fork", headers=self.h("dev"),
                                  json={"name": f"fork-{self._counter}"})
        self.assertEqual(forked.status_code, 200, forked.text)
        fork_id = forked.json()["repo_id"]
        found = {r["repository_id"] for r in self.code(q=marker).json()["results"]}
        self.assertEqual(found, {source, fork_id})

    def test_everywhere_needs_a_literal_of_three_characters(self):
        self.new_repo()
        self.assertEqual(self.code(q="ab").status_code, 400)
        self.assertEqual(self.code(q="a.+b", regex="true").status_code, 400)

    def test_go_to_file(self):
        repo = self.new_repo({"src/server/routes.py": "x\n", "docs/routes.md": "y\n",
                              "src/routes_test.py": "z\n"}, readers=("dev",))
        hidden = self.new_repo({"routes.py": "x\n"}, readers=())
        found = self.client.get("/api/search/paths", headers=self.h("dev"), params={"q": "routes.py"}).json()
        self.assertEqual(found["files"][0]["path"], "src/server/routes.py")
        self.assertNotIn(hidden, {f["repository_id"] for f in found["files"]})
        scoped = self.client.get("/api/search/paths", headers=self.h("dev"),
                                 params={"q": "src routes", "repository_id": repo}).json()
        self.assertEqual({f["path"] for f in scoped["files"]}, {"src/server/routes.py", "src/routes_test.py"})
        self.assertEqual(self.client.get("/api/search/paths", headers=self.h("outsider"),
                                         params={"q": "routes", "repository_id": repo}).status_code, 404)

    # --- notifications -------------------------------------------------------------------

    def test_mentions_in_a_pull_request_reach_readers_only(self):
        repo = self.new_repo(readers=("dev",))
        self.branch(repo, "feature")
        pr = self.open_pr(repo, "feature", description="@dev and @outsider please look")
        self.assertEqual(
            [n["payload"]["target"] for n in self.notifications("dev") if n["type"] == "pr_mention"
             and n["payload"]["pr_id"] == pr],
            [{"kind": "pull_request", "repository_id": repo, "pr_id": pr}])
        self.assertFalse(any(n["type"] == "pr_mention" for n in self.notifications("outsider")))
        comment = self.client.post(f"/api/repository/{repo}/pull-requests/{pr}/comments", headers=self.h(),
                                   json={"body": "@dev one more thing", "file_path": "app.py", "line": 1})
        self.assertEqual(comment.status_code, 200, comment.text)
        self.assertEqual(len([n for n in self.notifications("dev") if n["type"] == "pr_mention"
                              and n["payload"]["pr_id"] == pr]), 2)

    def test_closing_someone_elses_pull_request_tells_them(self):
        repo = self.new_repo(readers=("dev",))
        self.branch(repo, "feature", who="dev")
        pr = self.open_pr(repo, "feature", who="dev")
        self.client.post(f"/api/repository/{repo}/pull-requests/{pr}/close", headers=self.h())
        closed = [n for n in self.notifications("dev") if n["type"] == "pr_closed" and n["payload"]["pr_id"] == pr]
        self.assertEqual(len(closed), 1)
        self.assertTrue(any(r.activity_type == "close_pull_request" for r in self.rows(repo)))

    def test_a_failing_required_check_tells_the_author_once(self):
        repo = self.new_repo(readers=("dev", "ci"))
        self.client.put(f"/api/repository/{repo}/branch-policy", headers=self.h(),
                        json={"required_status_checks": ["ci/build"]})
        self.branch(repo, "feature", who="dev")
        _, head = self.push(repo, "feature", {"app.py": "change\n"}, "feature", who="lead")
        pr = self.open_pr(repo, "feature", who="dev")
        for _ in range(2):
            self.client.post(f"/api/repository/{repo}/commits/{head}/statuses", headers=self.h("ci"),
                             json={"context": "ci/build", "state": "failure", "description": "3 tests failed"})
        self.client.post(f"/api/repository/{repo}/commits/{head}/statuses", headers=self.h("ci"),
                         json={"context": "ci/lint", "state": "failure"})
        failed = [n for n in self.notifications("dev") if n["type"] == "check_failed" and n["payload"]["pr_id"] == pr]
        self.assertEqual(len(failed), 1)
        self.assertIn("3 tests failed", failed[0]["body"])

    def test_review_notifications_say_where_to_go(self):
        repo = self.new_repo(readers=("dev",))
        self.branch(repo, "feature", who="dev")
        pr = self.open_pr(repo, "feature", who="dev")
        self.client.post(f"/api/repository/{repo}/pull-requests/{pr}/reviews", headers=self.h(),
                         json={"state": "approved"})
        reviewed = [n for n in self.notifications("dev") if n["type"] == "pr_review"]
        self.assertEqual(reviewed[0]["payload"]["target"], {"kind": "pull_request", "repository_id": repo, "pr_id": pr})

    def test_the_server_address_given_to_new_collaborators_is_this_server(self):
        from app.services import notifications as notify
        source = open(notify.__file__, encoding="utf-8").read()
        self.assertNotIn("192.168.15.207", source)
        with mock.patch.dict(os.environ, {"FOXNEST_PUBLIC_URL": "https://vcs.example.org/"}):
            self.assertEqual(notify.public_server_url(), "https://vcs.example.org")
        with mock.patch.dict(os.environ, {"FOXNEST_PUBLIC_URL": ""}):
            self.assertTrue(notify.public_server_url().startswith("http://"))


if __name__ == "__main__":
    unittest.main()
