"""The activity log: who did what, to which repository, when, and with which credential.

What it replaces was a table written from about twenty-five places in free text. Most
ways a branch moves were never recorded -- branch creation and deletion, cherry-pick,
approving a queued commit -- because each route had to remember to write a row, and no
row said which commit or branch it was about, or whether the change came from a
password session, a CI token or an SSH key.

Two rules make it complete without every route having to remember:

* **Branch moves are recorded where branches move.** ``refs`` is the only component
  allowed to change a branch and calls ``ref_events.emit`` after every change, which
  calls ``record_ref_move`` here. Push, merge, cherry-pick, revert, rebase, rollback,
  conflict resolution, branch create/delete/rename: all of them, from any code path.
* **One change is one row.** A route that also describes what it did ("merged pull
  request #4") records that with ``record``. Within one request, the description and
  the branch move it caused are merged into a single row: the route's words, with the
  branch, before and after commits attached.

The request (and so the credential and client address) reaches this module through
``AuditContextMiddleware``, so service code deep below a route needs no extra
arguments. Writes outside a request -- background jobs -- are recorded without them.

Nothing here edits or deletes a row except retention pruning, which is off unless an
administrator sets ``FOXNEST_ACTIVITY_RETENTION_DAYS``.
"""

import contextvars
import csv
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session, joinedload

from database.models import Activity, Commit, Repository, User

logger = logging.getLogger(__name__)


# --- what each type means ----------------------------------------------------------

CATEGORY_CODE = "code"
CATEGORY_REVIEW = "review"
CATEGORY_ACCESS = "access"
CATEGORY_SETTINGS = "settings"
CATEGORY_REPOSITORY = "repository"

#: Every activity type, with how a person reads it. Served to clients so the audit
#: view never shows a raw identifier or offers a filter for a type that does not exist.
TYPES: Dict[str, Dict[str, str]] = {
    # code
    "push_commit": {"label": "Pushed", "category": CATEGORY_CODE},
    "branch_created": {"label": "Created a branch", "category": CATEGORY_CODE},
    "branch_deleted": {"label": "Deleted a branch", "category": CATEGORY_CODE},
    "branch_renamed": {"label": "Renamed a branch", "category": CATEGORY_CODE},
    "branch_updated": {"label": "Moved a branch", "category": CATEGORY_CODE},
    "branch_force_updated": {"label": "Force-updated a branch", "category": CATEGORY_CODE},
    "merge_branch": {"label": "Merged a branch", "category": CATEGORY_CODE},
    "publish_branch": {"label": "Fast-forwarded a branch", "category": CATEGORY_CODE},
    "copy_files": {"label": "Copied files between branches", "category": CATEGORY_CODE},
    "rollback_branch": {"label": "Rolled back a branch", "category": CATEGORY_CODE},
    "rollback_file": {"label": "Rolled back a file", "category": CATEGORY_CODE},
    "cherry_pick": {"label": "Cherry-picked", "category": CATEGORY_CODE},
    "revert": {"label": "Reverted", "category": CATEGORY_CODE},
    "rebase": {"label": "Rebased", "category": CATEGORY_CODE},
    "resolve_merge_conflicts": {"label": "Resolved conflicts", "category": CATEGORY_CODE},
    "delete_commit": {"label": "Removed a branch tip", "category": CATEGORY_CODE},
    # review
    "pending_commit": {"label": "Submitted for review", "category": CATEGORY_REVIEW},
    "approve_commit": {"label": "Approved a commit", "category": CATEGORY_REVIEW},
    "reject_commit": {"label": "Rejected a commit", "category": CATEGORY_REVIEW},
    "create_pull_request": {"label": "Opened a pull request", "category": CATEGORY_REVIEW},
    "review_pull_request": {"label": "Reviewed a pull request", "category": CATEGORY_REVIEW},
    "merge_pull_request": {"label": "Merged a pull request", "category": CATEGORY_REVIEW},
    "close_pull_request": {"label": "Closed a pull request", "category": CATEGORY_REVIEW},
    # repository
    "create_repository": {"label": "Created a repository", "category": CATEGORY_REPOSITORY},
    "request_repository": {"label": "Requested a repository", "category": CATEGORY_REPOSITORY},
    "fork_repository": {"label": "Forked a repository", "category": CATEGORY_REPOSITORY},
    "archive_repository": {"label": "Archived a repository", "category": CATEGORY_REPOSITORY},
    "delete_repository": {"label": "Deleted a repository", "category": CATEGORY_REPOSITORY},
    "tag_created": {"label": "Created a tag", "category": CATEGORY_REPOSITORY},
    "tag_deleted": {"label": "Deleted a tag", "category": CATEGORY_REPOSITORY},
    "release_created": {"label": "Published a release", "category": CATEGORY_REPOSITORY},
    "generate_docs": {"label": "Generated docs", "category": CATEGORY_REPOSITORY},
    "generate_project_docs": {"label": "Generated project docs", "category": CATEGORY_REPOSITORY},
    "gc": {"label": "Collected garbage", "category": CATEGORY_REPOSITORY},
    # settings
    "set_default_branch": {"label": "Changed the default branch", "category": CATEGORY_SETTINGS},
    "update_branch_policy": {"label": "Changed the branch policy", "category": CATEGORY_SETTINGS},
    "branch_policy_changed": {"label": "Changed branch protection", "category": CATEGORY_SETTINGS},
    "branch_protection_removed": {"label": "Removed branch protection", "category": CATEGORY_SETTINGS},
    "webhook_created": {"label": "Added a webhook", "category": CATEGORY_SETTINGS},
    "webhook_updated": {"label": "Changed a webhook", "category": CATEGORY_SETTINGS},
    "webhook_deleted": {"label": "Removed a webhook", "category": CATEGORY_SETTINGS},
    "webhook_secret_rotated": {"label": "Rotated a webhook secret", "category": CATEGORY_SETTINGS},
    "push_rule_created": {"label": "Added a push rule", "category": CATEGORY_SETTINGS},
    "push_rule_updated": {"label": "Changed a push rule", "category": CATEGORY_SETTINGS},
    "push_rule_deleted": {"label": "Removed a push rule", "category": CATEGORY_SETTINGS},
    # access
    "grant_permission": {"label": "Granted access", "category": CATEGORY_ACCESS},
    "revoke_permission": {"label": "Revoked access", "category": CATEGORY_ACCESS},
    "grant_access_to_issue": {"label": "Granted issue access", "category": CATEGORY_ACCESS},
    "deny_access_request": {"label": "Denied an access request", "category": CATEGORY_ACCESS},
    "token_created": {"label": "Created an access token", "category": CATEGORY_ACCESS},
    "token_revoked": {"label": "Revoked an access token", "category": CATEGORY_ACCESS},
    "ssh_key_added": {"label": "Added an SSH key", "category": CATEGORY_ACCESS},
    "ssh_key_removed": {"label": "Removed an SSH key", "category": CATEGORY_ACCESS},
    "credentials_revoked": {"label": "Revoked someone's credentials", "category": CATEGORY_ACCESS},
    "create_user": {"label": "Created a user", "category": CATEGORY_ACCESS},
    "update_user": {"label": "Changed a user", "category": CATEGORY_ACCESS},
    "delete_user": {"label": "Deleted a user", "category": CATEGORY_ACCESS},
    "password_reset": {"label": "Reset a password", "category": CATEGORY_ACCESS},
    "password_reset_otp_sent": {"label": "Sent a password reset code", "category": CATEGORY_ACCESS},
}

#: Descriptions of something that moves a branch. Recorded in the same request as that
#: move, the two become one row.
REF_ACTIONS = frozenset({
    "push_commit", "approve_commit", "merge_branch", "publish_branch", "copy_files",
    "rollback_branch", "rollback_file", "cherry_pick", "revert", "rebase",
    "resolve_merge_conflicts", "merge_pull_request", "delete_commit",
})


def label_of(activity_type: Optional[str]) -> str:
    entry = TYPES.get(activity_type or "")
    return entry["label"] if entry else (activity_type or "").replace("_", " ")


def category_of(activity_type: Optional[str]) -> Optional[str]:
    entry = TYPES.get(activity_type or "")
    return entry["category"] if entry else None


# --- the request this write belongs to ---------------------------------------------

class _RequestAudit:
    """What the audit log knows about the request in progress."""

    __slots__ = ("scope", "ref_rows", "action_rows", "renamed_from", "_credential")

    def __init__(self, scope):
        self.scope = scope
        self.ref_rows: List[list] = []      # [activity id, repository id, absorbed]
        self.action_rows: List[list] = []   # [activity id, repository id, has a ref]
        self.renamed_from: Dict[str, str] = {}
        self._credential = None

    def client_ip(self) -> Optional[str]:
        client = self.scope.get("client")
        return client[0] if client else None

    def credential(self) -> Optional[str]:
        if self._credential is None:
            state = self.scope.get("state") or {}
            scheme = state.get("auth_scheme")
            token = state.get("access_token")
            label = None
            try:
                if scheme == "session":
                    label = "session"
                elif scheme == "ssh_key":
                    name = getattr(token, "name", "") or ""
                    label = "ssh_key:" + name.replace("SSH sign-in: ", "", 1)
                elif scheme == "access_token":
                    label = "token:" + (getattr(token, "name", "") or "")
            except Exception:
                label = scheme
            self._credential = (label or "")[:120]
        return self._credential or None


_current: contextvars.ContextVar[Optional[_RequestAudit]] = contextvars.ContextVar(
    "zanbeel_audit_request", default=None,
)


class AuditContextMiddleware:
    """Make the current request visible to the audit log from any layer.

    Pure ASGI rather than BaseHTTPMiddleware: it only sets a context variable, and the
    route's threadpool calls copy the context, so they see the same request.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = _current.set(_RequestAudit(scope))
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)


def current_request_scope() -> Optional[dict]:
    """The ASGI scope of the request in progress, if any (for building public URLs)."""
    ctx = _current.get()
    return ctx.scope if ctx else None


# --- writing -----------------------------------------------------------------------

def _dumps(payload: Optional[Dict[str, Any]]) -> Optional[str]:
    if not payload:
        return None
    return json.dumps(payload, separators=(",", ":"), default=str)


def _loads(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _repository_name(db: Session, repository_id: Optional[str]) -> Optional[str]:
    if not repository_id:
        return None
    found = db.query(Repository.name).filter(Repository.id == repository_id).first()
    return found[0] if found else None


def _username(db: Session, user_id: Optional[int]) -> Optional[str]:
    if not user_id:
        return None
    found = db.query(User.username).filter(User.id == user_id).first()
    return found[0] if found else None


def _stamp(row: Activity, ctx: Optional[_RequestAudit]) -> None:
    if ctx is None:
        return
    row.credential = ctx.credential()
    row.client_ip = ctx.client_ip()


def record(
    db: Session,
    user_id: Optional[int],
    action: str,
    *,
    repository_id: Optional[str] = None,
    description: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[Any] = None,
    commit_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Activity]:
    """Record one action. Never raises: a change that happened must not fail on its log."""
    ctx = _current.get()
    try:
        if ctx is not None and action in REF_ACTIONS:
            row = _absorb_ref_row(db, ctx, repository_id)
            if row is not None:
                row.activity_type = action
                if description:
                    row.description = description
                if user_id:
                    row.user_id = user_id
                    row.actor_username = _username(db, user_id)
                if target_type:
                    row.target_type, row.target_id = target_type, str(target_id)[:200]
                if commit_id:
                    row.commit_id = commit_id
                if payload:
                    row.payload_json = _dumps({**_loads(row.payload_json), **payload})
                db.commit()
                return row

        row = Activity(
            user_id=user_id,
            repository_id=repository_id,
            repository_name=_repository_name(db, repository_id),
            actor_username=_username(db, user_id),
            activity_type=action,
            description=description,
            target_type=target_type,
            target_id=str(target_id)[:200] if target_id is not None else None,
            commit_id=commit_id,
            payload_json=_dumps(payload),
        )
        _stamp(row, ctx)
        db.add(row)
        db.commit()
        if ctx is not None and action in REF_ACTIONS:
            ctx.action_rows.append([row.id, repository_id, False])
        return row
    except Exception:
        logger.exception("Could not record activity %s", action)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _absorb_ref_row(db: Session, ctx: _RequestAudit, repository_id: Optional[str]) -> Optional[Activity]:
    """The latest branch move of this request in this repository not yet described."""
    for entry in reversed(ctx.ref_rows):
        if entry[1] == repository_id and not entry[2]:
            row = db.query(Activity).filter(Activity.id == entry[0]).first()
            if row is not None:
                entry[2] = True
                return row
    return None


def _short(commit_id: Optional[str]) -> str:
    return (commit_id or "none")[:8]


def record_ref_move(
    db: Session,
    *,
    repository: Repository,
    actor: Optional[User],
    branch: str,
    before: Optional[str],
    after: Optional[str],
    operation: str,
    created: bool = False,
    deleted: bool = False,
) -> None:
    """Record one completed branch change. Called by ``ref_events.emit``. Never raises."""
    from app.services.branch_protection import OP_FORCE_UPDATE, OP_RENAME

    ctx = _current.get()
    try:
        ref = {"branch": branch, "before": before, "after": after, "operation": operation}
        # A route's description absorbs the move, so keep what kind of move it was.
        if created:
            ref["created"] = True
        if deleted:
            ref["deleted"] = True

        if operation == OP_RENAME:
            # A rename arrives as a delete of the old name, then a create of the new.
            if deleted and ctx is not None:
                ctx.renamed_from[repository.id] = branch
                return
            if created:
                old = ctx.renamed_from.pop(repository.id, None) if ctx is not None else None
                ref["renamed_from"] = old
                _insert_ref_row(db, ctx, repository, actor, "branch_renamed",
                                f"Renamed branch '{old}' to '{branch}'" if old else f"Renamed a branch to '{branch}'",
                                branch, after, ref)
                return

        # A description recorded earlier in this request takes the move as its own.
        if ctx is not None:
            for entry in reversed(ctx.action_rows):
                if entry[1] == repository.id and not entry[2]:
                    row = db.query(Activity).filter(Activity.id == entry[0]).first()
                    if row is None:
                        continue
                    entry[2] = True
                    row.payload_json = _dumps({**_loads(row.payload_json), "ref": ref})
                    if not row.target_type:
                        row.target_type, row.target_id = "branch", branch
                    if not row.commit_id and after:
                        row.commit_id = after
                    db.commit()
                    return

        subject = None
        if after and not deleted:
            commit = db.query(Commit).filter(Commit.id == after).first()
            subject = (commit.message or "").splitlines()[0][:80] if commit and commit.message else None
        if created:
            action, text = "branch_created", f"Created branch '{branch}' at {_short(after)}"
        elif deleted:
            action, text = "branch_deleted", f"Deleted branch '{branch}' (was {_short(before)})"
        elif operation == OP_FORCE_UPDATE:
            action, text = "branch_force_updated", f"Force-updated '{branch}' {_short(before)} → {_short(after)}"
        else:
            action, text = "branch_updated", f"Moved '{branch}' {_short(before)} → {_short(after)}"
        if subject:
            text += f": {subject}"
        _insert_ref_row(db, ctx, repository, actor, action, text, branch, None if deleted else after, ref)
    except Exception:
        logger.exception("Could not record the move of %s:%s", getattr(repository, "id", "?"), branch)
        try:
            db.rollback()
        except Exception:
            pass


def _insert_ref_row(db, ctx, repository, actor, action, text, branch, commit_id, ref) -> None:
    payload = {"ref": ref}
    user_id = getattr(actor, "id", None)
    if not user_id:
        # Every row needs a person. A move with no actor (a background job) is
        # attributed to the repository owner and says so.
        user_id = repository.owner_id
        payload["system"] = True
    row = Activity(
        user_id=user_id,
        repository_id=repository.id,
        repository_name=repository.name,
        actor_username=getattr(actor, "username", None) or _username(db, user_id),
        activity_type=action,
        description=text,
        target_type="branch",
        target_id=branch,
        commit_id=commit_id,
        payload_json=_dumps(payload),
    )
    _stamp(row, ctx)
    db.add(row)
    db.commit()
    if ctx is not None:
        ctx.ref_rows.append([row.id, repository.id, False])


# --- reading -----------------------------------------------------------------------

def _parse_when(value: Optional[str], *, end: bool = False) -> Optional[datetime]:
    """An ISO date or date-time. A bare date as an upper bound means the end of that day."""
    if not value:
        return None
    text = value.strip().replace("Z", "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"'{value}' is not a date (use YYYY-MM-DD or an ISO date-time)")
    # Stored times are naive UTC; a time with an offset is converted, one without is
    # taken as UTC.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    if end and len(text) == 10:
        parsed += timedelta(days=1)
    return parsed


def _like(value: str) -> str:
    """A LIKE pattern matching ``value`` literally, for use with ESCAPE '\\'."""
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def build_query(
    db: Session,
    *,
    readable: Optional[set],
    viewer_id: Optional[int],
    all_account_events: bool,
    user: Optional[str] = None,
    repository_id: Optional[str] = None,
    types: Optional[Iterable[str]] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    text: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    commit_id: Optional[str] = None,
):
    """Activity rows this viewer may see, newest first, narrowed by the filters.

    ``readable`` is the set of repository ids the viewer can read (None: all). Rows
    with no repository -- account and administration events -- are visible only to
    the person they are about, unless ``all_account_events``.
    """
    from sqlalchemy import or_, and_

    q = db.query(Activity).options(joinedload(Activity.user), joinedload(Activity.repository))

    repo_rows = Activity.repository_id.isnot(None)
    if readable is not None:
        repo_rows = Activity.repository_id.in_(readable) if readable else Activity.repository_id.in_([""])
    account_rows = Activity.repository_id.is_(None)
    if not all_account_events:
        account_rows = and_(account_rows, Activity.user_id == viewer_id)
    q = q.filter(or_(repo_rows, account_rows))

    if user:
        q = q.join(User, Activity.user_id == User.id).filter(User.username.ilike(user.strip()))
    if repository_id:
        q = q.filter(Activity.repository_id == repository_id)
    wanted = [t for t in (types or []) if t]
    if wanted:
        q = q.filter(Activity.activity_type.in_(wanted))
    start, finish = _parse_when(since), _parse_when(until, end=True)
    if start:
        q = q.filter(Activity.created_at >= start)
    if finish:
        q = q.filter(Activity.created_at < finish)
    if text and text.strip():
        q = q.filter(Activity.description.ilike(_like(text.strip()), escape="\\"))
    if target_type:
        q = q.filter(Activity.target_type == target_type)
    if target_id:
        q = q.filter(Activity.target_id == str(target_id))
    if commit_id:
        q = q.filter(Activity.commit_id.like(commit_id.strip().lower() + "%"))
    return q.order_by(Activity.created_at.desc(), Activity.id.desc())


def serialize(row: Activity, *, include_ip: bool = False) -> Dict[str, Any]:
    payload = _loads(row.payload_json)
    out = {
        "id": row.id,
        "user": getattr(row.user, "username", None) or row.actor_username,
        "repository": getattr(row.repository, "name", None) or row.repository_name,
        "repository_id": row.repository_id,
        "activity_type": row.activity_type,
        "label": label_of(row.activity_type),
        "category": category_of(row.activity_type),
        "description": row.description,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "commit_id": row.commit_id,
        "payload": payload,
        "credential": row.credential,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if include_ip:
        out["client_ip"] = row.client_ip
    return out


CSV_COLUMNS = ("created_at", "user", "repository", "repository_id", "activity_type", "description",
               "target_type", "target_id", "commit_id", "credential", "client_ip")


def to_csv(rows: Iterable[Activity]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        item = serialize(row, include_ip=True)
        writer.writerow([
            # A leading = + - or @ makes a spreadsheet evaluate the cell; prefix it.
            ("'" + value) if isinstance(value, str) and value[:1] in ("=", "+", "-", "@") else value
            for value in (item.get(column) for column in CSV_COLUMNS)
        ])
    return buffer.getvalue()


def prune(db: Session) -> int:
    """Delete rows older than FOXNEST_ACTIVITY_RETENTION_DAYS. Off (0) by default."""
    try:
        days = int(os.getenv("FOXNEST_ACTIVITY_RETENTION_DAYS", "0") or 0)
    except ValueError:
        days = 0
    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    removed = db.query(Activity).filter(Activity.created_at < cutoff).delete(synchronize_session=False)
    db.commit()
    return removed
