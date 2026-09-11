"""Recent activity feed."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.crud import ActivityCRUD
from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user
from app.core.pagination import paginate_query


router = APIRouter()


@router.get("/api/activities")
async def get_recent_activities(
    limit: Optional[int] = 20,
    offset: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get recent activities.

    Requires authentication: the feed names users, repositories and commit subjects, so
    it should not be readable by anyone who can reach the port.
    """
    try:
        # Paged in the database rather than fetching every row and slicing:
        # this table only grows.
        from database.models import Activity
        query = db.query(Activity).order_by(Activity.created_at.desc())
        activities, meta = paginate_query(query, limit, offset)
        return {
            "success": True,
            "pagination": meta,
            "activities": [
                {
                    "id": activity.id,
                    "user": activity.user.username,
                    "repository": activity.repository.name if activity.repository else None,
                    "activity_type": activity.activity_type,
                    "description": activity.description,
                    "created_at": activity.created_at.isoformat() if activity.created_at else None
                }
                for activity in activities
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
