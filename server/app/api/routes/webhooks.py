"""Webhook management and delivery history.

Access rule: a webhook receives repository contents, so managing one requires
the same standing as administering the repository. Global hooks (no repository)
are admin-only, since they see every repository on the server.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import Repository, User, Webhook, WebhookDelivery

from app.core.dependencies import get_current_user, require_admin_user
from app.services import webhooks as hook_service


router = APIRouter()


def _assert_may_manage(db: Session, user: User, repository_id: Optional[str]) -> None:
    """Only the repository owner or an admin/team lead may manage its hooks."""
    role = (getattr(user, "role", "developer") or "").lower()
    if role in ("team_lead", "admin"):
        return

    if not repository_id:
        raise HTTPException(
            status_code=403,
            detail="Only admins and team leads may manage server-wide webhooks",
        )

    repository = db.query(Repository).filter(Repository.id == repository_id).first()
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    if repository.owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the repository owner or an admin may manage its webhooks",
        )


def _validate_events(events: Optional[List[str]]) -> str:
    """Turn a requested event list into the stored comma string.

    Unknown names are refused rather than stored: a hook silently subscribed to
    a misspelled event looks configured and never fires, which is the worst
    possible failure mode for an integration.
    """
    if not events:
        return "*"
    cleaned = []
    for raw in events:
        name = (raw or "").strip()
        if not name:
            continue
        if name == "*":
            return "*"
        if name not in hook_service.ALL_EVENTS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown event '{name}'. Valid events: {', '.join(hook_service.ALL_EVENTS)}",
            )
        if name not in cleaned:
            cleaned.append(name)
    return ",".join(cleaned) if cleaned else "*"


class WebhookRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=500)
    repository_id: Optional[str] = None
    secret: Optional[str] = Field(None, max_length=128)
    events: Optional[List[str]] = None
    content_type: str = "application/json"
    active: bool = True

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("Webhook URL must start with http:// or https://")
        return value


class WebhookUpdateRequest(BaseModel):
    url: Optional[str] = Field(None, max_length=500)
    secret: Optional[str] = Field(None, max_length=128)
    events: Optional[List[str]] = None
    content_type: Optional[str] = None
    active: Optional[bool] = None

    @field_validator("url")
    @classmethod
    def url_must_be_http(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("Webhook URL must start with http:// or https://")
        return value


@router.get("/api/webhooks")
async def list_webhooks(
    repository_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List webhooks for a repository, or the global ones when none is given."""
    _assert_may_manage(db, current_user, repository_id)

    query = db.query(Webhook)
    if repository_id:
        query = query.filter(Webhook.repository_id == repository_id)
    else:
        query = query.filter(Webhook.repository_id.is_(None))

    hooks = query.order_by(Webhook.created_at.desc()).all()
    return {
        "success": True,
        "webhooks": [hook_service.serialize(hook) for hook in hooks],
        "valid_events": list(hook_service.ALL_EVENTS),
    }


@router.post("/api/webhooks")
async def create_webhook(
    request: WebhookRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Register a webhook."""
    _assert_may_manage(db, current_user, request.repository_id)

    if request.repository_id:
        exists = db.query(Repository).filter(Repository.id == request.repository_id).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Repository not found")

    hook = Webhook(
        repository_id=request.repository_id,
        url=request.url,
        secret=request.secret or None,
        events=_validate_events(request.events),
        content_type=request.content_type or "application/json",
        active=request.active,
        created_by_id=current_user.id,
    )
    db.add(hook)
    db.commit()
    db.refresh(hook)

    return {"success": True, "webhook": hook_service.serialize(hook)}


@router.put("/api/webhooks/{webhook_id}")
async def update_webhook(
    webhook_id: int,
    request: WebhookUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a webhook. Omitted fields are left alone."""
    hook = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not hook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    _assert_may_manage(db, current_user, hook.repository_id)

    if request.url is not None:
        hook.url = request.url
    if request.secret is not None:
        # An empty string clears the secret; None means "leave it".
        hook.secret = request.secret or None
    if request.events is not None:
        hook.events = _validate_events(request.events)
    if request.content_type is not None:
        hook.content_type = request.content_type
    if request.active is not None:
        hook.active = request.active

    db.commit()
    db.refresh(hook)
    return {"success": True, "webhook": hook_service.serialize(hook)}


@router.delete("/api/webhooks/{webhook_id}")
async def delete_webhook(
    webhook_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a webhook and its delivery history."""
    hook = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not hook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    _assert_may_manage(db, current_user, hook.repository_id)

    db.query(WebhookDelivery).filter(WebhookDelivery.webhook_id == hook.id).delete()
    db.delete(hook)
    db.commit()
    return {"success": True}


@router.post("/api/webhooks/{webhook_id}/ping")
async def ping_webhook(
    webhook_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a test delivery and wait for the result.

    Synchronous on purpose, unlike real events: someone who just configured a
    URL wants to know *now* whether it works, and is willing to wait for the
    answer. Real events must never block a push.
    """
    hook = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not hook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    _assert_may_manage(db, current_user, hook.repository_id)

    import json as _json
    import hashlib as _hashlib
    import time as _time

    envelope = {
        "event": hook_service.EVENT_PING,
        "delivered_at": None,
        "repository_id": hook.repository_id,
        "data": {"message": "Ping from Zanbeel", "webhook_id": hook.id},
    }
    body = _json.dumps(envelope, separators=(",", ":"), default=str).encode("utf-8")
    delivery_id = _hashlib.sha256(body + str(_time.time()).encode()).hexdigest()[:32]

    hook_service.deliver(hook.id, hook_service.EVENT_PING, body, delivery_id)

    latest = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.webhook_id == hook.id)
        .order_by(WebhookDelivery.id.desc())
        .first()
    )
    return {
        "success": True,
        "delivery": hook_service.serialize_delivery(latest) if latest else None,
    }


@router.get("/api/webhooks/{webhook_id}/deliveries")
async def list_deliveries(
    webhook_id: int,
    limit: int = 30,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Recent delivery attempts, newest first."""
    hook = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not hook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    _assert_may_manage(db, current_user, hook.repository_id)

    deliveries = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.webhook_id == hook.id)
        .order_by(WebhookDelivery.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return {
        "success": True,
        "deliveries": [hook_service.serialize_delivery(d) for d in deliveries],
    }
