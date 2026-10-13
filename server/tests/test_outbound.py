"""Outbound requests to configured URLs (webhooks, push-rule policy services).

The server must not be usable to reach places its users cannot: its own loopback port,
the LAN, the cloud metadata service. These pin the rules and the mechanics that enforce
them -- including that the connection goes to the address that was checked.
"""
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from app.services import outbound  # noqa: E402


class _Server(BaseHTTPRequestHandler):
    hits = []
    redirect = None
    big = False

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Server.hits.append({"path": self.path, "host": self.headers.get("Host")})
        if _Server.redirect:
            self.send_response(302)
            self.send_header("Location", _Server.redirect)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"x" * (outbound.MAX_RESPONSE_BYTES * 3) if _Server.big else b"pong")

    def log_message(self, *args):
        pass


class ClassificationTests(unittest.TestCase):
    def test_public_addresses_are_allowed(self):
        for address in ("93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"):
            with self.subTest(address=address):
                self.assertIsNone(outbound.blocked_reason(address, allow_private=False))

    def test_loopback_link_local_and_friends_are_refused(self):
        for address, word in (("127.0.0.1", "loopback"), ("127.8.8.8", "loopback"), ("::1", "loopback"),
                              ("169.254.169.254", "link-local"), ("fe80::1", "link-local"),
                              ("0.0.0.0", "unspecified"), ("224.0.0.1", "multicast"),
                              ("240.0.0.1", "reserved")):
            with self.subTest(address=address):
                self.assertIn(word, outbound.blocked_reason(address, allow_private=True))

    def test_private_ranges_need_the_setting(self):
        for address in ("10.0.0.5", "172.16.3.4", "192.168.1.10", "100.64.0.1", "fd00::5"):
            with self.subTest(address=address):
                self.assertIn("private", outbound.blocked_reason(address, allow_private=False))
                self.assertIsNone(outbound.blocked_reason(address, allow_private=True))

    def test_ipv4_mapped_ipv6_is_judged_as_ipv4(self):
        self.assertIn("loopback", outbound.blocked_reason("::ffff:127.0.0.1", allow_private=True))


class ResolveTests(unittest.TestCase):
    def test_only_http_and_https(self):
        for url in ("file:///etc/passwd", "gopher://x", "ftp://example.com/", "example.com/hook"):
            with self.subTest(url=url):
                with self.assertRaises(outbound.OutboundBlocked):
                    outbound.resolve(url)

    def test_every_address_a_name_resolves_to_must_pass(self):
        """One public and one private answer is not good enough: the private one could be used."""
        answers = [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 80)),
                   (socket.AF_INET, 1, 6, "", ("10.0.0.7", 80))]
        with mock.patch.object(outbound.socket, "getaddrinfo", return_value=answers):
            with self.assertRaises(outbound.OutboundBlocked) as ctx:
                outbound.resolve("http://mixed.example/hook")
        self.assertIn("10.0.0.7", ctx.exception.message)

    def test_allowlisted_names_networks_and_wildcards(self):
        private = [(socket.AF_INET, 1, 6, "", ("10.0.5.20", 80))]
        with mock.patch.object(outbound.socket, "getaddrinfo", return_value=private):
            for allowed in ("ci.corp.example", "*.corp.example", "10.0.5.0/24", "10.0.5.20"):
                with self.subTest(allowed=allowed), mock.patch.dict(
                        os.environ, {"FOXNEST_OUTBOUND_ALLOWED_HOSTS": allowed}):
                    target = outbound.resolve("http://ci.corp.example/hook")
                    self.assertEqual(target.addresses, ["10.0.5.20"])
            with mock.patch.dict(os.environ, {"FOXNEST_OUTBOUND_ALLOWED_HOSTS": "other.example"}):
                with self.assertRaises(outbound.OutboundBlocked):
                    outbound.resolve("http://ci.corp.example/hook")

    def test_an_unresolvable_name_is_allowed_to_be_saved_with_a_warning(self):
        with mock.patch.object(outbound.socket, "getaddrinfo", side_effect=socket.gaierror):
            warning = outbound.check_url("https://not-yet.example/hook")
        self.assertIn("checked again before each delivery", warning)

    def test_the_error_names_the_address_and_the_setting(self):
        with self.assertRaises(outbound.OutboundBlocked) as ctx:
            outbound.resolve("http://127.0.0.1:33333/api")
        self.assertIn("127.0.0.1", ctx.exception.message)
        self.assertIn("FOXNEST_OUTBOUND_ALLOWED_HOSTS", ctx.exception.message)


class PostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), _Server)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        _Server.hits, _Server.redirect, _Server.big = [], None, False
        env = mock.patch.dict(os.environ, {"FOXNEST_OUTBOUND_ALLOWED_HOSTS": "127.0.0.1,pinned.test"})
        env.start()
        self.addCleanup(env.stop)

    def test_the_connection_goes_to_the_checked_address_with_the_original_host(self):
        """DNS answers once; a second, different answer at connect time cannot be used."""
        calls = []

        def resolver(host, port, *args, **kwargs):
            calls.append(host)
            return [(socket.AF_INET, 1, 6, "", ("127.0.0.1", port))]

        with mock.patch.object(outbound.socket, "getaddrinfo", side_effect=resolver):
            response = outbound.post(f"http://pinned.test:{self.port}/hook", data=b"{}",
                                     headers={}, timeout=5)
        self.assertEqual(response.status_code, 200)
        # The name is looked up once, by the check. The connection then asks only about
        # the checked address itself (a literal, which needs no DNS).
        self.assertEqual(calls.count("pinned.test"), 1)
        self.assertEqual(set(calls) - {"pinned.test"}, {"127.0.0.1"})
        self.assertEqual(_Server.hits[0]["host"], f"pinned.test:{self.port}")

    def test_redirects_are_returned_not_followed(self):
        _Server.redirect = f"http://127.0.0.1:{self.port}/second"
        response = outbound.post(f"http://127.0.0.1:{self.port}/first", data=b"{}", headers={}, timeout=5)
        self.assertEqual(response.status_code, 302)
        self.assertEqual([hit["path"] for hit in _Server.hits], ["/first"])

    def test_the_reply_is_capped(self):
        _Server.big = True
        response = outbound.post(f"http://127.0.0.1:{self.port}/big", data=b"{}", headers={}, timeout=5)
        self.assertEqual(len(response.content), outbound.MAX_RESPONSE_BYTES)

    def test_a_blocked_destination_is_never_contacted(self):
        with mock.patch.dict(os.environ, {"FOXNEST_OUTBOUND_ALLOWED_HOSTS": ""}):
            with self.assertRaises(outbound.OutboundBlocked):
                outbound.post(f"http://127.0.0.1:{self.port}/hook", data=b"{}", headers={}, timeout=5)
        self.assertEqual(_Server.hits, [])


if __name__ == "__main__":
    unittest.main()
