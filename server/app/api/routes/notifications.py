"""In-app notification feed."""

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from database.crud import NotificationCRUD
from database.database import get_db
from database.models import Notification, User

from app.core.dependencies import get_current_user
from app.schemas import MarkNotificationsReadRequest


router = APIRouter()


def _unread_count(db: Session, user_id: int) -> int:
    return db.query(func.count(Notification.id)).filter(
        Notification.user_id == user_id,
        Notification.is_read == False,  # noqa: E712
    ).scalar() or 0


@router.get("/api/notifications/unread-count")
async def unread_notification_count(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """How many unread notifications there are.

    Counted here rather than derived from a page of results: the badge was computed
    from the most recent 50, so past 50 unread it quietly under-reported.
    """
    return {"success": True, "unread": _unread_count(db, current_user.id)}


@router.get("/api/notifications")
async def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    type: str = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List notifications for the current user."""
    query = db.query(Notification).filter(Notification.user_id == current_user.id)
    if unread_only:
        query = query.filter(Notification.is_read == False)  # noqa: E712
    if type:
        query = query.filter(Notification.notification_type == type)

    total = query.count()
    notifications = (
        query.order_by(Notification.created_at.desc())
        .offset(max(0, offset))
        .limit(max(1, min(limit, 200)))
        .all()
    )

    return {
        "success": True,
        "total": total,
        "offset": max(0, offset),
        "unread": _unread_count(db, current_user.id),
        "types": sorted({
            row[0] for row in db.query(Notification.notification_type)
            .filter(Notification.user_id == current_user.id).distinct().all()
            if row[0]
        }),
        "notifications": [
            {
                "id": n.id,
                "type": n.notification_type,
                "title": n.title,
                "body": n.body,
                "payload": json.loads(n.payload_json) if n.payload_json else {},
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat() if n.created_at else None,
                "read_at": n.read_at.isoformat() if n.read_at else None
            }
            for n in notifications
        ]
    }

@router.post("/api/notifications/mark-read")
async def mark_notifications_read(
    request: MarkNotificationsReadRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mark notifications as read."""
    if not request.ids:
        raise HTTPException(status_code=400, detail="ids required")
    
    updated_count = NotificationCRUD.mark_read(
        db,
        user_id=current_user.id,
        notification_ids=request.ids
    )
    
    return {
        "success": True,
        "updated": updated_count,
        "unread": _unread_count(db, current_user.id),
    }


@router.post("/api/notifications/mark-all-read")
async def mark_all_notifications_read(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mark every unread notification as read."""
    from datetime import datetime

    updated = db.query(Notification).filter(
        Notification.user_id == current_user.id,
        Notification.is_read == False,  # noqa: E712
    ).update(
        {"is_read": True, "read_at": datetime.utcnow()},
        synchronize_session=False,
    )
    db.commit()
    return {"success": True, "updated": updated, "unread": 0}


@router.post("/api/notifications/{notification_id}/unread")
async def mark_notification_unread(
    notification_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Put one notification back in the unread pile.

    Marking something read by opening it is only safe if it can be undone; otherwise
    people avoid opening things they mean to deal with later.
    """
    row = db.query(Notification).filter(
        Notification.id == notification_id,
        Notification.user_id == current_user.id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    row.is_read = False
    row.read_at = None
    db.commit()
    return {"success": True, "unread": _unread_count(db, current_user.id)}
