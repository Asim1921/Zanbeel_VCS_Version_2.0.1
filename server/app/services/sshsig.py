"""OpenSSH signatures (SSHSIG): the format ``ssh-keygen -Y sign`` writes.

Why this format rather than a raw signature over the challenge. A raw signature
turns the client into a signing oracle: whatever bytes the server sends, the
user's key signs. The same key usually also signs SSH logins and git commits,
so a hostile or impersonated server could hand over bytes that mean something
in one of those protocols. SSHSIG prefixes everything it signs with a magic
string and a *namespace* the client fixes, so a signature made here can only
ever be valid as a Zanbeel sign-in.

It is also what OpenSSH itself produces, so a key held in ssh-agent, a
passphrase-protected key, or a hardware-backed key works through
``ssh-keygen -Y sign`` without the client ever touching the private key.

Wire format (PROTOCOL.sshsig in the OpenSSH source):

    byte[6]  "SSHSIG"
    uint32   version (1)
    string   public key
    string   namespace
    string   reserved
    string   hash algorithm ("sha256" | "sha512")
    string   signature

and the bytes actually signed are

    byte[6]  "SSHSIG"
    string   namespace
    string   reserved
    string   hash algorithm
    string   H(message)
"""

import base64
import hashlib
import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.serialization import load_ssh_public_key

MAGIC = b"SSHSIG"
VERSION = 1

# The namespace every Zanbeel sign-in signature must carry. The client fixes it;
# it is never taken from the server, or the separation it provides would be the
# server's to waive.
NAMESPACE = "zanbeel-auth"

ARMOR_BEGIN = "-----BEGIN SSH SIGNATURE-----"
ARMOR_END = "-----END SSH SIGNATURE-----"

_HASHES = {"sha256": hashlib.sha256, "sha512": hashlib.sha512}

# ssh-rsa (RSA with SHA-1) is deliberately absent: OpenSSH stopped producing it
# for SSHSIG, and SHA-1 signatures should not authenticate anything new.
_RSA_SIGNATURE_HASHES = {
    "rsa-sha2-256": hashes.SHA256,
    "rsa-sha2-512": hashes.SHA512,
}

# RFC 5656: the curve fixes the hash.
_ECDSA_HASHES = {
    "ecdsa-sha2-nistp256": hashes.SHA256,
    "ecdsa-sha2-nistp384": hashes.SHA384,
    "ecdsa-sha2-nistp521": hashes.SHA512,
}


class SSHSigError(ValueError):
    """The signature could not be parsed."""


@dataclass(frozen=True)
class Signature:
    public_key_blob: bytes
    namespace: str
    hash_algorithm: str
    signature_type: str
    signature_blob: bytes

    @property
    def key_type(self) -> str:
        return _read_string(self.public_key_blob, 0)[0].decode("ascii", "replace")

    @property
    def fingerprint(self) -> str:
        """OpenSSH ``SHA256:...`` fingerprint of the embedded public key."""
        return fingerprint_of_blob(self.public_key_blob)


def fingerprint_of_blob(blob: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _read_string(data: bytes, offset: int):
    if offset + 4 > len(data):
        raise SSHSigError("Truncated signature")
    (length,) = struct.unpack(">I", data[offset:offset + 4])
    start = offset + 4
    end = start + length
    if end > len(data):
        raise SSHSigError("Truncated signature")
    return data[start:end], end


def _ssh_string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def _mpint(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=True) if data else 0


def looks_like_sshsig(text: str) -> bool:
    """True if ``text`` is an armored SSHSIG or the base64 of one."""
    text = (text or "").strip()
    if text.startswith(ARMOR_BEGIN):
        return True
    try:
        return base64.b64decode(text[:8] + "=" * (-len(text[:8]) % 4))[:6] == MAGIC
    except Exception:
        return False


def _unarmor(text: str) -> bytes:
    text = (text or "").strip()
    if text.startswith(ARMOR_BEGIN):
        if ARMOR_END not in text:
            raise SSHSigError("Signature armor is not closed")
        body = text[len(ARMOR_BEGIN):text.index(ARMOR_END)]
    else:
        body = text
    try:
        return base64.b64decode("".join(body.split()), validate=True)
    except Exception:
        raise SSHSigError("Signature body is not valid base64")


def parse(text: str) -> Signature:
    """Parse an armored (or bare base64) SSHSIG blob."""
    blob = _unarmor(text)
    if blob[:6] != MAGIC:
        raise SSHSigError("Not an SSH signature")
    offset = 6
    if offset + 4 > len(blob):
        raise SSHSigError("Truncated signature")
    (version,) = struct.unpack(">I", blob[offset:offset + 4])
    offset += 4
    if version != VERSION:
        raise SSHSigError(f"Unsupported SSH signature version {version}")

    public_key_blob, offset = _read_string(blob, offset)
    namespace, offset = _read_string(blob, offset)
    _reserved, offset = _read_string(blob, offset)
    hash_algorithm, offset = _read_string(blob, offset)
    signature, offset = _read_string(blob, offset)
    if offset != len(blob):
        raise SSHSigError("Trailing bytes after the signature")

    signature_type, inner = _read_string(signature, 0)
    signature_blob, end = _read_string(signature, inner)
    if end != len(signature):
        raise SSHSigError("Trailing bytes inside the signature")

    try:
        return Signature(
            public_key_blob=public_key_blob,
            namespace=namespace.decode("utf-8"),
            hash_algorithm=hash_algorithm.decode("ascii"),
            signature_type=signature_type.decode("ascii"),
            signature_blob=signature_blob,
        )
    except UnicodeDecodeError:
        raise SSHSigError("Malformed signature fields")


def signed_data(namespace: str, hash_algorithm: str, message: bytes) -> bytes:
    """The exact bytes an SSHSIG signature covers."""
    digest = _HASHES[hash_algorithm](message).digest()
    return (
        MAGIC
        + _ssh_string(namespace.encode("utf-8"))
        + _ssh_string(b"")
        + _ssh_string(hash_algorithm.encode("ascii"))
        + _ssh_string(digest)
    )


def verify(signature: Signature, message: bytes, namespace: str = NAMESPACE) -> bool:
    """True only if ``signature`` is a valid signature of ``message`` in ``namespace``.

    Checks the namespace before any cryptography, and ties the signature
    algorithm to the key type, so an RSA key cannot be presented with an ECDSA
    signature or a SHA-1 one.
    """
    if signature.namespace != namespace:
        return False
    if signature.hash_algorithm not in _HASHES:
        return False

    try:
        key_type = signature.key_type
        public_key = load_ssh_public_key(
            f"{key_type} {base64.b64encode(signature.public_key_blob).decode()}".encode()
        )
    except Exception:
        return False

    data = signed_data(namespace, signature.hash_algorithm, message)
    sig_type = signature.signature_type

    try:
        if isinstance(public_key, ed25519.Ed25519PublicKey):
            if sig_type != "ssh-ed25519":
                return False
            public_key.verify(signature.signature_blob, data)
        elif isinstance(public_key, rsa.RSAPublicKey):
            hash_cls = _RSA_SIGNATURE_HASHES.get(sig_type)
            if hash_cls is None:
                return False
            public_key.verify(signature.signature_blob, data, padding.PKCS1v15(), hash_cls())
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            hash_cls = _ECDSA_HASHES.get(key_type)
            if hash_cls is None or sig_type != key_type:
                return False
            r_bytes, offset = _read_string(signature.signature_blob, 0)
            s_bytes, end = _read_string(signature.signature_blob, offset)
            if end != len(signature.signature_blob):
                return False
            der = encode_dss_signature(_mpint(r_bytes), _mpint(s_bytes))
            public_key.verify(der, data, ec.ECDSA(hash_cls()))
        else:
            return False
    except (InvalidSignature, SSHSigError, ValueError, TypeError):
        return False
    return True
