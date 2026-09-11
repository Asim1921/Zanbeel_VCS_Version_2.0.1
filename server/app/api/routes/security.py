"""Security operations: lockout visibility and backups.

Admin-only. Both surfaces name accounts and expose the shape of an attack, so
they carry the same standing as the rest of the admin API.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import BackupRecord, User

from app.core.dependencies import require_admin_user
from app.services import backup as backup_service
from app.services import rate_limit
from app.db.schema_check import inspect_schema


router = APIRouter()


class UnlockRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=150)


class BackupRequest(BaseModel):
    label: Optional[str] = Field(None, max_length=120)


@router.get("/api/admin/security/lockouts")
async def get_lockouts(
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Locked accounts, the active policy, and where failures are coming from."""
    rate_limit.prune(db)
    return {"success": True, **rate_limit.status(db)}


@router.post("/api/admin/security/unlock")
async def clear_lockout(
    request: UnlockRequest,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Clear a lock early. Also clears the failures that produced it, so the
    account does not re-lock on the next mistake."""
    was_locked = rate_limit.unlock(db, request.username)
    return {"success": True, "was_locked": was_locked, "username": request.username}


@router.get("/api/admin/backups")
async def list_backups(
    limit: int = Query(25, ge=1, le=200),
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Backups taken so far, newest first, with their verification result."""
    rows = (
        db.query(BackupRecord)
        .order_by(BackupRecord.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "success": True,
        "backups": [backup_service.serialize(row) for row in rows],
        "destination": str(backup_service.backup_root()),
    }


@router.post("/api/admin/backups")
async def create_backup(
    request: BackupRequest,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Take a verified backup of the database and its blob store.

    Synchronous: an administrator asking for a backup wants to know whether it
    worked, and a backup whose result nobody checked is not a backup.
    """
    try:
        record = backup_service.create_backup(
            db, label=request.label, actor_id=current_user.id
        )
    except backup_service.BackupError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return {"success": True, "backup": backup_service.serialize(record)}


@router.post("/api/admin/backups/{backup_id}/verify")
async def verify_backup(
    backup_id: int,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Re-check an existing backup against its manifest."""
    record = db.query(BackupRecord).filter(BackupRecord.id == backup_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Backup not found")
    try:
        result = backup_service.verify_backup(record.path)
    except backup_service.BackupError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)
    return {"success": True, "verification": result}


@router.get("/api/admin/schema")
async def get_schema_health(
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Whether the live schema matches the models.

    The same check the server runs at startup, exposed so drift introduced after
    boot -- someone dropping a table by hand, a half-applied migration -- is
    visible without reading logs.
    """
    return {"success": True, "schema": inspect_schema()}
