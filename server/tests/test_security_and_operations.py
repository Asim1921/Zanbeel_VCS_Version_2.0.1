"""Security and operations: sign-in limits, versioned migrations, verified backups.

Each test pins a gap that was open:

* sign-in limits were checked before an attempt was recorded, so a burst of concurrent
  guesses all passed; one address could lock any account for everyone; an unknown
  username answered faster than a wrong password; "inactive" and "password not set"
  were said before the password was checked; reset codes could be guessed across
  codes without limit; the attempt log was pruned only when an admin looked;
* schema changes were twenty-five startup patches that printed a warning and carried
  on; Alembic was configured but never ran; nothing recorded what was applied;
* "verified" backups were never read back: a corrupt blob, a changed snapshot, a
  missing avatar all passed; every backup was a full copy; a restore left the old
  write-ahead log beside the restored database; no restore had ever been tried.
"""
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

import importlib.util  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

SERVER_FILE = os.path.join(SERVER_ROOT, 'server.py')
_spec = importlib.util.spec_from_file_location('foxnest_server_secops', SERVER_FILE)
server_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_module)

from database.database import Base  # noqa: E402
from database.crud import BranchCRUD, CommitCRUD, RepositoryCRUD, UserCRUD  # noqa: E402
from database.models import (  # noqa: E402
    AccountLock, Activity, BackupRecord, Branch, FileObject, LoginAttempt, Notification,
    SchemaMigrationRun, User,
)
from app.api.routes import auth as auth_routes  # noqa: E402
from app.db import migrations as legacy, schema_check, schema_ops, upgrade  # noqa: E402
from app.services import backup, blob_store, gc, maintenance  # noqa: E402
from app.services import rate_limit as rl  # noqa: E402

PASSWORD = "Password123!"


def b64(data):
    data = data.encode("utf-8") if isinstance(data, str) else data
    return base64.b64encode(data).decode("ascii")


def sqlite_url(path):
    return "sqlite:///" + Path(path).as_posix()


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="foxnest_secops_"))
        self.addCleanup(self._rm, self.tmp)
        env = mock.patch.dict(os.environ, {"FOXNEST_BACKUP_DIR": str(self.tmp / "backups")})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def _rm(path):
        for _ in range(10):
            shutil.rmtree(path, ignore_errors=True)
            if not Path(path).exists():
                return
            time.sleep(0.2)


def migrated_db(path):
    """A database at head, built the way the server builds one."""
    upgrade.upgrade_database(sqlite_url(path), snapshot=False, log=lambda m: None)
    engine = create_engine(sqlite_url(path))
    return engine, sessionmaker(bind=engine, autocommit=False, autoflush=False)


# =====================================================================================
# Sign-in limits
# =====================================================================================

class SignInLimitTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.engine, Session = migrated_db(self.tmp / "rl.db")
        self.db = Session()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        for name, value in (("MAX_PAIR_FAILURES", 3), ("MAX_ACCOUNT_FAILURES", 6),
                            ("MAX_IP_FAILURES", 8), ("LOCKOUT_MINUTES", 10),
                            ("MAX_OTP_FAILURES", 4), ("MAX_RESET_REQUESTS", 3)):
            patcher = mock.patch.object(rl, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fail(self, username, ip, n=1):
        for _ in range(n):
            attempt = rl.begin_attempt(self.db, username, ip)
            rl.fail(self.db, attempt, username, ip)

    def test_attempts_in_flight_count_against_the_limit(self):
        """The old check ran before recording, so concurrent guesses all passed it."""
        for _ in range(rl.MAX_PAIR_FAILURES):
            rl.begin_attempt(self.db, "alice", "10.0.0.1")  # admitted, not yet decided
        with self.assertRaises(rl.RateLimited):
            rl.begin_attempt(self.db, "alice", "10.0.0.1")

    def test_concurrent_attempts_never_exceed_the_limit(self):
        """Twenty threads guessing at once: at most the limit reach the password check."""
        Session = sessionmaker(bind=self.engine)
        admitted, refused, errors = [], [], []
        barrier = threading.Barrier(20)

        def guess():
            db = Session()
            try:
                barrier.wait()
                admitted.append(rl.begin_attempt(db, "bob", "10.0.0.2"))
            except rl.RateLimited:
                refused.append(1)
            except Exception as exc:  # database busy and the like
                errors.append(exc)
            finally:
                db.close()

        threads = [threading.Thread(target=guess) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors, errors)
        self.assertLessEqual(len(admitted), rl.MAX_PAIR_FAILURES)
        self.assertEqual(len(admitted) + len(refused), 20)

    def test_one_address_cannot_lock_the_owner_out_from_theirs(self):
        self.fail("carol", "6.6.6.6", rl.MAX_PAIR_FAILURES)
        with self.assertRaises(rl.RateLimited) as ctx:
            rl.begin_attempt(self.db, "carol", "6.6.6.6")
        self.assertEqual(ctx.exception.scope, "pair")
        rl.begin_attempt(self.db, "carol", "10.0.0.3")  # the owner, elsewhere: allowed

    def test_guessing_from_many_addresses_locks_the_account(self):
        for i in range(rl.MAX_ACCOUNT_FAILURES):
            self.fail("dave", f"7.7.7.{i}")
        self.assertIsNotNone(rl.active_lock(self.db, "dave"))
        with self.assertRaises(rl.RateLimited) as ctx:
            rl.begin_attempt(self.db, "dave", "10.9.9.9")
        self.assertEqual(ctx.exception.scope, "locked")
        self.assertGreater(ctx.exception.retry_after_seconds, 9 * 60)

    def test_repeated_locks_get_longer(self):
        for i in range(rl.MAX_ACCOUNT_FAILURES):
            self.fail("erin", f"8.8.8.{i}")
        first = rl.active_lock(self.db, "erin")
        first_minutes = (first.locked_until - datetime.utcnow()).total_seconds() / 60
        # The lock lapses; the guessing carries on.
        first.locked_until = datetime.utcnow() - timedelta(seconds=1)
        self.db.commit()
        self.db.query(LoginAttempt).delete()
        self.db.commit()
        for i in range(rl.MAX_ACCOUNT_FAILURES):
            self.fail("erin", f"9.9.9.{i}")
        second = rl.active_lock(self.db, "erin")
        self.assertEqual(second.lock_count, 2)
        second_minutes = (second.locked_until - datetime.utcnow()).total_seconds() / 60
        self.assertGreater(second_minutes, first_minutes * 1.8)

    def test_a_success_resets_failures_and_escalation(self):
        self.fail("frank", "10.0.0.4", rl.MAX_PAIR_FAILURES - 1)
        attempt = rl.begin_attempt(self.db, "frank", "10.0.0.4")
        rl.succeed(self.db, attempt, "frank")
        self.fail("frank", "10.0.0.4", rl.MAX_PAIR_FAILURES - 1)
        rl.begin_attempt(self.db, "frank", "10.0.0.4")  # still under: it reset

    def test_an_unknown_username_locks_exactly_like_a_real_one(self):
        for i in range(rl.MAX_ACCOUNT_FAILURES):
            self.fail("nobody-at-all", f"5.5.5.{i}")
        with self.assertRaises(rl.RateLimited):
            rl.begin_attempt(self.db, "NOBODY-AT-ALL", "1.1.1.1")

    def test_one_address_spraying_many_accounts_is_throttled(self):
        for i in range(rl.MAX_IP_FAILURES):
            self.fail(f"victim{i}", "4.4.4.4")
        with self.assertRaises(rl.RateLimited) as ctx:
            rl.begin_attempt(self.db, "someone-new", "4.4.4.4")
        self.assertEqual(ctx.exception.scope, "address")
        rl.begin_attempt(self.db, "someone-new", "4.4.4.5")

    def test_retry_after_is_the_real_wait(self):
        self.fail("gina", "10.0.0.5", rl.MAX_PAIR_FAILURES)
        # Age every failure by ten minutes of a fifteen-minute window.
        self.db.query(LoginAttempt).update(
            {LoginAttempt.created_at: datetime.utcnow() - timedelta(minutes=10)})
        self.db.commit()
        with self.assertRaises(rl.RateLimited) as ctx:
            rl.begin_attempt(self.db, "gina", "10.0.0.5")
        self.assertTrue(4 * 60 < ctx.exception.retry_after_seconds <= 5 * 60 + 2,
                        ctx.exception.retry_after_seconds)
        self.assertIn("Try again in", ctx.exception.message)

    def test_reset_codes_are_limited_per_account_across_codes_without_locking_sign_in(self):
        for _ in range(rl.MAX_OTP_FAILURES):
            rl.begin_attempt(self.db, "hank@example.com", f"3.3.3.{_}", kind=rl.KIND_OTP)
        with self.assertRaises(rl.RateLimited) as ctx:
            rl.begin_attempt(self.db, "hank@example.com", "3.3.3.99", kind=rl.KIND_OTP)
        self.assertEqual(ctx.exception.scope, "otp")
        rl.begin_attempt(self.db, "hank", "3.3.3.99")  # password sign-in is untouched

    def test_reset_requests_are_limited_per_address(self):
        for i in range(rl.MAX_RESET_REQUESTS):
            rl.begin_attempt(self.db, f"user{i}@example.com", "2.2.2.2", kind=rl.KIND_RESET_REQUEST)
        with self.assertRaises(rl.RateLimited) as ctx:
            rl.begin_attempt(self.db, "another@example.com", "2.2.2.2", kind=rl.KIND_RESET_REQUEST)
        self.assertEqual(ctx.exception.scope, "reset_request")

    def test_the_sign_in_path_prunes_old_attempts(self):
        """It used to prune only when an administrator opened the lockout view."""
        self.fail("ivan", "10.0.0.6")
        self.db.query(LoginAttempt).update(
            {LoginAttempt.created_at: datetime.utcnow() - timedelta(hours=rl.RETENTION_HOURS + 1)})
        self.db.commit()
        with mock.patch.object(rl, "_last_prune", 0.0):
            rl.begin_attempt(self.db, "judy", "10.0.0.7")
        self.assertEqual(self.db.query(LoginAttempt).filter(LoginAttempt.username == "ivan").count(), 0)

    def test_status_shows_locks_throttled_pairs_and_addresses(self):
        self.fail("kim", "1.2.3.4", rl.MAX_PAIR_FAILURES)
        for i in range(rl.MAX_IP_FAILURES - rl.MAX_PAIR_FAILURES):
            self.fail(f"spray{i}", "1.2.3.4")
        report = rl.status(self.db)
        self.assertIn({"username": "kim", "ip_address": "1.2.3.4", "failures": rl.MAX_PAIR_FAILURES},
                      report["throttled_pairs"])
        self.assertIn("1.2.3.4", [a["ip_address"] for a in report["throttled_addresses"]])
        self.assertEqual(report["policy"]["max_failures_per_address"], rl.MAX_PAIR_FAILURES)

    def test_unlock_by_address_clears_its_failures(self):
        for i in range(rl.MAX_IP_FAILURES):
            self.fail(f"v{i}", "9.8.7.6")
        result = rl.unlock(self.db, ip="9.8.7.6")
        self.assertEqual(result["cleared_failures"], rl.MAX_IP_FAILURES)
        rl.begin_attempt(self.db, "anyone", "9.8.7.6")


# =====================================================================================
# The login route
# =====================================================================================

class LoginRouteTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.engine, self.Session = migrated_db(self.tmp / "login.db")
        self.addCleanup(self.engine.dispose)
        db = self.Session()
        password_hash = server_module.hash_password(PASSWORD)
        UserCRUD.create_user(db, username="alice", email="alice@example.com", password_hash=password_hash)
        inactive = UserCRUD.create_user(db, username="bob", email="bob@example.com", password_hash=password_hash)
        inactive.is_active = False
        UserCRUD.create_user(db, username="carol", email="carol@example.com")  # no password
        db.commit()
        db.close()

        def override():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        startup = list(server_module.app.router.on_startup)
        server_module.app.router.on_startup = []
        server_module.app.dependency_overrides[server_module.get_db] = override
        self.client = TestClient(server_module.app)

        def restore():
            server_module.app.dependency_overrides.clear()
            server_module.app.router.on_startup = startup
            self.client.close()
        self.addCleanup(restore)
        patcher = mock.patch.object(rl, "_last_prune", time.monotonic())
        patcher.start()
        self.addCleanup(patcher.stop)

    def login(self, username, password, ip=None):
        headers = {"X-Forwarded-For": ip} if ip else {}
        return self.client.post("/api/auth/login", json={"username": username, "password": password},
                                headers=headers)

    def test_every_failure_costs_the_same_password_hashing(self):
        """An unknown name or a missing password used to answer without any PBKDF2."""
        calls = []
        real = auth_routes.verify_password

        def counting(password, stored):
            calls.append(stored)
            return real(password, stored)

        with mock.patch.object(auth_routes, "verify_password", counting):
            unknown = self.login("no-such-user", "whatever1")
            wrong = self.login("alice", "wrong-password")
            unset = self.login("carol", "whatever1")
        self.assertEqual([r.status_code for r in (unknown, wrong, unset)], [401, 401, 401])
        self.assertEqual(len({r.json()["detail"] for r in (unknown, wrong, unset)}), 1)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(c and c.startswith("pbkdf2_sha256$") for c in calls))

    def test_inactive_is_said_only_to_someone_with_the_password(self):
        self.assertEqual(self.login("bob", "wrong-password").status_code, 401)
        right = self.login("bob", PASSWORD)
        self.assertEqual(right.status_code, 403)
        self.assertIn("inactive", right.json()["detail"])

    def test_a_limited_sign_in_answers_429_with_the_wait(self):
        with mock.patch.object(rl, "MAX_PAIR_FAILURES", 2):
            for _ in range(2):
                self.assertEqual(self.login("alice", "nope-nope").status_code, 401)
            limited = self.login("alice", PASSWORD)
        self.assertEqual(limited.status_code, 429)
        self.assertGreater(int(limited.headers["Retry-After"]), 0)
        self.assertIn("Try again in", limited.json()["detail"])

    def test_a_lock_tells_the_owner_and_the_activity_log(self):
        with mock.patch.object(rl, "MAX_ACCOUNT_FAILURES", 3), mock.patch.object(rl, "MAX_PAIR_FAILURES", 9):
            for _ in range(3):
                self.login("alice", "nope-nope")
            for _ in range(3):
                self.login("ghost-user", "nope-nope")
        db = self.Session()
        try:
            alice = db.query(User).filter(User.username == "alice").first()
            note = db.query(Notification).filter(Notification.user_id == alice.id,
                                                 Notification.notification_type == "account_locked").first()
            self.assertIsNotNone(note)
            self.assertIn("locked", note.title)
            rows = db.query(Activity).filter(Activity.activity_type == "account_locked").all()
            self.assertEqual([r.user_id for r in rows], [alice.id])  # nothing for the ghost
            self.assertIsNotNone(rl.active_lock(db, "ghost-user"))
        finally:
            db.close()

    def test_a_correct_password_clears_earlier_failures(self):
        self.login("alice", "nope-nope")
        self.assertEqual(self.login("alice", PASSWORD).status_code, 200)
        db = self.Session()
        try:
            self.assertEqual(db.query(LoginAttempt).filter(LoginAttempt.success.is_(False)).count(), 0)
        finally:
            db.close()

    def test_forgot_password_is_limited_per_address_and_says_the_same_thing_otherwise(self):
        with mock.patch.object(rl, "MAX_RESET_REQUESTS", 2):
            replies = [self.client.post("/api/auth/forgot-password", json={"email": e})
                       for e in ("alice@example.com", "nobody@example.com")]
            limited = self.client.post("/api/auth/forgot-password", json={"email": "alice@example.com"})
        self.assertEqual([r.status_code for r in replies], [200, 200])
        self.assertEqual(replies[0].json()["message"], replies[1].json()["message"])
        self.assertEqual(limited.status_code, 429)

    def test_wrong_reset_codes_are_limited_across_codes(self):
        with mock.patch.object(rl, "MAX_OTP_FAILURES", 2):
            for _ in range(2):
                wrong = self.client.post("/api/auth/reset-password", json={
                    "email": "alice@example.com", "otp": "000000", "new_password": "NewPassw0rd!"})
                self.assertEqual(wrong.status_code, 400)
            limited = self.client.post("/api/auth/reset-password", json={
                "email": "alice@example.com", "otp": "000000", "new_password": "NewPassw0rd!"})
        self.assertEqual(limited.status_code, 429)

    def test_a_locked_account_can_still_reset_and_the_reset_unlocks_it(self):
        from app.services import password_reset
        db = self.Session()
        rl._lock(db, "alice", 20, "1.1.1.1", None)
        db.close()
        with mock.patch.object(password_reset, "verify_and_consume", return_value=True):
            done = self.client.post("/api/auth/reset-password", json={
                "email": "alice@example.com", "otp": "123456", "new_password": "NewPassw0rd!"})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(self.login("alice", "NewPassw0rd!").status_code, 200)


# =====================================================================================
# Migrations
# =====================================================================================

class MigrationTests(TempDirTest):
    def url(self, name):
        return sqlite_url(self.tmp / name)

    def tables(self, name):
        con = sqlite3.connect(self.tmp / name)
        try:
            return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()

    def revision(self, name):
        con = sqlite3.connect(self.tmp / name)
        try:
            return con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        finally:
            con.close()

    def healthy(self, name):
        engine = create_engine(self.url(name))
        try:
            return schema_check.inspect_schema(engine)
        finally:
            engine.dispose()

    def test_a_new_database_is_built_to_head_with_every_declared_index(self):
        report = upgrade.upgrade_database(self.url("new.db"), log=lambda m: None)
        self.assertEqual(report["applied"], ["0001_baseline", "0002_startup_patches",
                                             "0003_security_and_operations"])
        self.assertIsNone(report["snapshot"])  # nothing to protect yet
        health = self.healthy("new.db")
        self.assertTrue(health["healthy"], health)
        self.assertEqual(health["missing_indexes"], [])

    def test_a_second_upgrade_does_nothing(self):
        upgrade.upgrade_database(self.url("twice.db"), log=lambda m: None)
        self.assertEqual(upgrade.upgrade_database(self.url("twice.db"), log=lambda m: None)["applied"], [])

    def test_a_database_from_before_alembic_is_upgraded_with_its_data(self):
        """No alembic_version, tables in their 2.0.1 shapes, rows in them."""
        path = self.tmp / "old.db"
        engine = create_engine(sqlite_url(path))
        old_tables = {"login_attempts", "account_locks", "backup_records", "ssh_keys", "access_tokens"}
        Base.metadata.create_all(engine, tables=[t for n, t in Base.metadata.tables.items()
                                                 if n not in old_tables and n != "schema_migration_runs"])
        with mock.patch.object(legacy, "engine", engine):
            legacy.ensure_security_tables()
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE ssh_keys (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title VARCHAR(100) "
                "NOT NULL, key_type VARCHAR(30) NOT NULL, public_key TEXT NOT NULL, fingerprint VARCHAR(80) "
                "NOT NULL UNIQUE, last_used_at TIMESTAMP, created_at TIMESTAMP)"))
            conn.execute(text(
                "CREATE TABLE access_tokens (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name VARCHAR(100) "
                "NOT NULL, prefix VARCHAR(16) NOT NULL, token_hash VARCHAR(64) NOT NULL UNIQUE, scopes "
                "VARCHAR(200) NOT NULL DEFAULT 'repo:read', expires_at TIMESTAMP, last_used_at TIMESTAMP, "
                "revoked_at TIMESTAMP, created_at TIMESTAMP)"))
            conn.execute(text("INSERT INTO backup_records (label, path, verified) VALUES ('old', '/x', 1)"))
            conn.execute(text("INSERT INTO account_locks (username, locked_until) VALUES ('old', '2020-01-01')"))
            conn.execute(text("INSERT INTO ssh_keys (user_id, title, key_type, public_key, fingerprint) "
                              "VALUES (1, 'k', 'ssh-ed25519', 'ssh-ed25519 AAAA', 'SHA256:k')"))
        engine.dispose()

        report = upgrade.upgrade_database(sqlite_url(path), log=lambda m: None)
        self.assertEqual(report["from"], None)
        self.assertTrue(report["snapshot"] and Path(report["snapshot"]).is_file())
        health = self.healthy("old.db")
        self.assertTrue(health["healthy"], schema_check.describe(health))
        con = sqlite3.connect(path)
        try:
            self.assertEqual(con.execute("SELECT status, format, verified FROM backup_records").fetchone(),
                             ("complete", 1, 1))
            self.assertEqual(con.execute("SELECT lock_count FROM account_locks").fetchone(), (1,))
            self.assertEqual(con.execute("SELECT scopes FROM ssh_keys").fetchone(), ("repo:write",))
            self.assertEqual(con.execute("SELECT count(*) FROM schema_migration_runs").fetchone(), (1,))
        finally:
            con.close()

    def test_a_database_stamped_at_the_baseline_runs_the_rest(self):
        upgrade.upgrade_database(self.url("stamped.db"), log=lambda m: None)
        con = sqlite3.connect(self.tmp / "stamped.db")
        con.execute("UPDATE alembic_version SET version_num = '0001_baseline'")
        con.commit()
        con.close()
        report = upgrade.upgrade_database(self.url("stamped.db"), log=lambda m: None)
        self.assertEqual(report["applied"], ["0002_startup_patches", "0003_security_and_operations"])

    def test_a_failing_revision_leaves_the_database_exactly_as_it_was(self):
        def boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        with mock.patch.object(schema_ops, "create_missing_model_indexes", boom):
            with self.assertRaises(upgrade.MigrationError) as ctx:
                upgrade.upgrade_database(self.url("fail.db"), log=lambda m: None)
        self.assertIn("0003_security_and_operations", str(ctx.exception))
        self.assertEqual(self.tables("fail.db"), set())  # create_all in 0002 rolled back too

    def test_a_database_from_a_newer_server_is_refused(self):
        upgrade.upgrade_database(self.url("newer.db"), log=lambda m: None)
        con = sqlite3.connect(self.tmp / "newer.db")
        con.execute("UPDATE alembic_version SET version_num = '0099_from_the_future'")
        con.commit()
        con.close()
        with self.assertRaises(upgrade.MigrationError) as ctx:
            upgrade.upgrade_database(self.url("newer.db"), log=lambda m: None)
        self.assertIn("newer version", str(ctx.exception))
        health = self.healthy("newer.db")
        self.assertFalse(health["healthy"])
        self.assertIn("newer version", health["revision_problem"])

    def test_pending_revisions_are_drift_when_startup_migration_is_off(self):
        upgrade.upgrade_database(self.url("behind.db"), log=lambda m: None)
        con = sqlite3.connect(self.tmp / "behind.db")
        con.execute("UPDATE alembic_version SET version_num = '0002_startup_patches'")
        con.commit()
        con.close()
        engine = create_engine(self.url("behind.db"))
        try:
            with mock.patch.dict(os.environ, {"FOXNEST_AUTO_MIGRATE": "0"}):
                with self.assertRaises(schema_check.SchemaDrift):
                    schema_check.verify_or_fail(engine)
        finally:
            engine.dispose()

    def test_two_servers_starting_together_apply_each_revision_once(self):
        results, errors = [], []
        barrier = threading.Barrier(2)

        def run():
            try:
                barrier.wait()
                results.append(upgrade.upgrade_database(self.url("race.db"), log=lambda m: None))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors, errors)
        applied = sorted(rev for r in results for rev in r["applied"])
        self.assertEqual(applied, ["0001_baseline", "0002_startup_patches", "0003_security_and_operations"])

    def test_a_copy_is_taken_before_upgrading_a_database_with_data_and_copies_rotate(self):
        upgrade.upgrade_database(self.url("copies.db"), log=lambda m: None)
        with mock.patch.object(upgrade, "SNAPSHOTS_KEEP", 2):
            for _ in range(3):
                con = sqlite3.connect(self.tmp / "copies.db")
                con.execute("UPDATE alembic_version SET version_num = '0002_startup_patches'")
                con.commit()
                con.close()
                report = upgrade.upgrade_database(self.url("copies.db"), log=lambda m: None)
                self.assertTrue(Path(report["snapshot"]).is_file())
                time.sleep(1.1)  # snapshot names are per second
        root = upgrade.snapshot_root()
        self.assertEqual(len(list(root.glob("*.sqlite3"))), 2)
        sidecar = json.loads(Path(report["snapshot"]).with_suffix(".json").read_text())
        self.assertEqual(sidecar["from_revision"], "0002_startup_patches")

    def test_a_missing_unique_rule_is_fatal_and_a_missing_index_is_not(self):
        upgrade.upgrade_database(self.url("drift.db"), log=lambda m: None)
        con = sqlite3.connect(self.tmp / "drift.db")
        con.execute("DROP INDEX ix_immutable_releases_repo_name")
        con.execute("DROP INDEX ix_commits_repo_created")
        con.commit()
        con.close()
        health = self.healthy("drift.db")
        self.assertFalse(health["healthy"])
        self.assertEqual([u["table"] for u in health["missing_unique"]], ["immutable_releases"])
        self.assertIn("ix_commits_repo_created", [i["index"] for i in health["missing_indexes"]])

    def test_the_legacy_patches_raise_instead_of_printing(self):
        engine = create_engine(self.url("strict.db"))
        try:
            with mock.patch.object(legacy, "engine", engine):
                with self.assertRaises(Exception):
                    legacy.ensure_versioning_schema()  # no commits table to index
        finally:
            engine.dispose()

    def test_downgrading_the_newest_revision_and_upgrading_again(self):
        from alembic import command
        upgrade.upgrade_database(self.url("down.db"), log=lambda m: None)
        engine = upgrade.migration_engine(self.url("down.db"))
        try:
            with engine.connect() as conn, conn.begin():
                config = upgrade.alembic_config(self.url("down.db"))
                config.attributes["connection"] = conn
                command.downgrade(config, "0002_startup_patches")
        finally:
            engine.dispose()
        self.assertNotIn("schema_migration_runs", self.tables("down.db"))
        self.assertEqual(upgrade.upgrade_database(self.url("down.db"), log=lambda m: None)["applied"],
                         ["0003_security_and_operations"])
        self.assertTrue(self.healthy("down.db")["healthy"])


# =====================================================================================
# Backups
# =====================================================================================

class BackupTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.db_path = self.tmp / "live.db"
        self.engine, self.Session = migrated_db(self.db_path)
        self.addCleanup(self.engine.dispose)
        self.store = blob_store.BlobStore(self.tmp / "blobs")
        self.docs = self.tmp / "docs"
        (self.docs / "repo").mkdir(parents=True)
        (self.docs / "repo" / "README.md").write_text("# generated\n", encoding="utf-8")
        for patcher in (
            mock.patch.object(blob_store, "store", self.store),
            mock.patch.object(backup, "DOCS_DIR", self.docs),
            mock.patch.object(backup, "session_factory", self.Session),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.db = self.Session()
        self.addCleanup(self.db.close)
        self.seed()

    def seed(self):
        db = self.db
        UserCRUD.create_user(db, username="lead", email="lead@example.com", role="team_lead")
        repo = RepositoryCRUD.create_repository(db, "lead", "tool", "test")
        self.repo_id = repo.id
        files = {"app.py": "print('one')\n", "data.bin": bytes(range(256)) * 4}
        self.commit_id = hashlib.sha1(b"first").hexdigest()
        CommitCRUD.create_commit(db, {"id": self.commit_id, "repository_id": repo.id, "author": "lead",
                                      "message": "first", "files": {k: b64(v) for k, v in files.items()}})
        if BranchCRUD.get_branch(db, repo.id, "main"):
            BranchCRUD.update_branch_head(db, repo.id, "main", self.commit_id)
        else:
            BranchCRUD.create_branch(db, repo.id, "main", self.commit_id, is_default=True)
        # A profile picture: in the blob store with no file_objects row of its own.
        user = db.query(User).filter(User.username == "lead").first()
        user.avatar_blob_hash = self.store.put(b"\x89PNG avatar bytes")
        db.commit()

    def take(self, **kwargs):
        return backup.start_backup(self.db, wait=True, **kwargs)

    def test_a_backup_is_verified_by_reading_everything_back(self):
        record = self.take()
        self.assertEqual(record.status, "complete")
        self.assertTrue(record.verified, record.error)
        manifest = json.loads((Path(record.path) / "manifest.json").read_text())
        self.assertEqual(manifest["format"], 2)
        self.assertEqual(manifest["database"]["integrity"], "ok")
        self.assertEqual(manifest["database"]["sha256"], record.database_sha256)
        self.assertEqual(manifest["schema_revision"], "0003_security_and_operations")
        self.assertEqual(manifest["docs"]["count"], 1)
        user = self.db.query(User).filter(User.username == "lead").first()
        self.assertTrue((Path(record.path) / "blobs" / user.avatar_blob_hash[:2] / user.avatar_blob_hash).is_file(),
                        "the old backup left profile pictures out")

    def test_the_next_backup_links_what_the_last_one_holds(self):
        first = self.take()
        second = self.take()
        self.assertEqual(second.linked_blobs, first.blob_count)
        any_blob = next(p for p in (Path(second.path) / "blobs").rglob("*") if p.is_file())
        self.assertGreaterEqual(os.stat(any_blob).st_nlink, 2)
        self.assertTrue(second.verified)

    def test_a_rotted_blob_in_the_last_backup_is_replaced_not_linked(self):
        first = self.take()
        victim = next(p for p in (Path(first.path) / "blobs").rglob("*") if p.is_file())
        victim.write_bytes(b"bit rot")
        second = self.take()
        self.assertTrue(second.verified, second.error)
        fresh = Path(second.path) / "blobs" / victim.parent.name / victim.name
        self.assertNotEqual(fresh.read_bytes(), b"bit rot")

    def test_a_corrupt_blob_in_the_store_is_not_called_verified(self):
        row = self.db.query(FileObject).first()
        self.store.path_for(row.hash).write_bytes(b"not what was stored")
        record = self.take()
        self.assertFalse(record.verified)
        self.assertIn("do not match their hash", record.error)

    def test_a_missing_blob_is_not_called_verified(self):
        row = self.db.query(FileObject).first()
        self.store.delete(row.hash)
        record = self.take()
        self.assertFalse(record.verified)
        self.assertEqual(record.missing_blobs, 1)

    def test_content_held_inline_by_old_rows_counts_as_present(self):
        content = b"stored before blobs moved to disk"
        digest = hashlib.sha1(content).hexdigest()
        self.db.add(FileObject(hash=digest, content=content, size=len(content)))
        self.db.commit()
        record = self.take()
        self.assertTrue(record.verified, record.error)
        self.assertEqual(backup.verify_backup(record.path)["inline"], 1)

    def test_reverification_catches_what_changed_on_disk(self):
        record = self.take()
        root = Path(record.path)
        self.assertTrue(backup.verify_backup(record.path)["verified"])
        blob = next(p for p in (root / "blobs").rglob("*") if p.is_file())
        original = blob.read_bytes()
        blob.write_bytes(b"flipped")
        report = backup.verify_backup(record.path)
        self.assertFalse(report["verified"])
        self.assertEqual(report["corrupt"], 1)
        self.assertTrue(backup.verify_backup(record.path, full=False)["verified"])  # presence only
        blob.write_bytes(original)
        (root / "docs" / "repo" / "README.md").write_text("edited", encoding="utf-8")
        self.assertIn("generated document", " ".join(backup.verify_backup(record.path)["problems"]))
        with open(root / "database.sqlite3", "ab") as handle:
            handle.write(b"\0")
        self.assertIn("has changed", " ".join(backup.verify_backup(record.path)["problems"]))

    def test_a_restore_moves_the_database_and_its_side_files_aside(self):
        record = self.take()
        target = self.tmp / "restore" / "foxnest.db"
        target.parent.mkdir()
        target.write_bytes(b"the database being replaced")
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(target) + suffix).write_bytes(b"stale " + suffix.encode())
        blobs = self.tmp / "restore" / "blobs"
        damaged = blob_store.BlobStore(blobs)
        row = self.db.query(FileObject).first()
        damaged.path_for(row.hash).parent.mkdir(parents=True)
        damaged.path_for(row.hash).write_bytes(b"damaged copy")

        result = backup.restore_backup(record.path, target_db=str(target), target_blobs=str(blobs),
                                       target_docs=str(self.tmp / "restore" / "docs"))
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(target) + suffix).exists(), f"stale {suffix} left beside the restore")
        self.assertIn("database-wal", result["moved_aside"])
        self.assertEqual(Path(result["previous_database_moved_to"]).read_bytes(), b"the database being replaced")
        self.assertEqual(result["replaced_damaged_blobs"], 1)
        self.assertEqual(damaged.get(row.hash) is not None, True)
        self.assertTrue((self.tmp / "restore" / "docs" / "repo" / "README.md").is_file())
        con = sqlite3.connect(target)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM commits").fetchone(), (1,))
        finally:
            con.close()

    def test_a_backup_that_does_not_verify_is_not_restored_unless_forced(self):
        record = self.take()
        blob = next(p for p in (Path(record.path) / "blobs").rglob("*") if p.is_file())
        blob.unlink()
        target = self.tmp / "refused.db"
        with self.assertRaises(backup.BackupError) as ctx:
            backup.restore_backup(record.path, target_db=str(target), target_blobs=str(self.tmp / "b2"),
                                  target_docs=str(self.tmp / "d2"))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertFalse(target.exists())

    def test_a_restore_says_when_the_signing_key_differs(self):
        record = self.take()
        with mock.patch.object(backup, "AUTH_SECRET", "z" * 64):
            result = backup.restore_backup(record.path, target_db=str(self.tmp / "k.db"),
                                           target_blobs=str(self.tmp / "kb"), target_docs=str(self.tmp / "kd"))
        self.assertFalse(result["signing_key_matches"])

    def test_the_restore_drill_proves_the_backup_and_cleans_up(self):
        record = self.take(then_test_restore=True)
        self.db.refresh(record)
        self.assertEqual(record.restore_test_status, "passed", record.restore_test_report)
        report = json.loads(record.restore_test_report)
        self.assertTrue(report["schema_healthy"])
        self.assertEqual(report["branches_checked"], 1)
        self.assertEqual(report["head_files_checked"], 2)
        self.assertFalse(any((backup.backup_root() / backup.SCRATCH_DIRNAME).glob("*")))

    def test_the_drill_fails_when_a_branch_file_does_not_read_back(self):
        record = self.take()
        commit_file = self.db.query(FileObject).first()
        path = Path(record.path) / "blobs" / commit_file.hash[:2] / commit_file.hash
        path.unlink()
        report = backup.test_restore(record.path)
        self.assertFalse(report["passed"])

    def test_the_drill_upgrades_an_older_backup_to_this_schema(self):
        record = self.take()
        old = self.tmp / "older"
        shutil.copytree(record.path, old)
        con = sqlite3.connect(old / "database.sqlite3")
        con.execute("UPDATE alembic_version SET version_num = '0002_startup_patches'")
        con.commit()
        con.close()
        manifest = json.loads((old / "manifest.json").read_text())
        manifest["database"]["sha256"] = backup._sha256(old / "database.sqlite3")
        (old / "manifest.json").write_text(json.dumps(manifest))
        report = backup.test_restore(str(old))
        self.assertTrue(report["passed"], report["problems"])
        self.assertEqual(report["upgraded_through"], ["0003_security_and_operations"])

    def test_a_backup_from_2_0_1_still_verifies_and_restores(self):
        record = self.take()
        v1 = self.tmp / "v1"
        shutil.copytree(record.path, v1, ignore=shutil.ignore_patterns("docs"))
        (v1 / "manifest.json").write_text(json.dumps({
            "created_at": "2026-09-25T06:22:56Z", "label": "manual", "database_file": "database.sqlite3",
            "blob_dir": "blobs", "verified": True}))
        self.assertTrue(backup.verify_backup(str(v1))["verified"])
        result = backup.restore_backup(str(v1), target_db=str(self.tmp / "v1.db"),
                                       target_blobs=str(self.tmp / "v1b"), target_docs=str(self.tmp / "v1d"))
        self.assertGreater(result["restored_blobs"], 0)

    def test_rotation_keeps_the_newest_verified_and_drops_the_rest(self):
        made = []
        for _ in range(4):
            made.append(self.take())
            time.sleep(1.1)  # backup directories are named per second
        broken = BackupRecord(label="broken", path=str(self.tmp / "nothing"), status="failed",
                              created_at=datetime.utcnow() - timedelta(days=1))
        self.db.add(broken)
        self.db.commit()
        pruned = backup.prune_backups(self.db, keep=2)
        self.assertEqual(sorted(pruned), sorted([made[0].id, made[1].id, broken.id]))
        for record in made[:2]:
            self.db.refresh(record)
            self.assertEqual(record.status, "pruned")
            self.assertFalse(Path(record.path).exists())
        for record in made[2:]:
            self.assertTrue(Path(record.path).exists())
        self.assertTrue(backup.verify_backup(made[3].path)["verified"], "linked blobs survived pruning")

    def test_one_maintenance_job_at_a_time(self):
        with maintenance.acquire("a test job"):
            with self.assertRaises(maintenance.MaintenanceBusy):
                backup.start_backup(self.db, wait=True)
            with self.assertRaises(maintenance.MaintenanceBusy):
                gc.collect(self.db, dry_run=False)
            gc.collect(self.db, dry_run=True)  # looking is always allowed
            self.assertEqual(maintenance.current()["job"], "a test job")
        self.assertIsNone(maintenance.current())

    def test_a_job_cut_off_by_a_shutdown_is_marked_interrupted(self):
        self.db.add(BackupRecord(label="cut", path="", status="running"))
        self.db.commit()
        self.assertEqual(backup.recover_interrupted(self.db), 1)
        row = self.db.query(BackupRecord).filter(BackupRecord.label == "cut").first()
        self.assertEqual(row.status, "interrupted")

    def test_the_schedule_takes_and_drills_a_backup_when_one_is_due(self):
        with mock.patch.dict(os.environ, {"FOXNEST_BACKUP_INTERVAL_HOURS": "24"}):
            self.assertTrue(backup.scheduled_backup_due(self.db))
            record_id = backup.run_schedule_once()
            self.assertIsNotNone(record_id)
            self.db.expire_all()
            row = self.db.query(BackupRecord).filter(BackupRecord.id == record_id).first()
            self.assertEqual((row.kind, row.verified, row.restore_test_status), ("scheduled", True, "passed"))
            self.assertFalse(backup.scheduled_backup_due(self.db))
            self.assertIsNone(backup.run_schedule_once())
        self.assertFalse(backup.scheduled_backup_due(self.db))  # interval 0: off

    def test_the_summary_flags_a_stale_or_missing_backup(self):
        self.assertTrue(backup.summary(self.db)["stale"])
        record = self.take()
        self.assertFalse(backup.summary(self.db)["stale"])
        record.created_at = datetime.utcnow() - timedelta(days=5)
        self.db.commit()
        self.assertTrue(backup.summary(self.db)["stale"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
