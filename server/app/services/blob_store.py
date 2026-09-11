"""Content-addressed blob storage on the filesystem.

File contents used to live in ``file_objects.content`` as a ``LargeBinary``, which meant
every backup, replica and database migration had to carry the whole object store -- 4.15 GB
of it, 92% build artifacts. Blobs now live on disk, addressed by their SHA-1, and the
database keeps only the hash, size and mime type.

Layout mirrors git's: a two-character shard directory keeps any one directory small.

    <root>/ab/abcdef0123...

Reads fall back to the database column, so rows written before the migration keep working
untouched. That fallback is what makes this change safe to deploy before the data has
moved; see ``tools/migrate_blobs_to_disk.py``.

Writes are atomic: content goes to a temporary file in the same directory and is renamed
into place, so a crash mid-write can never leave a partial blob under a valid hash.

Blobs are stored zlib-compressed behind a small frame, which saves ~41% on this
repository's content. Compression is skipped whenever it fails to win, so already-packed
formats (zip, exe, png) are not inflated. Decoding is transparent: `get` returns exactly
the bytes `put` was given, and blobs written before framing existed are read as raw.

Delta encoding between versions of a file was measured and deliberately not implemented:
only 1.4% of paths in this repository carry more than one version, and on those the gain
over plain zlib was 0.9% -- far too little to justify delta chains, whose broken links are
a real corruption risk. The frame carries an encoding byte, so a delta encoding can be
added later without rewriting the store.
"""

import hashlib
import os
import tempfile
import zlib
from pathlib import Path
from typing import Optional

from app.config import BLOB_DIR

#: Frame written ahead of every blob this version stores: 4-byte magic, 1-byte encoding.
#: Blobs written before framing existed have no header and are read as raw, which is what
#: lets compression be introduced without rewriting the store first.
MAGIC = b"FXB1"
ENCODING_RAW = 0
ENCODING_ZLIB = 1
HEADER_LEN = len(MAGIC) + 1

#: Level 6 is the usual balance; on this repository's blobs it saves ~41%.
COMPRESS_LEVEL = 6

#: Below this, framing overhead and CPU outweigh any saving.
MIN_COMPRESS_BYTES = 256


def _frame(content: bytes) -> bytes:
    """Encode content for storage, compressing only when it actually helps.

    Already-compressed payloads (zip, exe, png) inflate slightly under zlib, so the raw
    form is kept whenever compression fails to win.
    """
    if len(content) >= MIN_COMPRESS_BYTES:
        packed = zlib.compress(content, COMPRESS_LEVEL)
        if len(packed) < len(content):
            return MAGIC + bytes([ENCODING_ZLIB]) + packed
    return MAGIC + bytes([ENCODING_RAW]) + content


def _unframe(blob: bytes) -> bytes:
    """Decode stored bytes. Anything without the magic is a pre-framing raw blob."""
    if len(blob) < HEADER_LEN or not blob.startswith(MAGIC):
        return blob
    encoding = blob[len(MAGIC)]
    payload = blob[HEADER_LEN:]
    if encoding == ENCODING_ZLIB:
        try:
            return zlib.decompress(payload)
        except zlib.error:
            # Not actually framed: a legacy blob whose first bytes happen to match.
            return blob
    if encoding == ENCODING_RAW:
        return payload
    return blob


class BlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    # -- addressing --------------------------------------------------------------
    def path_for(self, blob_hash: str) -> Path:
        """Absolute path for a hash. Rejects anything that is not a plain hex digest,
        so a crafted value cannot escape the store via path traversal."""
        digest = (blob_hash or "").strip().lower()
        if len(digest) < 4 or not all(ch in "0123456789abcdef" for ch in digest):
            raise ValueError(f"invalid blob hash: {blob_hash!r}")
        return self.root / digest[:2] / digest

    @staticmethod
    def hash_bytes(content: bytes) -> str:
        return hashlib.sha1(content).hexdigest()

    # -- operations --------------------------------------------------------------
    def exists(self, blob_hash: str) -> bool:
        try:
            return self.path_for(blob_hash).is_file()
        except ValueError:
            return False

    def get(self, blob_hash: str) -> Optional[bytes]:
        try:
            target = self.path_for(blob_hash)
        except ValueError:
            return None
        try:
            return _unframe(target.read_bytes())
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def stored_size(self, blob_hash: str) -> Optional[int]:
        """Bytes this blob occupies on disk (compressed)."""
        try:
            return self.path_for(blob_hash).stat().st_size
        except (ValueError, OSError):
            return None

    def size(self, blob_hash: str) -> Optional[int]:
        """Length of the *content*, which is what callers mean by a blob's size.

        Compressed blobs occupy less on disk than they measure, so this decodes rather
        than trusting stat(); use stored_size() when the question is about disk usage.
        """
        content = self.get(blob_hash)
        return None if content is None else len(content)

    def put(self, content: bytes, blob_hash: Optional[str] = None) -> str:
        """Write content and return its hash. Idempotent: an existing blob is left alone."""
        digest = blob_hash or self.hash_bytes(content)
        target = self.path_for(digest)
        if target.is_file():
            return digest

        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(_frame(content))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return digest

    def delete(self, blob_hash: str) -> bool:
        """Remove a blob. Returns whether a file was actually removed."""
        try:
            target = self.path_for(blob_hash)
        except ValueError:
            return False
        try:
            target.unlink()
        except (FileNotFoundError, OSError):
            return False
        # Tidy the shard directory when it empties out; harmless if it is not empty.
        try:
            target.parent.rmdir()
        except OSError:
            pass
        return True

    def stats(self) -> dict:
        """Object count and bytes *occupied on disk* -- the compressed, framed size.

        This is deliberately disk usage rather than content length: it answers "how big is
        the store", and computing logical size would mean decoding every blob.
        """
        count = 0
        total = 0
        if self.root.is_dir():
            for shard in self.root.iterdir():
                if not shard.is_dir():
                    continue
                for blob in shard.iterdir():
                    if blob.is_file() and not blob.name.startswith(".tmp-"):
                        count += 1
                        try:
                            total += blob.stat().st_size
                        except OSError:
                            pass
        return {"root": str(self.root), "objects": count, "bytes": total}

    def integrity_check(self, limit: Optional[int] = None) -> dict:
        """Decode every blob and confirm it still hashes to its own filename.

        Content addressing makes this a complete check: a blob that decodes to the wrong
        bytes -- through disk corruption or a bad write -- cannot hash back to its name.
        """
        checked = bad = missing = 0
        corrupt = []
        if self.root.is_dir():
            for shard in sorted(self.root.iterdir()):
                if not shard.is_dir():
                    continue
                for blob in sorted(shard.iterdir()):
                    if not blob.is_file() or blob.name.startswith(".tmp-"):
                        continue
                    if limit and checked >= limit:
                        return {"checked": checked, "corrupt": bad, "missing": missing,
                                "examples": corrupt[:10], "truncated": True}
                    checked += 1
                    content = self.get(blob.name)
                    if content is None:
                        missing += 1
                        continue
                    if hashlib.sha1(content).hexdigest() != blob.name:
                        bad += 1
                        corrupt.append(blob.name)
        return {"checked": checked, "corrupt": bad, "missing": missing,
                "examples": corrupt[:10], "truncated": False}


#: Process-wide store. Tests point this at a temporary directory.
store = BlobStore(BLOB_DIR)
