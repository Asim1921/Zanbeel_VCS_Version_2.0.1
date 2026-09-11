"""Every endpoint must require authentication unless it is deliberately public.

This exists because `POST /api/repository/{id}/push` and `POST /api/repository/create`
shipped with no auth dependency at all, letting any caller who could reach the port act
as any user. The allow-list below is the entire public surface; adding an endpoint
without an auth dependency fails this test until it is either secured or consciously
added to the list.
"""
import os
import re
import sys
import unittest

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

ROUTES_DIR = os.path.join(SERVER_ROOT, "app", "api", "routes")

#: Endpoints that must work without credentials, and why.
PUBLIC = {
    ("POST", "/api/auth/login"):             "obtaining credentials cannot require credentials",
    ("POST", "/api/auth/bootstrap-password"): "gated by FOXNEST_PASSWORD_SETUP_KEY instead",
    ("POST", "/api/auth/register-request"):  "self-service signup, reviewed before it grants access",
    ("GET", "/"):                            "health check",
    ("GET", "/api/"):                        "API index",
    ("GET", "/api/download/client"):         "distributes the CLI",
    ("GET", "/api/download/fox.bat"):        "distributes the CLI launcher",
    # A machine needs the extension before it can obtain a credential, so
    # requiring one here would be circular. Nothing served is secret: it is the
    # same client every developer already runs.
    ("GET", "/api/download/extension"):      "distributes the VS Code extension",
    ("GET", "/api/download/extension/info"): "reports which extension version this server offers",
    # SSH key sign-in is how a client obtains credentials, so it cannot require
    # them. Safe to expose: a challenge is a single-use random nonce that is
    # worthless without the private key, both endpoints answer identically for
    # unknown accounts so neither enumerates users, and the nonce is destroyed
    # on first use whether verification passed or failed.
    ("POST", "/api/auth/ssh/challenge"):     "issues a one-time nonce; signing it is the authentication",
    ("POST", "/api/auth/ssh/verify"):        "exchanges a signed nonce for a session, like /auth/login",
}

ROUTE_RE = re.compile(
    r'@router\.(get|post|put|delete|patch)\("([^"]+)"\)\s*\nasync def (\w+)\(([\s\S]*?)\):'
)
AUTH_MARKERS = ("current_user", "require_admin_user", "require_reviewer_user",
                "get_optional_user")


def declared_endpoints():
    found = []
    for name in sorted(os.listdir(ROUTES_DIR)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(ROUTES_DIR, name), encoding="utf-8") as handle:
            source = handle.read()
        for method, path, handler, args in ROUTE_RE.findall(source):
            found.append((method.upper(), path, handler, args, name))
    return found


class EndpointAuthTests(unittest.TestCase):
    def test_no_unexpected_public_endpoints(self):
        unguarded = [
            (method, path, handler, module)
            for method, path, handler, args, module in declared_endpoints()
            if not any(marker in args for marker in AUTH_MARKERS)
        ]
        surprises = [
            (m, p, h, mod) for m, p, h, mod in unguarded if (m, p) not in PUBLIC
        ]
        self.assertEqual(
            surprises, [],
            "These endpoints take no authenticated user. Add an auth dependency, or add "
            "them to PUBLIC with a justification:\n  "
            + "\n  ".join(f"{m} {p}  ({h} in {mod})" for m, p, h, mod in surprises),
        )

    def test_the_endpoints_that_were_vulnerable_are_now_guarded(self):
        must_be_guarded = {
            ("POST", "/api/repository/{repo_id}/push"),
            ("POST", "/api/repository/create"),
            ("GET", "/api/activities"),
            ("POST", "/api/repository/{repo_id}/generate-docs"),
            ("POST", "/api/repository/{repo_id}/generate-project-docs"),
            ("GET", "/api/repository/{repo_id}/docs-status"),
        }
        by_key = {
            (m, p): args for m, p, _h, args, _mod in declared_endpoints()
        }
        for key in must_be_guarded:
            self.assertIn(key, by_key, f"{key[0]} {key[1]} no longer exists")
            self.assertTrue(
                any(marker in by_key[key] for marker in AUTH_MARKERS),
                f"{key[0]} {key[1]} lost its authentication dependency",
            )

    def test_public_list_has_no_stale_entries(self):
        # Keeps the allow-list honest: an entry that no longer matches a real unguarded
        # endpoint is either a typo or a leftover, and either way hides the next mistake.
        unguarded = {
            (m, p) for m, p, _h, args, _mod in declared_endpoints()
            if not any(marker in args for marker in AUTH_MARKERS)
        }
        stale = sorted(set(PUBLIC) - unguarded)
        self.assertEqual(stale, [], f"PUBLIC lists endpoints that are not public: {stale}")

    def test_every_route_module_was_scanned(self):
        # A guard against the regex silently matching nothing after a refactor.
        self.assertGreater(len(declared_endpoints()), 80)


if __name__ == "__main__":
    unittest.main()
