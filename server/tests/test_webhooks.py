"""Tests for webhook dispatch, signing, subscription filtering and retries.

Delivery is exercised against a real HTTP server on localhost rather than a
mock, so the signature is verified over the bytes that actually crossed the
wire — the failure mode where a receiver reserialises and computes a different
digest would not show up against a mock.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from database.database import Base  # noqa: E402
from database.models import Webhook, WebhookDelivery  # noqa: E402
from app.services import webhooks as wh  # noqa: E402


# --------------------------------------------------------------------------
# A real receiver, so signatures are checked over real transmitted bytes.
# --------------------------------------------------------------------------

class _Receiver(BaseHTTPRequestHandler):
    received = []
    status_to_return = 200
    fail_times = 0
    _failures = 0

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _Receiver.received.append({
            "body": body,
            "headers": {k: v for k, v in self.headers.items()},
            "path": self.path,
        })

        if _Receiver._failures < _Receiver.fail_times:
            _Receiver._failures += 1
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return

        self.send_response(_Receiver.status_to_return)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass  # keep test output readable


class WebhookTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), _Receiver)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.port}/hook"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        _Receiver.received = []
        _Receiver.status_to_return = 200
        _Receiver.fail_times = 0
        _Receiver._failures = 0
        wh.RETRY_BACKOFF = (0, 0)  # keep the suite fast

        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        # dispatch() opens its own session via SessionLocal; point it here.
        import database.database as dbmod
        self._real_sessionlocal = wh.SessionLocal
        wh.SessionLocal = self.Session

    def tearDown(self):
        wh.SessionLocal = self._real_sessionlocal
        self.db.close()
        self.engine.dispose()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def make_hook(self, **kw):
        hook = Webhook(
            repository_id=kw.get("repository_id", "repo1"),
            url=kw.get("url", self.url),
            secret=kw.get("secret"),
            events=kw.get("events", "*"),
            active=kw.get("active", True),
        )
        self.db.add(hook)
        self.db.commit()
        self.db.refresh(hook)
        return hook


class SignatureTests(unittest.TestCase):
    def test_no_secret_means_no_signature(self):
        self.assertIsNone(wh.sign_payload(None, b"{}"))
        self.assertIsNone(wh.sign_payload("", b"{}"))

    def test_signature_is_stable_and_prefixed(self):
        sig = wh.sign_payload("s3cret", b'{"a":1}')
        self.assertTrue(sig.startswith("sha256="))
        self.assertEqual(sig, wh.sign_payload("s3cret", b'{"a":1}'))

    def test_signature_changes_with_body(self):
        a = wh.sign_payload("s3cret", b'{"a":1}')
        b = wh.sign_payload("s3cret", b'{"a":2}')
        self.assertNotEqual(a, b)

    def test_signature_changes_with_secret(self):
        a = wh.sign_payload("one", b'{"a":1}')
        b = wh.sign_payload("two", b'{"a":1}')
        self.assertNotEqual(a, b)

    def test_verify_accepts_its_own_signature(self):
        body = b'{"event":"push"}'
        self.assertTrue(wh.verify_signature("k", body, wh.sign_payload("k", body)))

    def test_verify_rejects_a_tampered_body(self):
        body = b'{"event":"push"}'
        sig = wh.sign_payload("k", body)
        self.assertFalse(wh.verify_signature("k", b'{"event":"pwned"}', sig))

    def test_verify_rejects_the_wrong_secret(self):
        body = b'{"event":"push"}'
        self.assertFalse(wh.verify_signature("k", body, wh.sign_payload("other", body)))


class SubscriptionTests(WebhookTestBase):
    def test_star_subscribes_to_everything(self):
        hook = self.make_hook(events="*")
        self.assertTrue(wh.subscribes_to(hook, wh.EVENT_PUSH))
        self.assertTrue(wh.subscribes_to(hook, wh.EVENT_RELEASE_PUBLISHED))

    def test_named_events_filter(self):
        hook = self.make_hook(events="push,tag.created")
        self.assertTrue(wh.subscribes_to(hook, wh.EVENT_PUSH))
        self.assertTrue(wh.subscribes_to(hook, wh.EVENT_TAG_CREATED))
        self.assertFalse(wh.subscribes_to(hook, wh.EVENT_PR_MERGED))

    def test_repository_hooks_do_not_leak_across_repositories(self):
        self.make_hook(repository_id="repo1")
        found = wh.hooks_for(self.db, wh.EVENT_PUSH, "repo2")
        self.assertEqual(found, [])

    def test_global_hook_fires_for_every_repository(self):
        self.make_hook(repository_id=None)
        self.assertEqual(len(wh.hooks_for(self.db, wh.EVENT_PUSH, "repo1")), 1)
        self.assertEqual(len(wh.hooks_for(self.db, wh.EVENT_PUSH, "repo99")), 1)

    def test_inactive_hooks_are_skipped(self):
        self.make_hook(active=False)
        self.assertEqual(wh.hooks_for(self.db, wh.EVENT_PUSH, "repo1"), [])


class DeliveryTests(WebhookTestBase):
    def test_delivery_reaches_the_receiver_with_the_event_header(self):
        hook = self.make_hook()
        wh.dispatch(self.db, wh.EVENT_PUSH, "repo1",
                    {"branch": "main"}, background=False)
        self.assertEqual(len(_Receiver.received), 1)
        self.assertEqual(_Receiver.received[0]["headers"][wh.EVENT_HEADER], wh.EVENT_PUSH)

    def test_payload_is_wrapped_in_an_envelope(self):
        self.make_hook()
        wh.dispatch(self.db, wh.EVENT_PUSH, "repo1", {"branch": "main"}, background=False)
        body = json.loads(_Receiver.received[0]["body"])
        self.assertEqual(body["event"], wh.EVENT_PUSH)
        self.assertEqual(body["repository_id"], "repo1")
        self.assertEqual(body["data"]["branch"], "main")

    def test_receiver_can_verify_the_signature_over_the_wire_bytes(self):
        """The whole point of signing: verify the exact transmitted bytes."""
        self.make_hook(secret="shared-secret")
        wh.dispatch(self.db, wh.EVENT_PUSH, "repo1", {"branch": "main"}, background=False)
        sent = _Receiver.received[0]
        header = sent["headers"][wh.SIGNATURE_HEADER]
        self.assertTrue(wh.verify_signature("shared-secret", sent["body"], header))

    def test_no_signature_header_when_no_secret(self):
        self.make_hook(secret=None)
        wh.dispatch(self.db, wh.EVENT_PUSH, "repo1", {}, background=False)
        self.assertNotIn(wh.SIGNATURE_HEADER, _Receiver.received[0]["headers"])

    def test_success_is_recorded(self):
        hook = self.make_hook()
        wh.dispatch(self.db, wh.EVENT_PUSH, "repo1", {}, background=False)
        rows = self.db.query(WebhookDelivery).filter(
            WebhookDelivery.webhook_id == hook.id).all()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].success)
        self.assertEqual(rows[0].status_code, 200)

    def test_a_5xx_is_retried_then_recorded_as_failed(self):
        _Receiver.status_to_return = 500
        hook = self.make_hook()
        wh.dispatch(self.db, wh.EVENT_PUSH, "repo1", {}, background=False)
        rows = self.db.query(WebhookDelivery).filter(
            WebhookDelivery.webhook_id == hook.id).all()
        self.assertEqual(len(rows), wh.MAX_ATTEMPTS)
        self.assertTrue(all(not r.success for r in rows))

    def test_a_4xx_is_not_retried(self):
        """The receiver understood and refused; repeating cannot change that."""
        _Receiver.status_to_return = 404
        hook = self.make_hook()
        wh.dispatch(self.db, wh.EVENT_PUSH, "repo1", {}, background=False)
        rows = self.db.query(WebhookDelivery).filter(
            WebhookDelivery.webhook_id == hook.id).all()
        self.assertEqual(len(rows), 1)

    def test_a_transient_failure_recovers_on_retry(self):
        _Receiver.fail_times = 1
        hook = self.make_hook()
        wh.dispatch(self.db, wh.EVENT_PUSH, "repo1", {}, background=False)
        rows = self.db.query(WebhookDelivery).order_by(WebhookDelivery.id).all()
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0].success)
        self.assertTrue(rows[1].success)

    def test_an_unreachable_url_is_recorded_not_raised(self):
        """A dead integration must never break the operation that emitted it."""
        hook = self.make_hook(url="http://127.0.0.1:9/definitely-closed")
        queued = wh.dispatch(self.db, wh.EVENT_PUSH, "repo1", {}, background=False)
        self.assertEqual(queued, 1)
        rows = self.db.query(WebhookDelivery).all()
        self.assertTrue(rows)
        self.assertTrue(all(r.error for r in rows))

    def test_dispatch_with_no_hooks_is_a_no_op(self):
        self.assertEqual(wh.dispatch(self.db, wh.EVENT_PUSH, "repo1", {}, background=False), 0)

    def test_only_subscribed_hooks_receive(self):
        self.make_hook(events="tag.created")
        wh.dispatch(self.db, wh.EVENT_PUSH, "repo1", {}, background=False)
        self.assertEqual(_Receiver.received, [])


class SerialisationTests(WebhookTestBase):
    def test_secret_is_withheld_by_default(self):
        hook = self.make_hook(secret="top-secret")
        blob = wh.serialize(hook)
        self.assertNotIn("secret", blob)
        self.assertTrue(blob["has_secret"])
        self.assertNotIn("top-secret", repr(blob))

    def test_secret_included_only_when_explicitly_asked(self):
        hook = self.make_hook(secret="top-secret")
        self.assertEqual(wh.serialize(hook, include_secret=True)["secret"], "top-secret")

    def test_events_are_returned_as_a_list(self):
        hook = self.make_hook(events="push,tag.created")
        self.assertEqual(wh.serialize(hook)["events"], ["push", "tag.created"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
