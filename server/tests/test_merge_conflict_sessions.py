"""Tests for server-side resolution of a *branch* merge's conflicts.

Resolution sessions belonged to pull requests, so a conflicted `fox merge` reported the
conflict and stopped -- the person who hit it had nowhere to go. These cover the branch
sessions that close that gap, and the rules they must not let anyone around: the target
branch's protection mode and its review requirement both still apply, because a resolver
that ignored them would simply be the unreviewed door by another name.
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

from app.services import blob_store, branch_ops, merge_conflicts  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, BranchProtectionPolicy, Commit, CommitFile, CommitParent,
    MergeConflictSession, PullRequest, Repository, User,
)


def b64(text):
    return base64.b64encode(text.encode()).decode()


class BranchMergeSessionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_mcs_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_mcs_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.author = User(username="author", role="team_lead")
        self.alice = User(username="alice", role="team_lead")
        self.db.add_all([self.author, self.alice])
        self.db.flush()
        self.repo = Repository(id="r1", name="demo", owner_id=self.author.id)
        self.db.add(self.repo)
        self.db.commit()

        # Both branches edit the same line of shared.txt, so the merge conflicts.
        self.mk("base", {"shared.txt": "original\n", "quiet.txt": "same\n"})
        self.mk("mainwork", {"shared.txt": "from main\n", "quiet.txt": "same\n"}, ("base",))
        self.mk("feat", {"shared.txt": "from feature\n", "quiet.txt": "same\n"}, ("base",))
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

    def policy(self, pattern, rules, mode="open"):
        self.db.add(BranchProtectionPolicy(
            repository_id="r1", branch_pattern=pattern, mode=mode,
            policy_version=1, rules_json=json.dumps(rules),
        ))
        self.db.commit()

    def open_session(self, source="feature", target="main"):
        return merge_conflicts.open_branch_session(
            self.db, "r1", self.author, source_branch=source, target_branch=target)

    # --- the gap this closes ----------------------------------------------------

    def test_a_conflicted_branch_merge_reports_where_to_resolve_it(self):
        """Reporting the conflict and stopping is what this feature exists to fix."""
        with self.assertRaises(branch_ops.BranchOpError) as caught:
            branch_ops.merge_branches(self.db, self.repo, self.author, "feature", "main")
        payload = caught.exception.payload
        self.assertEqual(payload.get("code"), "MERGE_CONFLICT")
        self.assertIn("shared.txt", payload.get("conflicts", []))
        self.assertIn("resolve_url", payload)

    def test_a_branch_session_needs_no_pull_request(self):
        session = self.open_session()
        self.assertIsNone(session.pull_request_id)
        self.assertEqual(session.status, "open")

    def test_the_session_parks_all_three_sides(self):
        bundle = merge_conflicts.get_bundle(self.db, self.open_session())
        entry = next(f for f in bundle["files"] if f["path"] == "shared.txt")
        self.assertEqual(base64.b64decode(entry["base_b64"]).decode(), "original\n")
        self.assertEqual(base64.b64decode(entry["ours_b64"]).decode(), "from main\n")
        self.assertEqual(base64.b64decode(entry["theirs_b64"]).decode(), "from feature\n")

    def test_only_conflicted_paths_are_parked(self):
        bundle = merge_conflicts.get_bundle(self.db, self.open_session())
        self.assertEqual([f["path"] for f in bundle["files"]], ["shared.txt"])

    def test_a_clean_merge_is_refused_a_session(self):
        self.branch("clean", "base")
        with self.assertRaises(merge_conflicts.ConflictError) as caught:
            merge_conflicts.open_branch_session(
                self.db, "r1", self.author, source_branch="clean", target_branch="main")
        self.assertIn("cleanly", caught.exception.message)

    # --- reuse and staleness ----------------------------------------------------

    def test_reopening_reuses_the_session_rather_than_discarding_work(self):
        first = self.open_session()
        self.assertEqual(self.open_session().id, first.id)

    def test_a_moved_branch_retires_the_session(self):
        first = self.open_session()
        self.mk("feat2", {"shared.txt": "moved on\n", "quiet.txt": "same\n"}, ("feat",))
        branch = self.db.query(Branch).filter(Branch.name == "feature").first()
        branch.head_commit_id = "feat2"
        self.db.commit()

        second = self.open_session()
        self.assertNotEqual(second.id, first.id)
        self.db.refresh(first)
        self.assertEqual(first.status, "stale")

    def test_resolving_a_stale_session_is_refused(self):
        session = self.open_session()
        self.mk("feat2", {"shared.txt": "moved on\n", "quiet.txt": "same\n"}, ("feat",))
        branch = self.db.query(Branch).filter(Branch.name == "feature").first()
        branch.head_commit_id = "feat2"
        self.db.commit()

        with self.assertRaises(merge_conflicts.ConflictError) as caught:
            merge_conflicts.resolve(
                self.db, session, {"shared.txt": b64("anything\n")}, self.author)
        self.assertEqual(caught.exception.payload.get("code"), "SESSION_STALE")

    # --- resolving --------------------------------------------------------------

    def test_resolving_completes_the_branch_merge(self):
        session = self.open_session()
        result = merge_conflicts.resolve(
            self.db, session, {"shared.txt": b64("reconciled\n")}, self.author)
        self.assertEqual(result["status"], "merged")

        target = self.db.query(Branch).filter(Branch.name == "main").first()
        self.assertEqual(target.head_commit_id, result["merge_commit_id"])
        self.db.refresh(session)
        self.assertEqual(session.status, "resolved")

    def test_the_resolution_is_what_lands(self):
        session = self.open_session()
        merge_conflicts.resolve(
            self.db, session, {"shared.txt": b64("reconciled\n")}, self.author)
        from app.services.commit_graph import _get_commit_tree
        target = self.db.query(Branch).filter(Branch.name == "main").first()
        tree = _get_commit_tree(self.db, target.head_commit_id)
        self.assertEqual(tree["shared.txt"], b"reconciled\n")

    def test_a_partial_resolution_is_refused(self):
        self.mk("base2", {"a.txt": "a\n", "b.txt": "b\n"})
        self.mk("m2", {"a.txt": "main a\n", "b.txt": "main b\n"}, ("base2",))
        self.mk("f2", {"a.txt": "feat a\n", "b.txt": "feat b\n"}, ("base2",))
        self.branch("m2b", "m2")
        self.branch("f2b", "f2")
        session = self.open_session(source="f2b", target="m2b")

        with self.assertRaises(merge_conflicts.ConflictError) as caught:
            merge_conflicts.resolve(self.db, session, {"a.txt": b64("x\n")}, self.author)
        self.assertEqual(caught.exception.payload.get("code"), "INCOMPLETE")

    def test_resolving_a_path_that_is_not_conflicted_is_refused(self):
        session = self.open_session()
        with self.assertRaises(merge_conflicts.ConflictError) as caught:
            merge_conflicts.resolve(
                self.db, session,
                {"shared.txt": b64("ok\n"), "quiet.txt": b64("sneaky\n")}, self.author)
        self.assertIn("Not conflicted", caught.exception.message)

    def test_aborting_leaves_both_branches_alone(self):
        session = self.open_session()
        before = self.db.query(Branch).filter(Branch.name == "main").first().head_commit_id
        merge_conflicts.abort(self.db, session, self.author)
        self.db.refresh(session)
        self.assertEqual(session.status, "aborted")
        after = self.db.query(Branch).filter(Branch.name == "main").first().head_commit_id
        self.assertEqual(after, before)

    # --- the rules a resolver must not bypass -----------------------------------

    def test_resolving_into_a_review_required_branch_is_refused(self):
        """Otherwise the resolver is the unreviewed door branch merges no longer are."""
        session = self.open_session()
        self.policy("main", {"required_approvals": 1}, mode="protected")

        with self.assertRaises(merge_conflicts.ConflictError) as caught:
            merge_conflicts.resolve(
                self.db, session, {"shared.txt": b64("reconciled\n")}, self.author)
        self.assertEqual(caught.exception.payload.get("code"), "REVIEW_REQUIRED")

    def test_resolving_into_a_frozen_branch_is_refused(self):
        session = self.open_session()
        self.policy("main", {}, mode="frozen")

        with self.assertRaises(Exception) as caught:
            merge_conflicts.resolve(
                self.db, session, {"shared.txt": b64("reconciled\n")}, self.author)
        self.assertIn("frozen", str(caught.exception).lower())

    def test_a_refused_resolution_leaves_the_branch_untouched(self):
        session = self.open_session()
        self.policy("main", {}, mode="frozen")
        before = self.db.query(Branch).filter(Branch.name == "main").first().head_commit_id
        commits_before = self.db.query(Commit).filter(Commit.repository_id == "r1").count()

        with self.assertRaises(Exception):
            merge_conflicts.resolve(
                self.db, session, {"shared.txt": b64("reconciled\n")}, self.author)

        self.db.expire_all()
        after = self.db.query(Branch).filter(Branch.name == "main").first().head_commit_id
        self.assertEqual(after, before)
        self.assertEqual(
            self.db.query(Commit).filter(Commit.repository_id == "r1").count(),
            commits_before,
            "a refused resolution stored its merge commit anyway",
        )

    def test_an_unresolved_pull_request_session_is_not_a_branch_session(self):
        """The two kinds are addressed differently; a PR session keeps its own gate."""
        pr = PullRequest(repository_id="r1", title="t", source_branch="feature",
                         target_branch="main", status="open", created_by_id=self.author.id)
        self.db.add(pr)
        self.db.commit()
        pr_session = merge_conflicts.open_session(self.db, "r1", pr, self.author)

        branch_sessions = (
            self.db.query(MergeConflictSession)
            .filter(MergeConflictSession.pull_request_id.is_(None))
            .all()
        )
        self.assertNotIn(pr_session.id, [s.id for s in branch_sessions])


if __name__ == "__main__":
    unittest.main()
