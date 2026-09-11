"""Repository lifecycle: create, list, read, delete, archive, star and manuals.

Declaration order matters: the literal /api/repository/create and
/api/repository/list paths must stay ahead of /api/repository/{repo_id}."""

from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database.crud import (
    ActivityCRUD, PendingRepositoryCRUD, RepositoryCRUD, RepositoryStarCRUD, UserCRUD,
    contributors_for_repository,
)
from database.database import get_db
from database.models import PendingRepository, User

from app.core.dependencies import get_current_user
from app.core.pagination import paginate_list
from app.services import webhooks as hook_service
from app.core.permissions import (
    _is_privileged_role, _require_repository_read_access, _user_can_view_issues,
    require_repository_access,
)
from app.schemas import (
    CreateRepositoryRequest,
    ForkRepositoryRequest, UpdateRepositoryDetailsRequest, UpdateRepositoryDetailsResponse,
)
from app.services import forks
from app.services.serializers import repository_to_dict


router = APIRouter()


@router.post("/api/repository/create")
async def create_repository(
    request: CreateRepositoryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new repository.

    Requires authentication. `username` used to be taken on trust from the body, which
    let any unauthenticated caller create repositories in someone else's name; it is now
    only honoured when it matches the caller, or when a team lead/admin is deliberately
    acting for another user.
    """
    repo_name = (request.repo_name or "").strip()
    if not request.username or not repo_name:
        raise HTTPException(status_code=400, detail="Username and repo_name required")

    if request.username != current_user.username and \
            getattr(current_user, "role", "developer") not in ("team_lead", "admin"):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Authenticated as '{current_user.username}'; only team leads and admins "
                f"can create a repository on behalf of '{request.username}'."
            ),
        )

    try:
        # Check if user exists - DO NOT auto-create
        user = UserCRUD.get_user_by_username(db, request.username)
        if not user:
            raise HTTPException(
                status_code=403, 
                detail=f"User '{request.username}' does not exist. Please contact an administrator to create your account."
            )
        
        # Check if user is active
        if not user.is_active:
            raise HTTPException(
                status_code=403,
                detail=f"User '{request.username}' is inactive. Please contact an administrator."
            )
        
        # Determine the owner based on user role FIRST
        owner_username = request.username
        user_role = getattr(user, 'role', 'developer')
        
        if user_role == 'developer' and user.team_lead:
            # For developers, the team lead is the owner
            owner_username = user.team_lead.username
        
        # Generate repo_id with the OWNER's username (not the requester)
        repo_id = RepositoryCRUD.generate_repo_id(owner_username, repo_name)
        
        # Check if repository already exists (approved)
        existing_repo = RepositoryCRUD.get_repository(db, repo_id)
        if existing_repo:
            return {
                "success": True,
                "repo_id": existing_repo.id,
                "owner": existing_repo.owner.username,
                "message": "Repository already exists"
            }

        # Developer-approved repos are owned by the requester (not the team lead).
        requester_repo_id = RepositoryCRUD.generate_repo_id(request.username, repo_name)
        existing_requester_repo = RepositoryCRUD.get_repository(db, requester_repo_id)
        if existing_requester_repo:
            return {
                "success": True,
                "repo_id": existing_requester_repo.id,
                "owner": existing_requester_repo.owner.username,
                "message": "Repository already exists"
            }
        
        if user_role == 'developer' and user.team_lead:
            # Developer with team lead - team lead becomes owner
            owner_username = user.team_lead.username
            print(f"DEBUG - Repository creation: {request.username} (developer) requesting repo, owner will be team lead: {owner_username}")
            
            # Check if pending request already exists for this repo
            existing_pending = db.query(PendingRepository).filter(
                PendingRepository.repo_name == repo_name,
                PendingRepository.requested_by_id == user.id,
                PendingRepository.status == 'pending'
            ).first()
            
            if existing_pending:
                return {
                    "success": True,
                    "status": "pending_approval",
                    "team_lead": owner_username,
                    "pending_id": existing_pending.id,
                    "message": f"Repository creation request already pending approval from {owner_username}."
                }
            
            # For developers, create a pending repository request instead
            pending_repo = PendingRepositoryCRUD.create_pending_repository(
                db,
                repo_name=repo_name,
                description=request.description,
                requested_by_username=request.username,
                owner_username=owner_username
            )
            
            # Create activity for the request
            ActivityCRUD.create_activity(
                db, user.id, "request_repository", 
                f"Requested repository {repo_name}", None
            )
            
            return {
                "success": True,
                "status": "pending_approval",
                "team_lead": owner_username,
                "pending_id": pending_repo.id,
                "message": f"Repository creation request submitted. Awaiting approval from {owner_username}."
            }
        else:
            # Team lead or user without team lead can create directly
            print(f"DEBUG - Repository creation: {request.username} creating repo as owner (role: {user_role})")
            
            repository = RepositoryCRUD.create_repository(
                db, owner_username, repo_name, request.description
            )
            
            # Create activity
            ActivityCRUD.create_activity(
                db, user.id, "create_repository", 
                f"Created repository {repo_name}", repository.id
            )

            # Global hooks only: a brand-new repository has none of its own yet.
            hook_service.dispatch(db, hook_service.EVENT_REPOSITORY_CREATED, repository.id, {
                "repository": {
                    "id": repository.id,
                    "name": repository.name,
                    "owner": owner_username,
                    "description": request.description,
                },
                "created_by": user.username,
            })
            
            return {
                "success": True, 
                "repo_id": repository.id,
                "owner": owner_username,
                "message": None
            }
    
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/repository/list")
async def list_repositories(
    username: Optional[str] = None,
    repo_name: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List repositories.

    - No username: privileged users get all repos; developers get their accessible set
      (same scope as /api/repositories/all).
    - With username: return that user's owned repos (developers may only query themselves).
    - repo_name requires username (owner + name lookup).
    """
    is_privileged = _is_privileged_role(getattr(current_user, "role", "developer"))

    if repo_name and not username:
        raise HTTPException(status_code=400, detail="username is required when filtering by repo_name")

    if username and (not is_privileged) and username != current_user.username:
        raise HTTPException(status_code=403, detail="You can only view repositories you own")

    try:
        if not username:
            if is_privileged:
                repositories = RepositoryCRUD.get_all_repositories(db)
            else:
                repositories = RepositoryCRUD.get_repositories_for_web_browser(db, current_user)
        elif repo_name:
            repo_id = RepositoryCRUD.generate_repo_id(username, repo_name)
            repository = RepositoryCRUD.get_repository(db, repo_id)
            repositories = [repository] if repository else []
        else:
            target_user = UserCRUD.get_user_by_username(db, username)
            if target_user and not is_privileged:
                repositories = RepositoryCRUD.get_repositories_for_web_browser(db, target_user)
            else:
                repositories = RepositoryCRUD.get_repositories_by_user(db, username)

        # Paged after the permission filter, which runs in Python, so `total`
        # counts what this caller may actually see rather than what exists.
        page, meta = paginate_list(repositories, limit, offset)
        return {
            "success": True,
            "repositories": [repository_to_dict(repo, db=db, actor=current_user) for repo in page],
            "pagination": meta,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/repositories/all")
async def list_all_repositories(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List repositories by role scope (all for team lead/admin, accessible ones for developers)."""
    try:
        if _is_privileged_role(getattr(current_user, "role", "developer")):
            repositories = RepositoryCRUD.get_all_repositories(db)
        else:
            repositories = RepositoryCRUD.get_repositories_for_web_browser(db, current_user)

        return {
            "success": True, 
            "repositories": [repository_to_dict(repo, db=db, actor=current_user) for repo in repositories]
        }
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/repository/{repo_id}")
async def get_repository(
    repo_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get repository information. Requires authentication and repository visibility."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    if not _user_can_view_issues(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Not allowed to view this repository")

    return {"success": True, "repository": repository_to_dict(repository, db=db, actor=current_user)}


@router.get("/api/repository/{repo_id}/contributors")
async def get_repository_contributors(
    repo_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List named contributors for a repository (owner, authors, collaborators, issue participants)."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")

    if not _user_can_view_issues(db, current_user, repository):
        raise HTTPException(status_code=403, detail="Not allowed to view this repository")

    contributors = contributors_for_repository(db, repository)
    return {
        "success": True,
        "contributors": contributors,
        "count": len(contributors),
    }


@router.delete("/api/repository/{repo_id}")
async def delete_repository(repo_id: str, actor_username: Optional[str] = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Delete a repository permanently"""
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        resolved_actor = current_user.username if current_user else actor_username
        require_repository_access(db, resolved_actor, repository, "delete", required_scope="manage")
        
        repo_name = repository.name
        owner_name = repository.owner.username
        
        # Delete the repository
        RepositoryCRUD.delete_repository(db, repo_id)
        
        return {
            "success": True,
            "message": f"Repository '{repo_name}' owned by '{owner_name}' has been permanently deleted"
        }
    
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.post("/api/repository/{repo_id}/archive")
async def archive_repository(repo_id: str, reason: Optional[str] = None, actor_username: Optional[str] = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Archive a repository (move to archive without deleting)"""
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        resolved_actor = current_user.username if current_user else actor_username
        require_repository_access(db, resolved_actor, repository, "archive", required_scope="manage")
        
        if repository.is_archived:
            raise HTTPException(status_code=400, detail="Repository is already archived")
        
        repo_name = repository.name
        owner_name = repository.owner.username
        
        # Archive the repository
        RepositoryCRUD.archive_repository(db, repo_id, reason=reason or "Archived via web interface")
        
        return {
            "success": True,
            "message": f"Repository '{repo_name}' owned by '{owner_name}' has been archived",
            "repo_id": repo_id
        }
    
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.post("/api/repository/{repo_id}/star")
async def star_repository(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "star")
    RepositoryStarCRUD.add_star(db, current_user.id, repo_id)
    return {
        "success": True,
        "star_count": RepositoryStarCRUD.count_for_repository(db, repo_id),
        "starred": True,
    }


@router.delete("/api/repository/{repo_id}/star")
async def unstar_repository(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "unstar")
    RepositoryStarCRUD.remove_star(db, current_user.id, repo_id)
    return {
        "success": True,
        "star_count": RepositoryStarCRUD.count_for_repository(db, repo_id),
        "starred": False,
    }


@router.put("/api/repository/{repo_id}/details")
async def update_repository_details(repo_id: str, request: UpdateRepositoryDetailsRequest, actor_username: Optional[str] = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Update repository G1 coordinator and testing status"""
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        resolved_actor = current_user.username if current_user else actor_username
        require_repository_access(db, resolved_actor, repository, "update", required_scope="write")

        repository = RepositoryCRUD.update_repository_details(
            db, 
            repo_id, 
            g1_coordinator=request.g1_coordinator,
            tested=request.tested
        )
        
        return UpdateRepositoryDetailsResponse(
            success=True,
            repository={
                "id": repository.id,
                "name": repository.name,
                "g1_coordinator": repository.g1_coordinator,
                "tested": repository.tested,
                "instruction_manual_filename": repository.instruction_manual_filename
            }
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.post("/api/repository/{repo_id}/upload-manual")
async def upload_instruction_manual(
    repo_id: str,
    actor_username: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Upload instruction manual PDF for a repository"""
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        resolved_actor = current_user.username if current_user else actor_username
        require_repository_access(db, resolved_actor, repository, "upload instruction manual for", required_scope="write")

        # Validate file type
        if not file.filename.lower().endswith('.pdf'):
            raise HTTPException(status_code=400, detail="Only PDF files are allowed")
        
        # Create uploads directory if it doesn't exist
        uploads_dir = Path("/tmp/foxnest_uploads")
        uploads_dir.mkdir(exist_ok=True)
        
        # Generate unique filename
        file_extension = Path(file.filename).suffix
        unique_filename = f"{repo_id}_manual_{datetime.now().strftime('%Y%m%d_%H%M%S')}{file_extension}"
        file_path = uploads_dir / unique_filename
        
        # Save file
        content = await file.read()
        with open(file_path, "wb") as f:
            f.write(content)
        
        # Update repository with file path
        repository = RepositoryCRUD.update_repository_details(
            db,
            repo_id,
            instruction_manual_path=str(file_path),
            instruction_manual_filename=file.filename
        )
        
        return {
            "success": True,
            "message": "Instruction manual uploaded successfully",
            "filename": file.filename,
            "repository": {
                "id": repository.id,
                "name": repository.name,
                "instruction_manual_filename": repository.instruction_manual_filename
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.get("/api/repository/{repo_id}/download-manual")
async def download_instruction_manual(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download instruction manual PDF for a repository"""
    try:
        repository = RepositoryCRUD.get_repository(db, repo_id)
        if not repository:
            raise HTTPException(status_code=404, detail="Repository not found")

        _require_repository_read_access(db, current_user, repository, "download instruction manual")
        
        if not repository.instruction_manual_path:
            raise HTTPException(status_code=404, detail="No instruction manual found for this repository")
        
        file_path = Path(repository.instruction_manual_path)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Instruction manual file not found on server")
        
        return FileResponse(
            path=str(file_path),
            filename=repository.instruction_manual_filename or "instruction_manual.pdf",
            media_type="application/pdf"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.post("/api/repository/{repo_id}/fork")
async def fork_repository(
    repo_id: str,
    request: ForkRepositoryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create your own copy of a repository, keeping a pointer back to the original.

    Copies refs and metadata only. Blobs are content-addressed and shared, so forking a
    large repository costs rows rather than gigabytes.
    """
    source = RepositoryCRUD.get_repository(db, repo_id)
    if not source:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, source, "fork")

    try:
        fork = forks.fork_repository(db, source, current_user, request.name)
    except forks.ForkError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    ActivityCRUD.create_activity(
        db, current_user.id, "fork_repository",
        f"Forked '{source.name}' as '{fork.name}'", fork.id,
    )
    return {
        "success": True,
        "repo_id": fork.id,
        "name": fork.name,
        "forked_from": source.id,
    }


@router.get("/api/repository/{repo_id}/forks")
async def list_repository_forks(
    repo_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Where this repository came from, and what has been forked off it."""
    repository = RepositoryCRUD.get_repository(db, repo_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    _require_repository_read_access(db, current_user, repository, "view forks")

    return {"success": True, **forks.describe(db, repository)}
