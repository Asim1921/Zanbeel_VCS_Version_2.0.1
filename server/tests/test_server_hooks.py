"""Tests for the server-side pre-receive policy engine."""
import base64
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
from database.crud import RepositoryCRUD, UserCRUD  # noqa: E402
from database.models import ServerHook  # noqa: E402
from app.services import server_hooks as sh  # noqa: E402


def b64(text):
    return base64.b64encode(text.encode()).decode()


def commit(message="a reasonable commit message", files=None):
    return {"id": "a" * 40, "message": message, "author": "dev", "files": files or {}}


class ConfigValidationTests(unittest.TestCase):
    def test_unknown_rule_is_named(self):
        with self.assertRaises(sh.HookError) as ctx:
            sh.validate_config({"delete_the_database": True})
        self.assertIn("delete_the_database", ctx.exception.message)

    def test_bad_regex_is_rejected_at_save_time(self):
        """Better here than on some unlucky developer's push."""
        with self.assertRaises(sh.HookError):
            sh.validate_config({"commit_message_pattern": "([unclosed"})

    def test_bad_content_regex_is_rejected(self):
        with self.assertRaises(sh.HookError):
            sh.validate_config({"forbidden_content": ["(("]})

    def test_non_integer_min_length_is_rejected(self):
        with self.assertRaises(sh.HookError):
            sh.validate_config({"commit_message_min_length": "many"})

    def test_negative_min_length_is_rejected(self):
        with self.assertRaises(sh.HookError):
            sh.validate_config({"commit_message_min_length": -1})

    def test_non_http_external_url_is_rejected(self):
        with self.assertRaises(sh.HookError):
            sh.validate_config({"external_url": "file:///etc/passwd"})

    def test_valid_config_passes_through(self):
        cleaned = sh.validate_config({
            "commit_message_pattern": r"^\[\w+\]",
            "commit_message_min_length": 10,
            "forbidden_paths": ["*.env"],
        })
        self.assertEqual(cleaned["commit_message_min_length"], 10)
        self.assertEqual(cleaned["forbidden_paths"], ["*.env"])

    def test_empty_config_is_valid(self):
        self.assertEqual(sh.validate_config({}), {})


class MessageRuleTests(unittest.TestCase):
    def test_short_message_is_rejected_with_the_numbers(self):
        reasons = sh.evaluate({"commit_message_min_length": 20}, commit("wip"), None)
        self.assertTrue(reasons)
        self.assertIn("20", reasons[0])

    def test_long_enough_message_passes(self):
        self.assertEqual(sh.evaluate({"commit_message_min_length": 5}, commit("a proper message"), None), [])

    def test_pattern_must_match(self):
        config = {"commit_message_pattern": r"^\[(FIX|FEAT)\]"}
        self.assertTrue(sh.evaluate(config, commit("random text"), None))
        self.assertEqual(sh.evaluate(config, commit("[FIX] corrected the thing"), None), [])

    def test_both_message_rules_report_together(self):
        """One round-trip per push, not one per broken rule."""
        config = {"commit_message_min_length": 50, "commit_message_pattern": r"^\[FIX\]"}
        self.assertEqual(len(sh.evaluate(config, commit("nope"), None)), 2)


class PathRuleTests(unittest.TestCase):
    def test_forbidden_path_blocks(self):
        config = {"forbidden_paths": ["*.env", "secrets/**"]}
        reasons = sh.evaluate(config, commit(files={".env": b64("KEY=1")}), None)
        self.assertTrue(reasons)
        self.assertIn(".env", reasons[0])

    def test_allowed_path_passes(self):
        config = {"forbidden_paths": ["*.env"]}
        self.assertEqual(sh.evaluate(config, commit(files={"main.py": b64("x=1")}), None), [])

    def test_directory_glob_matches_nested_files(self):
        config = {"forbidden_paths": ["secrets/**"]}
        reasons = sh.evaluate(config, commit(files={"secrets/deep/key.pem": b64("k")}), None)
        self.assertTrue(reasons)

    def test_protected_path_blocks_an_unlisted_user(self):
        class U:
            username = "dev"
        config = {"protected_paths": ["deploy/*"], "protected_paths_allowed_users": ["release-bot"]}
        reasons = sh.evaluate(config, commit(files={"deploy/prod.yml": b64("a")}), U())
        self.assertTrue(reasons)
        self.assertIn("release-bot", reasons[0])

    def test_protected_path_allows_a_listed_user(self):
        class U:
            username = "release-bot"
        config = {"protected_paths": ["deploy/*"], "protected_paths_allowed_users": ["release-bot"]}
        self.assertEqual(sh.evaluate(config, commit(files={"deploy/prod.yml": b64("a")}), U()), [])

    def test_allow_list_is_case_insensitive(self):
        class U:
            username = "Release-Bot"
        config = {"protected_paths": ["deploy/*"], "protected_paths_allowed_users": ["release-bot"]}
        self.assertEqual(sh.evaluate(config, commit(files={"deploy/prod.yml": b64("a")}), U()), [])

    def test_protected_with_no_allow_list_freezes_the_path(self):
        class U:
            username = "anyone"
        config = {"protected_paths": ["generated/*"]}
        reasons = sh.evaluate(config, commit(files={"generated/api.ts": b64("a")}), U())
        self.assertTrue(reasons)
        self.assertIn("nobody", reasons[0])


class SizeAndContentTests(unittest.TestCase):
    def test_oversized_file_is_rejected_with_its_size(self):
        config = {"max_file_bytes": 10}
        reasons = sh.evaluate(config, commit(files={"big.bin": b64("x" * 100)}), None)
        self.assertTrue(reasons)
        self.assertIn("big.bin", reasons[0])

    def test_small_file_passes(self):
        config = {"max_file_bytes": 1000}
        self.assertEqual(sh.evaluate(config, commit(files={"a.txt": b64("hi")}), None), [])

    def test_forbidden_content_catches_a_committed_secret(self):
        config = {"forbidden_content": [r"AKIA[0-9A-Z]{16}"]}
        payload = b64("aws_key = AKIAIOSFODNN7EXAMPLE\n")
        reasons = sh.evaluate(config, commit(files={"config.py": payload}), None)
        self.assertTrue(reasons)
        self.assertIn("config.py", reasons[0])

    def test_clean_content_passes(self):
        config = {"forbidden_content": [r"AKIA[0-9A-Z]{16}"]}
        self.assertEqual(
            sh.evaluate(config, commit(files={"config.py": b64("x = 1")}), None), []
        )

    def test_binary_files_are_not_scanned(self):
        """A regex hit inside a PNG is noise, and scanning them is wasted time."""
        config = {"forbidden_content": ["secret"]}
        blob = base64.b64encode(b"\x89PNG\x00\x00secret").decode()
        self.assertEqual(sh.evaluate(config, commit(files={"logo.png": blob}), None), [])

    def test_one_reason_per_file_even_with_many_hits(self):
        config = {"forbidden_content": ["alpha", "beta"]}
        payload = b64("alpha beta alpha beta")
        reasons = sh.evaluate(config, commit(files={"a.txt": payload}), None)
        self.assertEqual(len(reasons), 1)


# --------------------------------------------------------------------------
# External policy service
# --------------------------------------------------------------------------

class _Policy(BaseHTTPRequestHandler):
    allow = True
    body_seen = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        _Policy.body_seen.append(json.loads(self.rfile.read(n)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        payload = {"allow": True} if _Policy.allow else {"allow": False, "message": "denied by policy"}
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *a):
        pass


class ExternalPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), _Policy)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.port}/policy"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        _Policy.allow = True
        _Policy.body_seen = []

    def test_allow_lets_the_push_through(self):
        self.assertEqual(sh.evaluate({"external_url": self.url}, commit(), None), [])

    def test_deny_blocks_with_the_services_message(self):
        _Policy.allow = False
        reasons = sh.evaluate({"external_url": self.url}, commit(), None)
        self.assertEqual(reasons, ["denied by policy"])

    def test_file_contents_are_never_sent(self):
        """A policy endpoint has no business receiving source code."""
        payload = b64("SUPER SECRET SOURCE")
        sh.evaluate({"external_url": self.url}, commit(files={"a.py": payload}), None)
        body = json.dumps(_Policy.body_seen[0])
        self.assertIn("a.py", body)
        self.assertNotIn("SUPER SECRET SOURCE", body)
        self.assertNotIn(payload, body)

    def test_an_unreachable_service_fails_closed(self):
        """Otherwise the hook is bypassed by knocking the service over."""
        reasons = sh.evaluate({"external_url": "http://127.0.0.1:9/nope"}, commit(), None)
        self.assertTrue(reasons)
        self.assertIn("unreachable", reasons[0])


class RunPreReceiveTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = UserCRUD.create_user(self.db, username="lead", email="l@e.com", role="team_lead")
        self.repo = RepositoryCRUD.create_repository(self.db, "lead", "proj", "d")

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def add_hook(self, config, enabled=True, repository_id="__self__", name="policy"):
        hook = ServerHook(
            repository_id=self.repo.id if repository_id == "__self__" else repository_id,
            name=name,
            hook_type=sh.HOOK_PRE_RECEIVE,
            enabled=enabled,
            config_json=json.dumps(config),
        )
        self.db.add(hook)
        self.db.commit()
        return hook

    def test_no_hooks_accepts_everything(self):
        sh.run_pre_receive(self.db, self.repo, commit("x"), self.user)

    def test_a_matching_hook_rejects(self):
        self.add_hook({"commit_message_min_length": 100})
        with self.assertRaises(sh.HookRejection) as ctx:
            sh.run_pre_receive(self.db, self.repo, commit("short"), self.user)
        self.assertEqual(ctx.exception.hook_name, "policy")

    def test_a_disabled_hook_does_not_run(self):
        self.add_hook({"commit_message_min_length": 100}, enabled=False)
        sh.run_pre_receive(self.db, self.repo, commit("short"), self.user)

    def test_a_global_hook_applies_to_this_repository(self):
        self.add_hook({"commit_message_min_length": 100}, repository_id=None, name="global")
        with self.assertRaises(sh.HookRejection) as ctx:
            sh.run_pre_receive(self.db, self.repo, commit("short"), self.user)
        self.assertEqual(ctx.exception.hook_name, "global")

    def test_another_repositorys_hook_does_not_apply(self):
        self.add_hook({"commit_message_min_length": 100}, repository_id="someone-else")
        sh.run_pre_receive(self.db, self.repo, commit("short"), self.user)

    def test_corrupt_config_rejects_rather_than_waving_through(self):
        hook = self.add_hook({})
        hook.config_json = "{not json"
        self.db.commit()
        with self.assertRaises(sh.HookRejection):
            sh.run_pre_receive(self.db, self.repo, commit("anything"), self.user)

    def test_rejection_carries_every_reason(self):
        self.add_hook({"commit_message_min_length": 50, "forbidden_paths": ["*.env"]})
        with self.assertRaises(sh.HookRejection) as ctx:
            sh.run_pre_receive(
                self.db, self.repo, commit("no", files={".env": b64("K=1")}), self.user
            )
        self.assertEqual(len(ctx.exception.reasons), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
