"""Tests for the commit graph: what it contains, and the order it must be in.

The ordering invariant is the one that matters. A renderer assigns lanes by tracking
which commits are still waiting to be drawn, so a commit must appear before its
parents. Sorting by timestamp looks like it does that and does not: clock skew between
machines, or a rebase that rewrites dates, is enough to put a parent first.
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.services import blob_store, commit_dag  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, Commit, CommitFile, CommitParent, Repository, Tag, User,
)


class CommitGraphTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_dag_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_dag_", suffix=".db")
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

        self.clock = datetime(2026, 1, 1, 12, 0, 0)

        #        c1 ── c2 ───── m1        (main)
        #          \           /
        #           f1 ── f2 ─┘           (feature, merged into main at m1)
        #            \
        #             t1                  (topic, still open)
        self.mk("c1", [])
        self.mk("c2", ["c1"])
        self.mk("f1", ["c1"])
        self.mk("f2", ["f1"])
        self.mk("t1", ["f1"])
        self.mk("m1", ["c2", "f2"])

        self.branch("main", "m1", default=True)
        self.branch("feature", "f2")
        self.branch("topic", "t1")

    def tearDown(self):
        blob_store.store = self._real_store
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def mk(self, cid, parents, when=None):
        self.clock += timedelta(minutes=5)
        self.db.add(Commit(
            id=cid, repository_id="r1", author_id=self.author.id, message=f"commit {cid}",
            parent_commit_id=parents[0] if parents else None,
            created_at=when or self.clock,
        ))
        self.db.flush()
        obj = FileObjectCRUD.store_file_object(self.db, f"{cid}\n".encode())
        self.db.add(CommitFile(commit_id=cid, file_path="a.txt",
                               file_hash=obj.hash, file_size=obj.size))
        for order, pid in enumerate(parents):
            self.db.add(CommitParent(commit_id=cid, parent_commit_id=pid, parent_order=order))
        self.db.commit()

    def branch(self, name, head, default=False):
        self.db.add(Branch(repository_id="r1", name=name, head_commit_id=head,
                           is_default=default))
        self.db.commit()

    def build(self, **kwargs):
        return commit_dag.build(self.db, self.repo, **kwargs)

    def ids(self, graph):
        return [c["id"] for c in graph["commits"]]

    def assertChildrenBeforeParents(self, graph):
        position = {c["id"]: i for i, c in enumerate(graph["commits"])}
        for commit in graph["commits"]:
            for parent in commit["parents"]:
                if parent in position:
                    self.assertLess(
                        position[commit["id"]], position[parent],
                        f"{commit['id']} must be drawn before its parent {parent}",
                    )

    # --- the ordering invariant -------------------------------------------------

    def test_children_always_precede_their_parents(self):
        self.assertChildrenBeforeParents(self.build())

    def test_ordering_survives_a_parent_with_a_newer_timestamp(self):
        """Clock skew and rewritten dates must not reorder the graph."""
        skewed = self.db.query(Commit).filter(Commit.id == "c1").first()
        skewed.created_at = self.clock + timedelta(days=365)
        self.db.commit()

        graph = self.build()
        self.assertChildrenBeforeParents(graph)
        # And the point: sorting by date would have put c1 first.
        self.assertNotEqual(self.ids(graph)[0], "c1")

    def test_the_newest_tip_is_drawn_first(self):
        self.assertEqual(self.ids(self.build())[0], "m1")

    # --- what it contains -------------------------------------------------------

    def test_the_graph_spans_every_branch_not_just_one(self):
        ids = set(self.ids(self.build()))
        self.assertEqual(ids, {"c1", "c2", "f1", "f2", "t1", "m1"})

    def test_a_branch_only_reachable_from_its_own_tip_is_included(self):
        """t1 is on no other branch; a single-branch listing would miss it."""
        self.assertIn("t1", self.ids(self.build()))

    def test_a_merge_reports_both_parents_in_order(self):
        merge = next(c for c in self.build()["commits"] if c["id"] == "m1")
        self.assertEqual(merge["parents"], ["c2", "f2"])
        self.assertTrue(merge["is_merge"])

    def test_ordinary_commits_are_not_marked_as_merges(self):
        c2 = next(c for c in self.build()["commits"] if c["id"] == "c2")
        self.assertFalse(c2["is_merge"])

    def test_branch_tips_are_labelled(self):
        by_id = {c["id"]: c for c in self.build()["commits"]}
        self.assertEqual([h["name"] for h in by_id["m1"]["branch_heads"]], ["main"])
        self.assertEqual([h["name"] for h in by_id["t1"]["branch_heads"]], ["topic"])
        self.assertEqual(by_id["c2"]["branch_heads"], [])

    def test_the_default_branch_is_flagged(self):
        by_id = {c["id"]: c for c in self.build()["commits"]}
        self.assertTrue(by_id["m1"]["branch_heads"][0]["is_default"])

    def test_tags_are_labelled(self):
        self.db.add(Tag(repository_id="r1", name="v1.0", commit_id="c2"))
        self.db.commit()
        by_id = {c["id"]: c for c in self.build()["commits"]}
        self.assertEqual(by_id["c2"]["tags"], ["v1.0"])

    def test_every_commit_carries_what_a_row_needs(self):
        commit = self.build()["commits"][0]
        for key in ("id", "short_id", "message", "author", "timestamp", "parents"):
            self.assertIn(key, commit)
        # Test ids here are short by design; what matters is that short_id is a
        # prefix of the real id and never longer than the display width.
        self.assertTrue(commit["id"].startswith(commit["short_id"]))
        self.assertLessEqual(len(commit["short_id"]), 8)

    def test_branches_are_reported_alongside_the_commits(self):
        names = sorted(b["name"] for b in self.build()["branches"])
        self.assertEqual(names, ["feature", "main", "topic"])

    # --- limits -----------------------------------------------------------------

    def test_a_limit_truncates_and_says_so(self):
        graph = self.build(limit=3)
        self.assertEqual(len(graph["commits"]), 3)
        self.assertTrue(graph["truncated"])
        self.assertEqual(graph["total"], 6)

    def test_a_truncated_graph_still_orders_correctly(self):
        self.assertChildrenBeforeParents(self.build(limit=4))

    def test_a_parent_cut_by_the_limit_is_marked(self):
        """So the renderer draws the rail continuing rather than ending the lane."""
        graph = self.build(limit=2)
        present = {c["id"] for c in graph["commits"]}
        for commit in graph["commits"]:
            for parent in commit["parents"]:
                if parent not in present:
                    self.assertIn(parent, commit["truncated_parents"])

    def test_truncation_keeps_recent_history_across_branches(self):
        """Breadth-first from every tip, so a small graph is not one branch only."""
        graph = self.build(limit=3)
        self.assertIn("m1", self.ids(graph))
        self.assertIn("t1", self.ids(graph))

    def test_narrowing_to_one_branch_excludes_the_others(self):
        ids = set(self.ids(self.build(branch="topic")))
        self.assertIn("t1", ids)
        self.assertNotIn("m1", ids)
        self.assertNotIn("c2", ids)

    # --- edge cases -------------------------------------------------------------

    def test_a_repository_with_no_branches_returns_an_empty_graph(self):
        empty = Repository(id="r2", name="empty", owner_id=self.author.id)
        self.db.add(empty)
        self.db.commit()
        graph = commit_dag.build(self.db, empty)
        self.assertEqual(graph["commits"], [])
        self.assertFalse(graph["truncated"])

    def test_a_branch_with_no_head_is_skipped_without_failing(self):
        self.branch("empty-branch", None)
        self.assertChildrenBeforeParents(self.build())

    def test_two_branches_at_the_same_commit_are_both_labelled(self):
        self.branch("release", "m1")
        by_id = {c["id"]: c for c in self.build()["commits"]}
        self.assertEqual(
            sorted(h["name"] for h in by_id["m1"]["branch_heads"]), ["main", "release"])


if __name__ == "__main__":
    unittest.main()
