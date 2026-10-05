"""The fox CLI's merge, driven for real in a throwaway repository.

Before this work a clean `fox merge` wrote the merged files and never committed; a
conflicted one said "commit the merge" with nothing remembering it was a merge; and
`fox pr resolve --submit` overwrote the edited .resolved files with the server's
conflict-marked suggestion before sending them. These pin the Git behaviour that replaced
all three.
"""
import base64
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
FOX_PATH = REPO_ROOT / "client" / "client1" / "fox.py"

spec = importlib.util.spec_from_file_location("fox_cli_under_test", FOX_PATH)
fox_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fox_module)
FoxClient = fox_module.FoxClient


class _Repo(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._home = os.environ.get("HOME"), os.environ.get("USERPROFILE")
        self.dir = Path(tempfile.mkdtemp(prefix="fox_cli_merge_"))
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.dir / "home")
        work = self.dir / "work"
        work.mkdir()
        os.chdir(work)
        self.quiet(lambda: FoxClient().init(username="dev", repo_name="demo"))

    def tearDown(self):
        os.chdir(self._cwd)
        for key, value in zip(("HOME", "USERPROFILE"), self._home):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.dir, ignore_errors=True)

    @staticmethod
    def quiet(fn):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = fn()
        return result, out.getvalue()

    def fox(self):
        return FoxClient()

    def write(self, path, text):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(text.encode() if isinstance(text, str) else text)

    def read(self, path):
        return Path(path).read_text(encoding="utf-8")

    def commit_files(self, message, files):
        for path, text in files.items():
            self.write(path, text)
        fox = self.fox()
        self.quiet(lambda: fox.add(list(files)))
        ok, out = self.quiet(lambda: fox.commit(message))
        self.assertTrue(ok, out)
        return fox.get_head_commit_id()

    def checkout(self, branch):
        ok, out = self.quiet(lambda: self.fox().checkout_branch(branch))
        self.assertTrue(ok, out)

    def head(self):
        return self.fox().get_head_commit_id()

    def parents(self, commit_id):
        return self.fox().get_commit_by_id(commit_id).get("parents")

    def tree(self, commit_id):
        return {p: c.decode() for p, c in self.fox()._commit_tree(commit_id).items()}


BASE = "1\n2\n3\n4\n5\n"


class LocalMergeTests(_Repo):
    def setUp(self):
        super().setUp()
        self.base = self.commit_files("base", {"a.txt": BASE, "b.txt": "b\n"})
        self.quiet(lambda: self.fox().create_branch("feature"))

    def diverge(self, main_a, feature_a):
        main = self.commit_files("main work", {"a.txt": main_a})
        self.checkout("feature")
        feature = self.commit_files("feature work", {"a.txt": feature_a})
        self.checkout("main")
        return main, feature

    def test_commit_builds_on_head_not_on_the_last_commit_written(self):
        self.commit_files("main only", {"main-only.txt": "m\n"})
        self.checkout("feature")
        feature = self.commit_files("feature work", {"a.txt": BASE + "6\n"})
        self.assertNotIn("main-only.txt", self.tree(feature))

    def test_separate_edits_merge_and_commit_with_two_parents(self):
        main, feature = self.diverge("ONE\n2\n3\n4\n5\n", "1\n2\n3\n4\nFIVE\n")
        ok, out = self.quiet(lambda: self.fox().merge_branch("feature"))
        self.assertTrue(ok, out)
        merge = self.head()
        self.assertEqual(self.parents(merge), [main, feature])
        self.assertEqual(self.tree(merge)["a.txt"], "ONE\n2\n3\n4\nFIVE\n")
        self.assertEqual(self.read("a.txt"), "ONE\n2\n3\n4\nFIVE\n")

    def test_a_conflict_is_recorded_and_continue_makes_the_merge_commit(self):
        main, feature = self.diverge("1\n2\nmain\n4\n5\n", "1\n2\nfeature\n4\n5\n")
        ok, out = self.quiet(lambda: self.fox().merge_branch("feature"))
        self.assertFalse(ok)
        self.assertIn("CONFLICT (content): Merge conflict in a.txt", out)
        self.assertIn("<<<<<<< main\n", self.read("a.txt"))
        self.assertIn(">>>>>>> feature\n", self.read("a.txt"))
        self.assertTrue(Path(".fox/MERGE_HEAD").exists())
        self.assertEqual(self.head(), main)

        # A plain commit would lose the merge, so it is refused.
        self.quiet(lambda: self.fox().add(["a.txt"]))
        refused, out = self.quiet(lambda: self.fox().commit("oops"))
        self.assertFalse(refused)
        self.assertIn("merge --continue", out)
        shutil.rmtree(".fox/staging")
        Path(".fox/staging").mkdir()

        # Markers left in are refused...
        ok, out = self.quiet(lambda: self.fox().merge_continue())
        self.assertFalse(ok)
        self.assertIn("a.txt", out)

        # ...and once fixed, --continue records both parents.
        self.write("a.txt", "1\n2\nboth\n4\n5\n")
        ok, out = self.quiet(lambda: self.fox().merge_continue())
        self.assertTrue(ok, out)
        merge = self.head()
        self.assertEqual(self.parents(merge), [main, feature])
        self.assertEqual(self.tree(merge)["a.txt"], "1\n2\nboth\n4\n5\n")
        self.assertFalse(Path(".fox/MERGE_HEAD").exists())

        # The next merge has nothing left to do: history knows feature is merged.
        ok, out = self.quiet(lambda: self.fox().merge_branch("feature"))
        self.assertTrue(ok)
        self.assertIn("Already up to date", out)

    def test_abort_puts_the_work_tree_back(self):
        main, _ = self.diverge("1\n2\nmain\n4\n5\n", "1\n2\nfeature\n4\n5\n")
        self.quiet(lambda: self.fox().merge_branch("feature"))
        ok, out = self.quiet(lambda: self.fox().merge_abort())
        self.assertTrue(ok, out)
        self.assertEqual(self.read("a.txt"), "1\n2\nmain\n4\n5\n")
        self.assertEqual(self.head(), main)
        self.assertFalse(Path(".fox/MERGE_STATE.json").exists())

    def test_status_lists_unmerged_paths(self):
        self.diverge("1\n2\nmain\n4\n5\n", "1\n2\nfeature\n4\n5\n")
        self.quiet(lambda: self.fox().merge_branch("feature"))
        _, out = self.quiet(lambda: self.fox().status())
        self.assertIn("You are merging 'feature' into 'main'", out)
        self.assertIn("both modified:   a.txt", out)

    def test_switching_branches_mid_merge_is_refused(self):
        self.diverge("1\n2\nmain\n4\n5\n", "1\n2\nfeature\n4\n5\n")
        self.quiet(lambda: self.fox().merge_branch("feature"))
        ok, _ = self.quiet(lambda: self.fox().checkout_branch("feature"))
        self.assertFalse(ok)

    def test_fast_forward_when_this_branch_has_nothing_new(self):
        self.checkout("feature")
        feature = self.commit_files("ahead", {"a.txt": BASE + "6\n"})
        self.checkout("main")
        ok, out = self.quiet(lambda: self.fox().merge_branch("feature"))
        self.assertTrue(ok, out)
        self.assertIn("Fast-forward", out)
        self.assertEqual(self.head(), feature)
        self.assertEqual(self.read("a.txt"), BASE + "6\n")

    def test_uncommitted_work_the_merge_would_overwrite_is_protected(self):
        self.diverge("ONE\n2\n3\n4\n5\n", "1\n2\n3\n4\nFIVE\n")
        self.write("a.txt", "my unsaved edit\n")
        ok, out = self.quiet(lambda: self.fox().merge_branch("feature"))
        self.assertFalse(ok)
        self.assertIn("would be overwritten", out)
        self.assertEqual(self.read("a.txt"), "my unsaved edit\n")

    def test_modify_delete_can_be_resolved_by_deleting(self):
        fox = self.fox()
        tree = fox._commit_tree(self.head())
        tree.pop("b.txt")
        main = fox._write_snapshot_commit("delete b", [self.head()], tree)
        Path("b.txt").unlink()
        self.checkout("feature")
        feature = self.commit_files("edit b", {"b.txt": "b edited\n"})
        self.checkout("main")

        ok, out = self.quiet(lambda: self.fox().merge_branch("feature"))
        self.assertFalse(ok)
        self.assertIn("CONFLICT (modify/delete): b.txt deleted in main", out)
        self.assertEqual(self.read("b.txt"), "b edited\n")   # the surviving version

        Path("b.txt").unlink()                               # take the deletion
        ok, out = self.quiet(lambda: self.fox().merge_continue())
        self.assertTrue(ok, out)
        self.assertNotIn("b.txt", self.tree(self.head()))
        self.assertEqual(self.parents(self.head()), [main, feature])

    def test_a_snapshot_keeps_identical_files_at_different_paths(self):
        fox = self.fox()
        commit = fox._write_snapshot_commit(
            "dupes", [self.head()], {"pkg/__init__.py": b"", "lib/__init__.py": b"", "x": b"1"})
        self.assertEqual(set(self.tree(commit)), {"pkg/__init__.py", "lib/__init__.py", "x"})


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def b64(text):
    return base64.b64encode(text.encode()).decode()


class ServerResolveTests(_Repo):
    """`fox pr resolve` against a fake server: the edits are what gets sent."""

    def setUp(self):
        super().setUp()
        self.suggested = "<<<<<<< main\nm\n||||||| base\nb\n=======\nf\n>>>>>>> feature\n"
        self.bundle = {
            "success": True,
            "session": {
                "id": 12, "status": "open", "stale_reason": None, "lands_on": "source",
                "source_branch": "feature", "target_branch": "main",
                "source_head_commit_id": "srchead", "target_head_commit_id": "tgthead",
                "commit_branch": "feature",
            },
            "files": [{
                "path": "src/app.txt", "kind": "content", "is_binary": False,
                "base_b64": b64("b\n"), "ours_b64": b64("m\n"), "theirs_b64": b64("f\n"),
                "suggested_b64": b64(self.suggested), "resolved": False, "resolution": None,
                "resolved_b64": None,
            }, {
                "path": "old.txt", "kind": "modify_delete", "is_binary": False,
                "deleted_in": "target", "base_b64": b64("o\n"), "ours_b64": None,
                "theirs_b64": b64("o2\n"), "suggested_b64": b64("o2\n"), "resolved": False,
                "resolution": None, "resolved_b64": None,
            }],
        }
        self.sent = []

        def fake_post(_self, url, json=None, params=None, timeout=30):
            self.sent.append((url, json))
            if url.endswith("/resolve"):
                return FakeResponse(200, {
                    "success": True, "status": "resolved", "lands_on": "source",
                    "merge_commit_id": "abcdef1234567890", "commit_branch": "feature",
                    "resolved_files": 2, "deleted_files": ["old.txt"],
                    "next_step": "approvals are stale",
                })
            return FakeResponse(200, self.bundle)

        fakes = {
            "_repo_api": lambda _self, suffix: "http://server/api/repository/r1" + suffix,
            "_authed_post": fake_post,
            "_authed_get": lambda _self, url, params=None, timeout=30: FakeResponse(200, self.bundle),
        }
        for name, fake in fakes.items():
            patcher = mock.patch.object(FoxClient, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def resolved_path(self):
        return Path(".fox/merge-conflicts/pr_7_session_12/src/app.txt.resolved")

    def test_submit_sends_the_edited_file_not_the_suggestion(self):
        ok, out = self.quiet(lambda: self.fox().pr_resolve(7))
        self.assertTrue(ok, out)
        self.assertEqual(self.resolved_path().read_text(), self.suggested)

        self.resolved_path().write_bytes(b"mine\n")
        ok, out = self.quiet(lambda: self.fox().pr_resolve(7, submit=True, deletes=["old.txt"]))
        self.assertTrue(ok, out)

        url, payload = self.sent[-1]
        self.assertTrue(url.endswith("/conflicts/12/resolve"))
        self.assertEqual(base64.b64decode(payload["resolutions"]["src/app.txt"]), b"mine\n")
        self.assertIsNone(payload["resolutions"]["old.txt"])
        self.assertEqual(payload["expected_head_commit_id"], "srchead")
        self.assertEqual(self.resolved_path().read_text(), "mine\n")   # untouched
        self.assertIn("Committed the resolution to 'feature'", out)

    def test_reopening_never_overwrites_an_edited_file(self):
        self.quiet(lambda: self.fox().pr_resolve(7))
        self.resolved_path().write_bytes(b"mine\n")
        self.quiet(lambda: self.fox().pr_resolve(7))
        self.assertEqual(self.resolved_path().read_text(), "mine\n")

    def test_markers_left_in_are_refused_before_anything_is_sent(self):
        self.quiet(lambda: self.fox().pr_resolve(7))
        before = len(self.sent)
        ok, out = self.quiet(lambda: self.fox().pr_resolve(7, submit=True, deletes=["old.txt"]))
        self.assertFalse(ok)
        self.assertIn("conflict markers", out)
        self.assertFalse(any(u.endswith("/resolve") for u, _ in self.sent[before:]))

    def test_unsafe_paths_from_the_server_are_not_written(self):
        self.bundle["files"][0]["path"] = "../../escape.txt"
        self.quiet(lambda: self.fox().pr_resolve(7))
        self.assertFalse((Path.cwd().parent / "escape.txt.resolved").exists())
        self.assertFalse((Path.cwd().parent.parent / "escape.txt.resolved").exists())


if __name__ == "__main__":
    unittest.main()
