"""Manual resolution of conflicted pull request merges.

The merge engine detects conflicts and stops. Until now that was the end of the road:
there was no way to settle one, so a conflicted pull request could only be abandoned or
resolved by pushing over it. This module parks the three sides of every conflicted file
in a session, lets a human supply the resolved content, and then completes the merge.

The session records the exact commits the conflict was computed against. If either branch
moves while someone is resolving, completing the merge is refused rather than silently
merging their decisions against content they never saw -- which is the failure mode that
makes manual conflict resolution dangerous.
"""

import base64
import hashlib
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database.crud import ActivityCRUD, BranchCRUD, CommitCRUD, FileObjectCRUD, PullRequestCRUD
from database.models import (
    FileObject, MergeConflictFile, MergeConflictSession, PullRequest, User,
)

from app.services.commit_graph import _find_merge_base, _get_commit_tree
from app.services.merge import _merge_trees, _merge_text, _try_decode_text
from app.services import refs
from app.services.signing import sign_commit


class ConflictError(Exception):
    def __init__(self, message: str, status_code: int = 400, payload: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


def _blob(db: Session, file_hash: Optional[str]) -> Optional[bytes]:
    if not file_hash:
        return None
    row = db.query(FileObject).filter(FileObject.hash == file_hash).first()
    return row.content if row else None


def _b64(data: Optional[bytes]) -> Optional[str]:
    return None if data is None else base64.b64encode(data).decode("ascii")


def _store(db: Session, content: Optional[bytes]) -> Optional[str]:
    if content is None:
        return None
    return FileObjectCRUD.store_file_object(db, content).hash


def open_session(
    db: Session,
    repo_id: str,
    pr: PullRequest,
    actor: User,
) -> MergeConflictSession:
    """Compute the conflicts for a pull request and park them for manual resolution.

    Reuses an existing open session when the branches have not moved, so reopening the
    resolver does not discard work in progress.
    """
    source = BranchCRUD.get_branch(db, repo_id, pr.source_branch)
    target = BranchCRUD.get_branch(db, repo_id, pr.target_branch)
    if not source or not target:
        raise ConflictError("Branch not found", 404)

    base_id = _find_merge_base(db, target.head_commit_id, source.head_commit_id)
    base_tree = _get_commit_tree(db, base_id)
    ours_tree = _get_commit_tree(db, target.head_commit_id)
    theirs_tree = _get_commit_tree(db, source.head_commit_id)
    _merged, conflicts = _merge_trees(base_tree, ours_tree, theirs_tree)

    if not conflicts:
        raise ConflictError("This pull request merges cleanly; no resolution needed.", 409)

    existing = (
        db.query(MergeConflictSession)
        .filter(
            MergeConflictSession.pull_request_id == pr.id,
            MergeConflictSession.status == "open",
        )
        .first()
    )
    if existing:
        if (existing.source_head_commit_id == source.head_commit_id
                and existing.target_head_commit_id == target.head_commit_id):
            return existing
        # A branch moved: the parked sides are stale, so retire the session rather than
        # letting someone resolve against content that no longer exists.
        existing.status = "stale"
        db.flush()

    session = MergeConflictSession(
        repository_id=repo_id,
        pull_request_id=pr.id,
        source_branch=pr.source_branch,
        target_branch=pr.target_branch,
        base_commit_id=base_id or target.head_commit_id,
        source_head_commit_id=source.head_commit_id,
        target_head_commit_id=target.head_commit_id,
        status="open",
        created_by_id=actor.id,
    )
    db.add(session)
    db.flush()

    for path in sorted(conflicts):
        base_bytes = base_tree.get(path)
        ours_bytes = ours_tree.get(path)
        theirs_bytes = theirs_tree.get(path)
        binary = any(
            blob is not None and _try_decode_text(blob) is None
            for blob in (ours_bytes, theirs_bytes)
        )
        db.add(MergeConflictFile(
            session_id=session.id,
            file_path=path,
            base_file_hash=_store(db, base_bytes),
            ours_file_hash=_store(db, ours_bytes),
            theirs_file_hash=_store(db, theirs_bytes),
            is_binary=binary,
        ))

    db.commit()
    db.refresh(session)
    return session


def _is_stale(db: Session, session: MergeConflictSession) -> Optional[str]:
    """Whether either branch has moved since the session was opened."""
    source = BranchCRUD.get_branch(db, session.repository_id, session.source_branch)
    target = BranchCRUD.get_branch(db, session.repository_id, session.target_branch)
    if not source or not target:
        return "A branch involved in this merge no longer exists."
    if source.head_commit_id != session.source_head_commit_id:
        return f"'{session.source_branch}' has moved since this session was opened."
    if target.head_commit_id != session.target_head_commit_id:
        return f"'{session.target_branch}' has moved since this session was opened."
    return None


def get_bundle(db: Session, session: MergeConflictSession) -> Dict[str, Any]:
    """Everything the resolver UI needs: all three sides plus a suggested merge."""
    files: List[Dict[str, Any]] = []
    for row in sorted(session.files, key=lambda f: f.file_path):
        base_bytes = _blob(db, row.base_file_hash)
        ours_bytes = _blob(db, row.ours_file_hash)
        theirs_bytes = _blob(db, row.theirs_file_hash)

        suggested = None
        if not row.is_binary:
            # The conflict-marked merge, which is the usual starting point for editing.
            merged, _conflicted = _merge_text(base_bytes, ours_bytes, theirs_bytes)
            suggested = merged

        files.append({
            "path": row.file_path,
            "is_binary": bool(row.is_binary),
            "base_b64": _b64(base_bytes),
            "ours_b64": _b64(ours_bytes),
            "theirs_b64": _b64(theirs_bytes),
            "suggested_b64": _b64(suggested),
            "resolved": row.resolved_file_hash is not None,
        })

    return {
        "session": {
            "id": session.id,
            "status": session.status,
            "repository_id": session.repository_id,
            "pull_request_id": session.pull_request_id,
            "source_branch": session.source_branch,
            "target_branch": session.target_branch,
            "source_head_commit_id": session.source_head_commit_id,
            "target_head_commit_id": session.target_head_commit_id,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "stale_reason": _is_stale(db, session),
        },
        "files": files,
    }


def resolve(
    db: Session,
    session: MergeConflictSession,
    resolutions: Dict[str, str],
    actor: User,
    expected_head_commit_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply the supplied resolutions and complete the merge."""
    if session.status != "open":
        raise ConflictError(f"This session is {session.status}.", 409)

    stale = _is_stale(db, session)
    if stale:
        session.status = "stale"
        db.commit()
        raise ConflictError(
            f"{stale} Your resolutions were not applied -- reopen the resolver to see the "
            f"current conflicts.", 409, {"code": "SESSION_STALE"},
        )

    # The review gate applies here too: resolving conflicts must not be a way around it.
    pr_for_gate = db.query(PullRequest).filter(
        PullRequest.id == session.pull_request_id).first()
    if pr_for_gate is not None and pr_for_gate.status == "open":
        from app.services import pr_reviews

        repository = pr_for_gate.repository
        if repository is not None:
            try:
                pr_reviews.enforce_merge_gate(db, repository, pr_for_gate)
            except pr_reviews.ReviewError as exc:
                raise ConflictError(exc.message, exc.status_code, exc.payload)

    target = BranchCRUD.get_branch(db, session.repository_id, session.target_branch)
    if expected_head_commit_id is not None and expected_head_commit_id != target.head_commit_id:
        raise ConflictError(
            f"'{session.target_branch}' is at {(target.head_commit_id or 'none')[:12]}, "
            f"not {expected_head_commit_id[:12]}.", 409, {"code": "HEAD_MISMATCH"},
        )

    by_path = {row.file_path: row for row in session.files}
    unknown = sorted(set(resolutions) - set(by_path))
    if unknown:
        raise ConflictError(
            f"Not conflicted in this session: {', '.join(unknown[:5])}", 400
        )

    missing = sorted(path for path in by_path if path not in resolutions)
    if missing:
        raise ConflictError(
            f"{len(missing)} conflicted file(s) still unresolved: {', '.join(missing[:5])}",
            400, {"code": "INCOMPLETE", "unresolved": missing},
        )

    # Record each decision before touching the branch, so a failure part-way leaves the
    # session recoverable rather than half-applied.
    resolved_bytes: Dict[str, bytes] = {}
    for path, encoded in resolutions.items():
        try:
            content = base64.b64decode((encoded or "").encode("ascii"), validate=False)
        except Exception:
            raise ConflictError(f"Resolution for '{path}' is not valid base64.", 400)
        resolved_bytes[path] = content
        by_path[path].resolved_file_hash = _store(db, content)
    db.flush()

    # Rebuild the merged tree: the clean parts from the engine, the conflicted parts as
    # decided here.
    base_tree = _get_commit_tree(db, session.base_commit_id)
    ours_tree = _get_commit_tree(db, session.target_head_commit_id)
    theirs_tree = _get_commit_tree(db, session.source_head_commit_id)
    merged_tree, _conflicts = _merge_trees(base_tree, ours_tree, theirs_tree)
    for path, content in resolved_bytes.items():
        merged_tree[path] = content

    commit_id = hashlib.sha256(
        f"merge-resolved:{session.repository_id}:{session.id}:"
        f"{datetime.utcnow().isoformat()}".encode()
    ).hexdigest()[:40]

    entries = []
    for path, content in merged_tree.items():
        stored = FileObjectCRUD.store_file_object(db, content)
        entries.append(
            SimpleNamespace(file_path=path, file_hash=stored.hash, file_size=len(content))
        )

    commit = CommitCRUD.create_commit_from_file_hashes(
        db,
        {
            "id": commit_id,
            "repository_id": session.repository_id,
            "author": actor.username,
            "message": (
                f"Merge branch '{session.source_branch}' into '{session.target_branch}'"
                f"\n\nResolved {len(resolutions)} conflicting file(s)."
            ),
            "parents": [session.target_head_commit_id, session.source_head_commit_id],
            "timestamp": datetime.utcnow().isoformat(),
        },
        entries,
    )

    refs.update_reference_by_id(
        db, repository_id=session.repository_id, actor_username=actor.username,
        branch_name=session.target_branch, new_commit_id=commit.id,
        operation=refs.OP_MERGE,
    )
    sign_commit(db, commit, actor.username, actor.id)

    pr = db.query(PullRequest).filter(PullRequest.id == session.pull_request_id).first()
    if pr and pr.status == "open":
        PullRequestCRUD.mark_merged(db, pr, merge_commit_id=commit.id, reviewer_id=actor.id)

    session.status = "resolved"
    db.commit()

    ActivityCRUD.create_activity(
        db, actor.id, "resolve_merge_conflicts",
        f"Resolved {len(resolutions)} conflict(s) merging '{session.source_branch}' into "
        f"'{session.target_branch}' as {commit.id[:8]}",
        session.repository_id,
    )

    return {
        "status": "merged",
        "merge_commit_id": commit.id,
        "resolved_files": len(resolutions),
        "session_id": session.id,
    }


def abort(db: Session, session: MergeConflictSession, actor: User) -> Dict[str, Any]:
    """Discard a resolution session without touching either branch."""
    if session.status != "open":
        raise ConflictError(f"This session is already {session.status}.", 409)
    session.status = "aborted"
    db.commit()
    return {"status": "aborted", "session_id": session.id}
