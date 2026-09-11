"""Server-side pre-receive hooks: policy enforced before a push is accepted.

The gap this closes: message format, path restrictions and content rules could
only be enforced by editing the server. Now a repository admin declares them and
the server applies them on every push, including pushes from a modified client.

**Why these are declarative rather than scripts.** The obvious design is "upload
a shell script and we run it". That turns write access to a repository's
settings into remote code execution on the server, so a single compromised
maintainer account owns the host. Instead, rules are JSON evaluated by this
module, and anything needing real logic delegates to an external HTTP endpoint
that answers allow/deny — which runs on the integrator's machine, not ours.

Rules available on a ``pre-receive`` hook:

``commit_message_pattern``      regex the commit message must match
``commit_message_min_length``   minimum message length
``forbidden_paths``             glob patterns that may never be pushed
``protected_paths``             globs only ``protected_paths_allowed_users`` may touch
``max_file_bytes``              per-file ceiling, stricter than the server default
``forbidden_content``           regexes scanned inside text files (committed secrets)
``external_url``                POST the push summary; a non-allow answer rejects

A ``post-receive`` hook cannot block anything; it exists to notify. Delivery
reuses the webhook machinery rather than duplicating it.
"""

import base64
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
from sqlalchemy.orm import Session

from database.models import ServerHook, User

from app.services.ignore_rules import IgnoreMatcher

HOOK_PRE_RECEIVE = "pre-receive"
HOOK_POST_RECEIVE = "post-receive"
VALID_HOOK_TYPES = (HOOK_PRE_RECEIVE, HOOK_POST_RECEIVE)

# An external policy service gets a short leash: a push is a synchronous user
# action, and waiting 30s on someone's Lambda is indistinguishable from a hang.
EXTERNAL_TIMEOUT = 5

# Content scanning is capped so a large binary blob cannot turn one push into a
# minutes-long regex run.
MAX_SCAN_BYTES = 512 * 1024

RULE_KEYS = (
    "commit_message_pattern",
    "commit_message_min_length",
    "forbidden_paths",
    "protected_paths",
    "protected_paths_allowed_users",
    "max_file_bytes",
    "forbidden_content",
    "external_url",
)


class HookError(Exception):
    """A configuration mistake, reported to whoever is editing the hook."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class HookRejection(Exception):
    """A push refused by policy. Carries every reason, not just the first.

    Reporting one reason at a time turns a five-rule violation into five
    round-trips, so the whole list is collected and returned together.
    """

    def __init__(self, hook_name: str, reasons: List[str]):
        self.hook_name = hook_name
        self.reasons = reasons
        super().__init__(f"{hook_name}: " + "; ".join(reasons))


def validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Check a rule set and return it cleaned.

    Regexes are compiled here so a bad pattern is rejected when it is saved
    rather than on the next unlucky push.
    """
    if not isinstance(config, dict):
        raise HookError("Hook configuration must be a JSON object")

    unknown = [key for key in config if key not in RULE_KEYS]
    if unknown:
        raise HookError(
            f"Unknown rule(s): {', '.join(sorted(unknown))}. "
            f"Valid rules: {', '.join(RULE_KEYS)}"
        )

    cleaned: Dict[str, Any] = {}

    pattern = config.get("commit_message_pattern")
    if pattern:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise HookError(f"commit_message_pattern is not a valid regex: {exc}")
        cleaned["commit_message_pattern"] = pattern

    if config.get("commit_message_min_length") is not None:
        try:
            value = int(config["commit_message_min_length"])
        except (TypeError, ValueError):
            raise HookError("commit_message_min_length must be an integer")
        if value < 0:
            raise HookError("commit_message_min_length cannot be negative")
        cleaned["commit_message_min_length"] = value

    for key in ("forbidden_paths", "protected_paths", "protected_paths_allowed_users"):
        if config.get(key) is not None:
            value = config[key]
            if not isinstance(value, list):
                raise HookError(f"{key} must be a list")
            cleaned[key] = [str(item).strip() for item in value if str(item).strip()]

    if config.get("max_file_bytes") is not None:
        try:
            value = int(config["max_file_bytes"])
        except (TypeError, ValueError):
            raise HookError("max_file_bytes must be an integer")
        if value <= 0:
            raise HookError("max_file_bytes must be positive")
        cleaned["max_file_bytes"] = value

    if config.get("forbidden_content") is not None:
        value = config["forbidden_content"]
        if not isinstance(value, list):
            raise HookError("forbidden_content must be a list of regexes")
        compiled = []
        for raw in value:
            text = str(raw).strip()
            if not text:
                continue
            try:
                re.compile(text)
            except re.error as exc:
                raise HookError(f"forbidden_content pattern {text!r} is not valid: {exc}")
            compiled.append(text)
        cleaned["forbidden_content"] = compiled

    url = config.get("external_url")
    if url:
        url = str(url).strip()
        if not url.startswith(("http://", "https://")):
            raise HookError("external_url must start with http:// or https://")
        cleaned["external_url"] = url

    return cleaned


def hooks_for(db: Session, repository_id: Optional[str], hook_type: str) -> List[ServerHook]:
    """Enabled hooks that apply here: this repository's, plus the global ones."""
    query = db.query(ServerHook).filter(
        ServerHook.enabled.is_(True), ServerHook.hook_type == hook_type
    )
    if repository_id:
        query = query.filter(
            (ServerHook.repository_id == repository_id) | (ServerHook.repository_id.is_(None))
        )
    else:
        query = query.filter(ServerHook.repository_id.is_(None))
    return query.order_by(ServerHook.id.asc()).all()


def _decode(payload: Any) -> Optional[bytes]:
    """Best-effort decode of a commit file payload to raw bytes."""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, dict):
        payload = payload.get("content") or payload.get("data")
    if not isinstance(payload, str):
        return None
    try:
        return base64.b64decode(payload, validate=False)
    except Exception:
        return None


def _looks_textual(blob: bytes) -> bool:
    """Whether scanning this for patterns is meaningful.

    A NUL byte in the first kilobyte is the same heuristic diff tools use, and
    it keeps the scanner off images and archives where a regex hit would be
    noise anyway.
    """
    return b"\x00" not in blob[:1024]


def evaluate(
    config: Dict[str, Any],
    commit_data: Dict[str, Any],
    pusher: Optional[User],
    repository_name: Optional[str] = None,
    branch: Optional[str] = None,
) -> List[str]:
    """Apply one rule set. Returns every reason to reject; empty means accept."""
    reasons: List[str] = []
    message = (commit_data.get("message") or "").strip()
    files: Dict[str, Any] = commit_data.get("files") or {}
    username = getattr(pusher, "username", None)

    minimum = config.get("commit_message_min_length")
    if minimum and len(message) < minimum:
        reasons.append(
            f"commit message is {len(message)} characters; at least {minimum} required"
        )

    pattern = config.get("commit_message_pattern")
    if pattern and not re.search(pattern, message):
        reasons.append(f"commit message does not match required pattern {pattern!r}")

    forbidden = config.get("forbidden_paths") or []
    if forbidden and files:
        matcher = IgnoreMatcher.from_text("\n".join(forbidden))
        hits = [path for path in files if matcher.is_ignored(path)]
        if hits:
            reasons.append(
                "path(s) not allowed by policy: " + ", ".join(sorted(hits)[:10])
            )

    protected = config.get("protected_paths") or []
    if protected and files:
        allowed = {u.lower() for u in (config.get("protected_paths_allowed_users") or [])}
        # No allow-list means the paths are protected from everyone, which is a
        # coherent way to freeze generated files.
        if not username or username.lower() not in allowed:
            matcher = IgnoreMatcher.from_text("\n".join(protected))
            hits = [path for path in files if matcher.is_ignored(path)]
            if hits:
                who = ", ".join(sorted(allowed)) if allowed else "nobody"
                reasons.append(
                    f"protected path(s) {', '.join(sorted(hits)[:10])} may only be "
                    f"changed by: {who}"
                )

    ceiling = config.get("max_file_bytes")
    if ceiling and files:
        for path, payload in files.items():
            blob = _decode(payload)
            if blob is not None and len(blob) > ceiling:
                reasons.append(
                    f"'{path}' is {len(blob)} bytes; this repository's hook caps files at {ceiling}"
                )

    patterns = config.get("forbidden_content") or []
    if patterns and files:
        compiled = [(raw, re.compile(raw)) for raw in patterns]
        for path, payload in files.items():
            blob = _decode(payload)
            if blob is None or len(blob) > MAX_SCAN_BYTES or not _looks_textual(blob):
                continue
            text = blob.decode("utf-8", errors="replace")
            for raw, rx in compiled:
                if rx.search(text):
                    reasons.append(f"'{path}' matches forbidden content pattern {raw!r}")
                    break  # one reason per file is enough to act on

    url = config.get("external_url")
    if url:
        reasons.extend(
            _consult_external(url, commit_data, username, repository_name, branch)
        )

    return reasons


def _consult_external(
    url: str,
    commit_data: Dict[str, Any],
    username: Optional[str],
    repository_name: Optional[str],
    branch: Optional[str],
) -> List[str]:
    """Ask an external policy service. Only file *paths* are sent, never contents.

    A policy endpoint has no business receiving source code, and sending it
    would quietly turn every hook into an exfiltration channel.
    """
    payload = {
        "repository": repository_name,
        "branch": branch,
        "pusher": username,
        "commit": {
            "id": commit_data.get("id"),
            "message": commit_data.get("message"),
            "author": commit_data.get("author"),
            "paths": sorted((commit_data.get("files") or {}).keys()),
        },
    }
    try:
        response = requests.post(url, json=payload, timeout=EXTERNAL_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        # Fail closed. A pre-receive hook exists to stop things; treating an
        # unreachable policy service as approval would make it trivially
        # bypassable by knocking the service over.
        return [f"policy service {url} is unreachable ({exc.__class__.__name__}); push refused"]

    if response.status_code >= 500:
        return [f"policy service {url} returned {response.status_code}; push refused"]

    try:
        body = response.json()
    except ValueError:
        return [f"policy service {url} returned a non-JSON response; push refused"]

    if body.get("allow") is True:
        return []

    detail = body.get("message") or body.get("reason") or "rejected by policy service"
    return [str(detail)[:300]]


def run_pre_receive(
    db: Session,
    repository,
    commit_data: Dict[str, Any],
    pusher: Optional[User],
    branch: Optional[str] = None,
) -> None:
    """Apply every pre-receive hook. Raises HookRejection on the first that refuses.

    Stopping at the first failing hook is deliberate: hooks are independent
    policies, and reporting "you also violated the other four" is noise when the
    first one already blocks the push.
    """
    for hook in hooks_for(db, getattr(repository, "id", None), HOOK_PRE_RECEIVE):
        try:
            config = json.loads(hook.config_json or "{}")
        except ValueError:
            # A corrupt rule set must not silently wave pushes through.
            raise HookRejection(hook.name, ["hook configuration is not valid JSON"])

        reasons = evaluate(
            config,
            commit_data,
            pusher,
            repository_name=getattr(repository, "name", None),
            branch=branch,
        )
        if reasons:
            raise HookRejection(hook.name, reasons)


def serialize(hook: ServerHook) -> dict:
    try:
        config = json.loads(hook.config_json or "{}")
    except ValueError:
        config = {}
    return {
        "id": hook.id,
        "repository_id": hook.repository_id,
        "name": hook.name,
        "hook_type": hook.hook_type,
        "enabled": bool(hook.enabled),
        "config": config,
        "created_at": hook.created_at.isoformat() if hook.created_at else None,
        "updated_at": hook.updated_at.isoformat() if hook.updated_at else None,
    }
