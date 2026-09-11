#!/usr/bin/env python3
"""Recompress existing blobs in the object store.

Blobs written before compression was added sit on disk raw. This rewrites each one
through the current encoder, which typically reclaims ~40% for text and leaves
already-packed formats (zip, exe, png) untouched because the encoder declines to
compress what it cannot shrink.

Safe to interrupt and re-run. Each blob is verified to decode back to its own hash
*before* the old file is replaced, and the replacement is atomic, so a crash can never
leave a blob that does not match its name.

    python tools/compact_blobs.py --status
    python tools/compact_blobs.py --apply
    python tools/compact_blobs.py --verify     # decode every blob, check it hashes true
"""

import argparse
import hashlib
import os
import sys
import tempfile

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

os.environ.setdefault("FOXNEST_ALLOW_INSECURE_AUTH", "1")

from app.services.blob_store import MAGIC, _frame, store  # noqa: E402


def human(num_bytes):
    value = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024


def iter_blobs():
    root = store.root
    if not root.is_dir():
        return
    for shard in sorted(root.iterdir()):
        if not shard.is_dir():
            continue
        for blob in sorted(shard.iterdir()):
            if blob.is_file() and not blob.name.startswith(".tmp-"):
                yield blob


def cmd_status():
    total = framed = unframed = 0
    on_disk = 0
    for blob in iter_blobs():
        total += 1
        size = blob.stat().st_size
        on_disk += size
        with open(blob, "rb") as fh:
            head = fh.read(len(MAGIC))
        if head == MAGIC:
            framed += 1
        else:
            unframed += 1
    print(f"blob store : {store.root}")
    print(f"  objects  : {total:,}  ({human(on_disk)} on disk)")
    print(f"  encoded  : {framed:,}")
    print(f"  raw      : {unframed:,}  <- would be recompressed by --apply")
    if unframed:
        print("\nNext: --apply")
    else:
        print("\nEverything is already encoded.")
    return 0


def cmd_apply():
    before = after = 0
    rewritten = skipped = failed = 0

    for blob in iter_blobs():
        digest = blob.name
        try:
            original_size = blob.stat().st_size
            with open(blob, "rb") as fh:
                stored = fh.read()
            if stored.startswith(MAGIC):
                skipped += 1
                before += original_size
                after += original_size
                continue

            content = stored                      # pre-framing blobs are raw
            if hashlib.sha1(content).hexdigest() != digest:
                print(f"\n  ! {digest}: does not hash to its own name; left alone")
                failed += 1
                continue

            encoded = _frame(content)
            # Prove the new encoding round-trips before replacing anything.
            from app.services.blob_store import _unframe
            if _unframe(encoded) != content:
                print(f"\n  ! {digest}: re-encode did not round-trip; left alone")
                failed += 1
                continue

            fd, tmp = tempfile.mkstemp(dir=str(blob.parent), prefix=".tmp-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(encoded)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, blob)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

            rewritten += 1
            before += original_size
            after += len(encoded)
            if rewritten % 200 == 0:
                print(f"  recompressed {rewritten:,} ({human(before)} -> {human(after)})", end="\r")
        except Exception as exc:                  # noqa: BLE001 - report and continue
            failed += 1
            print(f"\n  ! {digest}: {exc}")

    print()
    saved = before - after
    pct = (100 * saved / before) if before else 0
    print(f"\nRecompressed {rewritten:,}, already encoded {skipped:,}, failed {failed}.")
    print(f"{human(before)} -> {human(after)}  ({human(saved)} reclaimed, {pct:.1f}%)")
    return 1 if failed else 0


def cmd_verify():
    print("Decoding every blob and checking it hashes to its own name...")
    report = store.integrity_check()
    print(f"  checked : {report['checked']:,}")
    print(f"  corrupt : {report['corrupt']}")
    print(f"  missing : {report['missing']}")
    for name in report["examples"]:
        print(f"    {name}")
    ok = report["corrupt"] == 0 and report["missing"] == 0
    print("\nOBJECT STORE IS " + ("INTACT" if ok else "DAMAGED"))
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="report encoding state (default)")
    group.add_argument("--apply", action="store_true", help="recompress raw blobs in place")
    group.add_argument("--verify", action="store_true", help="check every blob decodes and hashes true")
    args = parser.parse_args()

    if args.apply:
        return cmd_apply()
    if args.verify:
        return cmd_verify()
    return cmd_status()


if __name__ == "__main__":
    raise SystemExit(main())
