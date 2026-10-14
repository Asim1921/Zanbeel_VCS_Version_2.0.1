"""Repository management over HTTP: branches, tags, releases, source archives, docs.

Each test pins a gap that was open:

* a branch or tag whose name has a slash (``feature/login``) could not be renamed,
  deleted, made default or moved -- the routes stopped at the first slash and said 404;
* a branch, tag or release could be pointed at another repository's commit;
* renaming a branch stranded the open pull requests that used it, and deleting one
  removed it from under them; a default-branch change to a missing branch, a duplicate
  tag and a missing tag all answered 500;
* a tag could be deleted out from under its release, one tag could carry two releases, a
  release could be neither edited nor withdrawn, and a new release's tag ignored where
  it was asked to point;
* there was no way to download the files at a tag, branch or commit;
* anyone signed in could generate documentation for any repository and have it
  committed; the generated commit rebuilt the default branch from every file in recent
  history on any branch -- deleted files came back, other branches' files arrived,
  binaries were dropped -- and moved the repository head past branch protection without
  moving the branch; the read routes reported 401, 403 and 404 as 500.
"""
import base64
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
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
_spec = importlib.util.spec_from_file_location('foxnest_server_repo_mgmt', SERVER_FILE)
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

from database.database import Base  # noqa: E402
from database.crud import RepositoryCRUD, UserCRUD, UserPermissionCRUD  # noqa: E402
from database.models import (  # noqa: E402
    AccountLock, Activity, Branch, Commit, LoginAttempt, PullRequest, Repository,
)
from app.services import docs_jobs  # noqa: E402
from app.services import webhooks as hook_service  # noqa: E402
from app.api.routes import docs as docs_routes  # noqa: E402
import autodocs_v2  # noqa: E402
try:
    from app.services import search_index  # noqa: E402
except ImportError:
    search_index = None

PASSWORD = "Password123!"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def b64(data):
    data = data.encode("utf-8") if isinstance(data, str) else data
    return base64.b64encode(data).decode("ascii")


class StubDocsGenerator:
    """Stands in for the LLM pipeline: writes one valid README listing what it was shown."""

    seen = []
    templates = []
    selected = []

    def __init__(self, templates_dir=None):
        type(self).templates.append(templates_dir)

    def generate(self, project_path, output_dir, llm_model, selected_docs=None,
                 project_name_override=None):
        root = Path(project_path)
        files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
        type(self).seen.append(files)
        type(self).selected.append(selected_docs)
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        readme = out / "README.md"
        body = "\n".join(f"- `{name}` is part of {project_name_override} and is described here." for name in files)
        readme.write_text(
            f"# {project_name_override}\n\n"
            "This document describes the project as it stands on its default branch, file by "
            "file, so that a new reader can find their way around it.\n\n"
            f"## Files\n\n{body}\n",
            encoding="utf-8",
        )
        return {"generated_files": [str(readme)], "stats": {"pipeline": "stub"}}


class RepositoryManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls._db_path = tempfile.mkstemp(prefix="foxnest_repomgmt_", suffix=".db")
        os.close(fd)
        cls._index_dir = tempfile.mkdtemp(prefix="foxnest_index_")
        cls._docs_dir = Path(tempfile.mkdtemp(prefix="foxnest_docs_"))
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
                               ("outsider", "developer"), ("lead2", "team_lead")):
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
        shutil.rmtree(cls._docs_dir, ignore_errors=True)
        try:
            os.remove(cls._db_path)
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
        for patcher in (
            mock.patch.object(hook_service, "dispatch", return_value=0),
            mock.patch.object(docs_jobs, "DOCS_DIR", self._docs_dir),
            mock.patch.object(docs_routes, "DOCS_DIR", self._docs_dir),
            mock.patch.object(docs_jobs, "SessionLocal", self.Session),
            mock.patch.object(autodocs_v2, "AutoDocsV2Service", StubDocsGenerator),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        StubDocsGenerator.seen = []
        StubDocsGenerator.templates = []
        StubDocsGenerator.selected = []
        with docs_jobs.DOCS_GENERATION_LOCK:
            docs_jobs.DOCS_GENERATION_JOBS.clear()

    # --- helpers -----------------------------------------------------------------------

    @classmethod
    def session(cls, username):
        if username not in cls._sessions:
            response = cls.client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
            assert response.status_code == 200, response.text
            cls._sessions[username] = response.json()["access_token"]
        return cls._sessions[username]

    def h(self, who="lead"):
        return {"Authorization": f"Bearer {self.session(who)}"}

    def new_repo(self, files=None, writers=("dev",), owner="lead", name=None):
        type(self)._counter += 1
        db = self.Session()
        try:
            repo = RepositoryCRUD.create_repository(db, owner, name or f"tool{self._counter}", "test")
            for user in writers:
                UserPermissionCRUD.create_permission(db, user, repo.id, "write", owner)
            repo_id = repo.id
        finally:
            db.close()
        self.push(repo_id, "main", files or {"app.py": "print('hello')\n", "README.md": "readme\n"},
                  "base", who=owner)
        return repo_id

    def head(self, repo_id, branch):
        db = self.Session()
        try:
            row = db.query(Branch).filter(Branch.repository_id == repo_id, Branch.name == branch).first()
            return row.head_commit_id if row else None
        finally:
            db.close()

    def tree(self, commit_id):
        db = self.Session()
        try:
            commit = db.query(Commit).filter(Commit.id == commit_id).first()
            return {f.file_path.replace("\\", "/"): f.file_object.content for f in commit.files}
        finally:
            db.close()

    def push(self, repo_id, branch, files, message, who="lead", parent_branch=None, expect=200):
        parent = self.head(repo_id, branch) or (self.head(repo_id, parent_branch) if parent_branch else None)
        commit_id = hashlib.sha1(f"{repo_id}{branch}{message}{time.time_ns()}".encode()).hexdigest()
        response = self.client.post(f"/api/repository/{repo_id}/push", headers=self.h(who), json={
            "branch": branch,
            "commit": {"id": commit_id, "author": who, "message": message,
                       "parent": parent, "parents": [parent] if parent else [],
                       "files": {path: b64(data) for path, data in files.items()}},
        })
        if expect is not None:
            self.assertEqual(response.status_code, expect, response.text)
        return commit_id

    def branch(self, repo_id, name, who="lead", **body):
        body = body or {"from_branch": "main"}
        return self.client.post(f"/api/repository/{repo_id}/branches", headers=self.h(who),
                                json={"name": name, **body})

    def open_pr(self, repo_id, source, target="main"):
        response = self.client.post(f"/api/repository/{repo_id}/pull-requests", headers=self.h(), json={
            "title": f"{source} into {target}", "source_branch": source, "target_branch": target})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["pull_request"]["id"]

    def enc(self, name):
        return name.replace("/", "%2F")

    # --- branches ------------------------------------------------------------------------

    def test_a_branch_whose_name_has_a_slash_can_be_managed(self):
        """The routes stopped at the first slash: every one of these answered 404."""
        repo = self.new_repo()
        self.assertEqual(self.branch(repo, "feature/login").status_code, 200)

        renamed = self.client.put(f"/api/repository/{repo}/branches/feature/login/rename",
                                  headers=self.h(), json={"new_name": "feature/sign-in"})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        # Percent-encoded, as the web client sends it, too.
        tip = self.push(repo, "feature/sign-in", {"app.py": "v2\n"}, "work")
        moved = self.client.put(f"/api/repository/{repo}/branches/{self.enc('feature/sign-in')}/head",
                                headers=self.h(), json={"head_commit_id": self.head(repo, "main")})
        self.assertEqual(moved.status_code, 200, moved.text)
        moved = self.client.put(f"/api/repository/{repo}/branches/feature/sign-in/head",
                                headers=self.h(), json={"head_commit_id": tip})
        self.assertEqual(moved.status_code, 200, moved.text)

        made_default = self.client.put(f"/api/repository/{repo}/branches/feature/sign-in/default",
                                       headers=self.h())
        self.assertEqual(made_default.status_code, 200, made_default.text)
        self.assertEqual(self.client.put(f"/api/repository/{repo}/branches/main/default",
                                         headers=self.h()).status_code, 200)
        deleted = self.client.delete(f"/api/repository/{repo}/branches/feature/sign-in", headers=self.h())
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertIsNone(self.head(repo, "feature/sign-in"))

    def test_the_branch_list_reports_last_commit_distance_protection_and_pull_requests(self):
        repo = self.new_repo()
        self.assertEqual(self.branch(repo, "feature").status_code, 200)
        self.push(repo, "feature", {"app.py": "f1\n"}, "feature one")
        self.push(repo, "feature", {"app.py": "f2\n"}, "feature two")
        self.push(repo, "main", {"app.py": "m2\n", "README.md": "readme\n"}, "main moves on")
        self.client.put(f"/api/repository/{repo}/branch-protection", headers=self.h(),
                        json={"branch_pattern": "main", "mode": "protected"})
        pr = self.open_pr(repo, "feature")

        body = self.client.get(f"/api/repository/{repo}/branches", headers=self.h(),
                               params={"details": "true"}).json()
        rows = {b["name"]: b for b in body["branches"]}
        self.assertEqual(body["default_branch"], "main")
        self.assertEqual((rows["feature"]["ahead"], rows["feature"]["behind"]), (2, 1))
        self.assertEqual((rows["main"]["ahead"], rows["main"]["behind"]), (0, 0))
        self.assertEqual(rows["feature"]["head_commit"]["message"], "feature two")
        self.assertEqual(rows["feature"]["head_commit"]["author"], "lead")
        self.assertEqual(rows["main"]["protection"]["mode"], "protected")
        self.assertFalse(rows["main"]["protection"]["permits"]["delete"])
        self.assertEqual(rows["feature"]["protection"]["mode"], "open")
        self.assertEqual([p["id"] for p in rows["feature"]["pull_requests"]], [pr])
        self.assertEqual(body["permissions"], {"can_write": True, "can_manage": True})
        dev = self.client.get(f"/api/repository/{repo}/branches", headers=self.h("dev")).json()
        self.assertEqual(dev["permissions"], {"can_write": True, "can_manage": False})

    def test_a_branch_cannot_start_at_or_move_to_another_repositorys_commit(self):
        mine = self.new_repo()
        other = self.new_repo(files={"secret.txt": "other repository's file\n"}, writers=())
        foreign = self.head(other, "main")

        created = self.branch(mine, "leak", from_commit=foreign)
        self.assertEqual(created.status_code, 404, created.text)
        self.assertIsNone(self.head(mine, "leak"))
        self.assertEqual(self.branch(mine, "side").status_code, 200)
        moved = self.client.put(f"/api/repository/{mine}/branches/side/head", headers=self.h(),
                                json={"head_commit_id": foreign})
        self.assertEqual(moved.status_code, 404, moved.text)
        self.assertEqual(self.head(mine, "side"), self.head(mine, "main"))

    def test_a_fork_may_still_use_the_commits_it_shares_with_its_upstream(self):
        upstream = self.new_repo()
        old = self.head(upstream, "main")
        self.push(upstream, "main", {"app.py": "v2\n"}, "second")
        forked = self.client.post(f"/api/repository/{upstream}/fork", headers=self.h("lead2"),
                                  json={"name": f"fork{self._counter}"})
        self.assertEqual(forked.status_code, 200, forked.text)
        fork_id = forked.json()["repo_id"]
        self.assertEqual(self.branch(fork_id, "from-old", who="lead2", from_commit=old).status_code, 200)
        tagged = self.client.post(f"/api/repository/{fork_id}/tags", headers=self.h("lead2"),
                                  json={"name": "v0", "commit_id": old})
        self.assertEqual(tagged.status_code, 200, tagged.text)

    def test_renaming_a_branch_carries_its_pull_requests_and_its_own_rule(self):
        repo = self.new_repo()
        self.branch(repo, "topic")
        self.push(repo, "topic", {"app.py": "topic\n"}, "topic work")
        pr = self.open_pr(repo, "topic")
        self.client.put(f"/api/repository/{repo}/branch-protection", headers=self.h(),
                        json={"branch_pattern": "topic", "mode": "open",
                              "rules": {"required_approvals": 2}})

        renamed = self.client.put(f"/api/repository/{repo}/branches/topic/rename", headers=self.h(),
                                  json={"new_name": "topic-renamed"})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["retargeted_pull_requests"], [pr])
        shown = self.client.get(f"/api/repository/{repo}/pull-requests/{pr}", headers=self.h())
        self.assertEqual(shown.status_code, 200, shown.text)
        self.assertEqual(json.dumps(shown.json()).count('"topic-renamed"') >= 1, True)
        db = self.Session()
        try:
            self.assertEqual(db.query(PullRequest).get(pr).source_branch, "topic-renamed")
        finally:
            db.close()
        rules = self.client.get(f"/api/repository/{repo}/branch-protection", headers=self.h()).json()
        self.assertIn("topic-renamed", json.dumps(rules))

    def test_a_branch_an_open_pull_request_uses_is_not_deleted_and_a_deleted_one_can_be_restored(self):
        repo = self.new_repo()
        self.branch(repo, "busy")
        tip = self.push(repo, "busy", {"app.py": "busy\n"}, "busy work")
        pr = self.open_pr(repo, "busy")

        refused = self.client.delete(f"/api/repository/{repo}/branches/busy", headers=self.h())
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(refused.json()["detail"]["code"], "BRANCH_IN_USE")
        self.assertEqual(self.head(repo, "busy"), tip)

        self.client.post(f"/api/repository/{repo}/pull-requests/{pr}/close", headers=self.h())
        deleted = self.client.delete(f"/api/repository/{repo}/branches/busy", headers=self.h())
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["branch"]["head_commit_id"], tip)
        restored = self.branch(repo, "busy", from_commit=deleted.json()["branch"]["head_commit_id"])
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(self.head(repo, "busy"), tip)

    def test_missing_branches_and_moved_branches_answer_precisely(self):
        repo = self.new_repo()
        self.assertEqual(self.client.put(f"/api/repository/{repo}/branches/nope/default",
                                         headers=self.h()).status_code, 404)
        self.assertEqual(self.client.put(f"/api/repository/{repo}/branches/nope/rename", headers=self.h(),
                                         json={"new_name": "still-nope"}).status_code, 404)
        self.branch(repo, "side")
        stale = self.client.put(f"/api/repository/{repo}/branches/side/head", headers=self.h(),
                                json={"head_commit_id": self.head(repo, "main"),
                                      "expected_head_commit_id": "0" * 40})
        self.assertEqual(stale.status_code, 409, stale.text)

    # --- tags ----------------------------------------------------------------------------

    def test_tags_answer_409_and_404_rather_than_500(self):
        repo = self.new_repo()
        first = self.client.post(f"/api/repository/{repo}/tags", headers=self.h(), json={"name": "v1.0.0"})
        self.assertEqual(first.status_code, 200, first.text)
        again = self.client.post(f"/api/repository/{repo}/tags", headers=self.h(), json={"name": "v1.0.0"})
        self.assertEqual(again.status_code, 409, again.text)
        missing = self.client.delete(f"/api/repository/{repo}/tags/v9.9.9", headers=self.h())
        self.assertEqual(missing.status_code, 404, missing.text)
        bad = self.client.post(f"/api/repository/{repo}/tags", headers=self.h(), json={"name": "bad name"})
        self.assertEqual(bad.status_code, 400, bad.text)
        self.assertIn("Tag name", bad.json()["detail"])

    def test_a_tag_cannot_name_another_repositorys_commit(self):
        mine = self.new_repo()
        other = self.new_repo(files={"secret.txt": "not yours\n"}, writers=())
        response = self.client.post(f"/api/repository/{mine}/tags", headers=self.h("dev"),
                                    json={"name": "leak", "commit_id": self.head(other, "main")})
        self.assertEqual(response.status_code, 404, response.text)

    def test_a_tag_whose_name_has_a_slash_can_be_deleted(self):
        repo = self.new_repo()
        self.client.post(f"/api/repository/{repo}/tags", headers=self.h(), json={"name": "release/1.0"})
        response = self.client.delete(f"/api/repository/{repo}/tags/release/1.0", headers=self.h())
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_new_tag_names_the_default_branch_or_the_target_given(self):
        """repository.head_commit_id follows any push, to any branch; "current" is main."""
        repo = self.new_repo()
        main_tip = self.head(repo, "main")
        self.branch(repo, "feature")
        feature_tip = self.push(repo, "feature", {"app.py": "feature\n"}, "feature work")

        plain = self.client.post(f"/api/repository/{repo}/tags", headers=self.h(), json={"name": "t-main"})
        self.assertEqual(plain.json()["tag"]["commit_id"], main_tip)
        targeted = self.client.post(f"/api/repository/{repo}/tags", headers=self.h(),
                                    json={"name": "t-feature", "target": "feature"})
        self.assertEqual(targeted.json()["tag"]["commit_id"], feature_tip)
        short = self.client.post(f"/api/repository/{repo}/tags", headers=self.h(),
                                 json={"name": "t-short", "target": feature_tip[:10]})
        self.assertEqual(short.json()["tag"]["commit_id"], feature_tip)

        listed = self.client.get(f"/api/repository/{repo}/tags", headers=self.h()).json()
        row = next(t for t in listed["tags"] if t["name"] == "t-feature")
        self.assertEqual(row["commit"]["message"], "feature work")

    def test_a_tag_under_a_release_is_not_deleted(self):
        repo = self.new_repo()
        self.client.post(f"/api/repository/{repo}/releases", headers=self.h(), json={"version": "1.0.0"})
        response = self.client.delete(f"/api/repository/{repo}/tags/v1.0.0", headers=self.h())
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "TAG_IN_USE")
        listed = self.client.get(f"/api/repository/{repo}/releases", headers=self.h()).json()["releases"]
        self.assertEqual(listed[0]["commit_id"], self.head(repo, "main"))

    def test_tag_permissions(self):
        repo = self.new_repo(writers=("dev",))
        self.assertEqual(self.client.get(f"/api/repository/{repo}/tags",
                                         headers=self.h("outsider")).status_code, 403)
        created = self.client.post(f"/api/repository/{repo}/tags", headers=self.h("dev"), json={"name": "dev-tag"})
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(self.client.delete(f"/api/repository/{repo}/tags/dev-tag",
                                            headers=self.h("dev")).status_code, 403)

    # --- releases ------------------------------------------------------------------------

    def test_a_release_tags_its_target_and_leaves_nothing_behind_when_refused(self):
        repo = self.new_repo()
        self.branch(repo, "release-branch")
        tip = self.push(repo, "release-branch", {"app.py": "rc\n"}, "release candidate")

        published = self.client.post(f"/api/repository/{repo}/releases", headers=self.h(),
                                     json={"version": "2.0.0", "target": "release-branch"})
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()["release"]["commit_id"], tip)

        refused = self.client.post(f"/api/repository/{repo}/releases", headers=self.h(),
                                   json={"version": "2.1.0", "target": "no-such-branch"})
        self.assertEqual(refused.status_code, 404, refused.text)
        tags = [t["name"] for t in self.client.get(f"/api/repository/{repo}/tags", headers=self.h()).json()["tags"]]
        self.assertNotIn("v2.1.0", tags)

    def test_one_tag_carries_one_release(self):
        repo = self.new_repo()
        self.client.post(f"/api/repository/{repo}/releases", headers=self.h(), json={"version": "1.0.0"})
        second = self.client.post(f"/api/repository/{repo}/releases", headers=self.h(),
                                  json={"version": "1.0.1", "tag": "v1.0.0"})
        self.assertEqual(second.status_code, 409, second.text)
        duplicate = self.client.post(f"/api/repository/{repo}/releases", headers=self.h(),
                                     json={"version": "1.0.0"})
        self.assertEqual(duplicate.status_code, 409, duplicate.text)

    def test_a_release_can_be_edited_and_withdrawn_keeping_its_tag(self):
        repo = self.new_repo(writers=("dev",))
        self.client.post(f"/api/repository/{repo}/releases", headers=self.h(),
                         json={"version": "1.0.0", "title": "First", "notes": "old notes"})
        edited = self.client.patch(f"/api/repository/{repo}/releases/1.0.0", headers=self.h("dev"),
                                   json={"title": "First release", "notes": "## Fixed\n- typo"})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(sorted(edited.json()["changed"]), ["notes", "title"])
        listed = self.client.get(f"/api/repository/{repo}/releases", headers=self.h()).json()["releases"]
        self.assertEqual((listed[0]["title"], listed[0]["notes"]), ("First release", "## Fixed\n- typo"))

        self.assertEqual(self.client.delete(f"/api/repository/{repo}/releases/1.0.0",
                                            headers=self.h("dev")).status_code, 403)
        withdrawn = self.client.delete(f"/api/repository/{repo}/releases/1.0.0", headers=self.h())
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
        self.assertEqual(self.client.get(f"/api/repository/{repo}/releases", headers=self.h()).json()["releases"], [])
        tags = [t["name"] for t in self.client.get(f"/api/repository/{repo}/tags", headers=self.h()).json()["tags"]]
        self.assertIn("v1.0.0", tags)
        self.assertEqual(self.client.patch(f"/api/repository/{repo}/releases/1.0.0", headers=self.h(),
                                           json={"title": "x"}).status_code, 404)
        kinds = {r.activity_type for r in self._activity(repo)}
        self.assertTrue({"release_created", "release_updated", "release_deleted"} <= kinds, kinds)

    def test_latest_is_the_highest_stable_version_and_prereleases_are_marked(self):
        repo = self.new_repo()
        for version in ("1.9.0", "1.10.0", "2.0.0-rc.1", "1.2.1"):
            self.push(repo, "main", {"app.py": version}, f"for {version}")
            response = self.client.post(f"/api/repository/{repo}/releases", headers=self.h(),
                                        json={"version": version})
            self.assertEqual(response.status_code, 200, response.text)
        listed = self.client.get(f"/api/repository/{repo}/releases", headers=self.h()).json()["releases"]
        self.assertEqual([r["version"] for r in listed], ["1.2.1", "2.0.0-rc.1", "1.10.0", "1.9.0"])
        self.assertEqual([r["version"] for r in listed if r["latest"]], ["1.10.0"])
        self.assertEqual([r["version"] for r in listed if r["prerelease"]], ["2.0.0-rc.1"])

    def test_release_notes_are_drafted_from_what_changed_since_the_previous_release(self):
        repo = self.new_repo()
        self.client.post(f"/api/repository/{repo}/releases", headers=self.h(), json={"version": "1.0.0"})
        self.push(repo, "main", {"app.py": "fix\n"}, "Fix the login redirect")
        self.branch(repo, "feature/search")
        self.push(repo, "feature/search", {"app.py": "search\n"}, "Add search")
        pr = self.open_pr(repo, "feature/search")
        merged = self.client.post(f"/api/repository/{repo}/pull-requests/{pr}/merge", headers=self.h())
        self.assertEqual(merged.status_code, 200, merged.text)

        draft = self.client.get(f"/api/repository/{repo}/releases/notes", headers=self.h()).json()
        self.assertEqual(draft["previous_tag"], "v1.0.0")
        self.assertIn("Fix the login redirect", draft["notes"])
        self.assertIn(f"(#{pr})", draft["notes"])
        self.assertIn("v1.0.0...main", draft["notes"])
        self.assertNotIn("base", draft["notes"].split("**Full changelog**")[0])

    # --- source archives -----------------------------------------------------------------

    def test_the_files_at_a_tag_branch_or_commit_download_as_a_zip(self):
        repo = self.new_repo(files={"app.py": "v1\n", "docs/guide.md": "guide\n", "assets/logo.png": PNG})
        name = self._repo_name(repo)
        self.client.post(f"/api/repository/{repo}/tags", headers=self.h(), json={"name": "v1"})
        self.push(repo, "main", {"app.py": "v2\n", "assets/logo.png": PNG}, "v2")

        def files(ref):
            response = self.client.get(f"/api/repository/{repo}/source.zip", headers=self.h(),
                                       params={"ref": ref} if ref else None)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["content-type"], "application/zip")
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                return {n: archive.read(n) for n in archive.namelist()}

        at_tag = files("v1")
        self.assertEqual(at_tag[f"{name}-v1/app.py"], b"v1\n")
        self.assertEqual(at_tag[f"{name}-v1/assets/logo.png"], PNG)
        self.assertIn(f"{name}-v1/docs/guide.md", at_tag)
        at_main = files(None)
        self.assertEqual(at_main[f"{name}-main/app.py"], b"v2\n")
        self.assertNotIn(f"{name}-main/docs/guide.md", at_main)
        self.assertEqual(self.client.get(f"/api/repository/{repo}/source.zip", headers=self.h("outsider"),
                                         params={"ref": "v1"}).status_code, 403)
        self.assertEqual(self.client.get(f"/api/repository/{repo}/source.zip", headers=self.h(),
                                         params={"ref": "nothing-here"}).status_code, 404)

    # --- generated documentation ---------------------------------------------------------

    def generate(self, repo, who="lead", **params):
        response = self.client.post(f"/api/repository/{repo}/generate-project-docs", headers=self.h(who),
                                    params={"selected_docs": "readme", **params})
        return response

    def status(self, repo, who="lead"):
        response = self.client.get(f"/api/repository/{repo}/docs-status", headers=self.h(who))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_only_writers_generate_docs_and_only_readers_see_them(self):
        repo = self.new_repo(writers=("dev",))
        refused = self.generate(repo, who="outsider")
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(self.client.get(f"/api/repository/{repo}/docs-status",
                                         headers=self.h("outsider")).status_code, 403)
        self.assertEqual(self.client.get(f"/api/repository/{repo}/docs").status_code, 401)
        self.assertEqual(self.client.get(f"/api/repository/{repo}/docs",
                                         headers=self.h("outsider")).status_code, 403)
        self.assertEqual(self.client.get(f"/api/repository/{repo}/docs/index.md",
                                         headers=self.h("outsider")).status_code, 403)
        self.assertEqual(self.client.get("/api/repository/0000000000000000/docs",
                                         headers=self.h()).status_code, 404)

    def test_generated_docs_land_on_the_default_branch_and_change_nothing_else(self):
        repo = self.new_repo(files={"app.py": "v1\n", "old.txt": "removed later\n",
                                    "assets/logo.png": PNG, "docs/handwritten.md": "# Mine\n"})
        self.push(repo, "main", {"app.py": "v2\n", "assets/logo.png": PNG,
                                 "docs/handwritten.md": "# Mine\n"}, "remove old.txt")
        self.branch(repo, "experiment")
        self.push(repo, "experiment", {"app.py": "v2\n", "experiment.py": "x = 1\n"}, "experiment")
        base = self.head(repo, "main")
        before = self.tree(base)

        started = self.generate(repo)
        self.assertEqual(started.status_code, 200, started.text)
        state = self.status(repo)
        self.assertEqual(state["status"], "completed", state)

        # The generator was shown main as it is: no deleted file, no other branch.
        self.assertEqual(StubDocsGenerator.seen[-1], ["app.py"])
        # The docs commit, wherever it went: it is the repository head either way.
        db = self.Session()
        try:
            docs_commit = db.query(Repository).get(repo).head_commit_id
        finally:
            db.close()
        after = self.tree(docs_commit)
        for path, content in before.items():
            self.assertEqual(after.get(path), content, path)
        self.assertNotIn("old.txt", after)
        self.assertNotIn("experiment.py", after)
        added = sorted(set(after) - set(before))
        self.assertIn("docs/project/README.md", added)
        self.assertTrue(all(p.startswith("docs/") for p in added), added)
        # And it is on the branch, as a child of the tip it describes.
        tip = self.head(repo, "main")
        self.assertEqual(tip, docs_commit)
        db = self.Session()
        try:
            commit = db.query(Commit).get(tip)
            self.assertEqual(commit.parent_commit_id, base)
            self.assertEqual(commit.author.username, "lead")
        finally:
            db.close()
        self.assertTrue(state["result"]["commit"]["committed"], state)

        # One activity row: the description and the branch move together.
        rows = [r for r in self._activity(repo) if r.commit_id == tip]
        self.assertEqual([r.activity_type for r in rows], ["generate_project_docs"])
        self.assertEqual(json.loads(rows[0].payload_json)["ref"]["before"], base)

        listing = self.client.get(f"/api/repository/{repo}/docs", headers=self.h()).json()
        self.assertTrue(listing["up_to_date"])
        self.assertEqual(listing["last_generation"]["source_commit"], base)
        self.assertIn("project/README.md", [f["path"] for f in listing["files"]])
        self.push(repo, "main", {**{p: c for p, c in after.items()}, "app.py": "v3\n"}, "newer code")
        self.assertFalse(self.client.get(f"/api/repository/{repo}/docs", headers=self.h()).json()["up_to_date"])

    def test_docs_are_not_committed_past_branch_protection(self):
        repo = self.new_repo()
        self.client.put(f"/api/repository/{repo}/branch-protection", headers=self.h(),
                        json={"branch_pattern": "main", "mode": "protected"})
        base = self.head(repo, "main")
        self.generate(repo)
        state = self.status(repo)
        self.assertEqual(state["status"], "completed", state)
        db = self.Session()
        try:
            self.assertEqual(db.query(Repository).get(repo).head_commit_id, base)
        finally:
            db.close()
        self.assertEqual(self.head(repo, "main"), base)
        commit = state["result"]["commit"]
        self.assertFalse(commit["committed"])
        self.assertIn("protected", commit["reason"])
        self.assertFalse(state["last_generation"]["committed"])
        listing = self.client.get(f"/api/repository/{repo}/docs", headers=self.h()).json()
        self.assertIn("project/README.md", [f["path"] for f in listing["files"]])

    def test_a_developers_docs_are_kept_but_not_committed(self):
        repo = self.new_repo(writers=("dev",))
        base = self.head(repo, "main")
        self.assertEqual(self.generate(repo, who="dev").status_code, 200)
        state = self.status(repo, who="dev")
        self.assertEqual(state["status"], "completed", state)
        db = self.Session()
        try:
            # Not even the repository head: a developer's change waits for review.
            self.assertEqual(db.query(Repository).get(repo).head_commit_id, base)
        finally:
            db.close()
        self.assertFalse(state["result"]["commit"]["committed"])
        self.assertIn("review", state["result"]["commit"]["reason"])
        self.assertEqual(state["last_generation"]["generated_by"], "dev")

    def test_the_generator_is_given_its_templates_and_the_documents_asked_for(self):
        """The template path was relative to a file that moved out of server.py, so every
        document was skipped as "Missing template"; and the selection reached it as keys
        ("readme") where it matches names ("README.md"), so any selection wrote only the
        index. Either way, generation from the server produced nothing."""
        repo = self.new_repo()
        self.generate(repo, commit="false", selected_docs="readme,architecture")
        self.assertTrue(StubDocsGenerator.templates, "the generator was never constructed")
        folder = Path(StubDocsGenerator.templates[-1])
        self.assertTrue((folder / "TEMPLATE_README.md").is_file(), folder)
        self.assertEqual(sorted(StubDocsGenerator.selected[-1]), ["Architecture", "README.md"])

    def test_docs_can_be_generated_without_committing(self):
        repo = self.new_repo()
        base = self.head(repo, "main")
        self.generate(repo, commit="false")
        db = self.Session()
        try:
            self.assertEqual(db.query(Repository).get(repo).head_commit_id, base)
        finally:
            db.close()
        self.assertFalse(self.status(repo)["result"]["commit"]["committed"])

    def test_docs_files_are_served_inert_and_the_manifest_stays_private(self):
        repo = self.new_repo()
        self.generate(repo, commit="false")
        page = self.client.get(f"/api/repository/{repo}/docs/project/README.md", headers=self.h())
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.headers["x-content-type-options"], "nosniff")
        self.assertIn("sandbox", page.headers["content-security-policy"])
        self.assertEqual(self.client.get(f"/api/repository/{repo}/docs/{docs_jobs.MANIFEST_NAME}",
                                         headers=self.h()).status_code, 404)
        self.assertEqual(self.client.get(f"/api/repository/{repo}/docs/..%2F..%2Fserver.py",
                                         headers=self.h()).status_code, 404)
        archive = self.client.get(f"/api/repository/{repo}/docs/archive.zip", headers=self.h())
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
            names = zf.namelist()
        self.assertIn("project/README.md", names)
        self.assertFalse(any(n.startswith(".") for n in names), names)

    # --- small helpers -------------------------------------------------------------------

    def _activity(self, repo_id):
        db = self.Session()
        try:
            return db.query(Activity).filter(Activity.repository_id == repo_id).order_by(Activity.id).all()
        finally:
            db.close()

    def _repo_name(self, repo_id):
        db = self.Session()
        try:
            return db.query(Repository).get(repo_id).name
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
