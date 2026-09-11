"""Commit attestation: a tamper-evident record of what was pushed, by whom.

Commit ids are content hashes, which detects accidental corruption but proves nothing
about origin -- anyone able to write to the database can fabricate a commit and its id.
This module adds a signature the server computes over each accepted commit, covering:

  * the commit's identity and message
  * its parents
  * a digest of its complete file list (path + content hash for every file)
  * the *authenticated* user who pushed it, and when

The signature uses HMAC-SHA256 keyed with ``FOXNEST_AUTH_SECRET``. That is symmetric, so
it does not prove authorship to a third party the way a GPG signature would; what it does
prove is that a commit and its contents are exactly as the server accepted them. Editing
history directly in the database -- swapping a file, rewriting a message, reassigning an
author -- invalidates the signature and ``verify_commit`` reports precisely which part
changed.

The key never leaves the server, so a client cannot forge an attestation. Rotating
``FOXNEST_AUTH_SECRET`` invalidates existing signatures; ``verify_commit`` reports that as
``unverifiable`` rather than as tampering, since the two are genuinely different failures.
"""

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.config import AUTH_SECRET
from database.models import Commit, CommitFile, CommitSignature

ALGORITHM = "hmac-sha256-v1"


def _tree_digest(db: Session, commit_id: str) -> str:
    """Digest over the commit's complete file list.

    Sorted so it is stable, and covering both path and content hash so that neither
    swapping a file's contents nor renaming it can go unnoticed.
    """
    rows = (
        db.query(CommitFile.file_path, CommitFile.file_hash)
        .filter(CommitFile.commit_id == commit_id)
        .order_by(CommitFile.file_path)
        .all()
    )
    hasher = hashlib.sha256()
    for path, file_hash in rows:
        hasher.update(b"\x00")
        hasher.update((path or "").encode("utf-8"))
        hasher.update(b"\x01")
        hasher.update((file_hash or "").encode("utf-8"))
    hasher.update(f"\x02{len(rows)}".encode("utf-8"))
    return hasher.hexdigest()


def _parent_ids(db: Session, commit: Commit) -> List[str]:
    parents: List[str] = []
    if commit.parent_commit_id:
        parents.append(commit.parent_commit_id)
    for link in sorted(commit.parent_links or [], key=lambda l: getattr(l, "parent_order", 0)):
        if link.parent_commit_id and link.parent_commit_id not in parents:
            parents.append(link.parent_commit_id)
    return parents


def build_payload(db: Session, commit: Commit, pusher_username: str, signed_at: str) -> str:
    """Canonical JSON covering everything the signature commits to.

    sort_keys and fixed separators keep it byte-stable, which is what makes the signature
    reproducible on verification.
    """
    return json.dumps(
        {
            "v": 1,
            "commit_id": commit.id,
            "repository_id": commit.repository_id,
            "author": commit.author.username if commit.author else None,
            "message": commit.message or "",
            "parents": _parent_ids(db, commit),
            "tree_digest": _tree_digest(db, commit.id),
            "pusher": pusher_username,
            "signed_at": signed_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _sign(payload: str) -> str:
    return hmac.new(
        AUTH_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def sign_commit(
    db: Session,
    commit: Commit,
    pusher_username: str,
    pusher_id: Optional[int] = None,
) -> Optional[CommitSignature]:
    """Record an attestation for a freshly accepted commit.

    Best-effort by design: a signing failure must not reject a push that has otherwise
    been accepted, because that would make the whole server unusable if the secret were
    misconfigured. An unsigned commit simply verifies as ``unsigned``.
    """
    try:
        signed_at = datetime.utcnow().isoformat()
        payload = build_payload(db, commit, pusher_username, signed_at)

        existing = (
            db.query(CommitSignature).filter(CommitSignature.commit_id == commit.id).first()
        )
        if existing:
            return existing

        record = CommitSignature(
            commit_id=commit.id,
            repository_id=commit.repository_id,
            pusher_username=pusher_username,
            pusher_id=pusher_id,
            algorithm=ALGORITHM,
            signed_at_text=signed_at,
            signature=_sign(payload),
            tree_digest_at_signing=_tree_digest(db, commit.id),
            author_at_signing=commit.author.username if commit.author else None,
            message_digest_at_signing=hashlib.sha256(
                (commit.message or "").encode("utf-8")
            ).hexdigest(),
        )
        db.add(record)
        db.commit()
        return record
    except Exception:
        db.rollback()
        return None


def verify_commit(db: Session, commit_id: str) -> Dict[str, Any]:
    """Recompute a commit's signature from current state and compare.

    Returns a verdict plus, when it fails, which field no longer matches -- so the answer
    is "the file list changed" rather than an unhelpful "invalid".
    """
    commit = db.query(Commit).filter(Commit.id == commit_id).first()
    if not commit:
        return {"commit_id": commit_id, "status": "unknown",
                "detail": "No such commit."}

    record = db.query(CommitSignature).filter(CommitSignature.commit_id == commit_id).first()
    if not record:
        return {
            "commit_id": commit_id,
            "status": "unsigned",
            "detail": "This commit predates attestation, or signing was unavailable when "
                      "it was pushed. Absence of a signature is not evidence of tampering.",
        }

    if record.algorithm != ALGORITHM:
        return {"commit_id": commit_id, "status": "unverifiable",
                "detail": f"Signed with {record.algorithm}, which this server cannot check."}

    payload = build_payload(db, commit, record.pusher_username, record.signed_at_text)
    expected = _sign(payload)

    if hmac.compare_digest(expected, record.signature or ""):
        return {
            "commit_id": commit_id,
            "status": "valid",
            "pusher": record.pusher_username,
            "signed_at": record.signed_at_text,
            "algorithm": record.algorithm,
            "author": commit.author.username if commit.author else None,
        }

    # Narrow down what moved, so the report is actionable.
    changed = []
    current_tree = _tree_digest(db, commit.id)
    if record.tree_digest_at_signing and current_tree != record.tree_digest_at_signing:
        changed.append("file contents or file list")
    if record.author_at_signing and commit.author and \
            commit.author.username != record.author_at_signing:
        changed.append("author")
    if record.message_digest_at_signing:
        current_msg = hashlib.sha256((commit.message or "").encode("utf-8")).hexdigest()
        if current_msg != record.message_digest_at_signing:
            changed.append("commit message")

    return {
        "commit_id": commit_id,
        "status": "invalid",
        "changed": changed or ["unknown - the signing secret may have been rotated"],
        "detail": (
            "This commit no longer matches what the server signed. Either it was modified "
            "outside the API, or FOXNEST_AUTH_SECRET was rotated after it was pushed."
        ),
        "pusher": record.pusher_username,
        "signed_at": record.signed_at_text,
    }


def verify_repository(db: Session, repo_id: str, limit: int = 500) -> Dict[str, Any]:
    """Verify every commit in a repository and summarise."""
    commits = (
        db.query(Commit.id)
        .filter(Commit.repository_id == repo_id)
        .order_by(Commit.created_at.desc())
        .limit(limit)
        .all()
    )
    counts = {"valid": 0, "invalid": 0, "unsigned": 0, "unverifiable": 0, "unknown": 0}
    problems = []
    for (commit_id,) in commits:
        verdict = verify_commit(db, commit_id)
        counts[verdict["status"]] = counts.get(verdict["status"], 0) + 1
        if verdict["status"] == "invalid":
            problems.append(verdict)

    return {
        "repository_id": repo_id,
        "checked": len(commits),
        "truncated": len(commits) >= limit,
        **counts,
        "tampered": problems,
    }
