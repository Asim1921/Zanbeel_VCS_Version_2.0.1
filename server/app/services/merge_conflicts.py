"""Manual resolution of conflicted merges, done the way Git and GitHub do it.

The merge engine detects conflicts and stops. A session parks the three sides of every
conflicted file, lets people decide each one -- saved as they go, so progress survives a
closed browser and can be shared -- and then writes the merge commit.

**Where the resolution lands** follows GitHub:

* For a **pull request**, the resolution is committed to the pull request's own *source*
  branch as "Merge branch 'main' into 'feature'", exactly as GitHub's "Resolve conflicts"
  button does. Writing it straight onto the target, as this module used to, let content
  nobody had reviewed land under approvals given to something else: the source branch
  never moved, so those approvals stayed current. Now the source moves, the existing
  stale-approval rule asks reviewers to look again, and the pull request merges through
  the one ordinary merge path -- the one that sends webhooks and notifications and closes
  linked issues.
* For a **direct branch merge** there is no pull request to review it through, so the
  resolution lands on the target, as ``git merge`` and a push would. The target's own
  review rules still apply.

The session records the exact commits the conflict was computed against. If either branch
moves while someone is resolving, the session goes stale and completing it is refused,
rather than applying their decisions to content they never saw.
"""

import base64
import hashlib
from collections import OrderedDict
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from database.crud import ActivityCRUD, BranchCRUD, CommitCRUD, FileObjectCRUD, RepositoryCRUD
from database.models import (
    FileObject, MergeConflictFile, MergeConflictSession, PullRequest, User,
)

from app.services.commit_graph import _find_merge_base, _get_commit_tree
from app.services.merge import (
    CONFLICT_MODIFY_DELETE, _merge_trees, _try_decode_text, conflict_kind,
    has_conflict_markers, merge_text,
)
from app.services import pr_source, refs
from app.services.signing import sign_commit

RESOLUTION_CONTENT = "content"
RESOLUTION_DELETE = "delete"

LANDS_ON_SOURCE = "source"
LANDS_ON_TARGET = "target"


class ConflictError(Exception):
    def __init__(self, message: str, status_code: int = 400, payload: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}


# --- small helpers ---------------------------------------------------------------

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


def _decode_b64(path: str, encoded: Optional[str]) -> bytes:
    try:
        # Whitespace is tolerated (wrapped base64 is common); anything else is refused
        # rather than silently dropped, which would store a corrupted file.
        compact = "".join((encoded or "").split())
        return base64.b64decode(compact.encode("ascii"), validate=True)
    except Exception:
        raise ConflictError(f"Resolution for '{path}' is not valid base64.", 400,
                            {"code": "INVALID_CONTENT", "path": path})


def source_repository_id(session: MergeConflictSession) -> str:
    return session.source_repository_id or session.repository_id


def lands_on(session: MergeConflictSession) -> str:
    """Which side of the merge the resolution commit is written to."""
    return LANDS_ON_SOURCE if session.pull_request_id else LANDS_ON_TARGET


def commit_destination(session: MergeConflictSession) -> Tuple[str, str]:
    """(repository id, branch) the resolution commit will be written to."""
    if lands_on(session) == LANDS_ON_SOURCE:
        return source_repository_id(session), session.source_branch
    return session.repository_id, session.target_branch


def labels(session: MergeConflictSession) -> Tuple[str, str, str]:
    """Marker labels: ours is the target branch, theirs the source, as in the tables."""
    return session.target_branch, "base", session.source_branch


def _is_resolved(row: MergeConflictFile) -> bool:
    # Rows written before drafts existed carry a hash but no resolution.
    return row.resolution is not None or row.resolved_file_hash is not None


# --- opening ---------------------------------------------------------------------

def open_session(
    db: Session,
    repo_id: str,
    pr: PullRequest,
    actor: User,
) -> MergeConflictSession:
    """Park a pull request's conflicts for manual resolution."""
    return _open(
        db, repo_id, actor,
        source_branch=pr.source_branch,
        target_branch=pr.target_branch,
        pr=pr,
        source_repo_id=pr_source.repository_id(pr),
    )


def open_branch_session(
    db: Session,
    repo_id: str,
    actor: User,
    source_branch: str,
    target_branch: str,
) -> MergeConflictSession:
    """Park a direct branch merge's conflicts for manual resolution.

    The same three sides as a pull request conflict, without one. A conflicted
    `fox merge` previously reported the conflict and stopped, leaving the person who
    hit it no way forward on the server at all.
    """
    return _open(
        db, repo_id, actor,
        source_branch=source_branch,
        target_branch=target_branch,
        pr=None,
        source_repo_id=repo_id,
    )


def _open(
    db: Session,
    repo_id: str,
    actor: User,
    *,
    source_branch: str,
    target_branch: str,
    pr: Optional[PullRequest] = None,
    source_repo_id: Optional[str] = None,
) -> MergeConflictSession:
    """Compute the conflicts between two branches and park all three sides.

    Reuses an existing open session when neither branch has moved, so reopening the
    resolver keeps the decisions already saved; retires it when one has, because the
    parked sides then describe content that is no longer there.
    """
    source_repo_id = source_repo_id or repo_id
    source = BranchCRUD.get_branch(db, source_repo_id, source_branch)
    target = BranchCRUD.get_branch(db, repo_id, target_branch)
    if not source or not target:
        raise ConflictError("Branch not found", 404, {"code": "BRANCH_NOT_FOUND"})

    base_id = _find_merge_base(db, target.head_commit_id, source.head_commit_id)
    base_tree = _get_commit_tree(db, base_id)
    ours_tree = _get_commit_tree(db, target.head_commit_id)
    theirs_tree = _get_commit_tree(db, source.head_commit_id)
    _merged, conflicts = _merge_trees(base_tree, ours_tree, theirs_tree)

    if not conflicts:
        subject = "This pull request" if pr else f"'{source_branch}'"
        raise ConflictError(
            f"{subject} merges cleanly into '{target_branch}'; no resolution needed.", 409,
            {"code": "NO_CONFLICTS"},
        )

    query = db.query(MergeConflictSession).filter(
        MergeConflictSession.repository_id == repo_id,
        MergeConflictSession.status == "open",
    )
    if pr:
        query = query.filter(MergeConflictSession.pull_request_id == pr.id)
    else:
        # A branch session is identified by the pair it reconciles, since it has no
        # pull request to name it.
        query = query.filter(
            MergeConflictSession.pull_request_id.is_(None),
            MergeConflictSession.source_branch == source_branch,
            MergeConflictSession.target_branch == target_branch,
        )
    existing = query.first()

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
        pull_request_id=pr.id if pr else None,
        source_repository_id=source_repo_id if source_repo_id != repo_id else None,
        source_branch=source_branch,
        target_branch=target_branch,
        base_commit_id=base_id,
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
            conflict_kind=conflict_kind(base_bytes, ours_bytes, theirs_bytes),
        ))

    db.commit()
    db.refresh(session)
    return session


# --- reading ---------------------------------------------------------------------

def _is_stale(db: Session, session: MergeConflictSession) -> Optional[str]:
    """Whether either branch has moved since the session was opened."""
    source = BranchCRUD.get_branch(db, source_repository_id(session), session.source_branch)
    target = BranchCRUD.get_branch(db, session.repository_id, session.target_branch)
    if not source or not target:
        return "A branch involved in this merge no longer exists."
    if source.head_commit_id != session.source_head_commit_id:
        return f"'{session.source_branch}' has moved since this session was opened."
    if target.head_commit_id != session.target_head_commit_id:
        return f"'{session.target_branch}' has moved since this session was opened."
    return None


def describe(db: Session, session: MergeConflictSession) -> Dict[str, Any]:
    """The session as a client lists it: who, which branches, how far along."""
    rows = list(session.files)
    resolved = sum(1 for row in rows if _is_resolved(row))
    dest_repo, dest_branch = commit_destination(session)
    return {
        "id": session.id,
        "status": session.status,
        "repository_id": session.repository_id,
        "source_repository_id": source_repository_id(session),
        "pull_request_id": session.pull_request_id,
        "source_branch": session.source_branch,
        "target_branch": session.target_branch,
        "base_commit_id": session.base_commit_id,
        "source_head_commit_id": session.source_head_commit_id,
        "target_head_commit_id": session.target_head_commit_id,
        # Where "Commit merge" will write, so a client can say so before anyone presses it.
        "lands_on": lands_on(session),
        "commit_repository_id": dest_repo,
        "commit_branch": dest_branch,
        "files": len(rows),
        "resolved": resolved,
        "unresolved": len(rows) - resolved,
        "created_by": session.created_by.username if session.created_by else None,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "result_commit_id": session.result_commit_id,
        "stale_reason": _is_stale(db, session) if session.status == "open" else None,
    }


def get_bundle(db: Session, session: MergeConflictSession) -> Dict[str, Any]:
    """Everything a resolver needs: all three sides, a suggested merge, saved decisions."""
    marker_labels = labels(session)
    files: List[Dict[str, Any]] = []
    for row in sorted(session.files, key=lambda f: f.file_path):
        base_bytes = _blob(db, row.base_file_hash)
        ours_bytes = _blob(db, row.ours_file_hash)
        theirs_bytes = _blob(db, row.theirs_file_hash)
        kind = row.conflict_kind or conflict_kind(base_bytes, ours_bytes, theirs_bytes)

        suggested: Optional[bytes] = None
        hunks: List[Dict[str, Any]] = []
        if kind == CONFLICT_MODIFY_DELETE:
            # The surviving side, as Git leaves it in the work tree. The choice to make
            # is keep or delete, not which lines.
            suggested = ours_bytes if ours_bytes is not None else theirs_bytes
        elif not row.is_binary:
            result = merge_text(base_bytes, ours_bytes, theirs_bytes, marker_labels)
            suggested, hunks = result.content, result.hunks

        resolved_bytes = (
            _blob(db, row.resolved_file_hash)
            if row.resolution in (None, RESOLUTION_CONTENT) and row.resolved_file_hash
            else None
        )
        resolution = row.resolution or (RESOLUTION_CONTENT if row.resolved_file_hash else None)

        files.append({
            "path": row.file_path,
            "kind": kind,
            "is_binary": bool(row.is_binary),
            # Which side removed the file, for a modify/delete conflict.
            "deleted_in": (
                ("target" if ours_bytes is None else "source")
                if kind == CONFLICT_MODIFY_DELETE else None
            ),
            "base_b64": _b64(base_bytes),
            "ours_b64": _b64(ours_bytes),
            "theirs_b64": _b64(theirs_bytes),
            "suggested_b64": _b64(suggested),
            "hunks": hunks,
            "conflict_count": len(hunks),
            "resolved": _is_resolved(row),
            "resolution": resolution,
            "resolved_b64": _b64(resolved_bytes),
            "resolved_has_markers": has_conflict_markers(resolved_bytes),
            "resolved_by": _username(db, row.resolved_by_id),
            "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        })

    return {
        "session": describe(db, session),
        # "ours" and "theirs" are the stored column names; these say which branch each is.
        "sides": {
            "ours": {"role": "target", "branch": session.target_branch},
            "theirs": {"role": "source", "branch": session.source_branch},
        },
        "files": files,
    }


def _username(db: Session, user_id: Optional[int]) -> Optional[str]:
    if not user_id:
        return None
    user = db.query(User).filter(User.id == user_id).first()
    return user.username if user else None


def list_open(db: Session, repository_id: str, *, pull_request_id: Optional[int] = None,
              branch_only: bool = False) -> List[Dict[str, Any]]:
    """Open sessions, newest first, so an interrupted resolution can be found again."""
    query = db.query(MergeConflictSession).filter(
        MergeConflictSession.repository_id == repository_id,
        MergeConflictSession.status == "open",
    )
    if pull_request_id is not None:
        query = query.filter(MergeConflictSession.pull_request_id == pull_request_id)
    elif branch_only:
        query = query.filter(MergeConflictSession.pull_request_id.is_(None))
    return [describe(db, row) for row in query.order_by(MergeConflictSession.id.desc()).all()]


# --- deciding, one file at a time -------------------------------------------------

def _refuse_if_not_workable(db: Session, session: MergeConflictSession) -> None:
    if session.status != "open":
        raise ConflictError(f"This session is {session.status}.", 409,
                            {"code": "SESSION_CLOSED", "status": session.status})
    stale = _is_stale(db, session)
    if stale:
        session.status = "stale"
        db.commit()
        raise ConflictError(
            f"{stale} Nothing was saved -- reopen the resolver to see the current "
            f"conflicts.", 409, {"code": "SESSION_STALE"},
        )


def save_resolution(
    db: Session,
    session: MergeConflictSession,
    path: str,
    actor: User,
    *,
    content_b64: Optional[str] = None,
    delete: bool = False,
    clear: bool = False,
) -> Dict[str, Any]:
    """Record (or withdraw) the decision for one conflicted file without merging."""
    _refuse_if_not_workable(db, session)

    row = next((f for f in session.files if f.file_path == path), None)
    if row is None:
        raise ConflictError(f"Not conflicted in this session: {path}", 404,
                            {"code": "NOT_CONFLICTED", "path": path})

    content: Optional[bytes] = None
    if clear:
        row.resolution = None
        row.resolved_file_hash = None
        row.resolved_by_id = None
        row.resolved_at = None
    else:
        if delete:
            row.resolution = RESOLUTION_DELETE
            row.resolved_file_hash = None
        else:
            if content_b64 is None:
                raise ConflictError("Supply content_b64, delete or clear.", 400,
                                    {"code": "INVALID_CONTENT", "path": path})
            content = _decode_b64(path, content_b64)
            row.resolution = RESOLUTION_CONTENT
            row.resolved_file_hash = _store(db, content)
        row.resolved_by_id = actor.id
        row.resolved_at = datetime.utcnow()

    db.commit()
    db.refresh(session)
    summary = describe(db, session)
    return {
        "path": path,
        "resolution": row.resolution,
        # Saving a draft that still has markers is allowed -- it is a draft -- but say so,
        # because completing the merge will refuse it.
        "has_markers": has_conflict_markers(content),
        "resolved": summary["resolved"],
        "unresolved": summary["unresolved"],
    }


# --- completing ------------------------------------------------------------------

def _final_decisions(
    session: MergeConflictSession,
    resolutions: Optional[Dict[str, Optional[str]]],
    db: Session,
) -> Dict[str, Optional[bytes]]:
    """Saved decisions overlaid with any sent now. None means delete the file."""
    by_path = {row.file_path: row for row in session.files}
    resolutions = resolutions or {}

    unknown = sorted(set(resolutions) - set(by_path))
    if unknown:
        raise ConflictError(
            f"Not conflicted in this session: {', '.join(unknown[:5])}", 400,
            {"code": "NOT_CONFLICTED", "paths": unknown},
        )

    decided: Dict[str, Optional[bytes]] = {}
    for path, row in by_path.items():
        if row.resolution == RESOLUTION_DELETE:
            decided[path] = None
        elif _is_resolved(row):
            saved = _blob(db, row.resolved_file_hash)
            # A saved decision whose bytes cannot be read is not a decision -- and must
            # never be mistaken for None, which means "delete".
            if saved is not None:
                decided[path] = saved

    for path, encoded in resolutions.items():
        # An explicit null is the API's spelling of "delete this file".
        decided[path] = None if encoded is None else _decode_b64(path, encoded)

    missing = sorted(path for path in by_path if path not in decided)
    if missing:
        raise ConflictError(
            f"{len(missing)} conflicted file(s) still unresolved: {', '.join(missing[:5])}",
            400, {"code": "INCOMPLETE", "unresolved": missing},
        )
    return decided


def resolve(
    db: Session,
    session: MergeConflictSession,
    resolutions: Optional[Dict[str, Optional[str]]],
    actor: User,
    expected_head_commit_id: Optional[str] = None,
    allow_markers: bool = False,
) -> Dict[str, Any]:
    """Apply every decision and write the merge commit.

    ``resolutions`` may be partial or empty: files already decided through
    ``save_resolution`` count. ``expected_head_commit_id`` guards the branch the commit
    is written to (``describe()['commit_branch']``).
    """
    if session.status != "open":
        raise ConflictError(f"This session is {session.status}.", 409,
                            {"code": "SESSION_CLOSED", "status": session.status})

    stale = _is_stale(db, session)
    if stale:
        session.status = "stale"
        db.commit()
        raise ConflictError(
            f"{stale} Your resolutions were not applied -- reopen the resolver to see the "
            f"current conflicts.", 409, {"code": "SESSION_STALE"},
        )

    pr = (
        db.query(PullRequest).filter(PullRequest.id == session.pull_request_id).first()
        if session.pull_request_id else None
    )
    if session.pull_request_id and (pr is None or pr.status != "open"):
        raise ConflictError(
            f"Pull request #{session.pull_request_id} is "
            f"{pr.status if pr else 'gone'}; there is nothing left to resolve.", 409,
            {"code": "PULL_REQUEST_CLOSED"},
        )

    decided = _final_decisions(session, resolutions, db)

    # A marker left in by mistake would land as source code. Refuse unless the caller
    # says the file genuinely contains marker-like text.
    marked = sorted(path for path, content in decided.items() if has_conflict_markers(content))
    if marked and not allow_markers:
        raise ConflictError(
            f"{len(marked)} file(s) still contain conflict markers: {', '.join(marked[:5])}. "
            f"Remove them, or confirm the markers are intended.", 409,
            {"code": "UNRESOLVED_MARKERS", "paths": marked},
        )

    dest_repo_id, dest_branch = commit_destination(session)
    toward_source = lands_on(session) == LANDS_ON_SOURCE
    dest_head = session.source_head_commit_id if toward_source else session.target_head_commit_id
    other_head = session.target_head_commit_id if toward_source else session.source_head_commit_id
    merged_in = session.target_branch if toward_source else session.source_branch

    if expected_head_commit_id is not None and expected_head_commit_id != dest_head:
        raise ConflictError(
            f"'{dest_branch}' is at {(dest_head or 'none')[:12]}, "
            f"not {expected_head_commit_id[:12]}.", 409,
            {"code": "HEAD_MISMATCH", "branch": dest_branch, "current_commit": dest_head},
        )

    # The review rules of whichever branch receives the commit. For a direct branch merge
    # that is the target, so resolving is not a way around its review requirement. For a
    # pull request it is the source branch: GitHub likewise will not resolve conflicts
    # onto a protected head branch from the browser.
    from app.services import merge_gate

    dest_repository = RepositoryCRUD.get_repository(db, dest_repo_id)
    if dest_repository is not None:
        try:
            merge_gate.enforce(
                db,
                repository=dest_repository,
                target_branch=dest_branch,
                source_branch=merged_in,
                actor=actor,
                pull_request=None,
                operation="resolve",
            )
        except merge_gate.MergeGateError as exc:
            raise ConflictError(exc.message, exc.status_code, exc.payload)

    # Rebuild the merged tree: the clean parts from the engine, the conflicted parts as
    # decided here.
    base_tree = _get_commit_tree(db, session.base_commit_id)
    ours_tree = _get_commit_tree(db, session.target_head_commit_id)
    theirs_tree = _get_commit_tree(db, session.source_head_commit_id)
    merged_tree, _conflicts = _merge_trees(base_tree, ours_tree, theirs_tree)
    deleted: List[str] = []
    for path, content in decided.items():
        if content is None:
            merged_tree.pop(path, None)
            deleted.append(path)
        else:
            merged_tree[path] = content

    # Keep the decisions on the rows, so the session records what was chosen.
    for row in session.files:
        content = decided[row.file_path]
        if content is None:
            row.resolution, row.resolved_file_hash = RESOLUTION_DELETE, None
        else:
            row.resolution, row.resolved_file_hash = RESOLUTION_CONTENT, _store(db, content)
        row.resolved_by_id = row.resolved_by_id or actor.id
        row.resolved_at = row.resolved_at or datetime.utcnow()
    db.flush()

    entries = []
    for path, content in merged_tree.items():
        stored = FileObjectCRUD.store_file_object(db, content)
        entries.append(
            SimpleNamespace(file_path=path, file_hash=stored.hash, file_size=len(content))
        )

    # Ask the branch before writing: create_commit_from_file_hashes() commits its own
    # transaction, so refusing afterwards would leave the merge commit stored on a
    # branch that never accepted it.
    refs.precheck_by_id(
        db, repository_id=dest_repo_id, actor_username=actor.username,
        branch_name=dest_branch, operation=refs.OP_MERGE,
    )

    commit_id = hashlib.sha256(
        f"merge-resolved:{dest_repo_id}:{session.id}:{datetime.utcnow().isoformat()}".encode()
    ).hexdigest()[:40]
    summary = f"Resolved {len(decided)} conflicting file(s)."
    if deleted:
        summary += f" Deleted: {', '.join(sorted(deleted)[:10])}."
    commit = CommitCRUD.create_commit_from_file_hashes(
        db,
        {
            "id": commit_id,
            "repository_id": dest_repo_id,
            "author": actor.username,
            "message": f"Merge branch '{merged_in}' into '{dest_branch}'\n\n{summary}",
            # First parent is the branch being written to, as for any merge.
            "parents": [dest_head, other_head],
            "timestamp": datetime.utcnow().isoformat(),
        },
        entries,
    )

    refs.update_reference_by_id(
        db, repository_id=dest_repo_id, actor_username=actor.username,
        branch_name=dest_branch, new_commit_id=commit.id,
        operation=refs.OP_MERGE, expected_commit_id=dest_head,
    )
    sign_commit(db, commit, actor.username, actor.id)

    session.status = "resolved"
    session.result_commit_id = commit.id
    session.resolved_by_id = actor.id
    session.resolved_at = datetime.utcnow()
    db.commit()

    if toward_source:
        description = (
            f"Resolved {len(decided)} conflict(s) in PR #{session.pull_request_id} by merging "
            f"'{session.target_branch}' into '{session.source_branch}' as {commit.id[:8]}"
        )
    else:
        description = (
            f"Resolved {len(decided)} conflict(s) merging '{session.source_branch}' into "
            f"'{session.target_branch}' as {commit.id[:8]}"
        )
    ActivityCRUD.create_activity(
        db, actor.id, "resolve_merge_conflicts", description, session.repository_id,
    )

    result: Dict[str, Any] = {
        # A pull request is not merged yet: its branch now contains the resolution and
        # it merges through the normal path, after any re-review.
        "status": "resolved" if toward_source else "merged",
        "merge_commit_id": commit.id,
        "lands_on": lands_on(session),
        "commit_repository_id": dest_repo_id,
        "commit_branch": dest_branch,
        "resolved_files": len(decided),
        "deleted_files": sorted(deleted),
        "session_id": session.id,
        "pull_request_id": session.pull_request_id,
    }
    if toward_source:
        result["next_step"] = (
            f"'{session.source_branch}' now merges cleanly into '{session.target_branch}'. "
            f"Approvals given before this commit are stale; once the pull request is "
            f"approved again it can be merged."
        )
    return result


def abort(db: Session, session: MergeConflictSession, actor: User) -> Dict[str, Any]:
    """Discard a resolution session without touching either branch."""
    if session.status != "open":
        raise ConflictError(f"This session is already {session.status}.", 409,
                            {"code": "SESSION_CLOSED", "status": session.status})
    session.status = "aborted"
    db.commit()
    return {"status": "aborted", "session_id": session.id}


# --- will it merge? ----------------------------------------------------------------

# Mergeability depends only on the two heads, so the answer is cached per pair: listing
# pull requests should not rebuild every tree on every refresh.
_MERGEABILITY_CACHE: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
_MERGEABILITY_CACHE_SIZE = 256


def _merge_preview(db: Session, source_head: str, target_head: str) -> Dict[str, Any]:
    key = (source_head, target_head)
    cached = _MERGEABILITY_CACHE.get(key)
    if cached is not None:
        _MERGEABILITY_CACHE.move_to_end(key)
        return cached

    base_id = _find_merge_base(db, target_head, source_head)
    if base_id == source_head:
        preview = {"conflicts": [], "base_commit_id": base_id, "up_to_date": True}
    elif base_id == target_head:
        # Target is an ancestor: the merge takes the source as it stands.
        preview = {"conflicts": [], "base_commit_id": base_id, "up_to_date": False}
    else:
        _merged, conflicts = _merge_trees(
            _get_commit_tree(db, base_id),
            _get_commit_tree(db, target_head),
            _get_commit_tree(db, source_head),
        )
        preview = {"conflicts": sorted(conflicts), "base_commit_id": base_id,
                   "up_to_date": False}

    _MERGEABILITY_CACHE[key] = preview
    while len(_MERGEABILITY_CACHE) > _MERGEABILITY_CACHE_SIZE:
        _MERGEABILITY_CACHE.popitem(last=False)
    return preview


def mergeability(db: Session, repository_id: str, pr: PullRequest) -> Dict[str, Any]:
    """Whether a pull request would merge cleanly, without merging it."""
    source = pr_source.branch(db, pr)
    target = BranchCRUD.get_branch(db, repository_id, pr.target_branch)
    answer: Dict[str, Any] = {
        "pull_request_id": pr.id,
        "source_branch": pr.source_branch,
        "target_branch": pr.target_branch,
        "source_repository_id": pr_source.repository_id(pr),
        "source_head_commit_id": source.head_commit_id if source else None,
        "target_head_commit_id": target.head_commit_id if target else None,
        "conflicts": [],
        "open_session_id": None,
    }

    if pr.status != "open":
        answer.update(state=pr.status, mergeable=False)
        return answer
    if not source or not target or not source.head_commit_id or not target.head_commit_id:
        answer.update(state="unknown", mergeable=None,
                      reason="The source or target branch no longer exists.")
        return answer

    preview = _merge_preview(db, source.head_commit_id, target.head_commit_id)
    answer["base_commit_id"] = preview["base_commit_id"]
    answer["conflicts"] = list(preview["conflicts"])

    if preview["conflicts"]:
        answer.update(state="conflicts", mergeable=False)
        session = (
            db.query(MergeConflictSession)
            .filter(
                MergeConflictSession.pull_request_id == pr.id,
                MergeConflictSession.status == "open",
                MergeConflictSession.source_head_commit_id == source.head_commit_id,
                MergeConflictSession.target_head_commit_id == target.head_commit_id,
            )
            .order_by(MergeConflictSession.id.desc())
            .first()
        )
        answer["open_session_id"] = session.id if session else None
    elif preview["up_to_date"]:
        answer.update(state="up_to_date", mergeable=True)
    else:
        answer.update(state="clean", mergeable=True)
    return answer
