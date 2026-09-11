"""Password hashing and signed access-token issuing/verification."""

import json
import base64
import hashlib
import hmac
import secrets

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.config import AUTH_SECRET, PWD_ITERATIONS, TOKEN_EXPIRY_HOURS


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)

def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Password cannot be empty")
    salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PWD_ITERATIONS).hex()
    return f"pbkdf2_sha256${PWD_ITERATIONS}${salt}${pwd_hash}"

def verify_password(password: str, stored_hash: str) -> bool:
    if not password or not stored_hash:
        return False
    try:
        algorithm, iterations_str, salt, pwd_hash = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_str)
        check_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations).hex()
        return hmac.compare_digest(check_hash, pwd_hash)
    except Exception:
        return False

def create_access_token(username: str, role: str, expires_delta: Optional[timedelta] = None) -> str:
    expires = datetime.utcnow() + (expires_delta or timedelta(hours=TOKEN_EXPIRY_HOURS))
    payload = {
        "sub": username,
        "role": role,
        "exp": int(expires.timestamp()),
        "iat": int(datetime.utcnow().timestamp())
    }
    payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_encoded = _b64url_encode(payload_json)
    signature = hmac.new(AUTH_SECRET.encode("utf-8"), payload_encoded.encode("utf-8"), hashlib.sha256).digest()
    signature_encoded = _b64url_encode(signature)
    return f"{payload_encoded}.{signature_encoded}"

def decode_access_token(token: str) -> Dict[str, Any]:
    try:
        payload_part, signature_part = token.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid token format")

    expected_signature = hmac.new(AUTH_SECRET.encode("utf-8"), payload_part.encode("utf-8"), hashlib.sha256).digest()
    given_signature = _b64url_decode(signature_part)
    if not hmac.compare_digest(expected_signature, given_signature):
        raise HTTPException(status_code=401, detail="Invalid token signature")

    try:
        payload = json.loads(_b64url_decode(payload_part).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    exp = payload.get("exp")
    if not exp or int(exp) < int(datetime.utcnow().timestamp()):
        raise HTTPException(status_code=401, detail="Token expired")

    return payload
