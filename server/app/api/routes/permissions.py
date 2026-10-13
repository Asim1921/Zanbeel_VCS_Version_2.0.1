"""Per-repository permission grants."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database.crud import UserPermissionCRUD
from database.database import get_db
from database.models import User

from app.core.dependencies import require_admin_user
from app.schemas import CreatePermissionRequest
from app.services import audit
from app.services import notifications as notify


def _granted(db, current_user, permission, verb):
    audit.record(
        db, current_user.id, "grant_permission", repository_id=permission.repository_id,
        description=f"{verb} {permission.user.username} {permission.permission_level} access",
        target_type="user", target_id=permission.user.username,
        payload={"permission_level": permission.permission_level},
    )
    notify.access_changed(db, permission.user, permission.repository,
                          permission.permission_level, current_user)


router = APIRouter()


@router.post("/api/permissions/create")
async def create_permission(request: CreatePermissionRequest, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Grant a user permission to a repository"""
    try:
        permission = UserPermissionCRUD.create_permission(
            db,
            request.username,
            request.repo_id,
            request.permission_level,
            current_user.username,
        )
        _granted(db, current_user, permission, "Granted")
        
        return {
            "success": True,
            "permission": {
                "user": permission.user.username,
                "repository": permission.repository.name,
                "permission_level": permission.permission_level,
                "granted_at": permission.granted_at.isoformat() if permission.granted_at else None
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.put("/api/permissions/update")
async def update_permission(request: CreatePermissionRequest, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Update a user's permission level for a repository"""
    try:
        permission = UserPermissionCRUD.create_permission(
            db,
            request.username,
            request.repo_id,
            request.permission_level,
            current_user.username,
        )
        _granted(db, current_user, permission, "Granted")
        
        return {
            "success": True,
            "permission": {
                "user": permission.user.username,
                "repository": permission.repository.name,
                "permission_level": permission.permission_level,
                "granted_at": permission.granted_at.isoformat() if permission.granted_at else None
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/permissions/repository/{repo_id}")
async def get_repository_permissions(repo_id: str, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Get all permissions for a repository"""
    try:
        permissions = UserPermissionCRUD.get_repository_permissions(db, repo_id)
        return {
            "success": True,
            "permissions": [
                {
                    "user": p.user.username,
                    "permission_level": p.permission_level,
                    "granted_by": p.granted_by.username if p.granted_by else None,
                    "granted_at": p.granted_at.isoformat() if p.granted_at else None
                }
                for p in permissions
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/permissions/user/{username}")
async def get_user_permissions(username: str, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Get all permissions for a user"""
    try:
        permissions = UserPermissionCRUD.get_user_permissions(db, username)
        return {
            "success": True,
            "permissions": [
                {
                    "repository_id": p.repository.id,
                    "repository_name": p.repository.name,
                    "permission_level": p.permission_level,
                    "granted_by": p.granted_by.username if p.granted_by else None,
                    "granted_at": p.granted_at.isoformat() if p.granted_at else None
                }
                for p in permissions
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.delete("/api/permissions/revoke")
async def revoke_permission(username: str, repo_id: str, current_user: User = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Revoke a user's permission to a repository"""
    try:
        from database.crud import RepositoryCRUD, UserCRUD
        success = UserPermissionCRUD.revoke_permission(db, username, repo_id)
        if not success:
            raise HTTPException(status_code=404, detail="Permission not found")
        audit.record(db, current_user.id, "revoke_permission", repository_id=repo_id,
                     description=f"Revoked {username}'s access", target_type="user", target_id=username)
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if repository:
            notify.access_changed(db, UserCRUD.get_user_by_username(db, username), repository, None, current_user)
        return {"success": True, "message": "Permission revoked successfully"}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
