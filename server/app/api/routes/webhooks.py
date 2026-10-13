"""Webhook management and delivery history.

Access rule: a webhook receives repository contents, so managing one requires
the same standing as administering the repository. Global hooks (no repository)
are admin-only, since they see every repository on the server.

Every webhook is signed. One created without a secret gets a generated one, shown
once in the response; a secret can be replaced or rotated but not removed, because
an unsigned delivery is one the receiver cannot tell from a forgery.

URLs are checked against the outbound rules (app/services/outbound.py) when they are
saved, so a hook pointed at this server, the LAN or the metadata service is refused
with the reason rather than failing on every delivery.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import Repository, User, Webhook, WebhookDelivery

from app.core.dependencies import get_current_user, has_admin_powers
from app.services import outbound
from app.services import webhooks as hook_service


from app.services import audit

router = APIRouter()


def _assert_may_manage(
    http_request: Request, db: Session, user: User, repository_id: Optional[str]
) -> None:
    """Only the repository owner or an admin/team lead may manage its hooks."""
    # The role alone is not enough when a token is in use: an admin's repo:write
    # token is a repository credential, not a server-administration one.
    if has_admin_powers(http_request, user):
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


def _hook_or_404(db: Session, webhook_id: int) -> Webhook:
    hook = db.query(Webhook).filter(Webhook.id == webhook_id).first()
    if not hook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return hook


def _check_url(url: str) -> Optional[str]:
    """Refuse a URL the server may not call; return a warning if DNS cannot answer yet."""
    try:
        return outbound.check_url(url)
    except outbound.OutboundBlocked as exc:
        raise HTTPException(status_code=400, detail=exc.message)


def _check_secret(secret: str) -> str:
    secret = secret.strip()
    if len(secret) < hook_service.MIN_SECRET_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"A webhook secret must be at least {hook_service.MIN_SECRET_LENGTH} "
                   "characters. Leave it out to have one generated.",
        )
    return secret


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
    http_request: Request,
    repository_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List webhooks for a repository, or the global ones when none is given."""
    _assert_may_manage(http_request, db, current_user, repository_id)

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
def create_webhook(
    http_request: Request,
    request: WebhookRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Register a webhook. The response carries its secret, this once."""
    _assert_may_manage(http_request, db, current_user, request.repository_id)

    if request.repository_id:
        exists = db.query(Repository).filter(Repository.id == request.repository_id).first()
        if not exists:
            raise HTTPException(status_code=404, detail="Repository not found")

    warning = _check_url(request.url)
    generated = not (request.secret or "").strip()
    secret = hook_service.generate_secret() if generated else _check_secret(request.secret)

    hook = Webhook(
        repository_id=request.repository_id,
        url=request.url,
        secret=secret,
        events=_validate_events(request.events),
        content_type=request.content_type or "application/json",
        active=request.active,
        created_by_id=current_user.id,
    )
    db.add(hook)
    db.commit()
    db.refresh(hook)
    audit.record(db, current_user.id, "webhook_created", repository_id=hook.repository_id,
                 description=f"Added webhook {hook.url} ({hook.events})",
                 target_type="webhook", target_id=hook.id)

    return {
        "success": True,
        "webhook": hook_service.serialize(hook),
        "secret": secret,
        "secret_generated": generated,
        "warning": warning,
    }


@router.put("/api/webhooks/{webhook_id}")
def update_webhook(
    http_request: Request,
    webhook_id: int,
    request: WebhookUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update a webhook. Omitted fields are left alone."""
    hook = _hook_or_404(db, webhook_id)
    _assert_may_manage(http_request, db, current_user, hook.repository_id)

    warning = None
    if request.url is not None:
        warning = _check_url(request.url)
        hook.url = request.url
    if request.secret is not None:
        if not request.secret.strip():
            raise HTTPException(
                status_code=400,
                detail="Webhooks are always signed, so the secret cannot be removed. "
                       "Rotate it to get a new one.",
            )
        hook.secret = _check_secret(request.secret)
    if request.events is not None:
        hook.events = _validate_events(request.events)
    if request.content_type is not None:
        hook.content_type = request.content_type
    if request.active is not None:
        hook.active = request.active

    db.commit()
    db.refresh(hook)
    changed = [name for name in ("url", "secret", "events", "content_type", "active")
               if getattr(request, name) is not None]
    audit.record(db, current_user.id, "webhook_updated", repository_id=hook.repository_id,
                 description=f"Changed webhook {hook.url}: {', '.join(changed) or 'nothing'}",
                 target_type="webhook", target_id=hook.id)
    return {"success": True, "webhook": hook_service.serialize(hook), "warning": warning}


@router.post("/api/webhooks/{webhook_id}/rotate-secret")
async def rotate_webhook_secret(
    http_request: Request,
    webhook_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Replace the secret with a generated one, returned this once.

    Deliveries are signed with the new secret from now on, so the receiver needs it
    straight away. Also how a webhook from before secrets were required gets one.
    """
    hook = _hook_or_404(db, webhook_id)
    _assert_may_manage(http_request, db, current_user, hook.repository_id)

    secret = hook_service.generate_secret()
    hook.secret = secret
    db.commit()
    db.refresh(hook)
    audit.record(db, current_user.id, "webhook_secret_rotated", repository_id=hook.repository_id,
                 description=f"Rotated the secret of webhook {hook.url}", target_type="webhook", target_id=hook.id)
    return {"success": True, "webhook": hook_service.serialize(hook), "secret": secret}


@router.delete("/api/webhooks/{webhook_id}")
async def delete_webhook(
    http_request: Request,
    webhook_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a webhook and its delivery history."""
    hook = _hook_or_404(db, webhook_id)
    _assert_may_manage(http_request, db, current_user, hook.repository_id)

    url, repository_id, hook_id = hook.url, hook.repository_id, hook.id
    db.query(WebhookDelivery).filter(WebhookDelivery.webhook_id == hook.id).delete()
    db.delete(hook)
    db.commit()
    audit.record(db, current_user.id, "webhook_deleted", repository_id=repository_id,
                 description=f"Removed webhook {url}", target_type="webhook", target_id=hook_id)
    return {"success": True}


@router.post("/api/webhooks/{webhook_id}/ping")
def ping_webhook(
    http_request: Request,
    webhook_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a test delivery and wait for the result.

    Synchronous on purpose, unlike real events: someone who just configured a URL
    wants to know *now* whether it works. One attempt, no retries, and a plain ``def``
    route so the wait happens on a worker thread. It used to be an ``async`` route
    making a blocking request with retries, which held up every other request on the
    server until the receiver answered.
    """
    hook = _hook_or_404(db, webhook_id)
    _assert_may_manage(http_request, db, current_user, hook.repository_id)

    delivery = hook_service.ping(db, hook)
    return {
        "success": True,
        "delivery": hook_service.serialize_delivery(delivery) if delivery else None,
    }


@router.get("/api/webhooks/{webhook_id}/deliveries")
async def list_deliveries(
    http_request: Request,
    webhook_id: int,
    limit: int = 30,
    delivery_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Recent delivery attempts, newest first. ``delivery_id`` narrows to one delivery."""
    hook = _hook_or_404(db, webhook_id)
    _assert_may_manage(http_request, db, current_user, hook.repository_id)

    query = db.query(WebhookDelivery).filter(WebhookDelivery.webhook_id == hook.id)
    if delivery_id:
        query = query.filter(WebhookDelivery.delivery_guid == delivery_id)
    deliveries = query.order_by(WebhookDelivery.id.desc()).limit(max(1, min(limit, 200))).all()
    return {
        "success": True,
        "deliveries": [hook_service.serialize_delivery(d) for d in deliveries],
    }


@router.post("/api/webhooks/{webhook_id}/deliveries/{delivery_row_id}/redeliver")
def redeliver_webhook_delivery(
    http_request: Request,
    webhook_id: int,
    delivery_row_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a logged delivery again, as one new attempt with a new delivery id.

    For the receiver that was down when the event happened: the pipeline gets what it
    missed without anyone pushing again.
    """
    hook = _hook_or_404(db, webhook_id)
    _assert_may_manage(http_request, db, current_user, hook.repository_id)

    original = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.id == delivery_row_id, WebhookDelivery.webhook_id == hook.id)
        .first()
    )
    if not original:
        raise HTTPException(status_code=404, detail="Delivery not found")

    delivery = hook_service.redeliver(db, hook, original)
    return {
        "success": True,
        "delivery": hook_service.serialize_delivery(delivery) if delivery else None,
    }
