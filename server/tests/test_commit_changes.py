"""Tests for what a single commit changed.

The merge case is the one to get right. A merge has two parents, and diffing against
both reports every change that arrived from the branch being merged as though the merge
made them. Comparing against the first parent shows what landed on the target as a
result, which is what `git show` does and what someone reading a commit expects.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.services import blob_store, commit_changes  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, Commit, CommitFile, CommitParent, Repository, Tag, User,
)


class CommitChangesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_chg_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_chg_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.author = User(username="author", role="team_lead")
        self.db.add(self.author)
        self.db.flush()
        self.repo = Repository(id="r1", name="demo", owner_id=self.author.id)
        self.db.add(self.repo)
        self.db.commit()

        # root: a.txt, gone.txt
        self.mk("root", {"a.txt": "one\ntwo\nthree\n", "gone.txt": "bye\n"})
        # work: edits a.txt, adds new.txt, deletes gone.txt
        self.mk("work", {"a.txt": "one\nTWO\nthree\n", "new.txt": "fresh\n"}, ("root",))
        # side branch, then a merge into the mainline
        self.mk("side", {"a.txt": "one\ntwo\nthree\n", "gone.txt": "bye\n",
                         "side.txt": "from the side\n"}, ("root",))
        self.mk("merge", {"a.txt": "one\nTWO\nthree\n", "new.txt": "fresh\n",
                          "side.txt": "from the side\n"}, ("work", "side"))
        self.branch("main", "merge", default=True)

    def tearDown(self):
        blob_store.store = self._real_store
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def mk(self, cid, files, parents=()):
        self.db.add(Commit(id=cid, repository_id="r1", author_id=self.author.id,
                           message=f"{cid} subject\n\nbody line",
                           parent_commit_id=parents[0] if parents else None))
        self.db.flush()
        for path, text in files.items():
            obj = FileObjectCRUD.store_file_object(self.db, text.encode())
            self.db.add(CommitFile(commit_id=cid, file_path=path,
                                   file_hash=obj.hash, file_size=obj.size))
        for order, pid in enumerate(parents):
            self.db.add(CommitParent(commit_id=cid, parent_commit_id=pid, parent_order=order))
        self.db.commit()

    def branch(self, name, head, default=False):
        self.db.add(Branch(repository_id="r1", name=name, head_commit_id=head,
                           is_default=default))
        self.db.commit()

    def build(self, cid, **kwargs):
        return commit_changes.build(self.db, self.repo, cid, **kwargs)

    def paths(self, result):
        return sorted(f["file_path"] for f in result["files"])

    def file_for(self, result, path):
        return next(f for f in result["files"] if f["file_path"] == path)

    # --- only what changed ------------------------------------------------------

    def test_only_changed_files_are_listed(self):
        """The whole point: not the tree, just what this commit did to it."""
        self.assertEqual(self.paths(self.build("work")),
                         ["a.txt", "gone.txt", "new.txt"])

    def test_each_file_is_classified(self):
        result = self.build("work")
        self.assertEqual(self.file_for(result, "a.txt")["status"], "modified")
        self.assertEqual(self.file_for(result, "new.txt")["status"], "added")
        self.assertEqual(self.file_for(result, "gone.txt")["status"], "removed")

    def test_totals_summarise_the_commit(self):
        totals = self.build("work")["totals"]
        self.assertEqual(totals["files"], 3)
        self.assertEqual(totals["files_added"], 1)
        self.assertEqual(totals["files_modified"], 1)
        self.assertEqual(totals["files_removed"], 1)
        self.assertGreater(totals["added"], 0)
        self.assertGreater(totals["removed"], 0)

    def test_a_modified_file_carries_line_numbered_rows(self):
        rows = self.file_for(self.build("work"), "a.txt")["diff"]["rows"]
        changed = [r for r in rows if r["type"] == "changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["previous_line"], 2)
        self.assertEqual(changed[0]["current_line"], 2)

    def test_an_added_file_is_all_additions(self):
        rows = self.file_for(self.build("work"), "new.txt")["diff"]["rows"]
        self.assertTrue(rows)
        self.assertTrue(all(r["previous_line"] is None for r in rows))

    def test_a_removed_file_is_all_deletions(self):
        rows = self.file_for(self.build("work"), "gone.txt")["diff"]["rows"]
        self.assertTrue(rows)
        self.assertTrue(all(r["current_line"] is None for r in rows))


    # --- the row budget ---------------------------------------------------------

    def test_a_row_budget_drops_rows_but_keeps_the_counts(self):
        """A real commit here is 55,000 rows; the list must still be readable."""
        result = self.build("work", max_rows=0)
        for entry in result["files"]:
            self.assertEqual(entry["diff"]["rows"], [])
            self.assertTrue(entry.get("diff_omitted"))
            self.assertEqual(entry["diff"]["reason"], "not_loaded")
            # The counts survive, so the file still reports what it did.
            self.assertGreater(
                entry["diff"]["stats"]["added"] + entry["diff"]["stats"]["removed"], 0)

    def test_totals_describe_the_commit_not_what_fitted(self):
        full = self.build("work")["totals"]
        trimmed = self.build("work", max_rows=0)["totals"]
        self.assertEqual(trimmed["added"], full["added"])
        self.assertEqual(trimmed["removed"], full["removed"])
        self.assertEqual(trimmed["files"], full["files"])

    def test_omitted_files_are_counted(self):
        result = self.build("work", max_rows=0)
        self.assertEqual(result["totals"]["files_not_loaded"], len(result["files"]))

    def test_a_generous_budget_omits_nothing(self):
        result = self.build("work", max_rows=100000)
        self.assertEqual(result["totals"]["files_not_loaded"], 0)
        self.assertTrue(all(not f.get("diff_omitted") for f in result["files"]))

    def test_one_file_can_be_fetched_in_full(self):
        result = self.build("work", path="a.txt")
        self.assertEqual([f["file_path"] for f in result["files"]], ["a.txt"])
        self.assertTrue(result["files"][0]["diff"]["rows"])
        self.assertEqual(result["requested_path"], "a.txt")

    def test_fetching_one_file_still_reports_the_whole_commit_totals(self):
        """So a client loading one file does not lose the header it already showed."""
        self.assertEqual(self.build("work", path="a.txt")["totals"]["files"], 3)

    def test_asking_for_a_path_the_commit_did_not_touch_returns_nothing(self):
        self.assertEqual(self.build("work", path="not-here.txt")["files"], [])

    # --- merges -----------------------------------------------------------------

    def test_a_merge_is_compared_against_its_first_parent(self):
        """Against both parents it would claim every change the branch brought."""
        result = self.build("merge")
        self.assertEqual(result["compared_against"], "work")
        # side.txt is what the merge actually brought onto the mainline.
        self.assertEqual(self.paths(result), ["side.txt"])

    def test_a_merge_reports_its_other_parents(self):
        commit = self.build("merge")["commit"]
        self.assertTrue(commit["is_merge"])
        self.assertEqual(commit["parents"], ["work", "side"])

    def test_a_merge_can_be_compared_against_the_other_parent(self):
        result = self.build("merge", against="side")
        self.assertEqual(result["compared_against"], "side")
        self.assertEqual(self.paths(result), ["a.txt", "gone.txt", "new.txt"])

    def test_an_arbitrary_commit_cannot_be_used_as_a_base(self):
        """Only a real parent, so no one can fabricate a transition that never was."""
        result = self.build("merge", against="root")
        self.assertEqual(result["compared_against"], "work")

    # --- root commit ------------------------------------------------------------

    def test_a_root_commit_shows_its_whole_tree_as_additions(self):
        result = self.build("root")
        self.assertEqual(self.paths(result), ["a.txt", "gone.txt"])
        self.assertTrue(all(f["status"] == "added" for f in result["files"]))
        self.assertTrue(result["commit"]["is_root"])
        self.assertIsNone(result["compared_against"])

    # --- metadata ---------------------------------------------------------------

    def test_the_subject_is_separated_from_the_body(self):
        commit = self.build("work")["commit"]
        self.assertEqual(commit["subject"], "work subject")
        self.assertIn("body line", commit["message"])

    def test_the_commit_reports_which_branches_contain_it(self):
        self.assertEqual(self.build("work")["commit"]["branches"], ["main"])

    def test_a_commit_on_no_branch_reports_none(self):
        self.mk("orphan", {"z.txt": "z\n"}, ("root",))
        self.assertEqual(self.build("orphan")["commit"]["branches"], [])

    def test_tags_are_reported(self):
        self.db.add(Tag(repository_id="r1", name="v1.0", commit_id="work"))
        self.db.commit()
        self.assertEqual(self.build("work")["commit"]["tags"], ["v1.0"])

    def test_an_unknown_commit_is_refused(self):
        with self.assertRaises(commit_changes.CommitNotFound):
            self.build("nope")

    def test_a_commit_from_another_repository_is_not_visible(self):
        other = Repository(id="r2", name="other", owner_id=self.author.id)
        self.db.add(other)
        self.db.commit()
        with self.assertRaises(commit_changes.CommitNotFound):
            commit_changes.build(self.db, other, "work")


if __name__ == "__main__":
    unittest.main()
