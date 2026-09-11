"""Webhook dispatch.

Why: without this, an external system could only learn that a push, merge or
release happened by polling. Everything that integrates with a VCS — CI, chat,
deployment, audit — needs to be told.

Two decisions shape this module:

* **Delivery never blocks the request that triggered it.** A webhook points at
  a third-party URL that may be slow, wedged or gone. If a push waited on it,
  one broken integration would take pushing down for everyone. Dispatch happens
  on a worker thread and the caller returns immediately.

* **Every attempt is logged, successful or not.** "Did the integration actually
  receive the push?" is the first question asked when a pipeline does not run,
  and without a delivery record the answer is unknowable.

Payloads are signed with HMAC-SHA256 over the exact bytes sent, so a receiver
can verify the request really came from this server rather than from anyone who
learned the URL.
"""

import hashlib
import hmac
import json
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from sqlalchemy.orm import Session

from database.database import SessionLocal
from database.models import Webhook, WebhookDelivery

# Event names. Kept as constants so a typo at a call site fails at import rather
# than silently registering an event nothing is subscribed to.
EVENT_PUSH = "push"
EVENT_COMMIT_PENDING = "commit.pending"
EVENT_COMMIT_REVIEWED = "commit.reviewed"
EVENT_BRANCH_CREATED = "branch.created"
EVENT_BRANCH_MERGED = "branch.merged"
EVENT_PR_OPENED = "pull_request.opened"
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
    EVENT_BRANCH_MERGED,
    EVENT_PR_OPENED,
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


def _deliver_once(hook: Webhook, event: str, body: bytes, delivery_id: str) -> Dict[str, Any]:
    """One HTTP attempt. Never raises — the outcome is the return value."""
    headers = {
        "Content-Type": hook.content_type or "application/json",
        "User-Agent": "Zanbeel-Webhook/1.0",
        EVENT_HEADER: event,
        DELIVERY_HEADER: delivery_id,
    }
    signature = sign_payload(hook.secret, body)
    if signature:
        headers[SIGNATURE_HEADER] = signature

    started = time.monotonic()
    try:
        response = requests.post(
            hook.url, data=body, headers=headers, timeout=REQUEST_TIMEOUT
        )
        duration = int((time.monotonic() - started) * 1000)
        return {
            "status_code": response.status_code,
            "response_body": (response.text or "")[:MAX_RESPONSE_CHARS],
            "error": None,
            "duration_ms": duration,
            # 2xx is success. A 3xx is not followed: a webhook that redirects is
            # a misconfiguration, and following it could leak the signature to
            # an unintended host.
            "success": 200 <= response.status_code < 300,
        }
    except requests.exceptions.RequestException as exc:
        duration = int((time.monotonic() - started) * 1000)
        return {
            "status_code": None,
            "response_body": None,
            "error": str(exc)[:MAX_RESPONSE_CHARS],
            "duration_ms": duration,
            "success": False,
        }


def _record(db: Session, hook_id: int, event: str, body: bytes, attempt: int, outcome: Dict[str, Any]) -> None:
    """Persist one delivery attempt."""
    delivery = WebhookDelivery(
        webhook_id=hook_id,
        event=event,
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


def deliver(hook_id: int, event: str, body: bytes, delivery_id: str) -> bool:
    """Attempt delivery with retries, logging every attempt. Own DB session.

    Runs on a worker thread, so it must not touch the session belonging to the
    request that triggered it — SQLAlchemy sessions are not thread-safe.
    """
    db = SessionLocal()
    try:
        hook = db.query(Webhook).filter(Webhook.id == hook_id).first()
        if not hook or not hook.active:
            return False

        for attempt in range(1, MAX_ATTEMPTS + 1):
            outcome = _deliver_once(hook, event, body, delivery_id)
            try:
                _record(db, hook.id, event, body, attempt, outcome)
            except Exception:
                db.rollback()

            if outcome["success"]:
                return True

            # A 4xx means the receiver understood and refused; repeating it will
            # not change the answer, so only network errors and 5xx are retried.
            status = outcome["status_code"]
            if status is not None and 400 <= status < 500:
                return False

            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_BACKOFF[attempt - 1])

        return False
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
    was down.
    """
    try:
        hooks = hooks_for(db, event, repository_id)
    except Exception:
        return 0

    if not hooks:
        return 0

    envelope = {
        "event": event,
        "delivered_at": datetime.utcnow().isoformat() + "Z",
        "repository_id": repository_id,
        "data": payload,
    }
    body = json.dumps(envelope, separators=(",", ":"), default=str).encode("utf-8")
    delivery_id = hashlib.sha256(body + str(time.time()).encode()).hexdigest()[:32]

    queued = 0
    for hook in hooks:
        if background:
            thread = threading.Thread(
                target=deliver,
                args=(hook.id, event, body, delivery_id),
                daemon=True,
                name=f"webhook-{hook.id}-{event}",
            )
            thread.start()
        else:
            deliver(hook.id, event, body, delivery_id)
        queued += 1

    return queued


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
