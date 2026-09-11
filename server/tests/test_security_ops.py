"""Tests for login rate limiting, pagination, schema verification and backup."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from database.database import Base  # noqa: E402
from database.crud import UserCRUD  # noqa: E402
from database.models import AccountLock, LoginAttempt, User  # noqa: E402
from app.services import rate_limit as rl  # noqa: E402
from app.core.pagination import paginate_list, paginate_query, resolve_page  # noqa: E402


class DbTestBase(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Login rate limiting
# ---------------------------------------------------------------------------

class RateLimitTests(DbTestBase):
    def fail_n(self, username, n, ip="10.0.0.1"):
        for _ in range(n):
            rl.record_failure(self.db, username, ip)

    def test_a_fresh_account_is_not_limited(self):
        rl.check(self.db, "alice", "10.0.0.1")  # must not raise

    def test_lockout_after_the_threshold(self):
        self.fail_n("alice", rl.MAX_ACCOUNT_FAILURES)
        with self.assertRaises(rl.RateLimited):
            rl.check(self.db, "alice", "10.0.0.1")

    def test_one_below_the_threshold_still_allowed(self):
        self.fail_n("alice", rl.MAX_ACCOUNT_FAILURES - 1)
        rl.check(self.db, "alice", "10.0.0.1")  # must not raise

    def test_lockout_reports_a_wait(self):
        self.fail_n("alice", rl.MAX_ACCOUNT_FAILURES)
        with self.assertRaises(rl.RateLimited) as ctx:
            rl.check(self.db, "alice", "10.0.0.1")
        self.assertGreater(ctx.exception.retry_after_seconds, 0)

    def test_a_nonexistent_username_locks_the_same_way(self):
        """Otherwise the lockout tells an attacker which usernames are real."""
        self.fail_n("no-such-person", rl.MAX_ACCOUNT_FAILURES)
        with self.assertRaises(rl.RateLimited):
            rl.check(self.db, "no-such-person", "10.0.0.1")

    def test_usernames_are_case_insensitive(self):
        self.fail_n("Alice", rl.MAX_ACCOUNT_FAILURES)
        with self.assertRaises(rl.RateLimited):
            rl.check(self.db, "alice", "10.0.0.1")

    def test_locking_one_account_does_not_lock_another(self):
        self.fail_n("alice", rl.MAX_ACCOUNT_FAILURES)
        rl.check(self.db, "bob", "10.0.0.1")  # must not raise

    def test_success_clears_the_failure_history(self):
        self.fail_n("alice", rl.MAX_ACCOUNT_FAILURES - 1)
        rl.record_success(self.db, "alice", "10.0.0.1")
        self.fail_n("alice", rl.MAX_ACCOUNT_FAILURES - 1)
        rl.check(self.db, "alice", "10.0.0.1")  # still under, because it reset

    def test_success_removes_an_existing_lock(self):
        self.fail_n("alice", rl.MAX_ACCOUNT_FAILURES)
        self.assertIsNotNone(rl.active_lock(self.db, "alice"))
        rl.record_success(self.db, "alice", "10.0.0.1")
        self.assertIsNone(rl.active_lock(self.db, "alice"))

    def test_an_expired_lock_clears_itself(self):
        self.fail_n("alice", rl.MAX_ACCOUNT_FAILURES)
        lock = self.db.query(AccountLock).filter(AccountLock.username == "alice").first()
        lock.locked_until = datetime.utcnow() - timedelta(seconds=1)
        self.db.commit()
        self.assertIsNone(rl.active_lock(self.db, "alice"))
        rl.check(self.db, "alice", "10.0.0.1")  # must not raise

    def test_ip_throttle_catches_spraying_across_accounts(self):
        """One guess each against many accounts never trips a per-account limit."""
        for i in range(rl.MAX_IP_FAILURES):
            rl.record_failure(self.db, f"victim{i}", "10.0.0.9")
        with self.assertRaises(rl.RateLimited):
            rl.check(self.db, "someone-else", "10.0.0.9")

    def test_ip_throttle_is_scoped_to_that_address(self):
        for i in range(rl.MAX_IP_FAILURES):
            rl.record_failure(self.db, f"victim{i}", "10.0.0.9")
        rl.check(self.db, "someone-else", "10.0.0.10")  # a different host is fine

    def test_admin_unlock_clears_lock_and_history(self):
        self.fail_n("alice", rl.MAX_ACCOUNT_FAILURES)
        self.assertTrue(rl.unlock(self.db, "alice"))
        rl.check(self.db, "alice", "10.0.0.1")
        self.assertEqual(rl.unlock(self.db, "alice"), False)  # already clear

    def test_old_attempts_are_pruned(self):
        rl.record_failure(self.db, "alice", "10.0.0.1")
        row = self.db.query(LoginAttempt).first()
        row.created_at = datetime.utcnow() - timedelta(hours=rl.RETENTION_HOURS + 1)
        self.db.commit()
        rl.prune(self.db)
        self.assertEqual(self.db.query(LoginAttempt).count(), 0)

    def test_status_reports_locks_and_policy(self):
        self.fail_n("alice", rl.MAX_ACCOUNT_FAILURES)
        report = rl.status(self.db)
        self.assertIn("alice", [x["username"] for x in report["locked_accounts"]])
        self.assertEqual(report["policy"]["max_account_failures"], rl.MAX_ACCOUNT_FAILURES)

    def test_forwarded_header_is_ignored_unless_proxy_is_trusted(self):
        """Trusting it unconditionally lets any caller spoof past the IP limit."""
        class FakeRequest:
            headers = {"x-forwarded-for": "1.2.3.4"}
            class client:
                host = "10.0.0.1"

        os.environ.pop("FOXNEST_TRUST_PROXY", None)
        self.assertEqual(rl.client_ip(FakeRequest()), "10.0.0.1")
        os.environ["FOXNEST_TRUST_PROXY"] = "1"
        try:
            self.assertEqual(rl.client_ip(FakeRequest()), "1.2.3.4")
        finally:
            os.environ.pop("FOXNEST_TRUST_PROXY", None)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class PaginationTests(DbTestBase):
    def test_default_limit_applied_when_none_given(self):
        size, start = resolve_page(None, None)
        self.assertEqual(size, 200)
        self.assertEqual(start, 0)

    def test_limit_is_capped(self):
        size, _ = resolve_page(999999, 0)
        self.assertEqual(size, 1000)

    def test_negative_and_garbage_limits_are_safe(self):
        self.assertEqual(resolve_page(-5, None)[0], 1)
        self.assertEqual(resolve_page("nonsense", None)[0], 200)
        self.assertEqual(resolve_page(None, -10)[1], 0)

    def test_list_paging_slices_and_reports(self):
        page, meta = paginate_list(list(range(10)), 3, 0)
        self.assertEqual(page, [0, 1, 2])
        self.assertEqual(meta["total"], 10)
        self.assertTrue(meta["has_more"])
        self.assertEqual(meta["next_offset"], 3)

    def test_list_paging_last_page_has_no_more(self):
        _, meta = paginate_list(list(range(10)), 5, 5)
        self.assertFalse(meta["has_more"])
        self.assertIsNone(meta["next_offset"])

    def test_pages_do_not_overlap(self):
        a, _ = paginate_list(list(range(10)), 4, 0)
        b, _ = paginate_list(list(range(10)), 4, 4)
        self.assertEqual(set(a) & set(b), set())

    def test_offset_past_the_end_returns_empty_not_error(self):
        page, meta = paginate_list(list(range(3)), 10, 99)
        self.assertEqual(page, [])
        self.assertEqual(meta["total"], 3)
        self.assertFalse(meta["has_more"])

    def test_query_paging_counts_the_whole_set(self):
        for i in range(7):
            UserCRUD.create_user(self.db, username=f"u{i}", email=f"u{i}@e.com")
        rows, meta = paginate_query(self.db.query(User).order_by(User.id), 3, 0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(meta["total"], 7)
        self.assertTrue(meta["has_more"])


# ---------------------------------------------------------------------------
# Schema verification
# ---------------------------------------------------------------------------

class SchemaCheckTests(unittest.TestCase):
    def test_models_are_registered(self):
        """A check over empty metadata would pass against any database."""
        from database.database import Base as B
        import database.models  # noqa: F401
        self.assertGreater(len(B.metadata.tables), 30)

    def test_a_complete_database_is_healthy(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)
            from sqlalchemy import inspect as sa_inspect
            live = set(sa_inspect(engine).get_table_names())
            self.assertFalse(set(Base.metadata.tables) - live)
            engine.dispose()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_a_missing_table_is_detected(self):
        import sqlite3
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            engine = create_engine(f"sqlite:///{path}")
            Base.metadata.create_all(engine)
            engine.dispose()

            con = sqlite3.connect(path)
            con.execute("DROP TABLE account_locks")
            con.commit()
            con.close()

            engine = create_engine(f"sqlite:///{path}")
            from sqlalchemy import inspect as sa_inspect
            live = set(sa_inspect(engine).get_table_names())
            self.assertIn("account_locks", set(Base.metadata.tables) - live)
            engine.dispose()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
