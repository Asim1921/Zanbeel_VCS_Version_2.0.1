"""Tests for pull request review, approval and the merge gate.

Before this feature a pull request could be merged by anyone with access without a single
review. Most of these tests pin down the ways a gate can be quietly defeated: approving
your own work, approving and then pushing something else, or resolving conflicts instead
of merging cleanly.
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

from app.services import blob_store, branch_ops, pr_reviews  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from app.services.pr_reviews import ReviewError  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import BranchCRUD, FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, Commit, CommitFile, CommitParent, PullRequest, PullRequestReview,
    Repository, User,
)


class PullRequestReviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_pr_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_pr_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.author = User(username="author", role="team_lead")
        self.alice = User(username="alice", role="team_lead")
        self.bob = User(username="bob", role="team_lead")
        self.db.add_all([self.author, self.alice, self.bob])
        self.db.flush()
        self.repo = Repository(id="r1", name="demo", owner_id=self.author.id)
        self.db.add(self.repo)
        self.db.commit()

        self.mk("base", {"a.txt": "A"})
        self.mk("feat1", {"a.txt": "A", "f.txt": "F1"}, ("base",))
        self.branch("main", "base", default=True)
        self.branch("feature", "feat1")

        self.pr = PullRequest(repository_id="r1", title="add f", source_branch="feature",
                              target_branch="main", status="open",
                              created_by_id=self.author.id)
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

    def require(self, n):
        branch_ops.set_policy(self.db, self.repo, {"required_approvals": n})

    def summary(self):
        return pr_reviews.summarize(self.db, self.repo, self.pr)

    # ---------------- submitting ----------------

    def test_a_review_is_recorded(self):
        review = pr_reviews.submit(self.db, self.repo, self.pr, self.alice,
                                   "approved", "looks good")
        self.assertEqual(review.state, "approved")
        self.assertEqual(review.body, "looks good")
        self.assertEqual(review.commit_id, "feat1")     # the commit actually reviewed

    def test_invalid_state_is_rejected(self):
        with self.assertRaises(ReviewError):
            pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "lgtm")

    def test_authors_cannot_approve_their_own_pull_request(self):
        with self.assertRaises(ReviewError) as ctx:
            pr_reviews.submit(self.db, self.repo, self.pr, self.author, "approved")
        self.assertEqual(ctx.exception.payload.get("code"), "SELF_APPROVAL")

    def test_authors_may_still_comment_on_their_own_pull_request(self):
        pr_reviews.submit(self.db, self.repo, self.pr, self.author, "commented", "note")

    def test_a_closed_pull_request_cannot_be_reviewed(self):
        self.pr.status = "merged"
        self.db.commit()
        with self.assertRaises(ReviewError):
            pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "approved")

    def test_reviews_append_rather_than_replace(self):
        pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "changes_requested")
        pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "approved")
        self.assertEqual(self.db.query(PullRequestReview).count(), 2)
        self.assertEqual(len(pr_reviews.history(self.db, self.pr.id)), 2)

    # ---------------- the gate ----------------

    def test_gate_is_off_by_default(self):
        self.assertEqual(self.summary()["required_approvals"], 0)
        self.assertTrue(self.summary()["can_merge"])
        pr_reviews.enforce_merge_gate(self.db, self.repo, self.pr)

    def test_required_approvals_block_an_unreviewed_merge(self):
        self.require(1)
        summary = self.summary()
        self.assertFalse(summary["can_merge"])
        with self.assertRaises(ReviewError) as ctx:
            pr_reviews.enforce_merge_gate(self.db, self.repo, self.pr)
        self.assertEqual(ctx.exception.payload.get("code"), "REVIEW_REQUIRED")

    def test_an_approval_satisfies_the_gate(self):
        self.require(1)
        pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "approved")
        self.assertTrue(self.summary()["can_merge"])
        pr_reviews.enforce_merge_gate(self.db, self.repo, self.pr)

    def test_two_approvals_needed_means_two_distinct_reviewers(self):
        self.require(2)
        pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "approved")
        self.assertFalse(self.summary()["can_merge"])
        # the same reviewer approving twice does not count twice
        pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "approved")
        self.assertFalse(self.summary()["can_merge"])
        pr_reviews.submit(self.db, self.repo, self.pr, self.bob, "approved")
        self.assertTrue(self.summary()["can_merge"])

    def test_changes_requested_blocks_even_with_enough_approvals(self):
        self.require(1)
        pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "approved")
        pr_reviews.submit(self.db, self.repo, self.pr, self.bob, "changes_requested")

        summary = self.summary()
        self.assertFalse(summary["can_merge"])
        self.assertTrue(any("changes requested by bob" in b for b in summary["blockers"]))

    def test_a_reviewer_can_withdraw_their_objection(self):
        self.require(1)
        pr_reviews.submit(self.db, self.repo, self.pr, self.bob, "changes_requested")
        self.assertFalse(self.summary()["can_merge"])
        pr_reviews.submit(self.db, self.repo, self.pr, self.bob, "approved")
        self.assertTrue(self.summary()["can_merge"])

    def test_commenting_does_not_withdraw_an_approval(self):
        self.require(1)
        pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "approved")
        pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "commented", "one more thing")
        self.assertTrue(self.summary()["can_merge"])

    # ---------------- staleness: approve, then push something else ----------------

    def test_an_approval_goes_stale_when_the_branch_moves(self):
        self.require(1)
        pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "approved")
        self.assertTrue(self.summary()["can_merge"])

        # The author pushes more work after getting approval.
        self.mk("feat2", {"a.txt": "A", "f.txt": "F2", "sneaky.txt": "unreviewed"}, ("feat1",))
        BranchCRUD.update_branch_head(self.db, "r1", "feature", "feat2")

        summary = self.summary()
        self.assertFalse(summary["can_merge"])
        self.assertEqual(len(summary["stale_approvals"]), 1)
        self.assertEqual(summary["approvals"], [])
        self.assertTrue(any("stale" in b for b in summary["blockers"]))

    def test_re_approving_the_new_head_clears_staleness(self):
        self.require(1)
        pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "approved")
        self.mk("feat2", {"a.txt": "A", "f.txt": "F2"}, ("feat1",))
        BranchCRUD.update_branch_head(self.db, "r1", "feature", "feat2")
        self.assertFalse(self.summary()["can_merge"])

        pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "approved")
        self.assertTrue(self.summary()["can_merge"])

    def test_changes_requested_does_not_go_stale(self):
        # An objection stands until its author withdraws it, regardless of new pushes.
        self.require(0)
        pr_reviews.submit(self.db, self.repo, self.pr, self.bob, "changes_requested")
        self.mk("feat2", {"a.txt": "A", "f.txt": "F2"}, ("feat1",))
        BranchCRUD.update_branch_head(self.db, "r1", "feature", "feat2")

        self.assertFalse(self.summary()["can_merge"])

    # ---------------- policy plumbing ----------------

    def test_required_approvals_round_trips_through_the_policy(self):
        branch_ops.set_policy(self.db, self.repo, {"required_approvals": 3})
        self.assertEqual(pr_reviews.required_approvals(self.repo), 3)

    def test_a_negative_requirement_is_clamped(self):
        branch_ops.set_policy(self.db, self.repo, {"required_approvals": -5})
        self.assertEqual(pr_reviews.required_approvals(self.repo), 0)

    def test_a_non_numeric_requirement_is_rejected(self):
        with self.assertRaises(Exception):
            branch_ops.set_policy(self.db, self.repo, {"required_approvals": "two"})

    def test_summary_reports_who_approved(self):
        pr_reviews.submit(self.db, self.repo, self.pr, self.alice, "approved", "ship it")
        entry = self.summary()["approvals"][0]
        self.assertEqual(entry["reviewer"], "alice")
        self.assertEqual(entry["body"], "ship it")


if __name__ == "__main__":
    unittest.main()
