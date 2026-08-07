#!/usr/bin/env python3
"""Snapshot export/ before a run, and reconcile backup/ against it after.

The tool MOVES files out of export/, so once a run finishes there is no source
list left to compare the archive against. That is why a one-file accounting gap
in a real 325-file run could not be pinned to a specific photo: by the time the
gap was noticed, the evidence had been consumed.

Usage:
    # BEFORE running the tool
    python3 scripts/snapshot-export.py snapshot export/ /tmp/export-snapshot.json

    # AFTER running the tool
    python3 scripts/snapshot-export.py reconcile /tmp/export-snapshot.json backup/

Read-only with respect to export/ and backup/: it stats and hashes, never
writes, moves, or deletes. Nothing here imports the tool, so taking a snapshot
cannot perturb the run being measured.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

# Sidecars are deleted rather than archived, and .DS_Store is macOS's, not the
# tool's -- counting either as an expected landing would manufacture a false gap.
NON_ARCHIVED_SUFFIXES = {".aae"}
JUNK_NAMES = {".ds_store", ".localized", "thumbs.db"}


def _digest(path, limit=1 << 20):
    """Hash the first 1 MiB. Enough to identify a photo; fast on 6 GB of input."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read(limit))
    return h.hexdigest()


def _walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        # Mirror the tool's hidden-directory pruning (issue #56).
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.lower() in JUNK_NAMES:
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or os.path.isfile(full):
                yield full


def snapshot(export_dir, out_path):
    entries = []
    for full in _walk(export_dir):
        try:
            size = os.stat(full).st_size
            entries.append({
                "path": os.path.relpath(full, export_dir),
                "size": size,
                "sha256_1m": _digest(full),
                "archived": Path(full).suffix.lower() not in NON_ARCHIVED_SUFFIXES,
            })
        except OSError as error:
            entries.append({"path": os.path.relpath(full, export_dir),
                            "error": str(error)})

    payload = {"export_dir": os.path.abspath(export_dir), "entries": entries}
    Path(out_path).write_text(json.dumps(payload, indent=2))

    archived = [e for e in entries if e.get("archived")]
    sidecars = [e for e in entries if e.get("archived") is False]
    print(f"snapshot written: {out_path}")
    print(f"  files expected to be archived: {len(archived)}")
    print(f"  sidecars (deleted, not archived): {len(sidecars)}")
    print(f"  unreadable: {len([e for e in entries if 'error' in e])}")
    return 0


def reconcile(snapshot_path, backup_dir):
    payload = json.loads(Path(snapshot_path).read_text())
    expected = [e for e in payload["entries"] if e.get("archived")]

    # Index the archive by content digest. A HEIC that was converted to JPEG will
    # NOT match its source digest -- those are reported separately rather than
    # counted as missing, since re-encoding legitimately changes the bytes.
    landed = {}
    for full in _walk(backup_dir):
        landed.setdefault(_digest(full), []).append(
            os.path.relpath(full, backup_dir)
        )

    matched, unmatched = [], []
    for entry in expected:
        hits = landed.get(entry["sha256_1m"])
        if hits:
            matched.append((entry["path"], hits[0]))
        else:
            unmatched.append(entry)

    total_landed = sum(len(v) for v in landed.values())
    print(f"expected archived (from snapshot): {len(expected)}")
    print(f"files present in {backup_dir}:      {total_landed}")
    print(f"matched byte-for-byte:             {len(matched)}")
    print(f"unmatched (re-encoded or MISSING): {len(unmatched)}")

    heic = [e for e in unmatched if Path(e["path"]).suffix.lower() in {".heic", ".heif"}]
    other = [e for e in unmatched if e not in heic]
    print(f"  of those, HEIC (expected to differ after conversion): {len(heic)}")
    print(f"  of those, NON-HEIC (should have matched exactly):      {len(other)}")

    if other:
        print("\nNON-HEIC sources with no byte-identical file in the archive:")
        for entry in other:
            print(f"  {entry['path']}  ({entry['size']} bytes)")

    gap = len(expected) - total_landed
    print(f"\naccounting gap (expected - present): {gap}")
    return 1 if (other or gap != 0) else 0


def main(argv):
    if len(argv) != 4:
        print(__doc__)
        return 2
    mode, a, b = argv[1], argv[2], argv[3]
    if mode == "snapshot":
        return snapshot(a, b)
    if mode == "reconcile":
        return reconcile(a, b)
    print(f"unknown mode: {mode}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
