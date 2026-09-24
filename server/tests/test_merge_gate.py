"""Tests for the merge gate: the review rules a merge must satisfy, pull request or not.

The pull-request merge paths were always gated. A *branch* merge was not: ``branch_ops``
moved the target reference with ``OP_MERGE``, and protected mode permits ``merge`` by
design, so ``fox merge feature`` landed unreviewed code on exactly the branches most
protected from it. Every test here is that bypass, or a way of re-opening it.
"""
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

from app.services import blob_store, branch_ops, branch_protection, merge_gate  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, BranchProtectionPolicy, Commit, CommitFile, CommitParent, PullRequest,
    PullRequestReview, Repository, User,
)


class MergeGateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_gate_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_gate_", suffix=".db")
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

        # main and feature diverge, so a merge has real work to do.
        self.mk("base", {"a.txt": "A"})
        self.mk("mainwork", {"a.txt": "A", "m.txt": "M"}, ("base",))
        self.mk("feat1", {"a.txt": "A", "f.txt": "F1"}, ("base",))
        self.branch("main", "mainwork", default=True)
        self.branch("feature", "feat1")
        # A branch behind the others, for the fast-forward path.
        self.branch("ff", "base")

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

    def merge(self, source="feature", target="main"):
        return branch_ops.merge_branches(self.db, self.repo, self.author, source, target)

    # --- the bypass -------------------------------------------------------------

    def test_branch_merge_into_a_review_required_branch_is_refused(self):
        """The bypass. A branch merge landed unreviewed code on a protected branch."""
        self.policy("main", {"required_approvals": 2}, mode="protected")
        with self.assertRaises(branch_ops.BranchOpError) as caught:
            self.merge()
        self.assertEqual(caught.exception.payload.get("code"), "REVIEW_REQUIRED")
        self.assertIn("2 approval", caught.exception.message)

    def test_publish_fast_forward_is_gated_too(self):
        """Fast-forwarding is still landing content; it needs the same permission."""
        self.policy("main", {"required_approvals": 1})
        with self.assertRaises(branch_ops.BranchOpError) as caught:
            branch_ops.publish_branch(self.db, self.repo, self.author, "feature", "main")
        self.assertEqual(caught.exception.payload.get("code"), "REVIEW_REQUIRED")

    def test_code_owner_requirement_alone_blocks_a_bare_merge(self):
        """Requiring owners is requiring review, even with no numeric threshold."""
        self.policy("main", {"require_code_owners": True})
        with self.assertRaises(branch_ops.BranchOpError) as caught:
            self.merge()
        self.assertIn("code owner", caught.exception.message)

    def test_status_check_requirement_alone_blocks_a_bare_merge(self):
        self.policy("main", {"require_status_checks": ["ci/build"]})
        with self.assertRaises(branch_ops.BranchOpError) as caught:
            self.merge()
        self.assertIn("ci/build", caught.exception.message)

    def test_an_approved_pull_request_is_not_a_licence_to_merge_around_it(self):
        """Merging around an approved PR leaves it open and its approval unspent."""
        self.policy("main", {"required_approvals": 1})
        pr = PullRequest(repository_id="r1", title="t", source_branch="feature",
                         target_branch="main", status="open", created_by_id=self.author.id)
        self.db.add(pr)
        self.db.commit()
        self.db.add(PullRequestReview(pull_request_id=pr.id, reviewer_id=self.alice.id,
                                      state="approved", commit_id="feat1"))
        self.db.commit()

        with self.assertRaises(branch_ops.BranchOpError) as caught:
            self.merge()
        # The refusal names the pull request to use rather than just saying no.
        self.assertEqual(caught.exception.payload.get("open_pull_request_id"), pr.id)
        self.assertIn(str(pr.id), caught.exception.message)

    # --- no regression ----------------------------------------------------------

    def test_merge_still_works_when_no_review_is_required(self):
        """The default is zero approvals, so existing repositories are untouched."""
        result = self.merge()
        self.assertEqual(result["status"], "merged")

    def test_protected_mode_alone_does_not_require_review(self):
        """Protected blocks direct pushes; it does not imply an approval threshold."""
        self.policy("main", {}, mode="protected")
        self.assertEqual(self.merge()["status"], "merged")

    # --- scoping ----------------------------------------------------------------

    def test_rules_on_main_do_not_leak_onto_other_branches(self):
        self.policy("main", {"required_approvals": 2})
        # feature is the target here, and carries no policy of its own.
        result = branch_ops.merge_branches(self.db, self.repo, self.author, "ff", "feature")
        self.assertIn(result["status"], ("merged", "already_up_to_date"))

    def test_repository_wide_setting_is_still_honoured(self):
        """Repositories configured before per-branch policies keep working."""
        branch_ops.set_policy(self.db, self.repo, {"required_approvals": 1})
        with self.assertRaises(branch_ops.BranchOpError) as caught:
            self.merge()
        self.assertEqual(caught.exception.payload.get("code"), "REVIEW_REQUIRED")

    def test_a_weaker_pattern_cannot_undo_a_stricter_one(self):
        """Most restrictive wins, so a wildcard cannot relax an exact-name rule."""
        self.policy("main", {"required_approvals": 3})
        self.policy("*", {"required_approvals": 0})
        rules = branch_protection.review_rules(self.db, self.repo, "main")
        self.assertEqual(rules["required_approvals"], 3)

    def test_code_owner_flag_survives_combination(self):
        self.policy("main", {"require_code_owners": True})
        self.policy("*", {"require_code_owners": False})
        self.assertTrue(
            branch_protection.review_rules(self.db, self.repo, "main")["require_code_owners"]
        )

    def test_required_checks_are_unioned_across_patterns(self):
        self.policy("main", {"require_status_checks": ["ci/build"]})
        self.policy("*", {"require_status_checks": ["ci/lint"]})
        checks = branch_protection.review_rules(self.db, self.repo, "main")["require_status_checks"]
        self.assertEqual(sorted(checks), ["ci/build", "ci/lint"])

    def test_requires_review_is_false_for_an_unconfigured_branch(self):
        self.assertFalse(branch_protection.requires_review(self.db, self.repo, "main"))

    def test_open_pull_request_lookup_ignores_closed_ones(self):
        closed = PullRequest(repository_id="r1", title="old", source_branch="feature",
                             target_branch="main", status="closed",
                             created_by_id=self.author.id)
        self.db.add(closed)
        self.db.commit()
        self.assertIsNone(
            merge_gate.open_pull_request_for(self.db, self.repo, "feature", "main")
        )


if __name__ == "__main__":
    unittest.main()
