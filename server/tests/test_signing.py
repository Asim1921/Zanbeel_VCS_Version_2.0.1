"""Tests for commit attestation.

The point of the feature is that editing history behind the API's back is detectable, so
most of these tamper with the database directly and assert that verification notices.
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

from app.services import blob_store, signing  # noqa: E402
from app.services.blob_store import BlobStore  # noqa: E402
from app.services.signing import sign_commit, verify_commit, verify_repository  # noqa: E402
from database.database import Base  # noqa: E402
from database.crud import FileObjectCRUD  # noqa: E402
from database.models import (  # noqa: E402
    Commit, CommitFile, CommitSignature, Repository, User,
)


class SigningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_sign_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_sign_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.alice = User(username="alice", role="team_lead")
        self.mallory = User(username="mallory", role="developer")
        self.db.add_all([self.alice, self.mallory])
        self.db.flush()
        self.db.add(Repository(id="r1", name="demo", owner_id=self.alice.id))
        self.db.commit()

        self.commit = self.mk("c1", {"a.py": "print(1)"}, "legitimate change")
        sign_commit(self.db, self.commit, "alice", self.alice.id)

    def tearDown(self):
        blob_store.store = self._real_store
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def mk(self, cid, files, message="c"):
        commit = Commit(id=cid, repository_id="r1", author_id=self.alice.id, message=message)
        self.db.add(commit)
        self.db.flush()
        for path, text in files.items():
            obj = FileObjectCRUD.store_file_object(self.db, text.encode())
            self.db.add(CommitFile(commit_id=cid, file_path=path,
                                   file_hash=obj.hash, file_size=obj.size))
        self.db.commit()
        self.db.refresh(commit)
        return commit

    # ---------------- happy path ----------------

    def test_a_signed_commit_verifies(self):
        result = verify_commit(self.db, "c1")
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["pusher"], "alice")

    def test_verification_is_repeatable(self):
        self.assertEqual(verify_commit(self.db, "c1")["status"], "valid")
        self.assertEqual(verify_commit(self.db, "c1")["status"], "valid")

    def test_signing_is_idempotent(self):
        again = sign_commit(self.db, self.commit, "alice", self.alice.id)
        self.assertIsNotNone(again)
        self.assertEqual(self.db.query(CommitSignature).count(), 1)

    def test_unsigned_commit_is_reported_as_unsigned_not_invalid(self):
        self.mk("c2", {"b.py": "x"})
        result = verify_commit(self.db, "c2")
        self.assertEqual(result["status"], "unsigned")
        self.assertIn("not evidence of tampering", result["detail"])

    def test_unknown_commit(self):
        self.assertEqual(verify_commit(self.db, "nope")["status"], "unknown")

    # ---------------- tamper detection ----------------

    def test_rewriting_the_commit_message_is_detected(self):
        self.commit.message = "innocent looking message"
        self.db.commit()

        result = verify_commit(self.db, "c1")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("commit message", result["changed"])

    def test_reassigning_the_author_is_detected(self):
        self.commit.author_id = self.mallory.id
        self.db.commit()

        result = verify_commit(self.db, "c1")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("author", result["changed"])

    def test_swapping_a_files_content_is_detected(self):
        # The classic attack: point the same path at different bytes.
        evil = FileObjectCRUD.store_file_object(self.db, b"import os; os.system('rm -rf /')")
        row = self.db.query(CommitFile).filter(CommitFile.commit_id == "c1").first()
        row.file_hash = evil.hash
        self.db.commit()

        result = verify_commit(self.db, "c1")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("file contents or file list", result["changed"])

    def test_adding_a_file_to_a_signed_commit_is_detected(self):
        obj = FileObjectCRUD.store_file_object(self.db, b"backdoor")
        self.db.add(CommitFile(commit_id="c1", file_path="evil.py",
                               file_hash=obj.hash, file_size=obj.size))
        self.db.commit()

        self.assertEqual(verify_commit(self.db, "c1")["status"], "invalid")

    def test_removing_a_file_from_a_signed_commit_is_detected(self):
        self.db.query(CommitFile).filter(CommitFile.commit_id == "c1").delete()
        self.db.commit()

        self.assertEqual(verify_commit(self.db, "c1")["status"], "invalid")

    def test_renaming_a_path_is_detected(self):
        row = self.db.query(CommitFile).filter(CommitFile.commit_id == "c1").first()
        row.file_path = "somewhere/else.py"
        self.db.commit()

        self.assertEqual(verify_commit(self.db, "c1")["status"], "invalid")

    def test_forging_the_signature_itself_fails(self):
        record = self.db.query(CommitSignature).filter(
            CommitSignature.commit_id == "c1").first()
        record.signature = "0" * 64
        self.db.commit()

        self.assertEqual(verify_commit(self.db, "c1")["status"], "invalid")

    def test_claiming_a_different_pusher_fails(self):
        record = self.db.query(CommitSignature).filter(
            CommitSignature.commit_id == "c1").first()
        record.pusher_username = "mallory"
        self.db.commit()

        # the payload no longer matches what was signed
        self.assertEqual(verify_commit(self.db, "c1")["status"], "invalid")

    def test_unknown_algorithm_is_unverifiable_not_invalid(self):
        record = self.db.query(CommitSignature).filter(
            CommitSignature.commit_id == "c1").first()
        record.algorithm = "future-alg-v9"
        self.db.commit()

        self.assertEqual(verify_commit(self.db, "c1")["status"], "unverifiable")

    def test_a_signature_cannot_be_moved_to_another_commit(self):
        other = self.mk("c2", {"a.py": "print(1)"}, "legitimate change")
        record = self.db.query(CommitSignature).filter(
            CommitSignature.commit_id == "c1").first()
        stolen = record.signature
        self.db.add(CommitSignature(
            commit_id=other.id, repository_id="r1", pusher_username=record.pusher_username,
            algorithm=record.algorithm, signature=stolen,
            signed_at_text=record.signed_at_text,
        ))
        self.db.commit()

        # commit_id is part of the signed payload, so the copied signature does not fit
        self.assertEqual(verify_commit(self.db, "c2")["status"], "invalid")

    # ---------------- repository sweep ----------------

    def test_repository_verification_summarises(self):
        self.mk("c2", {"b.py": "y"})            # unsigned
        signed = self.mk("c3", {"c.py": "z"})
        sign_commit(self.db, signed, "alice", self.alice.id)

        report = verify_repository(self.db, "r1")
        self.assertEqual(report["checked"], 3)
        self.assertEqual(report["valid"], 2)
        self.assertEqual(report["unsigned"], 1)
        self.assertEqual(report["invalid"], 0)
        self.assertEqual(report["tampered"], [])

    def test_repository_verification_surfaces_tampered_commits(self):
        self.commit.message = "changed behind the api"
        self.db.commit()

        report = verify_repository(self.db, "r1")
        self.assertEqual(report["invalid"], 1)
        self.assertEqual(report["tampered"][0]["commit_id"], "c1")

    def test_signing_failure_does_not_raise(self):
        # A misconfigured secret must not break pushes; the commit is simply unsigned.
        original = signing.AUTH_SECRET
        try:
            signing.AUTH_SECRET = None          # will blow up inside _sign
            fresh = self.mk("c9", {"x.py": "1"})
            self.assertIsNone(sign_commit(self.db, fresh, "alice", self.alice.id))
        finally:
            signing.AUTH_SECRET = original


if __name__ == "__main__":
    unittest.main()
