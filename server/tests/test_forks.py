"""Tests for forks and cross-repository pull requests.

The property that makes forking cheap is that blobs are content-addressed and shared, so
a fork copies refs and metadata but not content. These tests check that the copy is
complete enough to work from, and that the shared blobs are not duplicated.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.services import blob_store, branch_ops, forks  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from app.services.forks import ForkError  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import BranchCRUD, FileObjectCRUD, RepositoryCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, Commit, CommitFile, FileObject, PullRequest, Repository, Tag, User,
)


class ForkTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_fk_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_fk_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.owner = User(username="owner", role="team_lead")
        self.outsider = User(username="outsider", role="developer")
        self.db.add_all([self.owner, self.outsider])
        self.db.commit()

        self.upstream = RepositoryCRUD.create_repository(self.db, "owner", "project", "orig")
        self.mk(self.upstream.id, "c1", {"a.py": "original", "README.md": "docs"})
        # create_repository already made a default 'main'; point it at the commit
        # rather than adding a second row with the same name.
        BranchCRUD.update_branch_head(self.db, self.upstream.id, "main", "c1")
        self.db.add(Branch(repository_id=self.upstream.id, name="dev", head_commit_id="c1"))
        self.db.add(Tag(repository_id=self.upstream.id, name="v1", commit_id="c1"))
        self.upstream.head_commit_id = "c1"
        self.db.commit()

    def tearDown(self):
        blob_store.store = self._real_store
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def mk(self, repo_id, cid, files, parent=None, author=None):
        self.db.add(Commit(id=cid, repository_id=repo_id,
                           author_id=(author or self.owner).id,
                           message=cid, parent_commit_id=parent))
        self.db.flush()
        for path, text in files.items():
            obj = FileObjectCRUD.store_file_object(self.db, text.encode())
            self.db.add(CommitFile(commit_id=cid, file_path=path,
                                   file_hash=obj.hash, file_size=obj.size))
        self.db.commit()

    # ---------------- forking ----------------

    def test_a_fork_is_owned_by_the_forker_and_points_home(self):
        fork = forks.fork_repository(self.db, self.upstream, self.outsider)
        self.assertEqual(fork.owner_id, self.outsider.id)
        self.assertEqual(fork.forked_from_id, self.upstream.id)
        self.assertNotEqual(fork.id, self.upstream.id)

    def test_branches_and_tags_come_across(self):
        fork = forks.fork_repository(self.db, self.upstream, self.outsider)
        names = {b.name for b in self.db.query(Branch).filter(
            Branch.repository_id == fork.id).all()}
        self.assertEqual(names, {"main", "dev"})
        default = BranchCRUD.get_branch(self.db, fork.id, "main")
        self.assertTrue(default.is_default)
        self.assertEqual(default.head_commit_id, "c1")

        tags = {t.name for t in self.db.query(Tag).filter(Tag.repository_id == fork.id).all()}
        self.assertEqual(tags, {"v1"})

    def test_forking_does_not_duplicate_blobs(self):
        before = self.db.query(FileObject).count()
        forks.fork_repository(self.db, self.outsider and self.upstream, self.outsider)
        self.assertEqual(self.db.query(FileObject).count(), before)

    def test_the_fork_can_read_the_original_content(self):
        fork = forks.fork_repository(self.db, self.upstream, self.outsider)
        from app.services.commit_graph import _get_commit_tree
        tree = _get_commit_tree(self.db, BranchCRUD.get_branch(self.db, fork.id, "main").head_commit_id)
        self.assertEqual(tree["a.py"], b"original")

    def test_a_fork_starts_with_default_branch_policy(self):
        branch_ops.set_policy(self.db, self.upstream, {"required_approvals": 3})
        fork = forks.fork_repository(self.db, self.upstream, self.outsider)
        # The upstream's rules are the upstream's decision, not this repository's.
        self.assertIsNone(fork.branch_policy_json)
        self.assertEqual(branch_ops.get_policy(fork)["required_approvals"], 0)

    def test_you_cannot_fork_your_own_repository(self):
        with self.assertRaises(ForkError):
            forks.fork_repository(self.db, self.upstream, self.owner)

    def test_forking_twice_under_the_same_name_is_refused(self):
        forks.fork_repository(self.db, self.upstream, self.outsider)
        with self.assertRaises(ForkError) as ctx:
            forks.fork_repository(self.db, self.upstream, self.outsider)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_a_fork_can_be_given_a_different_name(self):
        fork = forks.fork_repository(self.db, self.upstream, self.outsider, name="my-project")
        self.assertEqual(fork.name, "my-project")

    def test_describe_reports_both_directions(self):
        fork = forks.fork_repository(self.db, self.upstream, self.outsider)

        up = forks.describe(self.db, self.upstream)
        self.assertFalse(up["is_fork"])
        self.assertEqual([f["repo_id"] for f in up["forks"]], [fork.id])

        down = forks.describe(self.db, fork)
        self.assertTrue(down["is_fork"])
        self.assertEqual(down["forked_from"]["repo_id"], self.upstream.id)
        self.assertEqual(down["forked_from"]["owner"], "owner")

    # ---------------- cross-repository pull requests ----------------

    def open_pr(self, fork, **kw):
        kw.setdefault("source_branch", "main")
        kw.setdefault("target_branch", "main")
        kw.setdefault("title", "please take my change")
        return forks.open_cross_repo_pull_request(
            self.db, fork, self.upstream, self.outsider, **kw)

    def test_a_fork_can_propose_back_upstream(self):
        fork = forks.fork_repository(self.db, self.upstream, self.outsider)
        self.mk(fork.id, "f1", {"a.py": "improved", "README.md": "docs"}, "c1",
                author=self.outsider)
        BranchCRUD.update_branch_head(self.db, fork.id, "main", "f1")

        pr = self.open_pr(fork)
        self.assertEqual(pr.repository_id, self.upstream.id)   # lives upstream
        self.assertEqual(pr.source_repository_id, fork.id)     # sourced from the fork
        self.assertEqual(pr.status, "open")

    def test_the_contributor_needs_no_write_access_upstream(self):
        # outsider is a plain developer with no permission row on the upstream at all.
        from app.services.branch_ops import actor_scope
        self.assertEqual(actor_scope(self.db, self.outsider, self.upstream), "read")

        fork = forks.fork_repository(self.db, self.upstream, self.outsider)
        self.assertIsNotNone(self.open_pr(fork))

    def test_a_repository_that_is_not_a_fork_cannot_propose(self):
        unrelated = RepositoryCRUD.create_repository(self.db, "outsider", "unrelated")
        BranchCRUD.update_branch_head(self.db, unrelated.id, "main", "c1")
        with self.assertRaises(ForkError):
            forks.open_cross_repo_pull_request(
                self.db, unrelated, self.upstream, self.outsider, "main", "main", "t")

    def test_a_missing_branch_is_reported(self):
        fork = forks.fork_repository(self.db, self.upstream, self.outsider)
        with self.assertRaises(ForkError) as ctx:
            self.open_pr(fork, source_branch="nope")
        self.assertEqual(ctx.exception.status_code, 404)

        with self.assertRaises(ForkError):
            self.open_pr(fork, target_branch="nope")

    def test_a_duplicate_open_pull_request_is_refused(self):
        fork = forks.fork_repository(self.db, self.upstream, self.outsider)
        self.open_pr(fork)
        with self.assertRaises(ForkError) as ctx:
            self.open_pr(fork)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_a_title_is_required(self):
        fork = forks.fork_repository(self.db, self.upstream, self.outsider)
        with self.assertRaises(ForkError):
            self.open_pr(fork, title="   ")

    def test_the_upstream_sees_the_pull_request(self):
        fork = forks.fork_repository(self.db, self.upstream, self.outsider)
        pr = self.open_pr(fork)
        found = self.db.query(PullRequest).filter(
            PullRequest.repository_id == self.upstream.id).all()
        self.assertEqual([p.id for p in found], [pr.id])


if __name__ == "__main__":
    unittest.main()
