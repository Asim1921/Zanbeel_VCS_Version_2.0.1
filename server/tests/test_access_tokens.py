"""Tests for personal access tokens.

Builds its own temporary database rather than touching the live one.
"""
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
from app.services import access_tokens as ts  # noqa: E402


class TokenTestBase(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        self.dev = UserCRUD.create_user(
            self.db, username="dev", email="dev@example.com", role="developer"
        )
        self.lead = UserCRUD.create_user(
            self.db, username="lead", email="lead@example.com", role="team_lead"
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass


class ScopeTests(TokenTestBase):
    def test_default_scope_is_read_only(self):
        self.assertEqual(ts.normalise_scopes(None), [ts.SCOPE_REPO_READ])
        self.assertEqual(ts.normalise_scopes([]), [ts.SCOPE_REPO_READ])

    def test_unknown_scope_is_refused_by_name(self):
        with self.assertRaises(ValueError) as ctx:
            ts.normalise_scopes(["repo:delete-everything"])
        self.assertIn("repo:delete-everything", str(ctx.exception))

    def test_scopes_are_deduplicated_and_ranked(self):
        self.assertEqual(
            ts.normalise_scopes(["admin", "repo:read", "repo:read"]),
            [ts.SCOPE_REPO_READ, ts.SCOPE_ADMIN],
        )

    def test_write_implies_read(self):
        granted = ts.expand_scopes([ts.SCOPE_REPO_WRITE])
        self.assertIn(ts.SCOPE_REPO_READ, granted)

    def test_admin_implies_write_and_read(self):
        granted = ts.expand_scopes([ts.SCOPE_ADMIN])
        self.assertIn(ts.SCOPE_REPO_WRITE, granted)
        self.assertIn(ts.SCOPE_REPO_READ, granted)

    def test_read_does_not_imply_write(self):
        granted = ts.expand_scopes([ts.SCOPE_REPO_READ])
        self.assertNotIn(ts.SCOPE_REPO_WRITE, granted)


class CreationTests(TokenTestBase):
    def test_plaintext_is_returned_once_and_not_stored(self):
        token, plaintext = ts.create_token(self.db, self.dev, "laptop")
        self.assertTrue(plaintext.startswith(ts.TOKEN_PREFIX))
        # The secret itself must not appear anywhere on the row.
        self.assertNotIn(plaintext, token.token_hash)
        self.assertNotEqual(token.token_hash, plaintext)
        self.assertEqual(token.token_hash, ts.hash_token(plaintext))

    def test_prefix_is_not_enough_to_authenticate(self):
        token, plaintext = ts.create_token(self.db, self.dev, "laptop")
        self.assertTrue(plaintext.startswith(token.prefix))
        self.assertIsNone(ts.resolve_token(self.db, token.prefix))

    def test_two_tokens_never_collide(self):
        seen = set()
        for i in range(50):
            _, plaintext = ts.create_token(self.db, self.dev, f"t{i}")
            self.assertNotIn(plaintext, seen)
            seen.add(plaintext)

    def test_name_is_required(self):
        with self.assertRaises(ValueError):
            ts.create_token(self.db, self.dev, "   ")

    def test_developer_cannot_mint_admin_scope(self):
        """A token must never exceed the powers of whoever created it."""
        with self.assertRaises(ValueError) as ctx:
            ts.create_token(self.db, self.dev, "escalate", scopes=["admin"])
        self.assertIn("admin", str(ctx.exception).lower())

    def test_team_lead_may_mint_admin_scope(self):
        token, _ = ts.create_token(self.db, self.lead, "ci", scopes=["admin"])
        self.assertIn(ts.SCOPE_ADMIN, ts.scopes_of(token))


class ResolutionTests(TokenTestBase):
    def test_valid_token_resolves_to_its_row(self):
        token, plaintext = ts.create_token(self.db, self.dev, "laptop")
        resolved = ts.resolve_token(self.db, plaintext)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, token.id)

    def test_unknown_token_resolves_to_none(self):
        self.assertIsNone(ts.resolve_token(self.db, ts.TOKEN_PREFIX + "nope"))

    def test_session_token_shape_is_not_treated_as_a_token(self):
        """A session credential must never be looked up in the token table."""
        self.assertFalse(ts.looks_like_access_token("eyJhbGci.signature"))
        self.assertIsNone(ts.resolve_token(self.db, "eyJhbGci.signature"))

    def test_revoked_token_stops_resolving(self):
        token, plaintext = ts.create_token(self.db, self.dev, "laptop")
        self.assertIsNotNone(ts.resolve_token(self.db, plaintext))
        ts.revoke_token(self.db, token)
        self.assertIsNone(ts.resolve_token(self.db, plaintext))

    def test_expired_token_stops_resolving(self):
        token, plaintext = ts.create_token(
            self.db, self.dev, "short", expires_at=datetime.utcnow() - timedelta(seconds=1)
        )
        self.assertIsNone(ts.resolve_token(self.db, plaintext))

    def test_future_expiry_still_resolves(self):
        _, plaintext = ts.create_token(
            self.db, self.dev, "long", expires_at=datetime.utcnow() + timedelta(days=1)
        )
        self.assertIsNotNone(ts.resolve_token(self.db, plaintext))

    def test_revocation_is_idempotent_and_keeps_first_time(self):
        token, _ = ts.create_token(self.db, self.dev, "laptop")
        ts.revoke_token(self.db, token)
        first = token.revoked_at
        ts.revoke_token(self.db, token)
        self.assertEqual(token.revoked_at, first)

    def test_revoking_one_token_leaves_the_others_working(self):
        """The whole point: cut off one machine without cutting off the rest."""
        a, plain_a = ts.create_token(self.db, self.dev, "laptop")
        _, plain_b = ts.create_token(self.db, self.dev, "desktop")
        ts.revoke_token(self.db, a)
        self.assertIsNone(ts.resolve_token(self.db, plain_a))
        self.assertIsNotNone(ts.resolve_token(self.db, plain_b))

    def test_touch_records_usage(self):
        token, plaintext = ts.create_token(self.db, self.dev, "laptop")
        self.assertIsNone(token.last_used_at)
        ts.touch(self.db, token)
        self.assertIsNotNone(token.last_used_at)


class ListingAndSerialisationTests(TokenTestBase):
    def test_listing_hides_revoked_by_default(self):
        a, _ = ts.create_token(self.db, self.dev, "a")
        ts.create_token(self.db, self.dev, "b")
        ts.revoke_token(self.db, a)
        self.assertEqual(len(ts.list_tokens(self.db, self.dev.id)), 1)
        self.assertEqual(
            len(ts.list_tokens(self.db, self.dev.id, include_revoked=True)), 2
        )

    def test_listing_is_scoped_to_one_user(self):
        ts.create_token(self.db, self.dev, "mine")
        ts.create_token(self.db, self.lead, "theirs")
        mine = ts.list_tokens(self.db, self.dev.id)
        self.assertEqual([t.name for t in mine], ["mine"])

    def test_serialisation_never_leaks_the_secret_or_its_hash(self):
        token, plaintext = ts.create_token(self.db, self.dev, "laptop")
        blob = ts.serialize(token)
        flat = repr(blob)
        self.assertNotIn(plaintext, flat)
        self.assertNotIn(token.token_hash, flat)
        self.assertNotIn("token_hash", blob)

    def test_serialisation_reports_active_state(self):
        token, _ = ts.create_token(self.db, self.dev, "laptop")
        self.assertTrue(ts.serialize(token)["active"])
        ts.revoke_token(self.db, token)
        blob = ts.serialize(token)
        self.assertFalse(blob["active"])
        self.assertTrue(blob["revoked"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
