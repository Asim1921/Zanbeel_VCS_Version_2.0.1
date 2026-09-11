"""Tests for line-level blame.

Builds real commit history and asserts the exact commit each line is attributed to.
The subtle property: a line must be credited to the commit that *introduced its current
text*, not to the newest commit that happened to touch the file.
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

from app.services import blob_store  # noqa: E402
from app.services.blame import blame_file  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Commit, CommitFile, CommitParent, FileLineage, Repository, User,
)


class BlameTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_blame_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_blame_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.alice = User(username="alice", full_name="Alice A", role="team_lead")
        self.bob = User(username="bob", full_name="Bob B", role="team_lead")
        self.db.add_all([self.alice, self.bob])
        self.db.flush()
        self.db.add(Repository(id="r1", name="demo", owner_id=self.alice.id))
        self.db.commit()

    def tearDown(self):
        blob_store.store = self._real_store
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def commit(self, cid, author, files, parent=None, message="change"):
        """files: {path: text}. Records a commit holding exactly those paths."""
        self.db.add(Commit(id=cid, repository_id="r1", author_id=author.id,
                           message=message, parent_commit_id=parent))
        self.db.flush()
        for path, text in files.items():
            obj = FileObjectCRUD.store_file_object(self.db, text.encode())
            self.db.add(CommitFile(commit_id=cid, file_path=path,
                                   file_hash=obj.hash, file_size=obj.size))
        if parent:
            self.db.add(CommitParent(commit_id=cid, parent_commit_id=parent, parent_order=0))
        self.db.commit()
        return cid

    def attribution(self, result):
        return [line["commit_id"] for line in result["lines"]]

    # ------------------------------------------------------------------

    def test_single_commit_attributes_everything_to_it(self):
        self.commit("c1", self.alice, {"a.py": "one\ntwo\nthree\n"})
        result = blame_file(self.db, "r1", "a.py", "c1")
        self.assertEqual(self.attribution(result), ["c1", "c1", "c1"])
        self.assertEqual(result["line_count"], 3)

    def test_untouched_lines_keep_their_original_commit(self):
        self.commit("c1", self.alice, {"a.py": "one\ntwo\nthree\n"})
        self.commit("c2", self.bob, {"a.py": "one\nCHANGED\nthree\n"}, parent="c1")

        result = blame_file(self.db, "r1", "a.py", "c2")
        # only the middle line belongs to c2; the others were never touched
        self.assertEqual(self.attribution(result), ["c1", "c2", "c1"])

    def test_inserted_lines_belong_to_the_inserting_commit(self):
        self.commit("c1", self.alice, {"a.py": "one\ntwo\n"})
        self.commit("c2", self.bob, {"a.py": "one\ninserted\ntwo\n"}, parent="c1")

        self.assertEqual(self.attribution(blame_file(self.db, "r1", "a.py", "c2")),
                         ["c1", "c2", "c1"])

    def test_appended_lines(self):
        self.commit("c1", self.alice, {"a.py": "one\n"})
        self.commit("c2", self.bob, {"a.py": "one\ntwo\n"}, parent="c1")
        self.commit("c3", self.alice, {"a.py": "one\ntwo\nthree\n"}, parent="c2")

        self.assertEqual(self.attribution(blame_file(self.db, "r1", "a.py", "c3")),
                         ["c1", "c2", "c3"])

    def test_deletion_does_not_misattribute_survivors(self):
        self.commit("c1", self.alice, {"a.py": "one\ntwo\nthree\n"})
        self.commit("c2", self.bob, {"a.py": "one\nthree\n"}, parent="c1")

        self.assertEqual(self.attribution(blame_file(self.db, "r1", "a.py", "c2")),
                         ["c1", "c1"])

    def test_reverting_a_line_credits_the_commit_that_restored_it(self):
        # A line whose text returns to an earlier value belongs to the commit that
        # brought it back, not to the original -- it did not survive continuously.
        self.commit("c1", self.alice, {"a.py": "keep\noriginal\n"})
        self.commit("c2", self.bob, {"a.py": "keep\nmodified\n"}, parent="c1")
        self.commit("c3", self.alice, {"a.py": "keep\noriginal\n"}, parent="c2")

        self.assertEqual(self.attribution(blame_file(self.db, "r1", "a.py", "c3")),
                         ["c1", "c3"])

    def test_blame_at_an_older_commit(self):
        self.commit("c1", self.alice, {"a.py": "one\ntwo\n"})
        self.commit("c2", self.bob, {"a.py": "one\nCHANGED\n"}, parent="c1")

        # asking about c1 must not see c2's work
        self.assertEqual(self.attribution(blame_file(self.db, "r1", "a.py", "c1")),
                         ["c1", "c1"])

    def test_history_is_followed_through_a_rename(self):
        self.commit("c1", self.alice, {"old.py": "alpha\nbeta\n"})
        self.commit("c2", self.bob, {"new.py": "alpha\nbeta\n"}, parent="c1")
        self.db.add(FileLineage(repository_id="r1", commit_id="c2",
                                old_path="old.py", new_path="new.py", file_hash="x"))
        self.db.commit()

        result = blame_file(self.db, "r1", "new.py", "c2")
        # content is unchanged by the move, so it still belongs to c1
        self.assertEqual(self.attribution(result), ["c1", "c1"])

    def test_commit_metadata_is_returned(self):
        self.commit("c1", self.alice, {"a.py": "x\n"}, message="first commit\nbody")
        result = blame_file(self.db, "r1", "a.py", "c1")

        meta = result["commits"]["c1"]
        self.assertEqual(meta["author"], "alice")
        self.assertEqual(meta["author_name"], "Alice A")
        self.assertEqual(meta["summary"], "first commit")   # first line only
        self.assertIsNotNone(meta["date"])

    def test_every_line_is_attributed(self):
        self.commit("c1", self.alice, {"a.py": "\n".join(f"line{i}" for i in range(50)) + "\n"})
        self.commit("c2", self.bob,
                    {"a.py": "\n".join(("CHANGED" if i % 7 == 0 else f"line{i}")
                                       for i in range(50)) + "\n"}, parent="c1")

        result = blame_file(self.db, "r1", "a.py", "c2")
        self.assertEqual(len(result["lines"]), 50)
        self.assertTrue(all(line["commit_id"] for line in result["lines"]),
                        "every line must be attributed to some commit")
        changed = [i for i, line in enumerate(result["lines"]) if line["commit_id"] == "c2"]
        self.assertEqual(changed, [i for i in range(50) if i % 7 == 0])

    def test_missing_file_is_reported(self):
        self.commit("c1", self.alice, {"a.py": "x\n"})
        self.assertTrue(blame_file(self.db, "r1", "nope.py", "c1")["not_found"])

    def test_binary_file_is_reported(self):
        cid = "cb"
        self.db.add(Commit(id=cid, repository_id="r1", author_id=self.alice.id, message="bin"))
        self.db.flush()
        obj = FileObjectCRUD.store_file_object(self.db, bytes(range(256)))
        self.db.add(CommitFile(commit_id=cid, file_path="blob.bin",
                               file_hash=obj.hash, file_size=obj.size))
        self.db.commit()

        self.assertTrue(blame_file(self.db, "r1", "blob.bin", cid)["binary"])

    def test_line_numbers_are_one_based_and_content_preserved(self):
        self.commit("c1", self.alice, {"a.py": "first\nsecond\n"})
        lines = blame_file(self.db, "r1", "a.py", "c1")["lines"]
        self.assertEqual(lines[0]["line"], 1)
        self.assertEqual(lines[0]["content"], "first")
        self.assertEqual(lines[1]["line"], 2)
        self.assertEqual(lines[1]["content"], "second")


if __name__ == "__main__":
    unittest.main()
