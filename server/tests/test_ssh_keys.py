"""Tests for SSH public key auth.

Signatures are produced by real generated key pairs, not fixtures, so a
verification bug cannot hide behind a hard-coded blob that happens to match.
"""
import base64
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

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from database.database import Base  # noqa: E402
from database.crud import UserCRUD  # noqa: E402
from database.models import SSHChallenge  # noqa: E402
from app.services import ssh_keys as sk  # noqa: E402


def make_ed25519():
    private = ed25519.Ed25519PrivateKey.generate()
    line = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    return private, line


def make_rsa():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    line = private.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    return private, line


def sign(private, message: bytes) -> str:
    if isinstance(private, ed25519.Ed25519PrivateKey):
        sig = private.sign(message)
    else:
        sig = private.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


class KeyParsingTests(unittest.TestCase):
    def test_valid_ed25519_key_parses(self):
        _, line = make_ed25519()
        key_type, normalised, fingerprint = sk.parse_public_key(line)
        self.assertEqual(key_type, "ssh-ed25519")
        self.assertTrue(fingerprint.startswith("SHA256:"))
        self.assertTrue(normalised.startswith("ssh-ed25519 "))

    def test_valid_rsa_key_parses(self):
        _, line = make_rsa()
        key_type, _, fingerprint = sk.parse_public_key(line)
        self.assertEqual(key_type, "ssh-rsa")
        self.assertTrue(fingerprint.startswith("SHA256:"))

    def test_comment_is_preserved(self):
        _, line = make_ed25519()
        _, normalised, _ = sk.parse_public_key(line + " alice@laptop")
        self.assertTrue(normalised.endswith("alice@laptop"))

    def test_fingerprint_is_stable(self):
        _, line = make_ed25519()
        self.assertEqual(sk.parse_public_key(line)[2], sk.parse_public_key(line)[2])

    def test_different_keys_have_different_fingerprints(self):
        _, a = make_ed25519()
        _, b = make_ed25519()
        self.assertNotEqual(sk.parse_public_key(a)[2], sk.parse_public_key(b)[2])

    def test_empty_key_is_refused(self):
        with self.assertRaises(sk.SSHKeyError):
            sk.parse_public_key("   ")

    def test_a_pasted_private_key_is_caught(self):
        """The mistake worth catching loudly, not storing."""
        private, _ = make_ed25519()
        pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        with self.assertRaises(sk.SSHKeyError) as ctx:
            sk.parse_public_key(pem)
        self.assertIn("private key", ctx.exception.message.lower())

    def test_unsupported_type_is_named(self):
        with self.assertRaises(sk.SSHKeyError) as ctx:
            sk.parse_public_key("ssh-dss AAAAB3NzaC1kc3M=")
        self.assertIn("ssh-dss", ctx.exception.message)

    def test_malformed_base64_is_refused(self):
        with self.assertRaises(sk.SSHKeyError):
            sk.parse_public_key("ssh-ed25519 !!!not-base64!!!")

    def test_missing_body_is_refused(self):
        with self.assertRaises(sk.SSHKeyError):
            sk.parse_public_key("ssh-ed25519")


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.alice = UserCRUD.create_user(self.db, username="alice", email="a@e.com", role="developer")
        self.bob = UserCRUD.create_user(self.db, username="bob", email="b@e.com", role="developer")

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_add_and_list(self):
        _, line = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", line)
        keys = sk.list_keys(self.db, self.alice.id)
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0].title, "laptop")

    def test_title_is_required(self):
        _, line = make_ed25519()
        with self.assertRaises(sk.SSHKeyError):
            sk.add_key(self.db, self.alice, "  ", line)

    def test_the_same_key_twice_is_refused(self):
        _, line = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", line)
        with self.assertRaises(sk.SSHKeyError) as ctx:
            sk.add_key(self.db, self.alice, "laptop again", line)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_another_users_key_cannot_be_claimed(self):
        """Two accounts sharing a key would make sign-in ambiguous."""
        _, line = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", line)
        with self.assertRaises(sk.SSHKeyError) as ctx:
            sk.add_key(self.db, self.bob, "stolen", line)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_keys_are_scoped_per_user(self):
        _, a = make_ed25519()
        _, b = make_ed25519()
        sk.add_key(self.db, self.alice, "a", a)
        sk.add_key(self.db, self.bob, "b", b)
        self.assertEqual(len(sk.list_keys(self.db, self.alice.id)), 1)

    def test_delete_removes_only_your_own(self):
        _, line = make_ed25519()
        key = sk.add_key(self.db, self.alice, "laptop", line)
        self.assertFalse(sk.delete_key(self.db, self.bob, key.id))
        self.assertTrue(sk.delete_key(self.db, self.alice, key.id))
        self.assertEqual(sk.list_keys(self.db, self.alice.id), [])

    def test_serialisation_has_no_secret_material(self):
        _, line = make_ed25519()
        key = sk.add_key(self.db, self.alice, "laptop", line)
        blob = sk.serialize(key)
        self.assertIn("fingerprint", blob)
        self.assertNotIn("PRIVATE", repr(blob).upper())


class ChallengeTests(KeyStoreTests):
    def test_challenge_is_issued_for_an_unknown_user(self):
        """Refusing here would enumerate which accounts exist."""
        challenge = sk.create_challenge(self.db, "nobody-here")
        self.assertTrue(challenge.nonce)

    def test_two_challenges_never_repeat(self):
        a = sk.create_challenge(self.db, "alice").nonce
        b = sk.create_challenge(self.db, "alice").nonce
        self.assertNotEqual(a, b)

    def test_a_correct_signature_authenticates(self):
        private, line = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", line)
        challenge = sk.create_challenge(self.db, "alice")
        user = sk.verify_challenge(self.db, challenge.nonce, sign(private, challenge.nonce.encode()))
        self.assertIsNotNone(user)
        self.assertEqual(user.username, "alice")

    def test_an_rsa_signature_authenticates(self):
        private, line = make_rsa()
        sk.add_key(self.db, self.alice, "server", line)
        challenge = sk.create_challenge(self.db, "alice")
        user = sk.verify_challenge(self.db, challenge.nonce, sign(private, challenge.nonce.encode()))
        self.assertIsNotNone(user)

    def test_a_wrong_key_does_not_authenticate(self):
        _, registered = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", registered)
        attacker, _ = make_ed25519()
        challenge = sk.create_challenge(self.db, "alice")
        self.assertIsNone(
            sk.verify_challenge(self.db, challenge.nonce, sign(attacker, challenge.nonce.encode()))
        )

    def test_a_nonce_cannot_be_replayed(self):
        """The single most important property here."""
        private, line = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", line)
        challenge = sk.create_challenge(self.db, "alice")
        signature = sign(private, challenge.nonce.encode())
        self.assertIsNotNone(sk.verify_challenge(self.db, challenge.nonce, signature))
        self.assertIsNone(sk.verify_challenge(self.db, challenge.nonce, signature))

    def test_a_failed_attempt_still_burns_the_nonce(self):
        """Otherwise the nonce could be retried until a signature works."""
        _, line = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", line)
        attacker, _ = make_ed25519()
        challenge = sk.create_challenge(self.db, "alice")
        sk.verify_challenge(self.db, challenge.nonce, sign(attacker, challenge.nonce.encode()))
        self.assertIsNone(
            self.db.query(SSHChallenge).filter(SSHChallenge.nonce == challenge.nonce).first()
        )

    def test_an_expired_nonce_is_refused(self):
        private, line = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", line)
        challenge = sk.create_challenge(self.db, "alice")
        challenge.expires_at = datetime.utcnow() - timedelta(seconds=1)
        self.db.commit()
        self.assertIsNone(
            sk.verify_challenge(self.db, challenge.nonce, sign(private, challenge.nonce.encode()))
        )

    def test_a_signature_for_one_user_cannot_sign_in_another(self):
        """The nonce is bound to a username, not just to a key."""
        private, line = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", line)
        challenge = sk.create_challenge(self.db, "bob")
        self.assertIsNone(
            sk.verify_challenge(self.db, challenge.nonce, sign(private, challenge.nonce.encode()))
        )

    def test_an_unknown_nonce_is_refused(self):
        self.assertIsNone(sk.verify_challenge(self.db, "never-issued", "AAAA"))

    def test_a_garbage_signature_is_refused(self):
        _, line = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", line)
        challenge = sk.create_challenge(self.db, "alice")
        self.assertIsNone(sk.verify_challenge(self.db, challenge.nonce, "!!!not base64!!!"))

    def test_an_empty_signature_is_refused(self):
        _, line = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", line)
        challenge = sk.create_challenge(self.db, "alice")
        self.assertIsNone(sk.verify_challenge(self.db, challenge.nonce, ""))

    def test_a_user_with_no_keys_cannot_sign_in(self):
        private, _ = make_ed25519()
        challenge = sk.create_challenge(self.db, "alice")
        self.assertIsNone(
            sk.verify_challenge(self.db, challenge.nonce, sign(private, challenge.nonce.encode()))
        )

    def test_an_inactive_user_cannot_sign_in(self):
        private, line = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", line)
        self.alice.is_active = False
        self.db.commit()
        challenge = sk.create_challenge(self.db, "alice")
        self.assertIsNone(
            sk.verify_challenge(self.db, challenge.nonce, sign(private, challenge.nonce.encode()))
        )

    def test_any_of_a_users_keys_works(self):
        first, line_a = make_ed25519()
        second, line_b = make_ed25519()
        sk.add_key(self.db, self.alice, "laptop", line_a)
        sk.add_key(self.db, self.alice, "desktop", line_b)
        challenge = sk.create_challenge(self.db, "alice")
        self.assertIsNotNone(
            sk.verify_challenge(self.db, challenge.nonce, sign(second, challenge.nonce.encode()))
        )

    def test_successful_auth_records_key_usage(self):
        private, line = make_ed25519()
        key = sk.add_key(self.db, self.alice, "laptop", line)
        self.assertIsNone(key.last_used_at)
        challenge = sk.create_challenge(self.db, "alice")
        sk.verify_challenge(self.db, challenge.nonce, sign(private, challenge.nonce.encode()))
        self.db.refresh(key)
        self.assertIsNotNone(key.last_used_at)

    def test_removing_a_key_revokes_its_access(self):
        private, line = make_ed25519()
        key = sk.add_key(self.db, self.alice, "laptop", line)
        sk.delete_key(self.db, self.alice, key.id)
        challenge = sk.create_challenge(self.db, "alice")
        self.assertIsNone(
            sk.verify_challenge(self.db, challenge.nonce, sign(private, challenge.nonce.encode()))
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
