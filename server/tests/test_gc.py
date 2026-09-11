"""Tests for object-store garbage collection.

The critical property is that GC never deletes a blob something still points at --
including pending commits, which hold the only reference to their content until a
reviewer approves them.
"""
import os
import sys
import tempfile
import unittest

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from database.database import Base  # noqa: E402
from database.models import (  # noqa: E402
    Branch, Commit, CommitFile, CommitParent, FileLineage, FileObject,
    PendingCommit, PendingCommitFile, Repository, Tag, User,
)
from app.services import gc as gc_service  # noqa: E402


class GarbageCollectionTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_gc_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.Session()

        user = User(username="dev", role="team_lead")
        self.db.add(user)
        self.db.flush()

        repo = Repository(id="r1", name="demo", owner_id=user.id)
        self.db.add(repo)

        # live blob, reachable through a commit on a branch
        self.db.add(FileObject(hash="h_live", content=b"live", size=4))
        self.db.add(Commit(id="c1", repository_id="r1", author_id=user.id, message="one"))
        self.db.add(CommitFile(commit_id="c1", file_path="a.txt", file_hash="h_live", file_size=4))
        self.db.add(Branch(repository_id="r1", name="main", head_commit_id="c1", is_default=True))
        repo.head_commit_id = "c1"

        # blob referenced only by a pending commit -- must survive
        self.db.add(FileObject(hash="h_pending", content=b"pending", size=7))
        self.db.add(PendingCommit(id="p1", repository_id="r1", author_id=user.id, message="pending"))
        self.db.add(PendingCommitFile(pending_commit_id="p1", file_path="b.txt", file_hash="h_pending", file_size=7))

        # blob referenced only by a lineage record -- must survive
        self.db.add(FileObject(hash="h_lineage", content=b"lineage", size=7))
        self.db.add(FileLineage(repository_id="r1", commit_id="c1", old_path="x", new_path="y", file_hash="h_lineage"))

        # genuine garbage: referenced by nothing
        self.db.add(FileObject(hash="h_junk1", content=b"junk", size=4))
        self.db.add(FileObject(hash="h_junk2", content=b"junkjunk", size=8))

        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def hashes(self):
        return {h for (h,) in self.db.query(FileObject.hash)}

    def test_analyze_finds_only_real_garbage(self):
        report = gc_service.analyze(self.db)
        self.assertEqual(report["objects_total"], 5)
        self.assertEqual(report["unreferenced_objects"], 2)
        self.assertEqual(report["unreferenced_bytes"], 12)

    def test_dry_run_changes_nothing(self):
        before = self.hashes()
        report = gc_service.collect(self.db, dry_run=True)
        self.assertEqual(report["deleted_objects"], 0)
        self.assertEqual(self.hashes(), before)

    def test_sweep_deletes_only_unreferenced_blobs(self):
        report = gc_service.collect(self.db, dry_run=False)
        self.assertEqual(report["deleted_objects"], 2)
        self.assertEqual(report["deleted_bytes"], 12)
        self.assertEqual(self.hashes(), {"h_live", "h_pending", "h_lineage"})

    def test_pending_commit_blobs_are_never_collected(self):
        # The regression that would silently destroy work awaiting review.
        gc_service.collect(self.db, dry_run=False)
        self.assertIn("h_pending", self.hashes())

    def test_lineage_blobs_are_never_collected(self):
        gc_service.collect(self.db, dry_run=False)
        self.assertIn("h_lineage", self.hashes())

    def test_sweep_is_idempotent(self):
        gc_service.collect(self.db, dry_run=False)
        second = gc_service.collect(self.db, dry_run=False)
        self.assertEqual(second["deleted_objects"], 0)

    def test_limit_caps_a_sweep(self):
        report = gc_service.collect(self.db, dry_run=False, limit=1)
        self.assertEqual(report["deleted_objects"], 1)
        self.assertEqual(len(self.hashes()), 4)

    def test_merge_conflict_blobs_are_never_collected(self):
        """Regression: merge_conflict_files has four separate FKs into file_objects, and
        a sweep that overlooked them would delete the base/ours/theirs versions someone is
        mid-way through reconciling."""
        from sqlalchemy import text

        for name in ("h_base", "h_ours", "h_theirs", "h_resolved"):
            self.db.add(FileObject(hash=name, size=4))
        self.db.execute(text(
            "INSERT INTO merge_conflict_files "
            "(session_id, file_path, base_file_hash, ours_file_hash, theirs_file_hash, resolved_file_hash) "
            "VALUES (1, 'a.txt', 'h_base', 'h_ours', 'h_theirs', 'h_resolved')"
        ))
        self.db.commit()

        gc_service.collect(self.db, dry_run=False)

        surviving = self.hashes()
        for name in ("h_base", "h_ours", "h_theirs", "h_resolved"):
            self.assertIn(name, surviving, f"{name} was wrongly collected")
        # the genuine garbage still goes
        self.assertNotIn("h_junk1", surviving)

    def test_absent_unmapped_table_is_not_an_error(self):
        # A freshly created database has no merge_conflict_files table at all.
        report = gc_service.collect(self.db, dry_run=False)
        self.assertEqual(report["deleted_objects"], 2)

    def test_dangling_commits_are_reported_but_not_deleted_by_default(self):
        self.db.add(Commit(id="c_orphan", repository_id="r1", author_id=1, message="orphan"))
        self.db.commit()

        report = gc_service.collect(self.db, dry_run=False)
        self.assertIn("c_orphan", report["dangling_commits"])
        self.assertEqual(report["deleted_commits"], 0)
        self.assertIsNotNone(self.db.query(Commit).filter(Commit.id == "c_orphan").first())

    def test_dangling_commits_pruned_only_when_asked(self):
        self.db.add(Commit(id="c_orphan", repository_id="r1", author_id=1, message="orphan"))
        self.db.commit()

        report = gc_service.collect(self.db, dry_run=False, prune_dangling_commits=True)
        self.assertEqual(report["deleted_commits"], 1)
        self.assertIsNone(self.db.query(Commit).filter(Commit.id == "c_orphan").first())
        # the branch commit stays
        self.assertIsNotNone(self.db.query(Commit).filter(Commit.id == "c1").first())

    def test_commit_reachable_through_a_tag_is_not_dangling(self):
        self.db.add(Commit(id="c_tagged", repository_id="r1", author_id=1, message="tagged"))
        self.db.add(Tag(repository_id="r1", name="v1", commit_id="c_tagged"))
        self.db.commit()

        report = gc_service.analyze(self.db)
        self.assertNotIn("c_tagged", report["dangling_commits"])

    def test_ancestors_of_a_ref_are_reachable(self):
        self.db.add(Commit(id="c0", repository_id="r1", author_id=1, message="root"))
        self.db.add(CommitParent(commit_id="c1", parent_commit_id="c0", parent_order=0))
        self.db.commit()

        self.assertIn("c0", gc_service.reachable_commit_ids(self.db))
        self.assertNotIn("c0", gc_service.analyze(self.db)["dangling_commits"])


if __name__ == "__main__":
    unittest.main()
