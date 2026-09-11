"""Tests for commit status checks and the merge gate they feed."""
import json
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
from database.crud import RepositoryCRUD, UserCRUD  # noqa: E402
from app.services import status_checks as sc  # noqa: E402


class StatusTestBase(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        self.user = UserCRUD.create_user(
            self.db, username="lead", email="lead@example.com", role="team_lead"
        )
        self.repo = RepositoryCRUD.create_repository(self.db, "lead", "proj", "test repo")

        # Statuses are refused against commits that do not exist, so the ids the
        # tests report on have to be real rows.
        from database.models import Commit
        for cid in ("abc", "abc123", "aaa", "bbb"):
            self.db.add(Commit(id=cid, repository_id=self.repo.id, message="t",
                               author_id=self.user.id))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def set_required(self, contexts):
        self.repo.branch_policy_json = json.dumps({"required_status_checks": contexts})
        self.db.commit()
        self.db.refresh(self.repo)


class ReportingTests(StatusTestBase):
    def test_report_stores_a_status(self):
        s = sc.report(self.db, self.repo, "abc123", "ci/unit", "success", "42 passed")
        self.assertEqual(s.state, "success")
        self.assertEqual(s.context, "ci/unit")
        self.assertEqual(s.commit_id, "abc123")

    def test_invalid_state_is_refused_by_name(self):
        with self.assertRaises(sc.StatusError) as ctx:
            sc.report(self.db, self.repo, "abc123", "ci/unit", "kinda-ok")
        self.assertIn("kinda-ok", ctx.exception.message)

    def test_context_is_required(self):
        with self.assertRaises(sc.StatusError):
            sc.report(self.db, self.repo, "abc123", "  ", "success")

    def test_commit_id_is_required(self):
        with self.assertRaises(sc.StatusError):
            sc.report(self.db, self.repo, "", "ci/unit", "success")

    def test_reporting_appends_rather_than_overwrites(self):
        """History matters: 'went red then someone re-ran it green' must survive."""
        sc.report(self.db, self.repo, "abc123", "ci/unit", "failure")
        sc.report(self.db, self.repo, "abc123", "ci/unit", "success")
        rows = sc.history(self.db, "abc123")
        self.assertEqual(len(rows), 2)

    def test_latest_report_wins_for_current_state(self):
        sc.report(self.db, self.repo, "abc123", "ci/unit", "failure")
        sc.report(self.db, self.repo, "abc123", "ci/unit", "success")
        current = sc.latest_per_context(self.db, "abc123")
        self.assertEqual(current["ci/unit"].state, "success")

    def test_different_contexts_are_tracked_separately(self):
        sc.report(self.db, self.repo, "abc123", "ci/unit", "success")
        sc.report(self.db, self.repo, "abc123", "ci/lint", "failure")
        current = sc.latest_per_context(self.db, "abc123")
        self.assertEqual(len(current), 2)
        self.assertEqual(current["ci/unit"].state, "success")
        self.assertEqual(current["ci/lint"].state, "failure")

    def test_reporting_against_a_nonexistent_commit_is_refused(self):
        """A typo'd SHA would otherwise report green forever while the real
        commit stayed unchecked."""
        with self.assertRaises(sc.StatusError) as ctx:
            sc.report(self.db, self.repo, "deadbeef-not-a-commit", "ci/unit", "success")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_statuses_do_not_bleed_between_commits(self):
        sc.report(self.db, self.repo, "aaa", "ci/unit", "success")
        self.assertEqual(sc.latest_per_context(self.db, "bbb"), {})


class RollupTests(StatusTestBase):
    def test_no_statuses_and_no_requirements_is_none(self):
        self.assertEqual(sc.combined(self.db, self.repo, "abc")["state"], "none")

    def test_no_statuses_but_requirements_is_pending(self):
        self.set_required(["ci/unit"])
        self.assertEqual(sc.combined(self.db, self.repo, "abc")["state"], "pending")

    def test_all_green_is_success(self):
        sc.report(self.db, self.repo, "abc", "ci/unit", "success")
        sc.report(self.db, self.repo, "abc", "ci/lint", "success")
        self.assertEqual(sc.combined(self.db, self.repo, "abc")["state"], "success")

    def test_any_failure_makes_the_rollup_fail(self):
        sc.report(self.db, self.repo, "abc", "ci/unit", "success")
        sc.report(self.db, self.repo, "abc", "ci/lint", "failure")
        self.assertEqual(sc.combined(self.db, self.repo, "abc")["state"], "failure")

    def test_an_error_counts_as_failing(self):
        sc.report(self.db, self.repo, "abc", "ci/unit", "error")
        self.assertEqual(sc.combined(self.db, self.repo, "abc")["state"], "failure")

    def test_pending_keeps_the_rollup_pending(self):
        """Never report success while a check is still running."""
        sc.report(self.db, self.repo, "abc", "ci/unit", "success")
        sc.report(self.db, self.repo, "abc", "ci/slow", "pending")
        self.assertEqual(sc.combined(self.db, self.repo, "abc")["state"], "pending")

    def test_failure_beats_pending(self):
        sc.report(self.db, self.repo, "abc", "ci/unit", "failure")
        sc.report(self.db, self.repo, "abc", "ci/slow", "pending")
        self.assertEqual(sc.combined(self.db, self.repo, "abc")["state"], "failure")


class GateTests(StatusTestBase):
    def test_no_required_checks_never_blocks(self):
        sc.report(self.db, self.repo, "abc", "ci/unit", "failure")
        self.assertEqual(sc.merge_blockers(self.db, self.repo, "abc"), [])

    def test_a_failing_required_check_blocks(self):
        self.set_required(["ci/unit"])
        sc.report(self.db, self.repo, "abc", "ci/unit", "failure")
        blockers = sc.merge_blockers(self.db, self.repo, "abc")
        self.assertTrue(any("failing" in b for b in blockers))

    def test_a_passing_required_check_clears(self):
        self.set_required(["ci/unit"])
        sc.report(self.db, self.repo, "abc", "ci/unit", "success")
        self.assertEqual(sc.merge_blockers(self.db, self.repo, "abc"), [])

    def test_a_required_check_that_never_reported_blocks(self):
        """Silence is not success — this is the case the gate exists to catch."""
        self.set_required(["ci/unit"])
        blockers = sc.merge_blockers(self.db, self.repo, "abc")
        self.assertTrue(any("not reported" in b for b in blockers))

    def test_a_still_running_required_check_blocks(self):
        self.set_required(["ci/unit"])
        sc.report(self.db, self.repo, "abc", "ci/unit", "pending")
        blockers = sc.merge_blockers(self.db, self.repo, "abc")
        self.assertTrue(any("still running" in b for b in blockers))

    def test_an_unrequired_failure_does_not_block(self):
        self.set_required(["ci/unit"])
        sc.report(self.db, self.repo, "abc", "ci/unit", "success")
        sc.report(self.db, self.repo, "abc", "optional/spellcheck", "failure")
        self.assertEqual(sc.merge_blockers(self.db, self.repo, "abc"), [])

    def test_a_later_green_run_unblocks_a_red_one(self):
        self.set_required(["ci/unit"])
        sc.report(self.db, self.repo, "abc", "ci/unit", "failure")
        self.assertTrue(sc.merge_blockers(self.db, self.repo, "abc"))
        sc.report(self.db, self.repo, "abc", "ci/unit", "success")
        self.assertEqual(sc.merge_blockers(self.db, self.repo, "abc"), [])

    def test_no_commit_to_check_is_reported_not_ignored(self):
        self.set_required(["ci/unit"])
        blockers = sc.merge_blockers(self.db, self.repo, None)
        self.assertTrue(blockers)

    def test_required_checks_read_from_the_branch_policy(self):
        self.set_required(["ci/unit", "security/scan"])
        self.assertEqual(sc.required_checks(self.repo), ["ci/unit", "security/scan"])

    def test_required_checks_default_to_empty(self):
        self.assertEqual(sc.required_checks(self.repo), [])

    def test_required_satisfied_flag_tracks_the_gate(self):
        self.set_required(["ci/unit"])
        self.assertFalse(sc.combined(self.db, self.repo, "abc")["required_satisfied"])
        sc.report(self.db, self.repo, "abc", "ci/unit", "success")
        self.assertTrue(sc.combined(self.db, self.repo, "abc")["required_satisfied"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
