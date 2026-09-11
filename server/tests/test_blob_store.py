"""Tests for filesystem-backed blob storage.

The property that makes this deployable before the data has moved: a row whose bytes are
still in the database and a row whose bytes are on disk must be indistinguishable to
every reader.
"""
import hashlib
import os
import shutil
import sys
import tempfile
import unittest

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_AUTH_SECRET", "x" * 64)
os.environ.setdefault("FOXNEST_PASSWORD_SETUP_KEY", "y" * 64)

from pathlib import Path  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.services import blob_store  # noqa: E402
from app.services.blob_store import (  # noqa: E402
    MAGIC, BlobStore, _frame, _unframe,
)
from database.database import Base  # noqa: E402
from database.models import FileObject  # noqa: E402
from database.crud import FileObjectCRUD  # noqa: E402


class BlobStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_blobs_"))
        self.store = BlobStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_round_trip(self):
        digest = self.store.put(b"hello world")
        self.assertEqual(digest, self.store.hash_bytes(b"hello world"))
        self.assertTrue(self.store.exists(digest))
        self.assertEqual(self.store.get(digest), b"hello world")
        self.assertEqual(self.store.size(digest), 11)

    def test_sharded_layout(self):
        digest = self.store.put(b"x")
        self.assertEqual(self.store.path_for(digest).parent.name, digest[:2])
        self.assertTrue(self.store.path_for(digest).is_file())

    def test_put_is_idempotent(self):
        a = self.store.put(b"same")
        b = self.store.put(b"same")
        self.assertEqual(a, b)
        self.assertEqual(self.store.stats()["objects"], 1)

    def test_missing_blob_reads_as_none(self):
        self.assertIsNone(self.store.get("a" * 40))
        self.assertFalse(self.store.exists("a" * 40))

    def test_delete(self):
        digest = self.store.put(b"bye")
        self.assertTrue(self.store.delete(digest))
        self.assertIsNone(self.store.get(digest))
        self.assertFalse(self.store.delete(digest))   # already gone

    def test_path_traversal_is_rejected(self):
        for bad in ["../../etc/passwd", "..", "ab/../../x", "zz", "", "a/b"]:
            with self.assertRaises(ValueError):
                self.store.path_for(bad)
        # and the safe wrappers just say "no such blob" rather than exploding
        self.assertIsNone(self.store.get("../../etc/passwd"))
        self.assertFalse(self.store.exists("../../etc/passwd"))
        self.assertFalse(self.store.delete("../../etc/passwd"))

    def test_no_partial_file_left_behind(self):
        self.store.put(b"content")
        leftovers = [p.name for p in self.root.rglob(".tmp-*")]
        self.assertEqual(leftovers, [])

    def test_stats_counts_objects_and_disk_usage(self):
        self.store.put(b"a")
        self.store.put(b"bb")
        stats = self.store.stats()
        self.assertEqual(stats["objects"], 2)
        # stats() reports bytes on disk, which includes each blob's 5-byte frame --
        # not the 3 bytes of content. size()/stored_size() are the per-blob answers.
        self.assertEqual(stats["bytes"], 3 + 2 * 5)
        self.assertEqual(self.store.size(self.store.hash_bytes(b"bb")), 2)

    def test_binary_content_survives(self):
        payload = bytes(range(256)) * 40
        digest = self.store.put(payload)
        self.assertEqual(self.store.get(digest), payload)


class CompressionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_zip_"))
        self.store = BlobStore(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_round_trip_is_byte_exact(self):
        for payload in [
            b"",
            b"tiny",
            b"def f():\n    return 1\n" * 500,          # compressible text
            bytes(range(256)) * 200,                     # binary
            ("unicode éü中文 " * 400).encode("utf-8"),
        ]:
            digest = self.store.put(payload)
            self.assertEqual(self.store.get(digest), payload)

    def test_text_is_actually_compressed(self):
        payload = b"the quick brown fox jumps over the lazy dog\n" * 400
        digest = self.store.put(payload)
        self.assertLess(self.store.stored_size(digest), len(payload) // 2)
        self.assertEqual(self.store.get(digest), payload)

    def test_incompressible_content_is_not_inflated(self):
        payload = os.urandom(50_000)          # random data cannot be compressed
        digest = self.store.put(payload)
        # stored form is the raw bytes plus a 5-byte frame, never a larger zlib stream
        self.assertLessEqual(self.store.stored_size(digest), len(payload) + 16)
        self.assertEqual(self.store.get(digest), payload)

    def test_size_reports_content_length_not_disk_usage(self):
        payload = b"x" * 10_000
        digest = self.store.put(payload)
        self.assertEqual(self.store.size(digest), 10_000)
        self.assertLess(self.store.stored_size(digest), 10_000)

    def test_legacy_unframed_blobs_still_read(self):
        # Exactly how blobs were written before compression existed: raw, no header.
        payload = b"written before framing existed"
        digest = self.store.hash_bytes(payload)
        target = self.store.path_for(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

        self.assertEqual(self.store.get(digest), payload)
        self.assertEqual(self.store.size(digest), len(payload))

    def test_legacy_blob_that_happens_to_start_with_the_magic(self):
        # A raw blob whose first bytes collide with the frame magic must not be misread.
        payload = MAGIC + b" this is not really zlib"
        digest = self.store.hash_bytes(payload)
        target = self.store.path_for(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

        self.assertEqual(self.store.get(digest), payload)

    def test_frame_helpers_round_trip(self):
        for payload in [b"", b"short", b"long text " * 1000, os.urandom(2000)]:
            self.assertEqual(_unframe(_frame(payload)), payload)

    def test_integrity_check_passes_on_a_healthy_store(self):
        for i in range(5):
            self.store.put(f"content {i}".encode() * 100)
        report = self.store.integrity_check()
        self.assertEqual(report["checked"], 5)
        self.assertEqual(report["corrupt"], 0)

    def test_integrity_check_detects_a_corrupted_blob(self):
        digest = self.store.put(b"important content" * 100)
        self.store.path_for(digest).write_bytes(b"garbage that is not what was stored")

        report = self.store.integrity_check()
        self.assertEqual(report["corrupt"], 1)
        self.assertIn(digest, report["examples"])

    def test_hash_addressing_is_unaffected_by_compression(self):
        payload = b"content addressed" * 100
        digest = self.store.put(payload)
        self.assertEqual(digest, hashlib.sha1(payload).hexdigest())


class FileObjectStorageTests(unittest.TestCase):
    """The model must hide where the bytes actually live."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="foxnest_blobs_"))
        self._real_store = blob_store.store
        blob_store.store = BlobStore(self.root)

        fd, self.db_path = tempfile.mkstemp(prefix="foxnest_bs_", suffix=".db")
        os.close(fd)
        self.engine = create_engine("sqlite:///" + self.db_path.replace("\\", "/"))
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        blob_store.store = self._real_store
        self.db.close()
        self.engine.dispose()
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_new_objects_store_bytes_on_disk_not_in_the_row(self):
        obj = FileObjectCRUD.store_file_object(self.db, b"source code")
        self.assertTrue(obj.is_on_disk)
        self.assertIsNone(obj._content)                       # column left empty
        self.assertEqual(obj.content, b"source code")          # still readable
        self.assertTrue(blob_store.store.exists(obj.hash))

    def test_legacy_inline_rows_still_read(self):
        # A row as written before the migration: bytes in the column, nothing on disk.
        legacy = FileObject(hash="a" * 40, size=6)
        legacy.content = b"legacy"
        self.db.add(legacy)
        self.db.commit()

        fetched = self.db.query(FileObject).filter(FileObject.hash == "a" * 40).first()
        self.assertEqual(fetched.content, b"legacy")
        self.assertFalse(fetched.is_on_disk)
        self.assertFalse(blob_store.store.exists("a" * 40))

    def test_the_two_kinds_are_indistinguishable_to_a_reader(self):
        on_disk = FileObjectCRUD.store_file_object(self.db, b"payload")
        legacy = FileObject(hash="b" * 40, size=7)
        legacy.content = b"payload"
        self.db.add(legacy)
        self.db.commit()

        self.assertEqual(on_disk.content, legacy.content)

    def test_deduplication_still_works(self):
        first = FileObjectCRUD.store_file_object(self.db, b"same bytes")
        second = FileObjectCRUD.store_file_object(self.db, b"same bytes")
        self.assertEqual(first.hash, second.hash)
        self.assertEqual(self.db.query(FileObject).count(), 1)

    def test_missing_disk_blob_reads_as_none_rather_than_crashing(self):
        obj = FileObjectCRUD.store_file_object(self.db, b"will vanish")
        blob_store.store.delete(obj.hash)
        self.assertIsNone(obj.content)

    def test_existing_row_gets_its_blob_restored_if_missing_from_disk(self):
        obj = FileObjectCRUD.store_file_object(self.db, b"restore me")
        blob_store.store.delete(obj.hash)
        # Pushing the same content again should put the blob back.
        again = FileObjectCRUD.store_file_object(self.db, b"restore me")
        self.assertEqual(again.hash, obj.hash)
        self.assertEqual(again.content, b"restore me")


if __name__ == "__main__":
    unittest.main()
