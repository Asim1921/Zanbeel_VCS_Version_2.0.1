"""Tests for branch protection and the branch operations it governs.

`branch_policies.py` existed for a long time with no importer and no column to read, so
every repository ran on defaults with nothing enforced. These pin down that the rules now
actually bite, and that the three operations behave the way their names promise.
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

from app.services import blob_store, branch_ops  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from app.services.branch_ops import BranchOpError  # noqa: E402
from app.services.commit_graph import _get_commit_tree  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import BranchCRUD, FileObjectCRUD, UserPermissionCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, Commit, CommitFile, CommitParent, Repository, User,
)


class BranchOpsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_bo_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_bo_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.owner = User(username="owner", role="team_lead")
        self.dev = User(username="dev", role="developer")
        self.db.add_all([self.owner, self.dev])
        self.db.flush()
        self.repo = Repository(id="r1", name="demo", owner_id=self.owner.id)
        self.db.add(self.repo)
        self.db.commit()

        self.mk("c1", {"a.txt": "A1", "b.txt": "B1"})
        self.mk("c2", {"a.txt": "A2", "b.txt": "B1"}, ("c1",))          # ahead of c1
        self.mk("side", {"a.txt": "A1", "b.txt": "B_SIDE"}, ("c1",))    # diverged
        self.branch("main", "c1", default=True)
        self.branch("feature", "c2")
        self.branch("other", "side")

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
        self.db.add(Commit(id=cid, repository_id="r1", author_id=self.owner.id,
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

    def head(self, name):
        return BranchCRUD.get_branch(self.db, "r1", name).head_commit_id

    def tree(self, cid):
        return {p: b.decode() for p, b in _get_commit_tree(self.db, cid).items()}

    # ---------------- policy storage ----------------

    def test_defaults_apply_when_no_policy_is_set(self):
        policy = branch_ops.get_policy(self.repo)
        self.assertEqual(policy["create_branch_min_scope"], "write")
        self.assertEqual(policy["protected_branches"], [])

    def test_policy_round_trips(self):
        branch_ops.set_policy(self.db, self.repo, {
            "create_branch_min_scope": "manage",
            "protected_branches": ["release", "main"],
        })
        policy = branch_ops.get_policy(self.repo)
        self.assertEqual(policy["create_branch_min_scope"], "manage")
        self.assertEqual(policy["protected_branches"], ["main", "release"])   # sorted

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(BranchOpError):
            branch_ops.set_policy(self.db, self.repo, {"push_min_scope": "superuser"})

    def test_protected_branches_must_be_a_list(self):
        with self.assertRaises(BranchOpError):
            branch_ops.set_policy(self.db, self.repo, {"protected_branches": "main"})

    def test_unknown_keys_are_dropped_rather_than_stored(self):
        stored = branch_ops.set_policy(self.db, self.repo, {"nonsense": "value"})
        self.assertNotIn("nonsense", stored)

    # ---------------- enforcement ----------------

    def test_owner_has_the_highest_scope(self):
        self.assertEqual(branch_ops.actor_scope(self.db, self.owner, self.repo), "team_lead")

    def test_a_developer_without_permission_is_read_only(self):
        self.assertEqual(branch_ops.actor_scope(self.db, self.dev, self.repo), "read")

    def test_policy_blocks_an_operation_the_actor_cannot_reach(self):
        branch_ops.set_policy(self.db, self.repo, {"merge_min_scope": "team_lead"})
        with self.assertRaises(BranchOpError) as ctx:
            branch_ops.enforce(self.db, self.dev, self.repo, "merge", "feature")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_the_default_branch_is_protected_even_without_being_listed(self):
        branch_ops.set_policy(self.db, self.repo, {
            "push_min_scope": "read", "default_branch_push_min_scope": "team_lead",
        })
        # 'main' is the default branch, so the stricter rule applies to it...
        with self.assertRaises(BranchOpError) as ctx:
            branch_ops.enforce(self.db, self.dev, self.repo, "push", "main")
        self.assertEqual(ctx.exception.payload.get("code"), "PROTECTED_BRANCH")
        # ...but not to an ordinary branch
        branch_ops.enforce(self.db, self.dev, self.repo, "push", "feature")

    def test_explicitly_protected_branches_are_guarded(self):
        branch_ops.set_policy(self.db, self.repo, {
            "push_min_scope": "read", "default_branch_push_min_scope": "team_lead",
            "protected_branches": ["feature"],
        })
        with self.assertRaises(BranchOpError):
            branch_ops.enforce(self.db, self.dev, self.repo, "push", "feature")

    def test_a_permitted_actor_passes(self):
        branch_ops.set_policy(self.db, self.repo, {"merge_min_scope": "manage"})
        branch_ops.enforce(self.db, self.owner, self.repo, "merge", "main")

    # ---------------- merge ----------------

    def test_merge_combines_both_branches(self):
        result = branch_ops.merge_branches(self.db, self.repo, self.owner, "other", "feature")
        self.assertEqual(result["status"], "merged")
        tree = self.tree(result["merge_commit_id"])
        self.assertEqual(tree["a.txt"], "A2")        # feature's change
        self.assertEqual(tree["b.txt"], "B_SIDE")    # other's change

    def test_merge_of_an_ancestor_is_a_no_op(self):
        result = branch_ops.merge_branches(self.db, self.repo, self.owner, "main", "feature")
        self.assertEqual(result["status"], "already_up_to_date")

    def test_merge_conflict_is_reported_without_writing(self):
        self.mk("clash", {"a.txt": "CLASH", "b.txt": "B1"}, ("c1",))
        self.branch("clash-branch", "clash")
        with self.assertRaises(BranchOpError) as ctx:
            branch_ops.merge_branches(self.db, self.repo, self.owner, "clash-branch", "feature")
        self.assertEqual(ctx.exception.payload.get("code"), "MERGE_CONFLICT")
        self.assertEqual(self.head("feature"), "c2")

    def test_merging_a_branch_into_itself_is_rejected(self):
        with self.assertRaises(BranchOpError):
            branch_ops.merge_branches(self.db, self.repo, self.owner, "main", "main")

    # ---------------- publish ----------------

    def test_publish_fast_forwards(self):
        result = branch_ops.publish_branch(self.db, self.repo, self.owner, "feature", "main")
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.head("main"), "c2")

    def test_publish_refuses_when_the_target_has_diverged(self):
        # 'other' is not an ancestor of 'feature', so this cannot fast-forward.
        with self.assertRaises(BranchOpError) as ctx:
            branch_ops.publish_branch(self.db, self.repo, self.owner, "feature", "other")
        self.assertEqual(ctx.exception.payload.get("code"), "NOT_FAST_FORWARD")
        self.assertEqual(self.head("other"), "side")     # untouched

    def test_publish_when_already_current_is_a_no_op(self):
        branch_ops.publish_branch(self.db, self.repo, self.owner, "feature", "main")
        again = branch_ops.publish_branch(self.db, self.repo, self.owner, "feature", "main")
        self.assertEqual(again["status"], "already_up_to_date")

    def test_publish_creates_no_commit(self):
        before = self.db.query(Commit).count()
        branch_ops.publish_branch(self.db, self.repo, self.owner, "feature", "main")
        self.assertEqual(self.db.query(Commit).count(), before)

    # ---------------- copy files ----------------

    def test_copy_files_takes_only_the_named_paths(self):
        result = branch_ops.copy_files(self.db, self.repo, self.owner,
                                       "other", "feature", ["b.txt"])
        self.assertEqual(result["status"], "copied")
        tree = self.tree(result["commit_id"])
        self.assertEqual(tree["b.txt"], "B_SIDE")    # taken from 'other'
        self.assertEqual(tree["a.txt"], "A2")        # feature's own work untouched

    def test_copy_files_does_not_inherit_the_source_history(self):
        result = branch_ops.copy_files(self.db, self.repo, self.owner,
                                       "other", "feature", ["b.txt"])
        parents = [row.parent_commit_id for row in self.db.query(CommitParent)
                   .filter(CommitParent.commit_id == result["commit_id"]).all()]
        self.assertEqual(parents, ["c2"])            # target tip only, not 'side'

    def test_copying_an_absent_path_is_reported(self):
        with self.assertRaises(BranchOpError) as ctx:
            branch_ops.copy_files(self.db, self.repo, self.owner,
                                  "other", "feature", ["nope.txt"])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_copying_identical_content_is_a_no_op(self):
        result = branch_ops.copy_files(self.db, self.repo, self.owner,
                                       "other", "feature", ["a.txt"])
        # 'other' has a.txt == "A1"; feature has "A2", so this DOES change
        self.assertEqual(result["status"], "copied")
        again = branch_ops.copy_files(self.db, self.repo, self.owner,
                                      "other", "feature", ["a.txt"])
        self.assertEqual(again["status"], "already_up_to_date")

    def test_empty_path_list_is_rejected(self):
        with self.assertRaises(BranchOpError):
            branch_ops.copy_files(self.db, self.repo, self.owner, "other", "feature", [])

    # ---------------- concurrency guard ----------------

    def test_expected_head_mismatch_is_refused(self):
        for op in (
            lambda: branch_ops.merge_branches(self.db, self.repo, self.owner, "other",
                                              "feature", expected_head_commit_id="stale"),
            lambda: branch_ops.publish_branch(self.db, self.repo, self.owner, "feature",
                                              "main", expected_head_commit_id="stale"),
            lambda: branch_ops.copy_files(self.db, self.repo, self.owner, "other", "feature",
                                          ["b.txt"], expected_head_commit_id="stale"),
        ):
            with self.assertRaises(BranchOpError) as ctx:
                op()
            self.assertEqual(ctx.exception.payload.get("code"), "HEAD_MISMATCH")


if __name__ == "__main__":
    unittest.main()
