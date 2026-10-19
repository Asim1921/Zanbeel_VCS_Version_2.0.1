"""Paged lists over HTTP, and the operations API (backups, restore drills, unlock, schema).

The gap: list endpoints returned everything and grew without bound -- the review
queues kept every reviewed commit, pull requests every closed one, the access-request
queue stopped silently at 100 per repository and 1000 in all. Where 2.0.1 had added
paging, nothing told a client there was more, and the fox CLI quietly worked from the
first 200 commits. Now every growing list is paged in the database where it can be,
says so in the body (``pagination``) and in ``X-Total-Count`` and ``Link`` headers, and
orders by a unique column last so pages neither overlap nor skip.
"""
import base64
import hashlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

SERVER_FILE = os.path.join(SERVER_ROOT, 'server.py')
_spec = importlib.util.spec_from_file_location('foxnest_server_paging', SERVER_FILE)
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

from database.crud import BranchCRUD, RepositoryCRUD, UserCRUD, UserPermissionCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Activity, Issue, IssueAccessRequest, Notification, PendingCommit, PendingRepository,
    PendingUserRegistration, PullRequest, Repository, Tag, User, UserPermission,
)
from app.db import upgrade  # noqa: E402
from app.services import access_tokens as token_service  # noqa: E402
from app.services import backup, blob_store, maintenance  # noqa: E402
from app.services import webhooks as hook_service  # noqa: E402

PASSWORD = "Password123!"
LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


def b64(data):
    data = data.encode("utf-8") if isinstance(data, str) else data
    return base64.b64encode(data).decode("ascii")


class _AppTest(unittest.TestCase):
    """A migrated throwaway database behind the real app, with a few users."""

    @classmethod
    def build(cls, prefix):
        cls._tmp = Path(tempfile.mkdtemp(prefix=prefix))
        cls._db_path = cls._tmp / "app.db"
        url = "sqlite:///" + cls._db_path.as_posix()
        upgrade.upgrade_database(url, snapshot=False, log=lambda m: None)
        cls.engine = create_engine(url)
        cls.Session = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
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
        cls._tokens = {}
        cls._patchers = [mock.patch.object(hook_service, "dispatch", return_value=0)]
        for patcher in cls._patchers:
            patcher.start()

        db = cls.Session()
        try:
            password_hash = server_module.hash_password(PASSWORD)
            cls.users = {}
            for name, role in (("admin", "admin"), ("lead", "team_lead"), ("lead2", "team_lead"),
                               ("dev", "developer"), ("dev2", "developer")):
                cls.users[name] = UserCRUD.create_user(db, username=name, role=role, password_hash=password_hash,
                                                       email=f"{name}@example.com").id
            db.query(User).filter(User.username == "dev").update({User.team_lead_id: cls.users["lead"]})
            db.query(User).filter(User.username == "dev2").update({User.team_lead_id: cls.users["lead2"]})
            db.commit()
        finally:
            db.close()

    @classmethod
    def teardown(cls):
        for patcher in cls._patchers:
            patcher.stop()
        server_module.app.dependency_overrides.clear()
        server_module.app.router.on_startup = cls._startup
        cls.client.close()
        cls.engine.dispose()
        for _ in range(10):
            shutil.rmtree(cls._tmp, ignore_errors=True)
            if not cls._tmp.exists():
                break
            time.sleep(0.2)

    @classmethod
    def token(cls, who):
        if who not in cls._tokens:
            response = cls.client.post("/api/auth/login", json={"username": who, "password": PASSWORD})
            assert response.status_code == 200, response.text
            cls._tokens[who] = response.json()["access_token"]
        return cls._tokens[who]

    def h(self, who="admin"):
        return {"Authorization": f"Bearer {self.token(who)}"}


class PaginationTests(_AppTest):
    @classmethod
    def setUpClass(cls):
        cls.build("foxnest_paging_")
        db = cls.Session()
        try:
            for i in range(8):
                UserCRUD.create_user(db, username=f"u{i}", email=f"u{i}@example.com")
            repo = RepositoryCRUD.create_repository(db, "lead", "app", "paged")
            cls.repo = repo.id
            for i in range(4):
                RepositoryCRUD.create_repository(db, "lead", f"extra{i}", "more")
            UserPermissionCRUD.create_permission(db, "dev", repo.id, "write", "lead")
            UserPermissionCRUD.create_permission(db, "dev2", repo.id, "write", "lead")
            for i in range(3):
                UserPermissionCRUD.create_permission(db, f"u{i}", repo.id, "read", "lead")
            for i in range(3):
                fork = RepositoryCRUD.create_repository(db, "lead2", f"fork{i}", "fork")
                fork.forked_from_id = repo.id
            db.commit()
        finally:
            db.close()

        parent = None
        for i in range(5):
            commit_id = hashlib.sha1(f"commit{i}".encode()).hexdigest()
            response = cls.client.post(f"/api/repository/{cls.repo}/push",
                                       headers={"Authorization": f"Bearer {cls.token('lead')}"}, json={
                "branch": "main",
                "commit": {"id": commit_id, "author": "lead", "message": f"commit {i}",
                           "parent": parent, "parents": [parent] if parent else [],
                           "files": {"app.py": b64(f"v{i}\n"), "README.md": b64("readme\n")}},
            })
            assert response.status_code == 200, response.text
            parent = commit_id
        lead = {"Authorization": f"Bearer {cls.token('lead')}"}
        for i in range(5):
            assert cls.client.post(f"/api/repository/{cls.repo}/tags", headers=lead,
                                   json={"name": f"t{i}", "target": "main"}).status_code == 200
        for version in ("v1.0.0", "v2.0.0", "v1.5.0"):
            response = cls.client.post(f"/api/repository/{cls.repo}/releases", headers=lead,
                                       json={"version": version, "target": "main"})
            assert response.status_code == 200, response.text

        db = cls.Session()
        try:
            head = BranchCRUD.get_branch(db, cls.repo, "main").head_commit_id
            for i in range(5):
                BranchCRUD.create_branch(db, cls.repo, f"feature/{i}", head)
            for i in range(5):
                db.add(PullRequest(repository_id=cls.repo, title=f"pr {i}", source_branch=f"feature/{i}",
                                   target_branch="main", status="open" if i % 2 else "closed",
                                   created_by_id=cls.users["lead"]))
            for i in range(4):
                db.add(PendingCommit(id=hashlib.sha1(f"pend-dev{i}".encode()).hexdigest(), repository_id=cls.repo,
                                     author_id=cls.users["dev"], message=f"dev work {i}", status="pending"))
            for i in range(3):
                db.add(PendingCommit(id=hashlib.sha1(f"pend-dev2{i}".encode()).hexdigest(), repository_id=cls.repo,
                                     author_id=cls.users["dev2"], message=f"dev2 work {i}", status="pending"))
            for i in range(3):
                db.add(PendingRepository(repo_name=f"wanted{i}", requested_by_id=cls.users["dev"],
                                         owner_id=cls.users["lead"], status="pending"))
            for i in range(2):
                db.add(PendingRepository(repo_name=f"wanted-b{i}", requested_by_id=cls.users["dev2"],
                                         owner_id=cls.users["lead2"], status="pending"))
            issues = [Issue(repository_id=cls.repo, number=n, title=f"issue {n}", created_by_id=cls.users["dev"])
                      for n in range(1, 22)]
            db.add_all(issues)
            db.flush()
            requested = [db.query(User).filter(User.username == f"u{i}").first().id for i in range(5)]
            # More than the 100 the queue used to stop at, silently: one request per
            # (issue, person) pair, which is what the table allows.
            for i in range(105):
                db.add(IssueAccessRequest(issue_id=issues[i // 5].id, repository_id=cls.repo,
                                          requested_by_id=cls.users["dev"], requested_user_id=requested[i % 5],
                                          status="pending", request_reason=f"reason {i}"))
            for i in range(4):
                db.add(PendingUserRegistration(username=f"applicant{i}", password_hash="x", status="pending"))
            for i in range(5):
                db.add(Notification(user_id=cls.users["lead"], notification_type="test", title=f"note {i}"))
            db.commit()
            lead_user = db.query(User).filter(User.username == "lead").first()
            dev_user = db.query(User).filter(User.username == "dev").first()
            for i in range(3):
                token_service.create_token(db, lead_user, name=f"ci{i}", scopes=["repo:read"])
            token_service.create_token(db, dev_user, name="laptop", scopes=["repo:read"])
        finally:
            db.close()

    @classmethod
    def tearDownClass(cls):
        cls.teardown()

    # --- the walk ------------------------------------------------------------------------

    def walk(self, path, key, who="admin", limit=2):
        """Follow Link: rel="next" from the first page to the last. Returns (items, total)."""
        url = path + ("&" if "?" in path else "?") + f"limit={limit}"
        items, total, pages = [], None, 0
        while url:
            response = self.client.get(url, headers=self.h(who))
            self.assertEqual(response.status_code, 200, f"{url}: {response.text}")
            body = response.json()
            meta = body["pagination"]
            if total is None:
                total = meta["total"]
            self.assertEqual(meta["total"], total, "the total changed between pages")
            self.assertEqual(response.headers.get("X-Total-Count"), str(total))
            self.assertLessEqual(len(body[key]), limit)
            items.extend(body[key])
            pages += 1
            match = LINK_NEXT.search(response.headers.get("Link", ""))
            self.assertEqual(bool(match), meta["has_more"], response.headers.get("Link"))
            url = match.group(1) if match else None
            self.assertLess(pages, 200)
        self.assertEqual(len(items), total, f"{path}: walked {len(items)} of {total}")
        keys = [repr(sorted(item.items())) if isinstance(item, dict) else repr(item) for item in items]
        self.assertEqual(len(keys), len(set(keys)), f"{path}: a row appeared on two pages")
        return items, total

    def count(self, model, *criteria):
        db = self.Session()
        try:
            return db.query(model).filter(*criteria).count()
        finally:
            db.close()

    # --- every growing list --------------------------------------------------------------

    def test_repository_lists(self):
        _, total = self.walk("/api/repositories/all", "repositories")
        self.assertEqual(total, self.count(Repository))
        _, total = self.walk("/api/repository/list", "repositories")
        self.assertEqual(total, self.count(Repository))
        forks, total = self.walk(f"/api/repository/{self.repo}/forks", "forks")
        self.assertEqual(total, 3)
        contributors, total = self.walk(f"/api/repository/{self.repo}/contributors", "contributors")
        self.assertGreaterEqual(total, 3)

    def test_branches_tags_releases_and_pull_requests(self):
        _, total = self.walk(f"/api/repository/{self.repo}/branches", "branches")
        self.assertEqual(total, 6)
        _, total = self.walk(f"/api/repository/{self.repo}/branches?details=true", "branches")
        self.assertEqual(total, 6)
        _, total = self.walk(f"/api/repository/{self.repo}/tags", "tags")
        self.assertEqual(total, self.count(Tag, Tag.repository_id == self.repo))
        releases, total = self.walk(f"/api/repository/{self.repo}/releases", "releases", limit=1)
        self.assertEqual(total, 3)
        latest = [r["version"] for r in releases if r.get("latest")]
        self.assertEqual(latest, ["v2.0.0"], "latest is decided across pages, not within one")
        prs, total = self.walk(f"/api/repository/{self.repo}/pull-requests", "pull_requests")
        self.assertEqual(total, 5)
        _, total = self.walk(f"/api/repository/{self.repo}/pull-requests?status=open", "pull_requests")
        self.assertEqual(total, 2)

    def test_the_default_branch_is_named_on_every_page(self):
        body = self.client.get(f"/api/repository/{self.repo}/branches?limit=2&offset=4",
                               headers=self.h()).json()
        self.assertNotIn("main", [b["name"] for b in body["branches"]])
        self.assertEqual(body["default_branch"], "main")

    def test_review_queues(self):
        _, total = self.walk("/api/pending-commits", "pending_commits")
        self.assertEqual(total, 7)
        _, total = self.walk("/api/pending-repositories", "pending_repositories")
        self.assertEqual(total, 5)
        _, total = self.walk("/api/admin/pending-user-registrations", "pending_registrations")
        self.assertEqual(total, 4)
        _, total = self.walk(f"/api/access-requests/pending?repo_id={self.repo}", "access_requests", limit=50)
        self.assertEqual(total, 105, "the queue used to stop at 100 without saying so")
        _, total = self.walk("/api/access-requests/pending", "access_requests", limit=50)
        self.assertEqual(total, 105)

    def test_a_team_lead_sees_only_the_review_queue_they_are_responsible_for(self):
        """Any team lead could list any repository's queue by naming it."""
        _, as_owner = self.walk(f"/api/repository/{self.repo}/pending-commits", "pending_commits", who="lead")
        self.assertEqual(as_owner, 7)
        items, as_other_lead = self.walk(f"/api/repository/{self.repo}/pending-commits", "pending_commits",
                                         who="lead2")
        self.assertEqual(as_other_lead, 3)
        self.assertEqual({i["author"] for i in items}, {"dev2"})
        _, across = self.walk("/api/pending-commits", "pending_commits", who="lead2")
        self.assertEqual(across, 3)
        _, mine = self.walk("/api/pending-repositories", "pending_repositories", who="lead2")
        self.assertEqual(mine, 2)

    def test_people_and_credentials(self):
        _, total = self.walk("/api/users", "users")
        self.assertEqual(total, self.count(User))
        response = self.client.get("/api/admin/users/engagement?limit=2", headers=self.h())
        self.assertEqual(response.json()["summary"]["total"], self.count(User),
                         "the summary counts everyone, not the page")
        self.walk("/api/admin/users/engagement", "users")
        _, total = self.walk("/api/admin/credentials", "users", limit=1)
        self.assertEqual(total, 2)
        _, total = self.walk("/api/tokens", "tokens", who="lead")
        self.assertEqual(total, 3)
        _, total = self.walk(f"/api/permissions/repository/{self.repo}", "permissions")
        self.assertEqual(total, self.count(UserPermission, UserPermission.repository_id == self.repo))
        _, total = self.walk("/api/permissions/user/dev", "permissions")
        self.assertEqual(total, 1)

    def test_history_feeds(self):
        _, total = self.walk(f"/api/repository/{self.repo}/commits", "commits")
        self.assertEqual(total, 5)
        _, total = self.walk(f"/api/repository/{self.repo}/issues", "issues")
        self.assertEqual(total, self.count(Issue, Issue.repository_id == self.repo))
        _, total = self.walk("/api/notifications", "notifications", who="lead")
        self.assertEqual(total, self.count(Notification, Notification.user_id == self.users["lead"]))
        self.walk("/api/activities", "activities")
        self.walk(f"/api/repository/{self.repo}/activity", "activities")

    # --- the rules -----------------------------------------------------------------------

    def test_limits_are_clamped_and_garbage_is_refused(self):
        big = self.client.get("/api/users?limit=999999", headers=self.h()).json()["pagination"]
        self.assertEqual(big["limit"], 1000)
        full = self.client.get(f"/api/repository/{self.repo}/commits?full=true&limit=999",
                               headers=self.h()).json()["pagination"]
        self.assertEqual(full["limit"], 200, "commits with their file lists are capped lower")
        self.assertEqual(self.client.get("/api/users?limit=abc", headers=self.h()).status_code, 422)
        past = self.client.get("/api/users?limit=5&offset=10000", headers=self.h()).json()
        self.assertEqual((past["users"], past["pagination"]["has_more"]), ([], False))

    def test_no_limit_means_the_default_page_not_everything(self):
        body = self.client.get(f"/api/access-requests/pending?repo_id={self.repo}", headers=self.h()).json()
        self.assertEqual(body["pagination"]["limit"], 200)
        self.assertEqual(len(body["access_requests"]), 105)

    def test_a_browser_on_another_origin_can_read_the_paging_headers(self):
        response = self.client.get("/api/users?limit=1", headers={**self.h(), "Origin": "http://localhost:5173"})
        exposed = response.headers.get("access-control-expose-headers", "").lower()
        for header in ("link", "x-total-count", "retry-after"):
            self.assertIn(header, exposed)


class OperationsApiTests(_AppTest):
    """Backups as jobs, restore drills, unlock and schema health over HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.build("foxnest_opsapi_")

    @classmethod
    def tearDownClass(cls):
        cls.teardown()

    def setUp(self):
        self.store = blob_store.BlobStore(self._tmp / f"blobs{time.time_ns()}")
        for patcher in (
            mock.patch.object(blob_store, "store", self.store),
            mock.patch.object(backup, "DOCS_DIR", self._tmp / "docs"),
            mock.patch.object(backup, "session_factory", self.Session),
            mock.patch.dict(os.environ, {"FOXNEST_BACKUP_DIR": str(self._tmp / "backups")}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def wait_for(self, backup_id, field, seconds=60):
        deadline = time.time() + seconds
        while time.time() < deadline:
            row = self.client.get(f"/api/admin/backups/{backup_id}", headers=self.h()).json()["backup"]
            if row[field] not in ("running", None):
                return row
            time.sleep(0.2)
        self.fail(f"backup {backup_id} still running")

    def test_a_backup_runs_as_a_job_and_is_verified(self):
        started = self.client.post("/api/admin/backups", headers=self.h(), json={"label": "api"})
        self.assertEqual(started.status_code, 202, started.text)
        self.assertEqual(started.json()["backup"]["status"], "running")
        done = self.wait_for(started.json()["backup"]["id"], "status")
        self.assertEqual((done["status"], done["verified"]), ("complete", True), done)

    def test_wait_runs_it_to_completion_and_the_drill_restores_it(self):
        made = self.client.post("/api/admin/backups?wait=true", headers=self.h(), json={"test_restore": True})
        self.assertEqual(made.status_code, 200, made.text)
        backup_row = made.json()["backup"]
        self.assertTrue(backup_row["verified"])
        self.assertEqual(backup_row["restore_test_status"], "passed", backup_row["restore_test_report"])
        drill = self.client.post(f"/api/admin/backups/{backup_row['id']}/restore-test?wait=true", headers=self.h())
        self.assertEqual(drill.json()["backup"]["restore_test_status"], "passed")
        verify = self.client.post(f"/api/admin/backups/{backup_row['id']}/verify?wait=true", headers=self.h())
        self.assertEqual(verify.json()["backup"]["verify_status"], "passed")
        listing = self.client.get("/api/admin/backups?limit=1", headers=self.h())
        body = listing.json()
        self.assertIn("summary", body)
        self.assertFalse(body["summary"]["stale"])
        self.assertIn("X-Total-Count", listing.headers)

    def test_a_second_job_is_refused_while_one_runs(self):
        with maintenance.acquire("a test job"):
            self.assertEqual(self.client.post("/api/admin/backups", headers=self.h(), json={}).status_code, 409)
            gc = self.client.post("/api/admin/gc?dry_run=false", headers=self.h())
            self.assertEqual(gc.status_code, 409)
            self.assertEqual(self.client.post("/api/admin/gc?dry_run=true", headers=self.h()).status_code, 200)

    def test_only_administrators_run_backups(self):
        self.assertEqual(self.client.post("/api/admin/backups", headers=self.h("dev"), json={}).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/backups", headers=self.h("dev")).status_code, 403)

    def test_unlocking_by_address_is_recorded(self):
        self.assertEqual(self.client.post("/api/admin/security/unlock", headers=self.h(), json={}).status_code, 400)
        response = self.client.post("/api/admin/security/unlock", headers=self.h(),
                                    json={"ip_address": "203.0.113.9"})
        self.assertEqual(response.status_code, 200, response.text)
        db = self.Session()
        try:
            row = db.query(Activity).filter(Activity.activity_type == "account_unlocked").first()
            self.assertIsNotNone(row)
            self.assertIn("203.0.113.9", row.description)
        finally:
            db.close()

    def test_schema_health_reports_the_revision_and_the_upgrades(self):
        body = self.client.get("/api/admin/schema", headers=self.h()).json()
        self.assertTrue(body["schema"]["healthy"], body["schema"])
        self.assertEqual(body["schema"]["revision"]["current"], body["schema"]["revision"]["head"])
        self.assertEqual(body["migrations"][0]["to_revision"], body["schema"]["revision"]["head"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
