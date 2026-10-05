"""The resolution workflow as GitHub runs it, and the fork bug it had to fix first.

* A pull request's conflicts are resolved by merging the target *into the pull request's
  branch*; the approvals given before that go stale and the content is reviewed again.
* Decisions are saved per file, survive reopening, and can be "delete this file".
* A resolution that still carries conflict markers is refused unless confirmed.
* A pull request from a fork reads its source branch from the fork -- before this, a fork
  PR from ``main`` into ``main`` silently merged the upstream's own ``main``.
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

from app.services import blob_store, merge_conflicts, pr_reviews, pr_source  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from app.services.commit_graph import _get_commit_tree  # noqa: E402
from app.services.merge_conflicts import ConflictError  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import BranchCRUD, FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, BranchProtectionPolicy, Commit, CommitFile, CommitParent, PullRequest,
    Repository, User,
)


def b64(text):
    return base64.b64encode(text.encode()).decode("ascii")


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_mrw_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_mrw_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()
        merge_conflicts._MERGEABILITY_CACHE.clear()

        self.author = User(username="author", role="team_lead")
        self.alice = User(username="alice", role="team_lead")
        self.db.add_all([self.author, self.alice])
        self.db.flush()
        self.db.add(Repository(id="r1", name="demo", owner_id=self.author.id))
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

    def mk(self, cid, files, parents=(), repo="r1"):
        self.db.add(Commit(id=cid, repository_id=repo, author_id=self.author.id,
                           message=cid, parent_commit_id=parents[0] if parents else None))
        self.db.flush()
        for path, text in files.items():
            obj = FileObjectCRUD.store_file_object(self.db, text.encode())
            self.db.add(CommitFile(commit_id=cid, file_path=path,
                                   file_hash=obj.hash, file_size=obj.size))
        for order, pid in enumerate(parents):
            self.db.add(CommitParent(commit_id=cid, parent_commit_id=pid, parent_order=order))
        self.db.commit()

    def branch(self, name, head, repo="r1"):
        self.db.add(Branch(repository_id=repo, name=name, head_commit_id=head))
        self.db.commit()

    def head(self, name, repo="r1"):
        return BranchCRUD.get_branch(self.db, repo, name).head_commit_id

    def tree(self, cid):
        return {p: b.decode() for p, b in _get_commit_tree(self.db, cid).items()}

    def policy(self, pattern, rules, mode="protected", repo="r1"):
        self.db.add(BranchProtectionPolicy(
            repository_id=repo, branch_pattern=pattern, mode=mode,
            policy_version=1, rules_json=json.dumps(rules),
        ))
        self.db.commit()

    def pull_request(self, source="feature", target="main", source_repo=None):
        pr = PullRequest(repository_id="r1", source_repository_id=source_repo,
                         title="t", source_branch=source, target_branch=target,
                         status="open", created_by_id=self.author.id)
        self.db.add(pr)
        self.db.commit()
        self.db.refresh(pr)
        return pr


class ResolutionWorkflowTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.mk("base", {"a.txt": "1\n2\n3\n4\n5\n", "doc.txt": "keep\n", "gone.txt": "old\n"})
        # main edits line 2 and deletes gone.txt; feature edits line 2 and gone.txt.
        self.mk("m1", {"a.txt": "1\nMAIN\n3\n4\n5\n", "doc.txt": "keep\n"}, ("base",))
        self.mk("f1", {"a.txt": "1\nFEATURE\n3\n4\n5\n", "doc.txt": "keep\n",
                       "gone.txt": "edited\n"}, ("base",))
        self.branch("main", "m1")
        self.branch("feature", "f1")
        self.pr = self.pull_request()

    def open(self):
        return merge_conflicts.open_session(self.db, "r1", self.pr, self.author)

    def files(self, session):
        return {f["path"]: f for f in merge_conflicts.get_bundle(self.db, session)["files"]}

    # --- what the resolver is shown ---------------------------------------------

    def test_each_conflict_is_classified(self):
        files = self.files(self.open())
        self.assertEqual(files["a.txt"]["kind"], "content")
        self.assertEqual(files["a.txt"]["conflict_count"], 1)
        self.assertEqual(files["gone.txt"]["kind"], "modify_delete")
        self.assertEqual(files["gone.txt"]["deleted_in"], "target")
        # For keep-or-delete the suggestion is the surviving file, not a marker block.
        self.assertEqual(base64.b64decode(files["gone.txt"]["suggested_b64"]), b"edited\n")

    def test_the_bundle_names_which_branch_each_side_is(self):
        bundle = merge_conflicts.get_bundle(self.db, self.open())
        self.assertEqual(bundle["sides"]["ours"], {"role": "target", "branch": "main"})
        self.assertEqual(bundle["sides"]["theirs"], {"role": "source", "branch": "feature"})
        self.assertEqual(bundle["session"]["lands_on"], "source")
        self.assertEqual(bundle["session"]["commit_branch"], "feature")

    # --- saving decisions --------------------------------------------------------

    def test_saved_decisions_survive_reopening_the_resolver(self):
        session = self.open()
        merge_conflicts.save_resolution(self.db, session, "a.txt", self.author,
                                        content_b64=b64("1\nBOTH\n3\n4\n5\n"))
        again = self.open()
        self.assertEqual(again.id, session.id)
        entry = self.files(again)["a.txt"]
        self.assertTrue(entry["resolved"])
        self.assertEqual(base64.b64decode(entry["resolved_b64"]), b"1\nBOTH\n3\n4\n5\n")
        self.assertEqual(entry["resolved_by"], "author")
        self.assertEqual(merge_conflicts.describe(self.db, again)["unresolved"], 1)

    def test_saved_decisions_complete_the_merge_without_being_resent(self):
        session = self.open()
        merge_conflicts.save_resolution(self.db, session, "a.txt", self.author,
                                        content_b64=b64("1\nBOTH\n3\n4\n5\n"))
        merge_conflicts.save_resolution(self.db, session, "gone.txt", self.author, delete=True)

        result = merge_conflicts.resolve(self.db, session, None, self.author)
        tree = self.tree(result["merge_commit_id"])
        self.assertEqual(tree["a.txt"], "1\nBOTH\n3\n4\n5\n")
        self.assertNotIn("gone.txt", tree)
        self.assertEqual(result["deleted_files"], ["gone.txt"])

    def test_null_in_a_submission_means_delete(self):
        session = self.open()
        result = merge_conflicts.resolve(
            self.db, session, {"a.txt": b64("x\n"), "gone.txt": None}, self.author)
        self.assertNotIn("gone.txt", self.tree(result["merge_commit_id"]))

    def test_keeping_a_file_deleted_on_one_side(self):
        session = self.open()
        result = merge_conflicts.resolve(
            self.db, session, {"a.txt": b64("x\n"), "gone.txt": b64("edited\n")}, self.author)
        self.assertEqual(self.tree(result["merge_commit_id"])["gone.txt"], "edited\n")

    def test_clearing_a_decision_makes_the_file_unresolved_again(self):
        session = self.open()
        merge_conflicts.save_resolution(self.db, session, "gone.txt", self.author, delete=True)
        merge_conflicts.save_resolution(self.db, session, "gone.txt", self.author, clear=True)
        self.assertFalse(self.files(session)["gone.txt"]["resolved"])
        with self.assertRaises(ConflictError) as caught:
            merge_conflicts.resolve(self.db, session, {"a.txt": b64("x\n")}, self.author)
        self.assertEqual(caught.exception.payload["code"], "INCOMPLETE")

    def test_saving_a_path_that_is_not_conflicted_is_refused(self):
        with self.assertRaises(ConflictError) as caught:
            merge_conflicts.save_resolution(self.db, self.open(), "doc.txt", self.author,
                                            content_b64=b64("sneaky\n"))
        self.assertEqual(caught.exception.status_code, 404)

    def test_saving_into_a_stale_session_is_refused(self):
        session = self.open()
        self.mk("m2", {"a.txt": "moved\n", "doc.txt": "keep\n"}, ("m1",))
        BranchCRUD.update_branch_head(self.db, "r1", "main", "m2")
        with self.assertRaises(ConflictError) as caught:
            merge_conflicts.save_resolution(self.db, session, "a.txt", self.author,
                                            content_b64=b64("x\n"))
        self.assertEqual(caught.exception.payload["code"], "SESSION_STALE")
        self.assertEqual(session.status, "stale")

    def test_a_draft_with_markers_is_saved_but_flagged(self):
        session = self.open()
        suggested = self.files(session)["a.txt"]["suggested_b64"]
        saved = merge_conflicts.save_resolution(self.db, session, "a.txt", self.author,
                                                content_b64=suggested)
        self.assertTrue(saved["has_markers"])

    # --- the marker guard ----------------------------------------------------------

    def test_a_resolution_still_carrying_markers_is_refused(self):
        session = self.open()
        suggested = self.files(session)["a.txt"]["suggested_b64"]
        with self.assertRaises(ConflictError) as caught:
            merge_conflicts.resolve(self.db, session,
                                    {"a.txt": suggested, "gone.txt": None}, self.author)
        self.assertEqual(caught.exception.payload["code"], "UNRESOLVED_MARKERS")
        self.assertEqual(caught.exception.payload["paths"], ["a.txt"])
        self.assertEqual(self.head("feature"), "f1")     # nothing was written

    def test_markers_can_be_kept_when_confirmed(self):
        session = self.open()
        suggested = self.files(session)["a.txt"]["suggested_b64"]
        result = merge_conflicts.resolve(self.db, session,
                                         {"a.txt": suggested, "gone.txt": None},
                                         self.author, allow_markers=True)
        self.assertIn("<<<<<<< main", self.tree(result["merge_commit_id"])["a.txt"])

    # --- GitHub's model: resolve on the PR branch, then review again -----------------

    def test_approvals_given_before_the_resolution_go_stale(self):
        self.policy("main", {"required_approvals": 1})
        pr_reviews.submit(self.db, self.db.get(Repository, "r1"), self.pr, self.alice, "approved")
        self.assertTrue(pr_reviews.summarize(self.db, self.db.get(Repository, "r1"),
                                             self.pr)["can_merge"])

        merge_conflicts.resolve(self.db, self.open(),
                                {"a.txt": b64("x\n"), "gone.txt": None}, self.author)

        summary = pr_reviews.summarize(self.db, self.db.get(Repository, "r1"), self.pr)
        self.assertFalse(summary["can_merge"])
        self.assertEqual(len(summary["stale_approvals"]), 1)

    def test_a_protected_source_branch_refuses_a_browser_resolution(self):
        # GitHub will not resolve onto a protected head branch either.
        self.policy("feature", {"required_approvals": 1})
        with self.assertRaises(ConflictError) as caught:
            merge_conflicts.resolve(self.db, self.open(),
                                    {"a.txt": b64("x\n"), "gone.txt": None}, self.author)
        self.assertEqual(caught.exception.payload["code"], "REVIEW_REQUIRED")
        self.assertEqual(self.head("feature"), "f1")

    def test_the_head_guard_applies_to_the_branch_written(self):
        session = self.open()
        with self.assertRaises(ConflictError) as caught:
            merge_conflicts.resolve(self.db, session,
                                    {"a.txt": b64("x\n"), "gone.txt": None}, self.author,
                                    expected_head_commit_id="m1")   # target, not source
        self.assertEqual(caught.exception.payload["code"], "HEAD_MISMATCH")
        result = merge_conflicts.resolve(self.db, session,
                                         {"a.txt": b64("x\n"), "gone.txt": None}, self.author,
                                         expected_head_commit_id="f1")
        self.assertEqual(result["status"], "resolved")

    def test_a_closed_pull_request_cannot_be_resolved(self):
        session = self.open()
        self.pr.status = "closed"
        self.db.commit()
        with self.assertRaises(ConflictError) as caught:
            merge_conflicts.resolve(self.db, session,
                                    {"a.txt": b64("x\n"), "gone.txt": None}, self.author)
        self.assertEqual(caught.exception.payload["code"], "PULL_REQUEST_CLOSED")

    # --- mergeability ----------------------------------------------------------------

    def test_mergeability_reports_conflicts_and_the_open_session(self):
        before = merge_conflicts.mergeability(self.db, "r1", self.pr)
        self.assertEqual(before["state"], "conflicts")
        self.assertEqual(before["conflicts"], ["a.txt", "gone.txt"])
        self.assertIsNone(before["open_session_id"])

        session = self.open()
        self.assertEqual(
            merge_conflicts.mergeability(self.db, "r1", self.pr)["open_session_id"], session.id)

    def test_mergeability_of_a_clean_pull_request(self):
        self.mk("f2", {"a.txt": "1\n2\n3\n4\n5\n", "doc.txt": "keep\n", "new.txt": "n\n"},
                ("m1",))
        self.branch("topic", "f2")
        result = merge_conflicts.mergeability(self.db, "r1", self.pull_request(source="topic"))
        self.assertEqual((result["state"], result["mergeable"]), ("clean", True))

    def test_open_sessions_are_listed_for_resuming(self):
        session = self.open()
        listed = merge_conflicts.list_open(self.db, "r1", pull_request_id=self.pr.id)
        self.assertEqual([s["id"] for s in listed], [session.id])
        self.assertEqual(merge_conflicts.list_open(self.db, "r1", branch_only=True), [])


class ForkPullRequestTests(_Fixture):
    """The branch named by a fork's pull request lives in the fork."""

    def setUp(self):
        super().setUp()
        self.db.add(Repository(id="f1", name="demo", owner_id=self.alice.id,
                               forked_from_id="r1"))
        self.db.commit()
        self.mk("base", {"x.txt": "x\n", "y.txt": "y\n"})
        self.mk("up", {"x.txt": "upstream\n", "y.txt": "y\n"}, ("base",))
        self.branch("main", "up")
        # The fork's main carries the contribution; its commits live under the fork.
        self.mk("fk", {"x.txt": "x\n", "y.txt": "fork work\n"}, ("base",), repo="f1")
        self.branch("main", "fk", repo="f1")
        self.pr = self.pull_request(source="main", target="main", source_repo="f1")

    def test_the_source_is_read_from_the_fork(self):
        self.assertEqual(pr_source.head(self.db, self.pr), "fk")
        self.assertTrue(pr_source.is_cross_repository(self.pr))

    def test_mergeability_compares_the_fork_branch(self):
        result = merge_conflicts.mergeability(self.db, "r1", self.pr)
        self.assertEqual(result["source_head_commit_id"], "fk")
        self.assertEqual(result["state"], "clean")

    def test_a_review_pins_the_fork_head(self):
        # Before the fix this pinned upstream main ("up"), so the approval went stale or
        # stayed current according to the wrong branch.
        review = pr_reviews.submit(self.db, self.db.get(Repository, "r1"), self.pr,
                                   self.alice, "approved")
        self.assertEqual(review.commit_id, "fk")

    def test_a_fork_conflict_is_resolved_in_the_fork(self):
        self.mk("fk2", {"x.txt": "fork edit\n", "y.txt": "fork work\n"}, ("fk",), repo="f1")
        BranchCRUD.update_branch_head(self.db, "f1", "main", "fk2")

        session = merge_conflicts.open_session(self.db, "r1", self.pr, self.alice)
        self.assertEqual(session.source_repository_id, "f1")
        self.assertEqual(session.source_head_commit_id, "fk2")

        result = merge_conflicts.resolve(self.db, session, {"x.txt": b64("both\n")}, self.alice)
        self.assertEqual(result["commit_repository_id"], "f1")
        self.assertEqual(self.head("main", repo="f1"), result["merge_commit_id"])
        self.assertEqual(self.head("main"), "up")              # upstream untouched
        merged = self.db.get(Commit, result["merge_commit_id"])
        self.assertEqual(merged.repository_id, "f1")
        self.assertEqual(self.tree(result["merge_commit_id"]),
                         {"x.txt": "both\n", "y.txt": "fork work\n"})
        self.assertEqual(merge_conflicts.mergeability(self.db, "r1", self.pr)["state"], "clean")


if __name__ == "__main__":
    unittest.main()
