"""User administration and engagement reporting."""

from datetime import datetime

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.crud import ActivityCRUD, UserCRUD, UserEngagementCRUD
from database.database import get_db
from database.models import User

from app.core.dependencies import get_current_user, require_admin_user
from app.core.pagination import paginate_query
from app.core.security import hash_password


router = APIRouter()


@router.get("/api/users")
async def list_users(
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List users, newest last. Paged so the response stays bounded."""
    try:
        users, meta = paginate_query(db.query(User).order_by(User.id), limit, offset)
        return {
            "success": True,
            "pagination": meta,
            "users": [
                {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "full_name": user.full_name,
                    "role": user.role if hasattr(user, 'role') else 'developer',
                    "team_lead_id": user.team_lead_id if hasattr(user, 'team_lead_id') else None,
                    "team_lead_name": user.team_lead.username if hasattr(user, 'team_lead') and user.team_lead else None,
                    "created_at": user.created_at.isoformat() if user.created_at else None,
                    "last_login_at": user.last_login_at.isoformat() if getattr(user, "last_login_at", None) else None,
                    "is_active": user.is_active,
                    "repository_count": len(user.repositories)
                }
                for user in users
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.get("/api/admin/users/engagement")
async def get_users_engagement(
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Admin: per-user engagement metrics (status, login, work, commits, score)."""
    try:
        users = UserEngagementCRUD.get_all_user_engagement(db)
        summary = {
            "active": sum(1 for u in users if u["status"] == "active"),
            "idle": sum(1 for u in users if u["status"] == "idle"),
            "never_used": sum(1 for u in users if u["status"] == "never_used"),
            "total": len(users),
        }
        return {"success": True, "summary": summary, "users": users}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.get("/api/admin/users/{username}/detail")
async def get_user_engagement_detail(
    username: str,
    limit: int = 100,
    current_user: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    """Admin: user overview + activity feed + history timeline for detail tabs."""
    try:
        detail = UserEngagementCRUD.get_user_detail(db, username, limit=min(max(limit, 1), 250))
        if not detail:
            raise HTTPException(status_code=404, detail="User not found")
        return {"success": True, **detail}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.post("/api/users/create")
async def create_user(request: dict, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Create a new user"""
    try:
        username = request.get("username")
        email = request.get("email")
        full_name = request.get("full_name")
        password = request.get("password")
        role = request.get("role", "developer")  # Default to 'developer'
        team_lead_id = request.get("team_lead_id")  # ID of the team lead (for developers)
        
        if not username:
            raise HTTPException(status_code=400, detail="Username is required")
        
        if role not in ['developer', 'team_lead']:
            raise HTTPException(status_code=400, detail="Role must be 'developer' or 'team_lead'")
        
        # If role is developer and team_lead_id is provided, validate the team lead exists
        if role == 'developer' and team_lead_id:
            team_lead = UserCRUD.get_user_by_id(db, team_lead_id)
            if not team_lead:
                raise HTTPException(status_code=400, detail="Team lead user not found")
            if team_lead.role != 'team_lead':
                raise HTTPException(status_code=400, detail="Selected user is not a team lead")
        
        # Check if user already exists
        existing_user = UserCRUD.get_user_by_username(db, username)
        if existing_user:
            raise HTTPException(status_code=400, detail="Username already exists")
        
        password_hash = hash_password(password) if password else None
        user = UserCRUD.create_user(db, username, email, full_name, role, team_lead_id, password_hash=password_hash)
        
        return {
            "success": True,
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role,
                "team_lead_id": user.team_lead_id,
                "created_at": user.created_at.isoformat() if user.created_at else None
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.put("/api/users/{username}")
async def update_user(username: str, request: dict, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Update user information"""
    try:
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        is_self = current_user.username == username
        is_admin = getattr(current_user, "role", "developer") in ["team_lead", "admin"]
        if not is_self and not is_admin:
            raise HTTPException(status_code=403, detail="You can only update your own profile")
        
        if "email" in request:
            user.email = request["email"]
        if "full_name" in request:
            user.full_name = request["full_name"]
        if "is_active" in request:
            if not is_admin:
                raise HTTPException(status_code=403, detail="Only admin/team lead can change active status")
            user.is_active = request["is_active"]
        
        db.commit()
        db.refresh(user)
        
        return {
            "success": True,
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "full_name": user.full_name,
                "is_active": user.is_active
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.delete("/api/users/{username}")
async def delete_user(username: str, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Delete a user"""
    try:
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Check if user owns any repositories
        if len(user.repositories) > 0:
            raise HTTPException(
                status_code=400, 
                detail=f"Cannot delete user who owns repositories. User owns {len(user.repositories)} repository(ies)."
            )
        
        db.delete(user)
        db.commit()
        
        return {
            "success": True,
            "message": f"User {username} deleted successfully"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.post("/api/users/reset-password")
async def reset_user_password(request: dict, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Reset a user's password (admin only)"""
    try:
        username = request.get("username")
        new_password = request.get("new_password")
        
        if not username:
            raise HTTPException(status_code=400, detail="Username is required")
        
        if not new_password:
            raise HTTPException(status_code=400, detail="New password is required")
        
        if len(new_password) < 4:
            raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
        
        # Find user
        user = UserCRUD.get_user_by_username(db, username)
        if not user:
            raise HTTPException(status_code=404, detail=f"User '{username}' not found")
        
        # Hash new password
        new_hash = hash_password(new_password)
        
        # Update password
        user.password_hash = new_hash
        user.updated_at = datetime.now()
        db.commit()
        
        # Log activity
        ActivityCRUD.create_activity(
            db,
            user_id=current_user.id,
            activity_type="password_reset",
            description=f"Reset password for user: {username}"
        )
        
        return {
            "success": True,
            "message": f"Password reset successful for user '{username}'",
            "username": username
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
