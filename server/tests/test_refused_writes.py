"""A refused operation must leave nothing behind.

Every path that lands content does the same two things in the same order: create the
commit, then move the reference. But ``create_commit*`` commits its own transaction and
moves ``repository.head_commit_id``, so a refusal arriving at step two left the refused
commit stored and the repository head pointing at content the branch had just rejected.
The branch was protected; the write was not.

Each test here refuses an operation on a frozen branch and then checks the database is
exactly as it was. Asserting the refusal alone would pass against the broken behaviour --
it always refused correctly; it just wrote first.
"""
import base64
import json
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

from app.services import blob_store, branch_ops, history_ops, refs  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, BranchProtectionPolicy, Commit, CommitFile, CommitParent,
    Repository, User,
)


class RefusedWritesLeaveNoTraceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_refuse_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_refuse_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.author = User(username="author", role="team_lead")
        self.db.add(self.author)
        self.db.flush()
        self.repo = Repository(id="r1", name="demo", owner_id=self.author.id)
        self.db.add(self.repo)
        self.db.commit()

        self.mk("base", {"a.txt": "one\n"})
        self.mk("mainwork", {"a.txt": "one\n", "m.txt": "main\n"}, ("base",))
        self.mk("feat", {"a.txt": "one\n", "f.txt": "feature\n"}, ("base",))
        self.mk("pick", {"a.txt": "one\n", "p.txt": "picked\n"}, ("base",))
        self.branch("main", "mainwork", default=True)
        self.branch("feature", "feat")

    def tearDown(self):
        blob_store.store = self._real_store
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def mk(self, cid, files, parents=()):
        self.db.add(Commit(id=cid, repository_id="r1", author_id=self.author.id,
                           message=cid, parent_commit_id=parents[0] if parents else None))
        self.db.flush()
        for path, text in files.items():
            obj = FileObjectCRUD.store_file_object(self.db, text.encode())
            self.db.add(CommitFile(commit_id=cid, file_path=path,
                                   file_hash=obj.hash, file_size=obj.size))
        for order, pid in enumerate(parents):
            self.db.add(CommitParent(commit_id=cid, parent_commit_id=pid, parent_order=order))
        self.db.commit()

    def branch(self, name, head, default=False):
        self.db.add(Branch(repository_id="r1", name=name, head_commit_id=head,
                           is_default=default))
        self.db.commit()

    def freeze(self, pattern="main"):
        self.db.add(BranchProtectionPolicy(
            repository_id="r1", branch_pattern=pattern, mode="frozen",
            policy_version=1, rules_json=json.dumps({}),
        ))
        self.db.commit()

    def snapshot(self):
        self.db.expire_all()
        repo = self.db.query(Repository).filter(Repository.id == "r1").first()
        return {
            "commits": self.db.query(Commit).filter(Commit.repository_id == "r1").count(),
            "repo_head": repo.head_commit_id,
            "branches": {
                b.name: b.head_commit_id
                for b in self.db.query(Branch).filter(Branch.repository_id == "r1").all()
            },
        }

    def assertNothingChanged(self, before, what):
        after = self.snapshot()
        self.assertEqual(after["commits"], before["commits"],
                         f"{what} stored a commit anyway")
        self.assertEqual(after["repo_head"], before["repo_head"],
                         f"{what} moved repository.head_commit_id")
        self.assertEqual(after["branches"], before["branches"],
                         f"{what} moved a branch")

    # --- each write path --------------------------------------------------------

    def test_a_refused_branch_merge_leaves_no_trace(self):
        self.freeze()
        before = self.snapshot()
        with self.assertRaises(Exception):
            branch_ops.merge_branches(self.db, self.repo, self.author, "feature", "main")
        self.assertNothingChanged(before, "a refused branch merge")

    def test_a_refused_cherry_pick_leaves_no_trace(self):
        self.freeze()
        before = self.snapshot()
        with self.assertRaises(Exception):
            history_ops.cherry_pick(self.db, "r1", "pick", "main", "author")
        self.assertNothingChanged(before, "a refused cherry-pick")

    def test_a_refused_revert_leaves_no_trace(self):
        self.freeze()
        before = self.snapshot()
        with self.assertRaises(Exception):
            history_ops.revert(self.db, "r1", "mainwork", "main", "author")
        self.assertNothingChanged(before, "a refused revert")

    def test_a_refused_rebase_leaves_no_rewritten_chain(self):
        """The worst case: a rebase writes a whole chain before moving the reference."""
        self.freeze(pattern="feature")
        before = self.snapshot()
        with self.assertRaises(Exception):
            history_ops.rebase(self.db, "r1", "feature", "main", "author")
        self.assertNothingChanged(before, "a refused rebase")

    def test_a_refused_publish_leaves_no_trace(self):
        self.freeze()
        before = self.snapshot()
        with self.assertRaises(Exception):
            branch_ops.publish_branch(self.db, self.repo, self.author, "feature", "main")
        self.assertNothingChanged(before, "a refused publish")

    # --- the refusals themselves still happen -----------------------------------

    def test_the_refusals_are_still_refusals(self):
        """Guards against 'fixing' the leak by quietly allowing the operation."""
        self.freeze()
        with self.assertRaises(Exception) as caught:
            branch_ops.merge_branches(self.db, self.repo, self.author, "feature", "main")
        self.assertIn("frozen", str(caught.exception).lower())

    def test_an_allowed_merge_still_works(self):
        """The precheck must not refuse what protection permits."""
        result = branch_ops.merge_branches(
            self.db, self.repo, self.author, "feature", "main")
        self.assertEqual(result["status"], "merged")

    def test_an_allowed_cherry_pick_still_works(self):
        result = history_ops.cherry_pick(self.db, "r1", "pick", "main", "author")
        self.assertIn(result.get("status", "applied"), ("applied", "ok", "picked"))

    # --- the precheck classifies correctly --------------------------------------

    def test_a_fast_forward_push_is_classified_as_an_update(self):
        op = refs.classify_incoming_push(
            self.db, current_head="base", parent_ids=["base"], branch_exists=True)
        self.assertEqual(op, refs.OP_UPDATE)

    def test_a_push_that_discards_history_is_classified_as_a_force(self):
        # 'feat' is not a descendant of 'mainwork': landing it on main would discard.
        op = refs.classify_incoming_push(
            self.db, current_head="mainwork", parent_ids=["base"], branch_exists=True)
        self.assertEqual(op, refs.OP_FORCE_UPDATE)

    def test_a_push_to_a_new_branch_is_a_create(self):
        op = refs.classify_incoming_push(
            self.db, current_head=None, parent_ids=[], branch_exists=False)
        self.assertEqual(op, refs.OP_CREATE)

    def test_unknown_ancestry_is_treated_as_a_force_not_an_update(self):
        """The stricter reading, because unprovable ancestry is the risky case."""
        op = refs.classify_incoming_push(
            self.db, current_head="mainwork", parent_ids=["does-not-exist"],
            branch_exists=True)
        self.assertEqual(op, refs.OP_FORCE_UPDATE)


if __name__ == "__main__":
    unittest.main()
