"""In-app notification feed."""

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.crud import NotificationCRUD
from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user
from app.schemas import MarkNotificationsReadRequest


router = APIRouter()


@router.get("/api/notifications")
async def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List notifications for the current user."""
    notifications = NotificationCRUD.list_notifications(
        db,
        user_id=current_user.id,
        unread_only=unread_only,
        limit=limit
    )
    
    return {
        "success": True,
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
        "updated": updated_count
    }
