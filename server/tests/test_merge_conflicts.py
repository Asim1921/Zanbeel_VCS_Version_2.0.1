"""Tests for manual merge conflict resolution.

The dangerous failure here is not a bad merge, it is a *silent* one: applying someone's
decisions to content that changed while they were deciding. Several of these tests exist
only to pin that down.
"""
import base64
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

from app.services import blob_store, merge_conflicts  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from app.services.commit_graph import _get_commit_tree  # noqa: E402
from app.services.merge_conflicts import ConflictError  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import BranchCRUD, FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, Commit, CommitFile, CommitParent, MergeConflictSession, PullRequest,
    Repository, User,
)


def b64(text):
    return base64.b64encode(text.encode()).decode("ascii")


class MergeConflictTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_mc_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_mc_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.user = User(username="dev", role="team_lead")
        self.db.add(self.user)
        self.db.flush()
        self.db.add(Repository(id="r1", name="demo", owner_id=self.user.id))
        self.db.commit()

        # A genuine conflict: both branches change the same line from a common base.
        self.mk("base", {"a.txt": "original", "shared.txt": "untouched"})
        self.mk("ours", {"a.txt": "OUR VERSION", "shared.txt": "untouched"}, ("base",))
        self.mk("theirs", {"a.txt": "THEIR VERSION", "shared.txt": "untouched"}, ("base",))
        self.branch("main", "ours")
        self.branch("feature", "theirs")

        self.pr = PullRequest(repository_id="r1", title="merge me",
                              source_branch="feature", target_branch="main",
                              status="open", created_by_id=self.user.id)
        self.db.add(self.pr)
        self.db.commit()
        self.db.refresh(self.pr)

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
        self.db.add(Commit(id=cid, repository_id="r1", author_id=self.user.id,
                           message=cid, parent_commit_id=parents[0] if parents else None))
        self.db.flush()
        for path, text in files.items():
            obj = FileObjectCRUD.store_file_object(self.db, text.encode())
            self.db.add(CommitFile(commit_id=cid, file_path=path,
                                   file_hash=obj.hash, file_size=obj.size))
        for order, pid in enumerate(parents):
            self.db.add(CommitParent(commit_id=cid, parent_commit_id=pid, parent_order=order))
        self.db.commit()

    def branch(self, name, head):
        self.db.add(Branch(repository_id="r1", name=name, head_commit_id=head))
        self.db.commit()

    def head(self, name):
        return BranchCRUD.get_branch(self.db, "r1", name).head_commit_id

    def tree_text(self, cid):
        return {p: b.decode() for p, b in _get_commit_tree(self.db, cid).items()}

    def open_session(self):
        return merge_conflicts.open_session(self.db, "r1", self.pr, self.user)

    # ---------------- opening ----------------

    def test_session_captures_all_three_sides(self):
        session = self.open_session()
        bundle = merge_conflicts.get_bundle(self.db, session)

        self.assertEqual(len(bundle["files"]), 1)
        entry = bundle["files"][0]
        self.assertEqual(entry["path"], "a.txt")
        self.assertEqual(base64.b64decode(entry["base_b64"]).decode(), "original")
        self.assertEqual(base64.b64decode(entry["ours_b64"]).decode(), "OUR VERSION")
        self.assertEqual(base64.b64decode(entry["theirs_b64"]).decode(), "THEIR VERSION")

    def test_suggested_merge_carries_conflict_markers(self):
        bundle = merge_conflicts.get_bundle(self.db, self.open_session())
        suggested = base64.b64decode(bundle["files"][0]["suggested_b64"]).decode()
        self.assertIn("<<<<<<< OURS", suggested)
        self.assertIn(">>>>>>> THEIRS", suggested)

    def test_non_conflicted_files_are_not_included(self):
        bundle = merge_conflicts.get_bundle(self.db, self.open_session())
        self.assertNotIn("shared.txt", [f["path"] for f in bundle["files"]])

    def test_reopening_reuses_the_open_session(self):
        first = self.open_session()
        second = self.open_session()
        self.assertEqual(first.id, second.id)

    def test_a_clean_merge_refuses_to_open_a_session(self):
        # A commit carries its whole tree, so the branch must repeat base's files --
        # listing only the new one would read as deleting the rest, which conflicts.
        self.mk("clean", {"a.txt": "original", "shared.txt": "untouched",
                          "z.txt": "only here"}, ("base",))
        self.branch("clean-branch", "clean")
        pr = PullRequest(repository_id="r1", title="clean", source_branch="clean-branch",
                         target_branch="main", status="open", created_by_id=self.user.id)
        self.db.add(pr)
        self.db.commit()

        with self.assertRaises(ConflictError) as ctx:
            merge_conflicts.open_session(self.db, "r1", pr, self.user)
        self.assertEqual(ctx.exception.status_code, 409)

    # ---------------- resolving ----------------

    def test_resolution_merges_and_moves_the_branch(self):
        session = self.open_session()
        result = merge_conflicts.resolve(
            self.db, session, {"a.txt": b64("RECONCILED")}, self.user)

        self.assertEqual(result["status"], "merged")
        tree = self.tree_text(result["merge_commit_id"])
        self.assertEqual(tree["a.txt"], "RECONCILED")
        self.assertEqual(tree["shared.txt"], "untouched")   # clean parts came through
        self.assertEqual(self.head("main"), result["merge_commit_id"])

    def test_merge_commit_records_both_parents(self):
        session = self.open_session()
        result = merge_conflicts.resolve(
            self.db, session, {"a.txt": b64("X")}, self.user)

        parents = {
            row.parent_commit_id for row in
            self.db.query(CommitParent).filter(
                CommitParent.commit_id == result["merge_commit_id"]).all()
        }
        self.assertEqual(parents, {"ours", "theirs"})

    def test_pull_request_is_marked_merged(self):
        session = self.open_session()
        merge_conflicts.resolve(self.db, session, {"a.txt": b64("X")}, self.user)
        self.db.refresh(self.pr)
        self.assertEqual(self.pr.status, "merged")

    def test_session_is_marked_resolved(self):
        session = self.open_session()
        merge_conflicts.resolve(self.db, session, {"a.txt": b64("X")}, self.user)
        self.assertEqual(session.status, "resolved")

    def test_partial_resolution_is_refused(self):
        # Two conflicts, one answer: merging would mean guessing the other.
        self.mk("ours2", {"a.txt": "OURS", "b.txt": "OURS B"}, ("base",))
        self.mk("theirs2", {"a.txt": "THEIRS", "b.txt": "THEIRS B"}, ("base",))
        BranchCRUD.update_branch_head(self.db, "r1", "main", "ours2")
        BranchCRUD.update_branch_head(self.db, "r1", "feature", "theirs2")

        session = self.open_session()
        with self.assertRaises(ConflictError) as ctx:
            merge_conflicts.resolve(self.db, session, {"a.txt": b64("X")}, self.user)
        self.assertEqual(ctx.exception.payload.get("code"), "INCOMPLETE")
        self.assertIn("b.txt", ctx.exception.payload.get("unresolved", []))

    def test_resolving_a_path_that_is_not_conflicted_is_refused(self):
        session = self.open_session()
        with self.assertRaises(ConflictError):
            merge_conflicts.resolve(
                self.db, session,
                {"a.txt": b64("X"), "shared.txt": b64("sneaky")}, self.user)

    def test_invalid_base64_is_reported(self):
        session = self.open_session()
        with self.assertRaises(ConflictError):
            merge_conflicts.resolve(self.db, session, {"a.txt": "!!!not base64!!!"}, self.user)

    # ---------------- staleness: the dangerous case ----------------

    def test_resolution_is_refused_if_the_target_branch_moved(self):
        session = self.open_session()
        # Someone else pushes to main while the conflict is being resolved.
        self.mk("newer", {"a.txt": "SOMEONE ELSE", "shared.txt": "untouched"}, ("ours",))
        BranchCRUD.update_branch_head(self.db, "r1", "main", "newer")

        with self.assertRaises(ConflictError) as ctx:
            merge_conflicts.resolve(self.db, session, {"a.txt": b64("X")}, self.user)
        self.assertEqual(ctx.exception.payload.get("code"), "SESSION_STALE")
        self.assertEqual(self.head("main"), "newer")        # untouched

    def test_resolution_is_refused_if_the_source_branch_moved(self):
        session = self.open_session()
        self.mk("newer_src", {"a.txt": "MOVED", "shared.txt": "untouched"}, ("theirs",))
        BranchCRUD.update_branch_head(self.db, "r1", "feature", "newer_src")

        with self.assertRaises(ConflictError) as ctx:
            merge_conflicts.resolve(self.db, session, {"a.txt": b64("X")}, self.user)
        self.assertEqual(ctx.exception.payload.get("code"), "SESSION_STALE")

    def test_a_stale_session_is_retired_and_reopening_recomputes(self):
        first = self.open_session()
        self.mk("newer", {"a.txt": "SOMEONE ELSE", "shared.txt": "untouched"}, ("ours",))
        BranchCRUD.update_branch_head(self.db, "r1", "main", "newer")

        second = self.open_session()
        self.assertNotEqual(first.id, second.id)
        self.db.refresh(first)
        self.assertEqual(first.status, "stale")

    def test_bundle_reports_staleness_before_a_user_wastes_effort(self):
        session = self.open_session()
        self.mk("newer", {"a.txt": "SOMEONE ELSE", "shared.txt": "untouched"}, ("ours",))
        BranchCRUD.update_branch_head(self.db, "r1", "main", "newer")

        bundle = merge_conflicts.get_bundle(self.db, session)
        self.assertIsNotNone(bundle["session"]["stale_reason"])

    def test_head_mismatch_is_refused(self):
        session = self.open_session()
        with self.assertRaises(ConflictError) as ctx:
            merge_conflicts.resolve(self.db, session, {"a.txt": b64("X")}, self.user,
                                    expected_head_commit_id="something-else")
        self.assertEqual(ctx.exception.payload.get("code"), "HEAD_MISMATCH")

    # ---------------- abort ----------------

    def test_abort_leaves_both_branches_alone(self):
        session = self.open_session()
        merge_conflicts.abort(self.db, session, self.user)

        self.assertEqual(session.status, "aborted")
        self.assertEqual(self.head("main"), "ours")
        self.assertEqual(self.head("feature"), "theirs")
        self.db.refresh(self.pr)
        self.assertEqual(self.pr.status, "open")

    def test_a_resolved_session_cannot_be_resolved_twice(self):
        session = self.open_session()
        merge_conflicts.resolve(self.db, session, {"a.txt": b64("X")}, self.user)
        with self.assertRaises(ConflictError):
            merge_conflicts.resolve(self.db, session, {"a.txt": b64("Y")}, self.user)

    def test_an_aborted_session_cannot_be_resolved(self):
        session = self.open_session()
        merge_conflicts.abort(self.db, session, self.user)
        with self.assertRaises(ConflictError):
            merge_conflicts.resolve(self.db, session, {"a.txt": b64("X")}, self.user)


if __name__ == "__main__":
    unittest.main()
