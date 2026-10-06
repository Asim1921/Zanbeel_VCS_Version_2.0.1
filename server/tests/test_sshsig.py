"""SSHSIG: the OpenSSH signature format SSH-key sign-in now uses.

Every signature here comes from a real generated key, made either by the fox CLI's own
signer or by OpenSSH's `ssh-keygen -Y sign`, so the two can be checked against each
other: the server must accept what OpenSSH produces, and OpenSSH must accept what the
CLI produces. The OpenSSH half is skipped where ssh-keygen is not installed.
"""
import base64
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa  # noqa: E402

from app.services import sshsig  # noqa: E402

FOX_PATH = Path(SERVER_ROOT).parent / "client" / "client1" / "fox.py"
_spec = importlib.util.spec_from_file_location("fox_cli_for_sshsig", FOX_PATH)
fox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fox)

KEYGEN = fox.find_ssh_keygen()

MESSAGE = b"a-one-time-nonce"


def keys():
    """One private key of every supported kind."""
    return {
        "ed25519": ed25519.Ed25519PrivateKey.generate(),
        "rsa": rsa.generate_private_key(public_exponent=65537, key_size=2048),
        "p256": ec.generate_private_key(ec.SECP256R1()),
        "p384": ec.generate_private_key(ec.SECP384R1()),
        "p521": ec.generate_private_key(ec.SECP521R1()),
    }


def public_line(private):
    return private.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode()


def rebuild(parsed, **changes):
    """Re-encode a parsed signature with some fields replaced."""
    fields = dict(
        public_key_blob=parsed.public_key_blob, namespace=parsed.namespace,
        hash_algorithm=parsed.hash_algorithm, signature_type=parsed.signature_type,
        signature_blob=parsed.signature_blob,
    )
    fields.update(changes)
    s = fox._ssh_string
    signature = s(fields["signature_type"].encode()) + s(fields["signature_blob"])
    blob = (b"SSHSIG" + (1).to_bytes(4, "big") + s(fields["public_key_blob"])
            + s(fields["namespace"].encode()) + s(b"") + s(fields["hash_algorithm"].encode())
            + s(signature))
    return fox._armor_sshsig(blob)


class SignatureTests(unittest.TestCase):
    def test_every_key_type_round_trips(self):
        for name, private in keys().items():
            with self.subTest(key=name):
                parsed = sshsig.parse(fox.sshsig_sign_with_key(private, MESSAGE))
                self.assertTrue(sshsig.verify(parsed, MESSAGE))
                self.assertEqual(parsed.namespace, "zanbeel-auth")
                self.assertEqual(parsed.fingerprint, fox.ssh_fingerprint(public_line(private)))

    def test_a_different_message_fails(self):
        for name, private in keys().items():
            with self.subTest(key=name):
                parsed = sshsig.parse(fox.sshsig_sign_with_key(private, MESSAGE))
                self.assertFalse(sshsig.verify(parsed, MESSAGE + b"!"))

    def test_another_namespace_fails(self):
        """A signature made for git (or anything else) is not a Zanbeel sign-in."""
        private = ed25519.Ed25519PrivateKey.generate()
        parsed = sshsig.parse(fox.sshsig_sign_with_key(private, MESSAGE, namespace="git"))
        self.assertFalse(sshsig.verify(parsed, MESSAGE))
        self.assertTrue(sshsig.verify(parsed, MESSAGE, namespace="git"))

    def test_relabelling_the_namespace_breaks_the_signature(self):
        private = ed25519.Ed25519PrivateKey.generate()
        parsed = sshsig.parse(fox.sshsig_sign_with_key(private, MESSAGE, namespace="git"))
        forged = sshsig.parse(rebuild(parsed, namespace="zanbeel-auth"))
        self.assertFalse(sshsig.verify(forged, MESSAGE))

    def test_swapping_in_another_public_key_fails(self):
        signer, other = ed25519.Ed25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate()
        parsed = sshsig.parse(fox.sshsig_sign_with_key(signer, MESSAGE))
        other_blob = base64.b64decode(public_line(other).split()[1])
        self.assertFalse(sshsig.verify(sshsig.parse(rebuild(parsed, public_key_blob=other_blob)), MESSAGE))

    def test_tampered_signature_bytes_fail(self):
        for name, private in keys().items():
            with self.subTest(key=name):
                parsed = sshsig.parse(fox.sshsig_sign_with_key(private, MESSAGE))
                blob = bytearray(parsed.signature_blob)
                blob[-1] ^= 0x01
                self.assertFalse(sshsig.verify(sshsig.parse(rebuild(parsed, signature_blob=bytes(blob))), MESSAGE))

    def test_rsa_with_sha1_is_refused(self):
        """ssh-rsa signatures use SHA-1; they must not authenticate anything new."""
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        parsed = sshsig.parse(fox.sshsig_sign_with_key(private, MESSAGE))
        data = sshsig.signed_data("zanbeel-auth", "sha512", MESSAGE)
        sha1_sig = private.sign(data, padding.PKCS1v15(), hashes.SHA1())
        forged = sshsig.parse(rebuild(parsed, signature_type="ssh-rsa", signature_blob=sha1_sig))
        self.assertFalse(sshsig.verify(forged, MESSAGE))

    def test_rsa_sha2_256_is_accepted(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        parsed = sshsig.parse(fox.sshsig_sign_with_key(private, MESSAGE))
        data = sshsig.signed_data("zanbeel-auth", "sha512", MESSAGE)
        sig = private.sign(data, padding.PKCS1v15(), hashes.SHA256())
        self.assertTrue(sshsig.verify(sshsig.parse(rebuild(parsed, signature_type="rsa-sha2-256", signature_blob=sig)), MESSAGE))

    def test_signature_type_must_match_the_key(self):
        private = ec.generate_private_key(ec.SECP384R1())
        parsed = sshsig.parse(fox.sshsig_sign_with_key(private, MESSAGE))
        self.assertFalse(sshsig.verify(sshsig.parse(rebuild(parsed, signature_type="ecdsa-sha2-nistp256")), MESSAGE))

    def test_unknown_hash_algorithm_is_refused(self):
        private = ed25519.Ed25519PrivateKey.generate()
        parsed = sshsig.parse(fox.sshsig_sign_with_key(private, MESSAGE))
        self.assertFalse(sshsig.verify(sshsig.parse(rebuild(parsed, hash_algorithm="md5")), MESSAGE))

    def test_bare_base64_is_accepted_like_armor(self):
        private = ed25519.Ed25519PrivateKey.generate()
        armored = fox.sshsig_sign_with_key(private, MESSAGE)
        bare = "".join(armored.strip().splitlines()[1:-1])
        self.assertTrue(sshsig.looks_like_sshsig(bare))
        self.assertTrue(sshsig.verify(sshsig.parse(bare), MESSAGE))


class ParsingTests(unittest.TestCase):
    def test_garbage_is_refused(self):
        for text in ("", "not base64 !!", base64.b64encode(b"SSHSIG").decode(),
                     sshsig.ARMOR_BEGIN + "\nAAAA\n", base64.b64encode(b"x" * 64).decode()):
            with self.subTest(text=text[:20]):
                with self.assertRaises(sshsig.SSHSigError):
                    sshsig.parse(text)

    def test_trailing_bytes_are_refused(self):
        private = ed25519.Ed25519PrivateKey.generate()
        armored = fox.sshsig_sign_with_key(private, MESSAGE)
        blob = base64.b64decode("".join(armored.strip().splitlines()[1:-1])) + b"\x00"
        with self.assertRaises(sshsig.SSHSigError):
            sshsig.parse(base64.b64encode(blob).decode())

    def test_a_raw_legacy_signature_is_not_mistaken_for_sshsig(self):
        raw = ed25519.Ed25519PrivateKey.generate().sign(MESSAGE)
        self.assertFalse(sshsig.looks_like_sshsig(base64.b64encode(raw).decode()))


@unittest.skipUnless(KEYGEN, "ssh-keygen (OpenSSH) is not installed")
class OpenSSHInteropTests(unittest.TestCase):
    """The server and the CLI agree with OpenSSH itself, byte for byte."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fox_sshsig_"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def keygen(self, kind, bits=None, passphrase=""):
        path = self.dir / f"{kind}{bits or ''}"
        args = [KEYGEN, "-q", "-t", kind, "-N", passphrase, "-f", str(path), "-C", "t@fox"]
        if bits:
            args[2:2] = ["-b", str(bits)]
        subprocess.run(args, check=True, stdin=subprocess.DEVNULL)
        return path

    def openssh_verifies(self, key, armored):
        (self.dir / "allowed").write_text("tester " + Path(str(key) + ".pub").read_text())
        (self.dir / "sig").write_text(armored)
        result = subprocess.run(
            [KEYGEN, "-Y", "verify", "-f", str(self.dir / "allowed"), "-I", "tester",
             "-n", "zanbeel-auth", "-s", str(self.dir / "sig")],
            input=MESSAGE, capture_output=True,
        )
        return result.returncode == 0

    def test_server_accepts_what_openssh_signs(self):
        for kind, bits in (("ed25519", None), ("rsa", 3072), ("ecdsa", 256), ("ecdsa", 384), ("ecdsa", 521)):
            with self.subTest(kind=kind, bits=bits):
                key = self.keygen(kind, bits)
                armored = fox.sshsig_sign_with_ssh_keygen(key, MESSAGE, interactive=False)
                parsed = sshsig.parse(armored)
                self.assertTrue(sshsig.verify(parsed, MESSAGE))
                self.assertFalse(sshsig.verify(parsed, b"something else"))

    def test_openssh_accepts_what_the_cli_signs(self):
        for kind, bits in (("ed25519", None), ("rsa", 3072), ("ecdsa", 256), ("ecdsa", 384), ("ecdsa", 521)):
            with self.subTest(kind=kind, bits=bits):
                key = self.keygen(kind, bits)
                armored, how = fox.sign_ssh_challenge(key, MESSAGE, interactive=False)
                self.assertEqual(how, "fox")
                self.assertTrue(self.openssh_verifies(key, armored))

    def test_a_passphrase_key_fails_fast_without_a_terminal(self):
        """No terminal to ask on: say so, rather than hang a CI job."""
        key = self.keygen("ed25519", passphrase="correct horse")
        with self.assertRaises(fox.SSHSignError) as ctx:
            fox.sign_ssh_challenge(key, MESSAGE, interactive=False)
        self.assertIn("ssh-agent", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
