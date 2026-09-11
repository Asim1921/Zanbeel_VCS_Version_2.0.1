"""Tests for cherry-pick, revert and rebase.

The property under test is that these operations replay (or un-replay) *only the change a
commit introduced*, leaving unrelated work on the target branch alone. Cherry-pick and
revert must never mutate existing history; rebase may move a branch, but only atomically --
a conflict part-way through must leave the branch exactly where it was.
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

from app.services import blob_store  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from app.services.commit_graph import _get_commit_tree  # noqa: E402
from app.services.history_ops import (  # noqa: E402
    HistoryOpError, cherry_pick, rebase, revert,
)
from database.database import Base  # noqa: E402
from database.crud import BranchCRUD, FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, Commit, CommitFile, CommitParent, Repository, User,
)


class HistoryOpsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_hist_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_hist_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.user = User(username="dev", role="team_lead")
        self.db.add(self.user)
        self.db.flush()
        self.db.add(Repository(id="r1", name="demo", owner_id=self.user.id))
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

    def mk(self, cid, files, parents=(), message="c"):
        self.db.add(Commit(id=cid, repository_id="r1", author_id=self.user.id,
                           message=message,
                           parent_commit_id=parents[0] if parents else None))
        self.db.flush()
        for path, text in files.items():
            obj = FileObjectCRUD.store_file_object(self.db, text.encode())
            self.db.add(CommitFile(commit_id=cid, file_path=path,
                                   file_hash=obj.hash, file_size=obj.size))
        for order, pid in enumerate(parents):
            self.db.add(CommitParent(commit_id=cid, parent_commit_id=pid, parent_order=order))
        self.db.commit()
        return cid

    def branch(self, name, head):
        self.db.add(Branch(repository_id="r1", name=name, head_commit_id=head))
        self.db.commit()

    def tree_text(self, commit_id):
        return {p: b.decode() for p, b in _get_commit_tree(self.db, commit_id).items()}

    def head(self, name):
        return BranchCRUD.get_branch(self.db, "r1", name).head_commit_id

    # ---------------- cherry-pick ----------------

    def test_cherry_pick_applies_only_that_commits_change(self):
        self.mk("c1", {"a.txt": "A1", "b.txt": "B1"})
        self.mk("c2", {"a.txt": "A2", "b.txt": "B1"}, parents=("c1",))   # only a.txt changed
        self.mk("d1", {"a.txt": "A1", "b.txt": "B_OTHER"}, parents=("c1",))
        self.branch("main", "d1")

        result = cherry_pick(self.db, "r1", "c2", "main", "dev")
        self.assertEqual(result["status"], "applied")

        tree = self.tree_text(result["commit_id"])
        self.assertEqual(tree["a.txt"], "A2")        # the picked change came across
        self.assertEqual(tree["b.txt"], "B_OTHER")   # unrelated branch work survived

    def test_cherry_pick_adds_a_commit_and_moves_the_branch(self):
        self.mk("c1", {"a.txt": "A1"})
        self.mk("c2", {"a.txt": "A2"}, parents=("c1",))
        self.mk("d1", {"a.txt": "A1", "z.txt": "Z"}, parents=("c1",))
        self.branch("main", "d1")

        result = cherry_pick(self.db, "r1", "c2", "main", "dev")
        self.assertEqual(self.head("main"), result["commit_id"])
        # original commits untouched
        self.assertEqual(self.tree_text("c2")["a.txt"], "A2")
        self.assertEqual(self.tree_text("d1")["a.txt"], "A1")

    def test_cherry_pick_reports_conflicts_without_writing(self):
        self.mk("c1", {"a.txt": "base"})
        self.mk("c2", {"a.txt": "theirs"}, parents=("c1",))
        self.mk("d1", {"a.txt": "ours"}, parents=("c1",))
        self.branch("main", "d1")

        result = cherry_pick(self.db, "r1", "c2", "main", "dev")
        self.assertEqual(result["status"], "conflicts")
        self.assertIn("a.txt", result["conflicts"])
        self.assertEqual(self.head("main"), "d1")     # branch not moved

    def test_cherry_picking_an_already_present_change_is_a_no_op(self):
        self.mk("c1", {"a.txt": "A1"})
        self.mk("c2", {"a.txt": "A2"}, parents=("c1",))
        self.branch("main", "c2")

        result = cherry_pick(self.db, "r1", "c2", "main", "dev")
        self.assertEqual(result["status"], "no-op")
        self.assertEqual(self.head("main"), "c2")

    def test_dry_run_writes_nothing(self):
        self.mk("c1", {"a.txt": "A1"})
        self.mk("c2", {"a.txt": "A2"}, parents=("c1",))
        self.mk("d1", {"a.txt": "A1", "z.txt": "Z"}, parents=("c1",))
        self.branch("main", "d1")

        result = cherry_pick(self.db, "r1", "c2", "main", "dev", dry_run=True)
        self.assertEqual(result["status"], "clean")
        self.assertNotIn("commit_id", result)
        self.assertEqual(self.head("main"), "d1")

    def test_cherry_pick_of_a_root_commit_adds_its_files(self):
        self.mk("root", {"new.txt": "hello"})
        self.mk("other", {"x.txt": "X"})
        self.branch("main", "other")

        result = cherry_pick(self.db, "r1", "root", "main", "dev")
        tree = self.tree_text(result["commit_id"])
        self.assertEqual(tree["new.txt"], "hello")
        self.assertEqual(tree["x.txt"], "X")

    # ---------------- revert ----------------

    def test_revert_undoes_the_change(self):
        self.mk("c1", {"a.txt": "original"})
        self.mk("c2", {"a.txt": "broken"}, parents=("c1",))
        self.branch("main", "c2")

        result = revert(self.db, "r1", "c2", "main", "dev")
        self.assertEqual(result["status"], "reverted")
        self.assertEqual(self.tree_text(result["commit_id"])["a.txt"], "original")

    def test_revert_keeps_later_unrelated_work(self):
        self.mk("c1", {"a.txt": "A1", "b.txt": "B1"})
        self.mk("c2", {"a.txt": "A_BAD", "b.txt": "B1"}, parents=("c1",))
        self.mk("c3", {"a.txt": "A_BAD", "b.txt": "B_NEW"}, parents=("c2",))
        self.branch("main", "c3")

        result = revert(self.db, "r1", "c2", "main", "dev")
        tree = self.tree_text(result["commit_id"])
        self.assertEqual(tree["a.txt"], "A1")       # the bad change is undone
        self.assertEqual(tree["b.txt"], "B_NEW")    # later work is kept

    def test_revert_does_not_rewrite_history(self):
        self.mk("c1", {"a.txt": "original"})
        self.mk("c2", {"a.txt": "broken"}, parents=("c1",))
        self.branch("main", "c2")

        result = revert(self.db, "r1", "c2", "main", "dev")
        # the reverted commit still exists, unchanged
        self.assertIsNotNone(self.db.query(Commit).filter(Commit.id == "c2").first())
        self.assertEqual(self.tree_text("c2")["a.txt"], "broken")
        # and the revert is itself a normal commit on the branch
        self.assertEqual(self.head("main"), result["commit_id"])

    def test_revert_is_revertible(self):
        self.mk("c1", {"a.txt": "original"})
        self.mk("c2", {"a.txt": "changed"}, parents=("c1",))
        self.branch("main", "c2")

        first = revert(self.db, "r1", "c2", "main", "dev")
        self.assertEqual(self.tree_text(first["commit_id"])["a.txt"], "original")

        second = revert(self.db, "r1", first["commit_id"], "main", "dev")
        self.assertEqual(self.tree_text(second["commit_id"])["a.txt"], "changed")

    def test_revert_of_a_file_addition_removes_it(self):
        self.mk("c1", {"a.txt": "A"})
        self.mk("c2", {"a.txt": "A", "added.txt": "new"}, parents=("c1",))
        self.branch("main", "c2")

        result = revert(self.db, "r1", "c2", "main", "dev")
        self.assertNotIn("added.txt", self.tree_text(result["commit_id"]))

    def test_reverting_something_not_present_is_a_no_op(self):
        self.mk("c1", {"a.txt": "A1"})
        self.mk("c2", {"a.txt": "A2"}, parents=("c1",))
        self.mk("d1", {"a.txt": "A1", "z.txt": "Z"}, parents=("c1",))
        self.branch("side", "d1")

        result = revert(self.db, "r1", "c2", "side", "dev")
        self.assertEqual(result["status"], "no-op")

    # ---------------- guards ----------------

    def test_merge_commit_requires_an_explicit_mainline(self):
        self.mk("c1", {"a.txt": "A"})
        self.mk("c2", {"a.txt": "A", "b.txt": "B"}, parents=("c1",))
        self.mk("m", {"a.txt": "A", "b.txt": "B"}, parents=("c1", "c2"))
        self.branch("main", "m")

        with self.assertRaises(HistoryOpError) as ctx:
            revert(self.db, "r1", "m", "main", "dev")
        self.assertIn("mainline", ctx.exception.message)

        with self.assertRaises(HistoryOpError):
            revert(self.db, "r1", "m", "main", "dev", mainline=5)   # out of range

    def test_mainline_on_a_non_merge_is_rejected(self):
        self.mk("c1", {"a.txt": "A"})
        self.mk("c2", {"a.txt": "B"}, parents=("c1",))
        self.branch("main", "c2")

        with self.assertRaises(HistoryOpError):
            revert(self.db, "r1", "c2", "main", "dev", mainline=2)

    def test_unknown_commit_and_branch_are_reported(self):
        self.mk("c1", {"a.txt": "A"})
        self.branch("main", "c1")

        with self.assertRaises(HistoryOpError) as ctx:
            cherry_pick(self.db, "r1", "nope", "main", "dev")
        self.assertEqual(ctx.exception.status_code, 404)

        with self.assertRaises(HistoryOpError) as ctx:
            cherry_pick(self.db, "r1", "c1", "no-such-branch", "dev")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_commit_from_another_repository_is_rejected(self):
        other = User(username="other", role="team_lead")
        self.db.add(other)
        self.db.flush()
        self.db.add(Repository(id="r2", name="other", owner_id=other.id))
        self.db.add(Commit(id="foreign", repository_id="r2", author_id=other.id, message="x"))
        self.mk("c1", {"a.txt": "A"})
        self.branch("main", "c1")
        self.db.commit()

        with self.assertRaises(HistoryOpError):
            cherry_pick(self.db, "r1", "foreign", "main", "dev")

    def test_message_records_the_source_commit(self):
        self.mk("c1", {"a.txt": "A1"})
        self.mk("c2", {"a.txt": "A2"}, parents=("c1",))
        self.branch("main", "c2")

        result = revert(self.db, "r1", "c2", "main", "dev")
        commit = self.db.query(Commit).filter(Commit.id == result["commit_id"]).first()
        self.assertIn("c2", commit.message)
        self.assertIn("Revert", commit.message)


    # ---------------- rebase ----------------

    def test_rebase_replays_branch_commits_onto_another_branch(self):
        self.mk("c1", {"a.txt": "A1"})
        self.mk("main2", {"a.txt": "A1", "m.txt": "MAIN"}, parents=("c1",))
        self.mk("f1", {"a.txt": "A1", "f.txt": "F1"}, parents=("c1",))
        self.mk("f2", {"a.txt": "A1", "f.txt": "F2"}, parents=("f1",))
        self.branch("main", "main2")
        self.branch("feature", "f2")

        result = rebase(self.db, "r1", "feature", "main", "dev")
        self.assertEqual(result["status"], "rebased")
        self.assertEqual(result["replayed"], 2)

        tree = self.tree_text(result["new_head"])
        self.assertEqual(tree["f.txt"], "F2")      # the branch's final state
        self.assertEqual(tree["m.txt"], "MAIN")    # now sitting on main's work
        self.assertEqual(self.head("feature"), result["new_head"])

    def test_rebase_reports_the_previous_head_for_recovery(self):
        self.mk("c1", {"a.txt": "A1"})
        self.mk("main2", {"a.txt": "A1", "m.txt": "M"}, parents=("c1",))
        self.mk("f1", {"a.txt": "A1", "f.txt": "F"}, parents=("c1",))
        self.branch("main", "main2")
        self.branch("feature", "f1")

        result = rebase(self.db, "r1", "feature", "main", "dev")
        self.assertEqual(result["previous_head"], "f1")
        # the original commits still exist
        self.assertIsNotNone(self.db.query(Commit).filter(Commit.id == "f1").first())

    def test_rebase_is_all_or_nothing_on_conflict(self):
        self.mk("c1", {"a.txt": "base"})
        self.mk("main2", {"a.txt": "main-change"}, parents=("c1",))
        self.mk("f1", {"a.txt": "base", "ok.txt": "fine"}, parents=("c1",))
        self.mk("f2", {"a.txt": "feature-change", "ok.txt": "fine"}, parents=("f1",))
        self.branch("main", "main2")
        self.branch("feature", "f2")

        result = rebase(self.db, "r1", "feature", "main", "dev")
        self.assertEqual(result["status"], "conflicts")
        self.assertEqual(result["failed_at"], "f2")
        self.assertEqual(result["replayed"], 0)
        # nothing written, branch untouched -- even though f1 would have applied cleanly
        self.assertEqual(self.head("feature"), "f2")

    def test_rebase_preserves_original_authorship(self):
        other = User(username="author2", role="developer")
        self.db.add(other)
        self.db.flush()
        self.db.add(Commit(id="f1", repository_id="r1", author_id=other.id,
                           message="their work", parent_commit_id="c1"))
        self.mk("c1", {"a.txt": "A"})
        self.mk("main2", {"a.txt": "A", "m.txt": "M"}, parents=("c1",))
        obj = FileObjectCRUD.store_file_object(self.db, b"F")
        self.db.add(CommitFile(commit_id="f1", file_path="f.txt",
                               file_hash=obj.hash, file_size=obj.size))
        obj2 = FileObjectCRUD.store_file_object(self.db, b"A")
        self.db.add(CommitFile(commit_id="f1", file_path="a.txt",
                               file_hash=obj2.hash, file_size=obj2.size))
        self.db.add(CommitParent(commit_id="f1", parent_commit_id="c1", parent_order=0))
        self.branch("main", "main2")
        self.branch("feature", "f1")
        self.db.commit()

        result = rebase(self.db, "r1", "feature", "main", "dev")
        rebased = self.db.query(Commit).filter(Commit.id == result["new_head"]).first()
        self.assertEqual(rebased.author.username, "author2")   # not "dev"
        self.assertEqual(rebased.message, "their work")

    def test_rebase_dry_run_writes_nothing(self):
        self.mk("c1", {"a.txt": "A"})
        self.mk("main2", {"a.txt": "A", "m.txt": "M"}, parents=("c1",))
        self.mk("f1", {"a.txt": "A", "f.txt": "F"}, parents=("c1",))
        self.branch("main", "main2")
        self.branch("feature", "f1")

        result = rebase(self.db, "r1", "feature", "main", "dev", dry_run=True)
        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["replayed"], 1)
        self.assertEqual(self.head("feature"), "f1")

    def test_rebase_of_an_already_contained_branch_is_a_no_op(self):
        self.mk("c1", {"a.txt": "A"})
        self.mk("c2", {"a.txt": "B"}, parents=("c1",))
        self.branch("main", "c2")
        self.branch("old", "c1")

        self.assertEqual(rebase(self.db, "r1", "old", "main", "dev")["status"], "no-op")

    def test_rebase_onto_itself_is_rejected(self):
        self.mk("c1", {"a.txt": "A"})
        self.branch("main", "c1")
        with self.assertRaises(HistoryOpError):
            rebase(self.db, "r1", "main", "main", "dev")


if __name__ == "__main__":
    unittest.main()
