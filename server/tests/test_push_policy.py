"""Tests for .foxignore matching and push admission control.

These cover the logic that keeps build artifacts and oversized files out of the object
store. The paths used below are real ones taken from the production database, which is
92% archives by size precisely because none of this existed.
"""
import base64
import os
import sys
import unittest

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from fastapi import HTTPException  # noqa: E402

from app.services.ignore_rules import (  # noqa: E402
    IGNORE_FILENAME,
    IgnoreMatcher,
    build_matcher,
)
from app.services import push_policy  # noqa: E402


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class IgnoreMatcherTests(unittest.TestCase):
    def test_defaults_block_the_artifacts_that_bloated_the_store(self):
        matcher = IgnoreMatcher.default()
        for path in [
            "sm-suit.zip",
            "SMAT.zip",
            "DEA-101/DEA-101.zip",
            "uploads/Wireshark-4.4.6-x64.exe",
            "TVPasswordProtection 2/app/debug/app-debug.apk",
            "JS-CTSP/OfflineVulnDB.bacpac",
            "NSAT/Bjorn/NSATVENV/lib/python3.10/site-packages/numpy.libs/libopenblas.so",
            "node_modules/react/index.js",
            "a/b/__pycache__/mod.pyc",
            "build/main.o",
            ".git/config",
            ".fox/index",
            "server.log",
        ]:
            self.assertTrue(matcher.is_ignored(path), f"should be ignored: {path}")

    def test_defaults_do_not_catch_ordinary_source(self):
        matcher = IgnoreMatcher.default()
        for path in [
            "src/main.py",
            "README.md",
            "app/api/routes/commits.py",
            "src/components/Button.jsx",
            "requirements.txt",
            ".foxignore",
            ".env.example",
            # near-misses that a sloppy substring match would wrongly exclude
            "distributed/main.py",
            "binary_tree.py",
            "my.zipper.py",
            "outer/space.py",
        ]:
            self.assertFalse(matcher.is_ignored(path), f"should be kept: {path}")

    def test_gitignore_syntax(self):
        matcher = IgnoreMatcher.from_text(
            "\n".join([
                "*.zip",
                "!keep/important.zip",   # negation re-includes
                "/secrets.env",          # anchored to root
                "docs/**/*.tmp",         # ** spans directories
                "logs/",                 # directory and everything under it
                "# a comment",
                "",
            ])
        )
        for path, expected in [
            ("a/b.zip", True),
            ("keep/important.zip", False),
            ("secrets.env", True),
            ("sub/secrets.env", False),
            ("docs/a/b/c.tmp", True),
            ("docs/x.tmp", True),
            ("logs/2024/app.txt", True),
            ("mylogs/app.txt", False),
            ("src/app.py", False),
        ]:
            self.assertEqual(matcher.is_ignored(path), expected, f"{path}")

    def test_dotfile_prefix_is_not_stripped(self):
        # Regression: a character-set lstrip("./") turned ".git/config" into "git/config".
        self.assertTrue(IgnoreMatcher.default().is_ignored(".git/config"))
        self.assertTrue(IgnoreMatcher.default().is_ignored("./build/main.o"))

    def test_repository_foxignore_replaces_defaults(self):
        # A repo that genuinely tracks archives should not have to out-negate a hidden list.
        matcher = build_matcher("*.secret\n")
        self.assertTrue(matcher.is_ignored("keys.secret"))
        self.assertFalse(matcher.is_ignored("release.zip"))

    def test_blank_or_missing_foxignore_falls_back_to_defaults(self):
        self.assertTrue(build_matcher(None).is_ignored("a.zip"))
        self.assertTrue(build_matcher("").is_ignored("a.zip"))
        self.assertTrue(build_matcher("# only a comment\n").is_ignored("a.zip"))


class ScreenCommitFilesTests(unittest.TestCase):
    def setUp(self):
        self._orig = (
            push_policy.MAX_PUSH_FILE_BYTES,
            push_policy.MAX_PUSH_TOTAL_BYTES,
            push_policy.ENFORCE_IGNORE_ON_PUSH,
        )

    def tearDown(self):
        (
            push_policy.MAX_PUSH_FILE_BYTES,
            push_policy.MAX_PUSH_TOTAL_BYTES,
            push_policy.ENFORCE_IGNORE_ON_PUSH,
        ) = self._orig

    def test_ignored_paths_are_dropped_not_rejected(self):
        commit = {"files": {
            "src/main.py": b64(b"print('hi')"),
            "build/app.exe": b64(b"MZ binary"),
            "release.zip": b64(b"PK\x03\x04"),
        }}
        report = push_policy.screen_commit_files(commit)

        self.assertEqual(set(commit["files"]), {"src/main.py"})
        self.assertEqual(
            sorted(path for path, _pattern in report["dropped"]),
            ["build/app.exe", "release.zip"],
        )
        # the report names the rule so the developer can see why
        self.assertTrue(all(pattern for _path, pattern in report["dropped"]))

    def test_incoming_foxignore_applies_to_its_own_push(self):
        commit = {"files": {
            IGNORE_FILENAME: b64(b"*.data\n"),
            "keep.zip": b64(b"PK"),          # allowed: repo rules replace the defaults
            "dropme.data": b64(b"junk"),
        }}
        report = push_policy.screen_commit_files(commit)

        self.assertIn(IGNORE_FILENAME, commit["files"])
        self.assertIn("keep.zip", commit["files"])
        self.assertNotIn("dropme.data", commit["files"])
        self.assertEqual([p for p, _ in report["dropped"]], ["dropme.data"])

    def test_branch_foxignore_used_when_push_has_none(self):
        commit = {"files": {"notes.txt": b64(b"x"), "a.zip": b64(b"PK")}}
        push_policy.screen_commit_files(commit, fallback_foxignore_text="*.txt\n")

        self.assertIn("a.zip", commit["files"])       # defaults not in play
        self.assertNotIn("notes.txt", commit["files"])

    def test_foxignore_itself_is_always_tracked(self):
        commit = {"files": {IGNORE_FILENAME: b64(b"*\n")}}
        push_policy.screen_commit_files(commit)
        self.assertIn(IGNORE_FILENAME, commit["files"])

    def test_oversized_file_is_refused_with_413(self):
        push_policy.MAX_PUSH_FILE_BYTES = 1024
        push_policy.MAX_PUSH_TOTAL_BYTES = 0

        commit = {"files": {"data.bin": b64(b"A" * 5000)}}
        with self.assertRaises(HTTPException) as ctx:
            push_policy.screen_commit_files(commit)

        self.assertEqual(ctx.exception.status_code, 413)
        self.assertIn("data.bin", ctx.exception.detail)
        self.assertIn(IGNORE_FILENAME, ctx.exception.detail)   # tells them how to fix it

    def test_total_push_size_is_capped(self):
        push_policy.MAX_PUSH_FILE_BYTES = 0
        push_policy.MAX_PUSH_TOTAL_BYTES = 4096

        commit = {"files": {f"f{i}.bin": b64(b"A" * 1000) for i in range(10)}}
        with self.assertRaises(HTTPException) as ctx:
            push_policy.screen_commit_files(commit)

        self.assertEqual(ctx.exception.status_code, 413)

    def test_limits_measure_decoded_bytes_not_base64(self):
        # base64 inflates by ~33%; a 3 KB file must not trip a 3.5 KB cap.
        push_policy.MAX_PUSH_FILE_BYTES = 3500
        push_policy.MAX_PUSH_TOTAL_BYTES = 0

        payload = b64(b"A" * 3000)
        self.assertGreater(len(payload), 3500)          # the encoded form would trip it
        push_policy.screen_commit_files({"files": {"f.bin": payload}})   # decoded does not

    def test_ignored_files_do_not_count_toward_the_size_cap(self):
        # A huge artifact that .foxignore already excludes should not fail the push.
        push_policy.MAX_PUSH_FILE_BYTES = 1024
        push_policy.MAX_PUSH_TOTAL_BYTES = 2048

        commit = {"files": {
            "huge.zip": b64(b"A" * 100000),
            "src/main.py": b64(b"print(1)"),
        }}
        report = push_policy.screen_commit_files(commit)

        self.assertEqual(set(commit["files"]), {"src/main.py"})
        self.assertEqual([p for p, _ in report["dropped"]], ["huge.zip"])

    def test_zero_disables_a_limit(self):
        push_policy.MAX_PUSH_FILE_BYTES = 0
        push_policy.MAX_PUSH_TOTAL_BYTES = 0
        commit = {"files": {"big.bin": b64(b"A" * 2_000_000)}}
        push_policy.screen_commit_files(commit, fallback_foxignore_text="# nothing\n")
        self.assertIn("big.bin", commit["files"])

    def test_empty_commit_is_a_no_op(self):
        self.assertEqual(push_policy.screen_commit_files({})["kept"], 0)
        self.assertEqual(push_policy.screen_commit_files({"files": {}})["kept"], 0)

    def test_malformed_payload_does_not_crash(self):
        commit = {"files": {"a.txt": "!!!not base64!!!", "b.txt": None}}
        push_policy.screen_commit_files(commit)   # must not raise


if __name__ == "__main__":
    unittest.main()
