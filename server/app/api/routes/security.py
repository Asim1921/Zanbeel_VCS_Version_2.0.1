"""Security operations: lockouts, backups, restore drills and schema health.

Admin-only. These surfaces name accounts, expose the shape of an attack and the
server's file layout, so they carry the same standing as the rest of the admin API.

Backups, verification and restore drills run as background jobs: each reads every blob
back, which takes minutes on a real store, and a request that held the event loop that
long would stall every other user. The POST answers 202 with the job's record; poll
``GET /api/admin/backups/{id}``. ``?wait=true`` runs it to completion first, for scripts.
Only one maintenance job runs at a time; a second answers 409.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import BackupRecord, User

from app.core.dependencies import require_admin_user
from app.core.pagination import page_params, paginate_query, set_page_headers
from app.services import audit
from app.services import backup as backup_service
from app.services import rate_limit
from app.services.maintenance import MaintenanceBusy
from app.db import upgrade as schema_upgrade
from app.db.schema_check import inspect_schema


router = APIRouter()


class UnlockRequest(BaseModel):
    username: Optional[str] = Field(None, max_length=150)
    ip_address: Optional[str] = Field(None, max_length=64)


class BackupRequest(BaseModel):
    label: Optional[str] = Field(None, max_length=120)
    test_restore: bool = False


@router.get("/api/admin/security/lockouts")
async def get_lockouts(
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Locked accounts, throttled addresses, the active policy, and where failures come from."""
    rate_limit.prune(db)
    return {"success": True, **rate_limit.status(db)}


@router.post("/api/admin/security/unlock")
async def clear_lockout(
    request: UnlockRequest,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Clear a lock early, by account, by address, or both.

    Also clears the failures that produced it, so the account does not re-lock on the
    next mistake, and resets the escalation, so a later lock starts short again.
    """
    username = (request.username or "").strip() or None
    ip = (request.ip_address or "").strip() or None
    if not username and not ip:
        raise HTTPException(status_code=400, detail="Name an account (username) or an address (ip_address)")
    result = rate_limit.unlock(db, username=username, ip=ip)
    what = " and ".join(part for part in (f"'{username}'" if username else None,
                                          f"address {ip}" if ip else None) if part)
    audit.record(db, current_user.id, "account_unlocked",
                 description=f"Cleared sign-in limits for {what}",
                 target_type="user" if username else "address", target_id=username or ip,
                 payload={"username": username, "ip_address": ip, **result})
    return {"success": True, "username": username, "ip_address": ip, **result}


# --- backups ----------------------------------------------------------------------------

def _record_or_404(db: Session, backup_id: int) -> BackupRecord:
    record = db.query(BackupRecord).filter(BackupRecord.id == backup_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Backup not found")
    return record


def _busy(exc: MaintenanceBusy) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


@router.get("/api/admin/backups")
def list_backups(
    request: Request,
    response: Response,
    page=Depends(page_params(default=25)),
    include_pruned: bool = False,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Backups newest first, with their verification and restore-drill results, and the
    state of the schedule: when the last verified backup was taken and whether that is
    too long ago."""
    query = db.query(BackupRecord)
    if not include_pruned:
        query = query.filter(BackupRecord.status != "pruned")
    rows, meta = paginate_query(
        query.order_by(BackupRecord.created_at.desc(), BackupRecord.id.desc()), *page)
    set_page_headers(request, response, meta)
    summary = backup_service.summary(db)
    return {
        "success": True,
        "backups": [backup_service.serialize(row) for row in rows],
        "pagination": meta,
        "destination": summary["destination"],
        "summary": summary,
    }


@router.post("/api/admin/backups", status_code=202)
def create_backup(
    request: BackupRequest,
    response: Response,
    wait: bool = False,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Take a backup of the database, its blobs and generated documents, verified by
    reading every blob back. ``test_restore`` follows it with a restore drill."""
    try:
        record = backup_service.start_backup(
            db, label=request.label, actor_id=current_user.id, kind="manual",
            then_test_restore=request.test_restore, wait=wait)
    except MaintenanceBusy as exc:
        raise _busy(exc)
    except backup_service.BackupError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    if wait:
        response.status_code = 200
    return {"success": True, "backup": backup_service.serialize(record)}


@router.get("/api/admin/backups/{backup_id}")
def get_backup(
    backup_id: int,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """One backup, including the reports of its latest verification and restore drill."""
    return {"success": True, "backup": backup_service.serialize(_record_or_404(db, backup_id))}


@router.post("/api/admin/backups/{backup_id}/verify", status_code=202)
def verify_backup(
    backup_id: int,
    response: Response,
    wait: bool = False,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Re-read the backup against its manifest: the database checksum and integrity, and
    every blob decoded and re-hashed."""
    record = _record_or_404(db, backup_id)
    try:
        record = backup_service.start_verify(db, record, actor_id=current_user.id, wait=wait)
    except MaintenanceBusy as exc:
        raise _busy(exc)
    except backup_service.BackupError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    if wait:
        response.status_code = 200
    backup = backup_service.serialize(record)
    return {"success": True, "backup": backup, "verification": backup.get("verify_report")}


@router.post("/api/admin/backups/{backup_id}/restore-test", status_code=202)
def restore_test_backup(
    backup_id: int,
    response: Response,
    wait: bool = False,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Restore the backup into a scratch directory, upgrade it to this server's schema,
    and read it back. Touches nothing live; the real restore is a CLI step."""
    record = _record_or_404(db, backup_id)
    try:
        record = backup_service.start_restore_test(db, record, actor_id=current_user.id, wait=wait)
    except MaintenanceBusy as exc:
        raise _busy(exc)
    except backup_service.BackupError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    if wait:
        response.status_code = 200
    return {"success": True, "backup": backup_service.serialize(record)}


# --- schema -----------------------------------------------------------------------------

@router.get("/api/admin/schema")
async def get_schema_health(
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Whether the live schema matches the models and is at this code's revision.

    The same check the server runs at startup, exposed so drift introduced after
    boot -- someone dropping a table by hand, a half-applied migration -- is
    visible without reading logs. Includes the upgrades applied so far.
    """
    return {
        "success": True,
        "schema": inspect_schema(db.get_bind()),
        "migrations": schema_upgrade.recent_runs(db, limit=10),
    }
