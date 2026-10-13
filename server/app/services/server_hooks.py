"""Push rules: policy enforced before anything lands on a branch.

The gap this closes: message format, path restrictions and content rules could
only be enforced by editing the server. Now a repository admin declares them and
the server applies them -- on every push, and on every server-side write that puts
new content on a branch (pull-request and branch merges, conflict resolutions,
cherry-pick, revert, rebase, rollback, copying files between branches).

They used to run on ``fox push`` alone. Content that arrived any other way was never
checked: a conflict resolution's hand-edited files, a fork's pull request (its
commits were only ever checked against the fork's own rules), a rollback that put a
deleted secret back.

**Rules look at what changes, not at the whole tree.** A push is a full snapshot; the
rules used to scan all of it, so a protected file anywhere in the repository rejected
every push that did not touch it, every push rescanned every file, and deleting a
protected file passed because a deleted file is not in the snapshot. Now added and
modified files are checked, and deletions count for protected paths.

**Why these are declarative rather than scripts.** The obvious design is "upload
a shell script and we run it". That turns write access to a repository's
settings into remote code execution on the server, so a single compromised
maintainer account owns the host. Instead, rules are JSON evaluated by this
module, and anything needing real logic delegates to an external HTTP endpoint
that answers allow/deny -- which runs on the integrator's machine, not ours.

**Patterns cannot stall the server.** Patterns are written by repository owners and
run on every push. Python's ``re`` cannot be interrupted, and a pattern like
``(x+x+)+y`` takes forever on the wrong input, which inside the push route froze every
request on the server. Patterns run on the ``regex`` module with a time limit per
search and for the whole evaluation; running out of time refuses the push.

Rules available:

``commit_message_pattern``      regex the commit message must match (pushes)
``commit_message_min_length``   minimum message length (pushes)
``branch_name_pattern``         regex a new branch's name must match
``forbidden_paths``             globs that may not be added or changed
``protected_paths``             globs only ``protected_paths_allowed_users`` may change or delete
``max_file_bytes``              per-file ceiling, stricter than the server default
``forbidden_content``           regexes scanned inside changed text files
``block_secrets``               true: built-in checks for keys, tokens and secret files
``external_url``                POST the change summary; a non-allow answer rejects
``external_secret``             signs that request, so the policy service can trust it

Message rules apply to messages people write, which means pushes. A server-side write
writes its own message ("Merge branch ...", or a cherry-picked commit's original one,
already checked when it was pushed), so only the content and path rules apply to it.
"""

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import regex
from fastapi import HTTPException
from sqlalchemy.orm import Session

from database.models import CommitFile, ServerHook, User

from app.services import outbound
from app.services.ignore_rules import IgnoreMatcher

HOOK_PRE_RECEIVE = "pre-receive"
HOOK_POST_RECEIVE = "post-receive"
# post-receive hooks could be created but nothing ever ran them. Webhooks subscribed
# to "push" are the post-receive mechanism, so new ones are refused with that pointer.
VALID_HOOK_TYPES = (HOOK_PRE_RECEIVE,)

# An external policy service gets a short leash: a push is a synchronous user
# action, and waiting 30s on someone's Lambda is indistinguishable from a hang.
EXTERNAL_TIMEOUT = 5

# Content scanning is capped so a large binary blob cannot turn one push into a
# minutes-long regex run.
MAX_SCAN_BYTES = 512 * 1024

# Time limits for owner-written patterns: per search, and for one hook's evaluation.
PATTERN_TIMEOUT = 0.5
EVALUATION_BUDGET = 5.0

SIGNATURE_HEADER = "X-Zanbeel-Signature-256"
MIN_SECRET_LENGTH = 16

RULE_KEYS = (
    "commit_message_pattern",
    "commit_message_min_length",
    "branch_name_pattern",
    "forbidden_paths",
    "protected_paths",
    "protected_paths_allowed_users",
    "max_file_bytes",
    "forbidden_content",
    "block_secrets",
    "external_url",
    "external_secret",
)

# What block_secrets looks for. Deliberately specific: a rule that cries wolf on every
# push gets switched off, and then it catches nothing.
SECRET_PATTERNS = (
    ("a private key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
    ("an AWS access key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("a GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    ("a Zanbeel access token", r"\bfxp_[A-Za-z0-9_-]{40,}"),
    ("a Slack token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("a Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
)
SECRET_FILES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "*.p12", "*.pfx", "*.jks",
                "*.keystore", ".env")

_SECRET_REGEXES = [(label, regex.compile(pattern)) for label, pattern in SECRET_PATTERNS]


class HookError(Exception):
    """A configuration mistake, reported to whoever is editing the hook."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class HookRejection(HTTPException):
    """A write refused by policy. Carries every reason, not just the first.

    An HTTPException so that it reaches the client as a 422 with its reasons from any
    route, service-layer callers included, rather than as a generic server error.
    Reporting one reason at a time turns a five-rule violation into five round-trips,
    so the whole list is collected and returned together.
    """

    def __init__(self, hook_name: str, reasons: List[str]):
        self.hook_name = hook_name
        self.reasons = reasons
        message = f"Rejected by push rule '{hook_name}': " + "; ".join(reasons)
        super().__init__(status_code=422, detail={
            "code": "PRE_RECEIVE_REJECTED",
            "hook": hook_name,
            "reasons": reasons,
            "message": message,
        })

    def __str__(self) -> str:
        return f"{self.hook_name}: " + "; ".join(self.reasons)


# --- configuration --------------------------------------------------------------------

def _compile(pattern: str, key: str):
    try:
        return regex.compile(pattern)
    except regex.error as exc:
        raise HookError(f"{key} is not a valid regex: {exc}")


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

    for key in ("commit_message_pattern", "branch_name_pattern"):
        pattern = config.get(key)
        if pattern:
            _compile(str(pattern), key)
            cleaned[key] = str(pattern)

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
                regex.compile(text)
            except regex.error as exc:
                raise HookError(f"forbidden_content pattern {text!r} is not valid: {exc}")
            compiled.append(text)
        cleaned["forbidden_content"] = compiled

    if config.get("block_secrets") is not None:
        if not isinstance(config["block_secrets"], bool):
            raise HookError("block_secrets must be true or false")
        if config["block_secrets"]:
            cleaned["block_secrets"] = True

    url = config.get("external_url")
    if url:
        url = str(url).strip()
        try:
            outbound.check_url(url)
        except outbound.OutboundBlocked as exc:
            raise HookError(f"external_url: {exc.message}")
        cleaned["external_url"] = url

    secret = config.get("external_secret")
    if secret:
        if not cleaned.get("external_url"):
            raise HookError("external_secret only applies together with external_url")
        if len(str(secret)) < MIN_SECRET_LENGTH:
            raise HookError(f"external_secret must be at least {MIN_SECRET_LENGTH} characters")
        cleaned["external_secret"] = str(secret)

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


# --- what a write changes -------------------------------------------------------------

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


@dataclass
class ChangeSet:
    """Files a write adds or modifies (with their bytes), and paths it deletes."""

    changed: Dict[str, bytes] = field(default_factory=dict)
    deleted: List[str] = field(default_factory=list)


def _tree_hashes(db: Session, commit_id: Optional[str]) -> Dict[str, str]:
    if not commit_id:
        return {}
    return {
        row.file_path: row.file_hash
        for row in db.query(CommitFile).filter(CommitFile.commit_id == commit_id).all()
    }


def change_set(db: Session, base_commit_id: Optional[str], new_tree: Dict[str, Any]) -> ChangeSet:
    """Compare a new snapshot with ``base_commit_id`` by content hash.

    ``new_tree`` maps paths to bytes or to push payloads (base64). Only the base's
    hashes are read, never its file contents.
    """
    before = _tree_hashes(db, base_commit_id)
    result = ChangeSet()
    for path, payload in (new_tree or {}).items():
        content = _decode(payload)
        if content is None:
            content = b""
        if before.get(path) != hashlib.sha1(content).hexdigest():
            result.changed[path] = content
    result.deleted = sorted(path for path in before if path not in (new_tree or {}))
    return result


# --- evaluation -----------------------------------------------------------------------

def _looks_textual(blob: bytes) -> bool:
    """Whether scanning this for patterns is meaningful.

    A NUL byte in the first kilobyte is the same heuristic diff tools use, and
    it keeps the scanner off images and archives where a regex hit would be
    noise anyway.
    """
    return b"\x00" not in blob[:1024]


class _Clock:
    """The time an evaluation may still spend on patterns."""

    def __init__(self, budget: float):
        self.deadline = time.monotonic() + budget

    def left(self) -> float:
        return self.deadline - time.monotonic()


class _OutOfTime(Exception):
    def __init__(self, what: str):
        self.what = what


def _search(pattern, text: str, clock: _Clock, what: str):
    remaining = clock.left()
    if remaining <= 0:
        raise _OutOfTime(what)
    try:
        if isinstance(pattern, str):
            return regex.search(pattern, text, timeout=min(PATTERN_TIMEOUT, remaining))
        return pattern.search(text, timeout=min(PATTERN_TIMEOUT, remaining))
    except TimeoutError:
        raise _OutOfTime(what)


def evaluate(
    config: Dict[str, Any],
    commit_data: Dict[str, Any],
    pusher: Optional[User],
    repository_name: Optional[str] = None,
    branch: Optional[str] = None,
    *,
    deleted: Optional[List[str]] = None,
    message_rules: bool = True,
    new_branch: bool = False,
) -> List[str]:
    """Apply one rule set. Returns every reason to reject; empty means accept.

    ``commit_data["files"]`` holds the files the write adds or modifies; ``deleted``
    lists the paths it removes.
    """
    try:
        return _evaluate(config, commit_data, pusher, repository_name, branch,
                         deleted or [], message_rules, new_branch, _Clock(EVALUATION_BUDGET))
    except _OutOfTime as exc:
        # Fail closed: a rule that cannot finish has not said yes.
        return [f"{exc.what} took too long to evaluate; simplify the pattern"]


def _evaluate(config, commit_data, pusher, repository_name, branch, deleted,
              message_rules, new_branch, clock) -> List[str]:
    reasons: List[str] = []
    message = (commit_data.get("message") or "").strip()
    files: Dict[str, Any] = commit_data.get("files") or {}
    username = getattr(pusher, "username", None)

    if message_rules:
        minimum = config.get("commit_message_min_length")
        if minimum and len(message) < minimum:
            reasons.append(
                f"commit message is {len(message)} characters; at least {minimum} required"
            )
        pattern = config.get("commit_message_pattern")
        if pattern and not _search(pattern, message, clock, "commit_message_pattern"):
            reasons.append(f"commit message does not match required pattern {pattern!r}")

    name_pattern = config.get("branch_name_pattern")
    if new_branch and branch and name_pattern and not _search(
            name_pattern, branch, clock, "branch_name_pattern"):
        reasons.append(f"branch name '{branch}' does not match required pattern {name_pattern!r}")

    forbidden = config.get("forbidden_paths") or []
    if forbidden and files:
        matcher = IgnoreMatcher.from_text("\n".join(forbidden))
        hits = [path for path in files if matcher.is_ignored(path)]
        if hits:
            reasons.append(
                "path(s) not allowed by policy: " + ", ".join(sorted(hits)[:10])
            )

    protected = config.get("protected_paths") or []
    touched = list(files) + list(deleted)
    if protected and touched:
        allowed = {u.lower() for u in (config.get("protected_paths_allowed_users") or [])}
        # No allow-list means the paths are protected from everyone, which is a
        # coherent way to freeze generated files.
        if not username or username.lower() not in allowed:
            matcher = IgnoreMatcher.from_text("\n".join(protected))
            hits = [path for path in touched if matcher.is_ignored(path)]
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

    if config.get("block_secrets") and files:
        secret_files = IgnoreMatcher.from_text("\n".join(SECRET_FILES))
        named = [path for path in files if secret_files.is_ignored(path)]
        if named:
            reasons.append("secret file(s) may not be committed: " + ", ".join(sorted(named)[:10]))

    patterns = config.get("forbidden_content") or []
    scan_secrets = bool(config.get("block_secrets"))
    if (patterns or scan_secrets) and files:
        for path, payload in files.items():
            blob = _decode(payload)
            if blob is None or len(blob) > MAX_SCAN_BYTES or not _looks_textual(blob):
                continue
            text = blob.decode("utf-8", errors="replace")
            hit = None
            for raw in patterns:
                if _search(raw, text, clock, f"forbidden_content pattern {raw!r}"):
                    hit = f"'{path}' matches forbidden content pattern {raw!r}"
                    break
            if hit is None and scan_secrets:
                for label, compiled in _SECRET_REGEXES:
                    if _search(compiled, text, clock, "block_secrets"):
                        hit = f"'{path}' appears to contain {label}"
                        break
            if hit:
                reasons.append(hit)  # one reason per file is enough to act on

    url = config.get("external_url")
    if url:
        reasons.extend(_consult_external(
            url, config.get("external_secret"), commit_data, username, repository_name,
            branch, deleted,
        ))

    return reasons


def _consult_external(
    url: str,
    secret: Optional[str],
    commit_data: Dict[str, Any],
    username: Optional[str],
    repository_name: Optional[str],
    branch: Optional[str],
    deleted: List[str],
) -> List[str]:
    """Ask an external policy service. Only file *paths* are sent, never contents.

    A policy endpoint has no business receiving source code, and sending it would
    quietly turn every hook into an exfiltration channel. The request goes through the
    outbound rules and, with ``external_secret``, is signed like a webhook.
    """
    changed = sorted((commit_data.get("files") or {}).keys())
    payload = {
        "repository": repository_name,
        "branch": branch,
        "pusher": username,
        "commit": {
            "id": commit_data.get("id"),
            "message": commit_data.get("message"),
            "author": commit_data.get("author"),
            "paths": changed,
            "changed_paths": changed,
            "deleted_paths": sorted(deleted),
        },
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "Zanbeel-Push-Rules/1.1"}
    if secret:
        import hmac
        headers[SIGNATURE_HEADER] = "sha256=" + hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    try:
        response = outbound.post(url, data=body, headers=headers, timeout=EXTERNAL_TIMEOUT)
    except outbound.OutboundBlocked as exc:
        return [f"policy service not called: {exc.message}; push refused"]
    except Exception as exc:
        # Fail closed. A pre-receive hook exists to stop things; treating an
        # unreachable policy service as approval would make it trivially
        # bypassable by knocking the service over.
        return [f"policy service {url} is unreachable ({exc.__class__.__name__}); push refused"]

    if 300 <= response.status_code < 400:
        return [f"policy service {url} answered with a redirect, which is not followed; push refused"]
    if response.status_code >= 500:
        return [f"policy service {url} returned {response.status_code}; push refused"]

    try:
        answer = response.json()
    except ValueError:
        return [f"policy service {url} returned a non-JSON response; push refused"]

    if isinstance(answer, dict) and answer.get("allow") is True:
        return []

    detail = (answer.get("message") or answer.get("reason")) if isinstance(answer, dict) else None
    return [str(detail or "rejected by policy service")[:300]]


def _apply(db, repository, commit_data, pusher, branch, *, deleted, message_rules, new_branch):
    for hook in hooks_for(db, getattr(repository, "id", None), HOOK_PRE_RECEIVE):
        try:
            config = json.loads(hook.config_json or "{}")
        except ValueError:
            # A corrupt rule set must not silently wave pushes through.
            raise HookRejection(hook.name, ["hook configuration is not valid JSON"])

        reasons = evaluate(
            config, commit_data, pusher,
            repository_name=getattr(repository, "name", None), branch=branch,
            deleted=deleted, message_rules=message_rules, new_branch=new_branch,
        )
        if reasons:
            raise HookRejection(hook.name, reasons)


def run_pre_receive(
    db: Session,
    repository,
    commit_data: Dict[str, Any],
    pusher: Optional[User],
    branch: Optional[str] = None,
    *,
    new_branch: bool = False,
) -> None:
    """Apply every push rule to a push. Raises HookRejection on the first that refuses.

    The push's snapshot is compared with its parent, so the rules see what the commit
    changes. Stopping at the first failing hook is deliberate: hooks are independent
    policies, and reporting "you also violated the other four" is noise when the
    first one already blocks the push.
    """
    if not hooks_for(db, getattr(repository, "id", None), HOOK_PRE_RECEIVE):
        return
    parents = commit_data.get("parents") or [commit_data.get("parent")]
    parent = next((p for p in parents if p), None)
    changes = change_set(db, parent, commit_data.get("files") or {})
    data = dict(commit_data)
    data["files"] = changes.changed
    _apply(db, repository, data, pusher, branch, deleted=changes.deleted,
           message_rules=True, new_branch=new_branch)


def check_server_write(
    db: Session,
    repository,
    branch: str,
    base_commit_id: Optional[str],
    new_tree: Dict[str, Any],
    actor: Optional[User],
    *,
    commit_id: Optional[str] = None,
    message: Optional[str] = None,
) -> None:
    """Apply the content and path rules to a write the server makes on someone's behalf.

    Called before the commit is created, by every path that puts new content on a
    branch without a push. ``actor`` is whoever asked for it, so protected paths are
    judged against the person merging, not the person who wrote the change.
    """
    if not hooks_for(db, getattr(repository, "id", None), HOOK_PRE_RECEIVE):
        return
    changes = change_set(db, base_commit_id, new_tree)
    if not changes.changed and not changes.deleted:
        return
    data = {"id": commit_id, "message": message, "author": getattr(actor, "username", None),
            "files": changes.changed}
    _apply(db, repository, data, actor, branch, deleted=changes.deleted,
           message_rules=False, new_branch=False)


def check_server_change(
    db: Session,
    repository,
    branch: str,
    old_tree: Dict[str, bytes],
    new_tree: Dict[str, bytes],
    actor: Optional[User],
) -> None:
    """``check_server_write`` for a change between two in-memory trees.

    For writes that produce several commits at once (a rebase): each step is checked
    against the one before it, all before anything is written.
    """
    if not hooks_for(db, getattr(repository, "id", None), HOOK_PRE_RECEIVE):
        return
    changes = ChangeSet(
        changed={path: data for path, data in new_tree.items() if old_tree.get(path) != data},
        deleted=sorted(path for path in old_tree if path not in new_tree),
    )
    if not changes.changed and not changes.deleted:
        return
    data = {"author": getattr(actor, "username", None), "files": changes.changed}
    _apply(db, repository, data, actor, branch, deleted=changes.deleted,
           message_rules=False, new_branch=False)


def check_new_branch(db: Session, repository, branch: str, actor: Optional[User]) -> None:
    """Apply branch_name_pattern to a branch being created outside a push."""
    hooks = hooks_for(db, getattr(repository, "id", None), HOOK_PRE_RECEIVE)
    for hook in hooks:
        try:
            config = json.loads(hook.config_json or "{}")
        except ValueError:
            raise HookRejection(hook.name, ["hook configuration is not valid JSON"])
        if not config.get("branch_name_pattern"):
            continue
        reasons = evaluate({"branch_name_pattern": config["branch_name_pattern"]}, {}, actor,
                           branch=branch, message_rules=False, new_branch=True)
        if reasons:
            raise HookRejection(hook.name, reasons)


def serialize(hook: ServerHook) -> dict:
    try:
        config = json.loads(hook.config_json or "{}")
    except ValueError:
        config = {}
    has_external_secret = bool(config.pop("external_secret", None))
    return {
        "id": hook.id,
        "repository_id": hook.repository_id,
        "name": hook.name,
        "hook_type": hook.hook_type,
        "enabled": bool(hook.enabled),
        "config": config,
        "has_external_secret": has_external_secret,
        # A post-receive hook from before they were refused: it has never run.
        "never_runs": hook.hook_type != HOOK_PRE_RECEIVE,
        "created_at": hook.created_at.isoformat() if hook.created_at else None,
        "updated_at": hook.updated_at.isoformat() if hook.updated_at else None,
    }
