"""Webhook dispatch.

Why: without this, an external system could only learn that a push, merge or
release happened by polling. Everything that integrates with a VCS — CI, chat,
deployment, audit — needs to be told.

Decisions that shape this module:

* **Delivery never blocks the request that triggered it.** A webhook points at
  a third-party URL that may be slow, wedged or gone. If a push waited on it,
  one broken integration would take pushing down for everyone. Dispatch hands
  the work to a small, fixed pool of worker threads and returns at once; a burst
  of events queues instead of starting one thread per delivery.

* **Every attempt is logged, successful or not**, under the delivery's id, so the
  log shows one delivery and its retries rather than unrelated rows. "Did the
  integration actually receive the push?" is the first question asked when a
  pipeline does not run. A failed delivery can be sent again from the log.

* **Every delivery is signed.** Webhooks get a secret when they are created, and
  the HMAC-SHA256 signature covers the exact bytes sent. The body carries its
  own ``delivery_id`` and ``delivered_at``, so a receiver can refuse a replay
  without trusting anything outside the signature.

* **Requests go only where the server may send them** (see outbound.py): no
  loopback, LAN or metadata addresses unless an administrator allows them, no
  redirects, and a capped reply.
"""

import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests
from sqlalchemy.orm import Session

from database.database import SessionLocal
from database.models import Webhook, WebhookDelivery

from app.services import outbound

# Event names. Kept as constants so a typo at a call site fails at import rather
# than silently registering an event nothing is subscribed to.
EVENT_PUSH = "push"
EVENT_COMMIT_PENDING = "commit.pending"
EVENT_COMMIT_REVIEWED = "commit.reviewed"
EVENT_BRANCH_CREATED = "branch.created"
EVENT_BRANCH_DELETED = "branch.deleted"
EVENT_BRANCH_MERGED = "branch.merged"
EVENT_PR_OPENED = "pull_request.opened"
EVENT_PR_UPDATED = "pull_request.updated"
EVENT_PR_MERGED = "pull_request.merged"
EVENT_PR_CLOSED = "pull_request.closed"
EVENT_PR_REVIEWED = "pull_request.reviewed"
EVENT_TAG_CREATED = "tag.created"
EVENT_RELEASE_PUBLISHED = "release.published"
EVENT_REPOSITORY_CREATED = "repository.created"
EVENT_STATUS_REPORTED = "status.reported"
EVENT_PING = "ping"

ALL_EVENTS = (
    EVENT_PUSH,
    EVENT_COMMIT_PENDING,
    EVENT_COMMIT_REVIEWED,
    EVENT_BRANCH_CREATED,
    EVENT_BRANCH_DELETED,
    EVENT_BRANCH_MERGED,
    EVENT_PR_OPENED,
    EVENT_PR_UPDATED,
    EVENT_PR_MERGED,
    EVENT_PR_CLOSED,
    EVENT_PR_REVIEWED,
    EVENT_TAG_CREATED,
    EVENT_RELEASE_PUBLISHED,
    EVENT_REPOSITORY_CREATED,
    EVENT_STATUS_REPORTED,
)

SIGNATURE_HEADER = "X-Zanbeel-Signature-256"
EVENT_HEADER = "X-Zanbeel-Event"
DELIVERY_HEADER = "X-Zanbeel-Delivery"

REQUEST_TIMEOUT = 10           # seconds per attempt
MAX_ATTEMPTS = 3
RETRY_BACKOFF = (1, 4)         # seconds to wait before attempts 2 and 3
MAX_RESPONSE_CHARS = 2000      # keep a useful excerpt, not a whole HTML page
MIN_SECRET_LENGTH = 16


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


# How much delivery history to keep. Payloads are stored with every attempt, so an
# unbounded log grows with every push for as long as the server runs.
DELIVERY_RETENTION_DAYS = _int_env("FOXNEST_WEBHOOK_RETENTION_DAYS", 30)
MAX_DELIVERIES_PER_HOOK = _int_env("FOXNEST_WEBHOOK_MAX_DELIVERIES", 500)

_EXECUTOR = ThreadPoolExecutor(
    max_workers=_int_env("FOXNEST_WEBHOOK_WORKERS", 8),
    thread_name_prefix="webhook",
)


def generate_secret() -> str:
    return secrets.token_urlsafe(32)


def new_delivery_id() -> str:
    return uuid.uuid4().hex


def sign_payload(secret: Optional[str], body: bytes) -> Optional[str]:
    """The signature header value for a body, or None when no secret is set.

    Computed over the exact bytes transmitted rather than over a re-serialised
    dict — otherwise a receiver that reserialises differently (key order,
    spacing) would compute a different digest and reject valid deliveries.
    """
    if not secret:
        return None
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header_value: str) -> bool:
    """Receiver-side check. Exposed so tests and consumers share one implementation."""
    expected = sign_payload(secret, body)
    if not expected or not header_value:
        return False
    return hmac.compare_digest(expected, header_value)


def subscribes_to(hook: Webhook, event: str) -> bool:
    """Whether a hook wants this event."""
    raw = (hook.events or "*").strip()
    if raw == "*" or not raw:
        return True
    wanted = {part.strip() for part in raw.split(",") if part.strip()}
    return event in wanted


def hooks_for(db: Session, event: str, repository_id: Optional[str]) -> List[Webhook]:
    """Active hooks that should receive this event.

    Includes repository-scoped hooks for the repository in question *and*
    global hooks (repository_id NULL), which is what an org-wide audit sink or
    chat relay is.
    """
    query = db.query(Webhook).filter(Webhook.active.is_(True))
    if repository_id:
        query = query.filter(
            (Webhook.repository_id == repository_id) | (Webhook.repository_id.is_(None))
        )
    else:
        query = query.filter(Webhook.repository_id.is_(None))

    return [hook for hook in query.all() if subscribes_to(hook, event)]


def build_body(
    event: str,
    repository_id: Optional[str],
    data: Dict[str, Any],
    delivery_id: str,
    redelivery_of: Optional[str] = None,
) -> bytes:
    """The bytes sent and signed. The delivery id and time are inside the signature."""
    envelope = {
        "event": event,
        "delivery_id": delivery_id,
        "delivered_at": datetime.utcnow().isoformat() + "Z",
        "repository_id": repository_id,
        "data": data,
    }
    if redelivery_of:
        envelope["redelivery_of"] = redelivery_of
    return json.dumps(envelope, separators=(",", ":"), default=str).encode("utf-8")


def _deliver_once(hook: Webhook, event: str, body: bytes, delivery_id: str) -> Dict[str, Any]:
    """One HTTP attempt. Never raises — the outcome is the return value."""
    headers = {
        "Content-Type": hook.content_type or "application/json",
        "User-Agent": "Zanbeel-Webhook/1.1",
        EVENT_HEADER: event,
        DELIVERY_HEADER: delivery_id,
    }
    signature = sign_payload(hook.secret, body)
    if signature:
        headers[SIGNATURE_HEADER] = signature

    started = time.monotonic()

    def outcome(**kwargs):
        result = {"status_code": None, "response_body": None, "error": None,
                  "success": False, "retry": False}
        result.update(kwargs)
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        return result

    try:
        response = outbound.post(hook.url, data=body, headers=headers, timeout=REQUEST_TIMEOUT)
    except outbound.OutboundBlocked as exc:
        # Not sent at all. Retrying would only be refused again.
        return outcome(error=f"Not sent: {exc.message}")
    except requests.exceptions.RequestException as exc:
        return outcome(error=str(exc)[:MAX_RESPONSE_CHARS], retry=True)

    text = (response.text or "")[:MAX_RESPONSE_CHARS]
    status = response.status_code
    if 300 <= status < 400:
        # A redirect is a second destination that was never checked, and following
        # it would hand the signature to whoever it points at.
        location = response.headers.get("Location") or "an unspecified location"
        return outcome(status_code=status, response_body=text,
                       error=f"Redirect to {location} not followed; update the webhook URL")
    return outcome(
        status_code=status,
        response_body=text,
        success=200 <= status < 300,
        # A 4xx means the receiver understood and refused; repeating it will not
        # change the answer, so only network errors and 5xx are retried.
        retry=status >= 500,
    )


def _record(
    db: Session,
    hook_id: int,
    event: str,
    body: bytes,
    attempt: int,
    outcome: Dict[str, Any],
    delivery_id: str,
    redelivery_of: Optional[str],
) -> WebhookDelivery:
    """Persist one delivery attempt."""
    delivery = WebhookDelivery(
        webhook_id=hook_id,
        event=event,
        delivery_guid=delivery_id,
        redelivery_of=redelivery_of,
        payload=body.decode("utf-8", errors="replace"),
        status_code=outcome["status_code"],
        response_body=outcome["response_body"],
        error=outcome["error"],
        duration_ms=outcome["duration_ms"],
        success=outcome["success"],
        attempt=attempt,
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    return delivery


def prune(db: Session, hook_id: int) -> int:
    """Drop delivery rows past the retention window or beyond the per-hook cap."""
    removed = 0
    cutoff = datetime.utcnow() - timedelta(days=DELIVERY_RETENTION_DAYS)
    removed += (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.webhook_id == hook_id, WebhookDelivery.created_at < cutoff)
        .delete(synchronize_session=False)
    )
    keep = (
        db.query(WebhookDelivery.id)
        .filter(WebhookDelivery.webhook_id == hook_id)
        .order_by(WebhookDelivery.id.desc())
        .offset(MAX_DELIVERIES_PER_HOOK)
        .first()
    )
    if keep is not None:
        removed += (
            db.query(WebhookDelivery)
            .filter(WebhookDelivery.webhook_id == hook_id, WebhookDelivery.id <= keep[0])
            .delete(synchronize_session=False)
        )
    db.commit()
    return removed


def attempt_delivery(
    db: Session,
    hook: Webhook,
    event: str,
    body: bytes,
    delivery_id: str,
    *,
    max_attempts: int = MAX_ATTEMPTS,
    redelivery_of: Optional[str] = None,
) -> Optional[WebhookDelivery]:
    """Send with retries on ``db``, logging every attempt. Returns the last attempt."""
    last = None
    for attempt in range(1, max_attempts + 1):
        outcome = _deliver_once(hook, event, body, delivery_id)
        try:
            last = _record(db, hook.id, event, body, attempt, outcome, delivery_id, redelivery_of)
        except Exception:
            db.rollback()
        if outcome["success"] or not outcome["retry"]:
            break
        if attempt < max_attempts:
            time.sleep(RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)])
    try:
        prune(db, hook.id)
    except Exception:
        db.rollback()
    return last


def deliver(hook_id: int, event: str, body: bytes, delivery_id: str,
            redelivery_of: Optional[str] = None) -> bool:
    """Attempt delivery with retries, logging every attempt. Own DB session.

    Runs on a worker thread, so it must not touch the session belonging to the
    request that triggered it — SQLAlchemy sessions are not thread-safe.
    """
    db = SessionLocal()
    try:
        hook = db.query(Webhook).filter(Webhook.id == hook_id).first()
        if not hook or not hook.active:
            return False
        last = attempt_delivery(db, hook, event, body, delivery_id, redelivery_of=redelivery_of)
        return bool(last and last.success)
    except Exception:
        return False
    finally:
        db.close()


def dispatch(
    db: Session,
    event: str,
    repository_id: Optional[str],
    payload: Dict[str, Any],
    background: bool = True,
) -> int:
    """Send ``event`` to every subscribed hook. Returns how many were queued.

    Wrapped so a webhook failure can never break the operation that emitted it:
    a push that succeeded must not be reported as failed because a chat server
    was down. Each hook gets its own delivery id.
    """
    try:
        hooks = hooks_for(db, event, repository_id)
    except Exception:
        return 0

    queued = 0
    for hook in hooks:
        delivery_id = new_delivery_id()
        body = build_body(event, repository_id, payload, delivery_id)
        try:
            if background:
                _EXECUTOR.submit(deliver, hook.id, event, body, delivery_id)
            else:
                deliver(hook.id, event, body, delivery_id)
            queued += 1
        except Exception:
            continue
    return queued


def ping(db: Session, hook: Webhook) -> Optional[WebhookDelivery]:
    """One synchronous test delivery. No retries: the person is waiting for the answer."""
    delivery_id = new_delivery_id()
    body = build_body(
        EVENT_PING, hook.repository_id,
        {"message": "Ping from Zanbeel", "webhook_id": hook.id}, delivery_id,
    )
    return attempt_delivery(db, hook, EVENT_PING, body, delivery_id, max_attempts=1)


def redeliver(db: Session, hook: Webhook, original: WebhookDelivery) -> Optional[WebhookDelivery]:
    """Send a logged delivery again: same event and data, new delivery id, signed afresh.

    The data is taken from the logged payload, so a receiver gets what it would have
    got the first time. ``redelivery_of`` in the body names the delivery being repeated.
    """
    try:
        logged = json.loads(original.payload or "{}")
    except ValueError:
        logged = {}
    data = logged.get("data") if isinstance(logged, dict) else None
    delivery_id = new_delivery_id()
    original_id = original.delivery_guid or str(original.id)
    body = build_body(original.event, hook.repository_id, data or {}, delivery_id,
                      redelivery_of=original_id)
    return attempt_delivery(db, hook, original.event, body, delivery_id,
                            max_attempts=1, redelivery_of=original_id)


def serialize(hook: Webhook, include_secret: bool = False) -> dict:
    """Public representation of a webhook.

    The secret is withheld by default. It is a shared HMAC key: anyone who can
    read it can forge deliveries that verify, so listing hooks must not hand it
    out just because the caller can see the hook.
    """
    blob = {
        "id": hook.id,
        "repository_id": hook.repository_id,
        "url": hook.url,
        "events": [e.strip() for e in (hook.events or "*").split(",") if e.strip()],
        "content_type": hook.content_type,
        "active": bool(hook.active),
        "has_secret": bool(hook.secret),
        "signed": bool(hook.secret),
        "created_at": hook.created_at.isoformat() if hook.created_at else None,
        "updated_at": hook.updated_at.isoformat() if hook.updated_at else None,
    }
    if include_secret:
        blob["secret"] = hook.secret
    return blob


def serialize_delivery(delivery: WebhookDelivery) -> dict:
    return {
        "id": delivery.id,
        "webhook_id": delivery.webhook_id,
        "delivery_id": getattr(delivery, "delivery_guid", None),
        "redelivery_of": getattr(delivery, "redelivery_of", None),
        "event": delivery.event,
        "status_code": delivery.status_code,
        "success": bool(delivery.success),
        "error": delivery.error,
        "duration_ms": delivery.duration_ms,
        "attempt": delivery.attempt,
        "payload": delivery.payload,
        "response_body": delivery.response_body,
        "created_at": delivery.created_at.isoformat() if delivery.created_at else None,
    }
