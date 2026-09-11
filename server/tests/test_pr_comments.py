"""Tests for inline pull request comments.

The interesting behaviour is what happens to a comment when the code under it moves:
it must survive and be flagged, not silently disappear or silently mislead.
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

from app.services import blob_store, pr_comments  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from app.services.pr_comments import CommentError  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import BranchCRUD, FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Branch, Commit, CommitFile, PullRequest, PullRequestComment, Repository, User,
)


class PullRequestCommentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_prc_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_prc_", suffix=".db")
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

        self.mk("base", {"a.py": "line1\nline2\n", "b.py": "x\n"})
        self.mk("feat", {"a.py": "line1\nCHANGED\n", "b.py": "x\n"}, "base")
        self.branch("main", "base", default=True)
        self.branch("feature", "feat")

        self.pr = PullRequest(repository_id="r1", title="t", source_branch="feature",
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

    def mk(self, cid, files, parent=None):
        self.db.add(Commit(id=cid, repository_id="r1", author_id=self.author.id,
                           message=cid, parent_commit_id=parent))
        self.db.flush()
        for path, text in files.items():
            obj = FileObjectCRUD.store_file_object(self.db, text.encode())
            self.db.add(CommitFile(commit_id=cid, file_path=path,
                                   file_hash=obj.hash, file_size=obj.size))
        self.db.commit()

    def branch(self, name, head, default=False):
        self.db.add(Branch(repository_id="r1", name=name, head_commit_id=head,
                           is_default=default))
        self.db.commit()

    def add(self, **kw):
        kw.setdefault("file_path", "a.py")
        kw.setdefault("line", 2)
        kw.setdefault("body", "why this change?")
        return pr_comments.add(self.db, self.repo, self.pr, kw.pop("author", self.alice), **kw)

    def threads(self):
        return pr_comments.threads(self.db, self.repo, self.pr)

    # ---------------- basics ----------------

    def test_a_comment_anchors_to_a_file_and_line(self):
        c = self.add()
        self.assertEqual(c.file_path, "a.py")
        self.assertEqual(c.line, 2)
        self.assertEqual(c.side, "new")
        self.assertEqual(c.commit_id, "feat")     # the tip it was written against

    def test_threads_are_ordered_by_file_then_line(self):
        self.add(file_path="b.py", line=1, body="second file")
        self.add(file_path="a.py", line=5, body="later line")
        self.add(file_path="a.py", line=1, body="first line")

        paths = [(t["file_path"], t["line"]) for t in self.threads()["threads"]]
        self.assertEqual(paths, [("a.py", 1), ("a.py", 5), ("b.py", 1)])

    def test_empty_body_is_rejected(self):
        with self.assertRaises(CommentError):
            self.add(body="   ")

    def test_invalid_side_is_rejected(self):
        with self.assertRaises(CommentError):
            self.add(side="middle")

    def test_line_must_be_positive(self):
        with self.assertRaises(CommentError):
            self.add(line=0)

    def test_a_closed_pull_request_cannot_be_commented_on(self):
        self.pr.status = "merged"
        self.db.commit()
        with self.assertRaises(CommentError):
            self.add()

    # ---------------- threading ----------------

    def test_a_reply_joins_the_thread(self):
        root = self.add()
        reply = self.add(in_reply_to_id=root.id, body="because X", author=self.author)

        self.assertEqual(reply.in_reply_to_id, root.id)
        threads = self.threads()["threads"]
        self.assertEqual(len(threads), 1)
        self.assertEqual(len(threads[0]["replies"]), 1)
        self.assertEqual(threads[0]["replies"][0]["body"], "because X")

    def test_a_reply_inherits_the_threads_location(self):
        root = self.add(file_path="a.py", line=7)
        reply = self.add(in_reply_to_id=root.id, file_path="b.py", line=99, body="r")
        # the reply cannot drag the discussion onto a different line
        self.assertEqual((reply.file_path, reply.line), ("a.py", 7))

    def test_replying_to_a_reply_stays_in_the_same_thread(self):
        root = self.add()
        reply = self.add(in_reply_to_id=root.id, body="r1")
        nested = self.add(in_reply_to_id=reply.id, body="r2")

        self.assertEqual(nested.in_reply_to_id, root.id)   # flattened, not nested deeper
        self.assertEqual(len(self.threads()["threads"]), 1)
        self.assertEqual(len(self.threads()["threads"][0]["replies"]), 2)

    def test_replying_to_a_comment_on_another_pr_is_rejected(self):
        other = PullRequest(repository_id="r1", title="o", source_branch="feature",
                            target_branch="main", status="open", created_by_id=self.author.id)
        self.db.add(other)
        self.db.commit()
        foreign = PullRequestComment(pull_request_id=other.id, author_id=self.alice.id,
                                     file_path="a.py", line=1, side="new", body="x")
        self.db.add(foreign)
        self.db.commit()

        with self.assertRaises(CommentError):
            self.add(in_reply_to_id=foreign.id)

    # ---------------- outdated ----------------

    def test_a_comment_goes_outdated_when_its_file_changes(self):
        self.add(file_path="a.py", line=2)
        self.assertFalse(self.threads()["threads"][0]["outdated"])

        self.mk("feat2", {"a.py": "line1\nDIFFERENT\n", "b.py": "x\n"}, "feat")
        BranchCRUD.update_branch_head(self.db, "r1", "feature", "feat2")

        thread = self.threads()["threads"][0]
        self.assertTrue(thread["outdated"])
        self.assertEqual(thread["body"], "why this change?")   # kept, not deleted

    def test_a_comment_on_an_untouched_file_stays_current(self):
        self.add(file_path="b.py", line=1)
        # a.py changes; b.py does not
        self.mk("feat2", {"a.py": "line1\nDIFFERENT\n", "b.py": "x\n"}, "feat")
        BranchCRUD.update_branch_head(self.db, "r1", "feature", "feat2")

        self.assertFalse(self.threads()["threads"][0]["outdated"])

    def test_outdated_count_is_reported(self):
        self.add(file_path="a.py", line=1)
        self.add(file_path="b.py", line=1)
        self.mk("feat2", {"a.py": "changed\n", "b.py": "x\n"}, "feat")
        BranchCRUD.update_branch_head(self.db, "r1", "feature", "feat2")

        self.assertEqual(self.threads()["outdated"], 1)

    # ---------------- resolve ----------------

    def test_resolving_and_reopening(self):
        root = self.add()
        pr_comments.set_resolved(self.db, self.pr, root.id, self.author, True)
        thread = self.threads()["threads"][0]
        self.assertTrue(thread["resolved"])
        self.assertEqual(thread["resolved_by"], "author")
        self.assertEqual(self.threads()["unresolved"], 0)

        pr_comments.set_resolved(self.db, self.pr, root.id, self.author, False)
        self.assertFalse(self.threads()["threads"][0]["resolved"])
        self.assertEqual(self.threads()["unresolved"], 1)

    def test_a_resolved_thread_is_still_visible(self):
        root = self.add()
        pr_comments.set_resolved(self.db, self.pr, root.id, self.author, True)
        self.assertEqual(len(self.threads()["threads"]), 1)

    def test_only_the_thread_root_can_be_resolved(self):
        root = self.add()
        reply = self.add(in_reply_to_id=root.id, body="r")
        with self.assertRaises(CommentError):
            pr_comments.set_resolved(self.db, self.pr, reply.id, self.author, True)

    # ---------------- delete ----------------

    def test_the_author_can_delete_their_comment(self):
        root = self.add(author=self.alice)
        pr_comments.delete(self.db, self.pr, root.id, self.alice)
        self.assertEqual(self.threads()["total"], 0)

    def test_someone_else_cannot_delete_it(self):
        root = self.add(author=self.alice)
        with self.assertRaises(CommentError) as ctx:
            pr_comments.delete(self.db, self.pr, root.id, self.author)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_deleting_a_thread_root_removes_its_replies(self):
        root = self.add(author=self.alice)
        self.add(in_reply_to_id=root.id, body="r1", author=self.author)
        self.add(in_reply_to_id=root.id, body="r2", author=self.author)

        result = pr_comments.delete(self.db, self.pr, root.id, self.alice)
        self.assertEqual(result["deleted"], 3)
        self.assertEqual(self.threads()["total"], 0)


if __name__ == "__main__":
    unittest.main()
