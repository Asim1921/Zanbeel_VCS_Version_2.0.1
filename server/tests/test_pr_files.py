"""Tests for the pull request file diff: what changed, on which line, and who said what.

The line numbers matter more than they look. A review comment anchors to
``(file_path, line, side)``, so if the rows and the comments disagree about which line
is line 42, every thread renders against the wrong code.
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

from app.services import blob_store, pr_comments, pr_files  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, Commit, CommitFile, CommitParent, PullRequest, Repository, User,
)


class PullRequestFilesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_prfiles_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_prfiles_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.author = User(username="author", role="team_lead")
        self.alice = User(username="alice", role="team_lead")
        self.db.add_all([self.author, self.alice])
        self.db.flush()
        self.repo = Repository(id="r1", name="demo", owner_id=self.author.id)
        self.db.add(self.repo)
        self.db.commit()

        # base -> (main adds unrelated.txt) and (feature edits a.txt, adds new.txt)
        self.mk("base", {
            "a.txt": "one\ntwo\nthree\n",
            "gone.txt": "bye\n",
            "CODEOWNERS": "a.txt alice\n",
        })
        self.mk("mainwork", {
            "a.txt": "one\ntwo\nthree\n",
            "gone.txt": "bye\n",
            "CODEOWNERS": "a.txt alice\n",
            "unrelated.txt": "main only\n",
        }, ("base",))
        self.mk("feat1", {
            "a.txt": "one\nTWO\nthree\n",
            "new.txt": "fresh\n",
            "CODEOWNERS": "a.txt alice\n",
        }, ("base",))

        self.branch("main", "mainwork", default=True)
        self.branch("feature", "feat1")

        self.pr = PullRequest(repository_id="r1", title="edit a", source_branch="feature",
                              target_branch="main", status="open",
                              created_by_id=self.author.id)
        self.db.add(self.pr)
        self.db.commit()
        self.db.refresh(self.pr)

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
                           message=cid, parent_commit_id=parents[0] if parents else None))
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

    def build(self):
        return pr_files.build(self.db, self.repo, self.pr)

    def paths(self, bundle):
        return sorted(f["file_path"] for f in bundle["files"])

    def file_for(self, bundle, path):
        return next(f for f in bundle["files"] if f["file_path"] == path)

    # --- what the diff covers ---------------------------------------------------

    def test_diff_is_taken_from_the_merge_base_not_the_target_tip(self):
        """Comparing against the tip would attribute main's own changes to this PR."""
        bundle = self.build()
        self.assertEqual(bundle["base_commit_id"], "base")
        # unrelated.txt landed on main after the branch point; it is not this PR's doing.
        self.assertNotIn("unrelated.txt", self.paths(bundle))

    def test_changed_added_and_removed_files_are_all_reported(self):
        # feature edits a.txt, adds new.txt and deletes gone.txt.
        self.assertEqual(self.paths(self.build()), ["a.txt", "gone.txt", "new.txt"])

    def test_unchanged_files_are_omitted(self):
        self.assertNotIn("CODEOWNERS", self.paths(self.build()))

    def test_file_status_is_classified(self):
        bundle = self.build()
        self.assertEqual(self.file_for(bundle, "a.txt")["status"], "modified")
        self.assertEqual(self.file_for(bundle, "new.txt")["status"], "added")
        self.assertEqual(self.file_for(bundle, "gone.txt")["status"], "removed")

    def test_a_removed_file_still_has_reviewable_rows(self):
        """A deletion is the most worth reviewing, so it cannot render as an empty diff."""
        rows = self.file_for(self.build(), "gone.txt")["diff"]["rows"]
        self.assertTrue(rows)
        self.assertTrue(all(r["current_line"] is None for r in rows))
        self.assertEqual(rows[0]["previous_line"], 1)

    def test_totals_are_summed_across_files(self):
        totals = self.build()["totals"]
        self.assertEqual(totals["files"], 3)
        self.assertGreaterEqual(totals["added"], 2)

    # --- line numbers, which comments anchor to ---------------------------------

    def test_every_row_carries_the_line_it_came_from(self):
        rows = self.file_for(self.build(), "a.txt")["diff"]["rows"]
        changed = [r for r in rows if r["type"] == "changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["previous_line"], 2)
        self.assertEqual(changed[0]["current_line"], 2)

    def test_added_rows_have_no_previous_line(self):
        rows = self.file_for(self.build(), "new.txt")["diff"]["rows"]
        self.assertTrue(rows)
        self.assertTrue(all(r["previous_line"] is None for r in rows))
        self.assertEqual(rows[0]["current_line"], 1)

    # --- threads ----------------------------------------------------------------

    def test_comment_threads_are_attached_to_their_file(self):
        pr_comments.add(self.db, self.repo, self.pr, self.alice,
                        file_path="a.txt", line=2, side="new", body="why upper case?")
        bundle = self.build()
        self.assertEqual(len(self.file_for(bundle, "a.txt")["threads"]), 1)
        self.assertEqual(self.file_for(bundle, "new.txt")["threads"], [])
        self.assertEqual(bundle["comments"]["total"], 1)
        self.assertEqual(bundle["comments"]["unresolved"], 1)

    def test_a_file_with_no_comments_reports_an_empty_thread_list(self):
        for entry in self.build()["files"]:
            self.assertEqual(entry["threads"], [])

    # --- code owners ------------------------------------------------------------

    def test_owners_are_reported_per_file(self):
        bundle = self.build()
        self.assertEqual(self.file_for(bundle, "a.txt")["owners"], ["alice"])
        self.assertEqual(self.file_for(bundle, "new.txt")["owners"], [])


if __name__ == "__main__":
    unittest.main()
