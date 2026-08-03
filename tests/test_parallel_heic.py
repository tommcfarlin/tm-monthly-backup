"""
Tests for the parallel HEIC conversion phase (issue #42).

The whole point of parallelizing HEIC conversion is that it must produce
results *byte-for-byte identical* to the sequential path on a tool that deletes
originals. These tests prove that: the same input is run through both the real
process-pool path and a forced-sequential path and the resulting ``backup/``
trees -- paths, which original landed where, and failure records -- are
compared for equality. The remaining tests pin the worker cap, the pool
threshold, dry-run parity, and worker-failure handling.
"""

import os
import shutil
import tempfile
import unittest
from typing import Dict, List, Tuple
from unittest.mock import patch

import src.file_processor as fp_module
from src.file_processor import FileProcessor
from tests.fixtures import make_exif_heic


def _make_corrupt_heic(path: str) -> str:
    """Write a ``.heic`` file whose bytes do not decode as HEIF."""
    with open(path, "wb") as handle:
        handle.write(b"ftypheic this is not a real HEIF bitstream" * 4)
    return path


def _build_fixture_export(export_dir: str) -> None:
    """
    Populate ``export_dir`` with a deterministic HEIC batch.

    The batch deliberately exceeds ``HEIC_PARALLEL_THRESHOLD`` and contains:
    six files with distinct capture seconds, three files that share one capture
    second (to exercise collision resolution / determinism), and one corrupt
    HEIC (to exercise worker-failure recording). Filenames are fixed so the
    ``os.walk`` scan order is identical across runs.
    """
    os.makedirs(export_dir, exist_ok=True)
    for index in range(6):
        make_exif_heic(
            os.path.join(export_dir, f"IMG_{index:04d}.heic"),
            date_time_original=f"2024:03:10 09:15:{index:02d}",
        )
    # Three files colliding on the same second -> must bump to :30, :31, :32
    # in scan order, identically in both runs.
    for suffix in ("a", "b", "c"):
        make_exif_heic(
            os.path.join(export_dir, f"COLLIDE_{suffix}.heic"),
            date_time_original="2024:03:10 09:15:30",
        )
    _make_corrupt_heic(os.path.join(export_dir, "BROKEN.heic"))


def _snapshot_backup_tree(backup_dir: str) -> List[str]:
    """Return every path under ``backup_dir`` as sorted, relative strings."""
    entries = []
    for root, dirs, files in os.walk(backup_dir):
        for name in list(dirs) + list(files):
            rel = os.path.relpath(os.path.join(root, name), backup_dir)
            entries.append(rel)
    return sorted(entries)


def _landing_map(summary: Dict) -> Dict[str, str]:
    """Map each source basename to its landed backup-relative path."""
    result = {}
    for record in summary["processed_files"]:
        original = os.path.basename(record["original_path"])
        final = record["final_path"]
        # Reduce to a stable, temp-dir-independent suffix (category/name).
        parts = final.replace(os.sep, "/").split("/")
        result[original] = "/".join(parts[-2:])
    return result


def _failure_set(summary: Dict) -> set:
    """Failure records reduced to ``(op, basename, msg)`` for comparison."""
    return {
        (op, os.path.basename(path), msg)
        for op, path, msg in summary["failed_files"]
    }


class TestParallelSequentialEquivalence(unittest.TestCase):
    """The parallel path must match the sequential path exactly."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, tag: str, force_sequential: bool) -> Tuple[Dict, str]:
        export_dir = os.path.join(self.root, f"{tag}_export")
        backup_dir = os.path.join(self.root, f"{tag}_backup")
        _build_fixture_export(export_dir)
        processor = FileProcessor(export_dir, backup_dir)
        if force_sequential:
            # Raise the threshold out of reach so no pool spawns and every HEIC
            # is converted inline -- the genuine pre-#42 sequential code path.
            with patch.object(FileProcessor, "HEIC_PARALLEL_THRESHOLD", 10 ** 9):
                summary = processor.process_all_files(dry_run=False)
        else:
            summary = processor.process_all_files(dry_run=False)
        return summary, backup_dir

    def test_parallel_equals_sequential(self):
        """Same input, both paths -> identical tree, landings, and failures."""
        seq_summary, seq_backup = self._run("seq", force_sequential=True)
        par_summary, par_backup = self._run("par", force_sequential=False)

        # The backup trees must be structurally identical.
        self.assertEqual(
            _snapshot_backup_tree(seq_backup),
            _snapshot_backup_tree(par_backup),
        )
        # Each original must land at the SAME name (collision resolution and
        # order preserved) in both runs.
        self.assertEqual(_landing_map(seq_summary), _landing_map(par_summary))
        # Failure accounting must be identical (the corrupt HEIC, same message).
        self.assertEqual(_failure_set(seq_summary), _failure_set(par_summary))
        # Summary counts must agree exactly.
        for key in (
            "files_processed",
            "files_failed",
            "heic_conversions",
            "heic_conversion_failures",
        ):
            self.assertEqual(
                seq_summary[key], par_summary[key], f"mismatch on {key}"
            )

    def test_colliding_timestamps_resolve_identically(self):
        """The three colliding files bump to the same names both ways."""
        seq_summary, _ = self._run("seq", force_sequential=True)
        par_summary, _ = self._run("par", force_sequential=False)
        seq_map = _landing_map(seq_summary)
        par_map = _landing_map(par_summary)
        for name in ("COLLIDE_a.heic", "COLLIDE_b.heic", "COLLIDE_c.heic"):
            self.assertEqual(seq_map[name], par_map[name])
        # And the three landed on three distinct names (no overwrite).
        collided = {seq_map[f"COLLIDE_{s}.heic"] for s in ("a", "b", "c")}
        self.assertEqual(len(collided), 3)


class TestWorkerCap(unittest.TestCase):
    """The pool must be capped, never sized to os.cpu_count()."""

    def test_worker_count_capped_below_cpu_count(self):
        processor = FileProcessor("export", "backup")
        with patch("src.file_processor.os.cpu_count", return_value=10):
            # Plenty of files, but the cap (6) wins over 10 logical CPUs.
            self.assertEqual(processor._heic_worker_count(100), 6)

    def test_worker_count_bounded_by_file_count(self):
        processor = FileProcessor("export", "backup")
        with patch("src.file_processor.os.cpu_count", return_value=10):
            self.assertEqual(processor._heic_worker_count(3), 3)

    def test_pool_constructed_with_capped_workers(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        export_dir = os.path.join(root, "export")
        _build_fixture_export(export_dir)
        processor = FileProcessor(export_dir, os.path.join(root, "backup"))

        seen = {}
        real_pool = fp_module.ProcessPoolExecutor

        class SpyPool(real_pool):
            def __init__(self, *args, **kwargs):
                seen["max_workers"] = kwargs.get("max_workers")
                super().__init__(*args, **kwargs)

        with patch("src.file_processor.os.cpu_count", return_value=10):
            with patch.object(fp_module, "ProcessPoolExecutor", SpyPool):
                processor.process_all_files(dry_run=False)

        self.assertEqual(seen["max_workers"], 6)
        self.assertNotEqual(seen["max_workers"], 10)


class TestThreshold(unittest.TestCase):
    """Small batches must not spawn a pool but must still be correct."""

    def test_below_threshold_uses_no_pool(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        export_dir = os.path.join(root, "export")
        backup_dir = os.path.join(root, "backup")
        os.makedirs(export_dir)
        # Five HEIC files: below the threshold of eight.
        for index in range(5):
            make_exif_heic(
                os.path.join(export_dir, f"IMG_{index:04d}.heic"),
                date_time_original=f"2024:05:01 12:00:{index:02d}",
            )
        processor = FileProcessor(export_dir, backup_dir)

        with patch.object(
            fp_module, "ProcessPoolExecutor"
        ) as pool_ctor:
            summary = processor.process_all_files(dry_run=False)

        pool_ctor.assert_not_called()
        self.assertEqual(summary["files_processed"], 5)
        self.assertEqual(summary["files_failed"], 0)
        self.assertEqual(
            len(os.listdir(os.path.join(backup_dir, "photos"))), 5
        )


class TestDryRunNoPool(unittest.TestCase):
    """Dry-run converts nothing, so it must never spawn a pool (#10)."""

    def test_dry_run_with_many_heic_spawns_no_pool(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        export_dir = os.path.join(root, "export")
        backup_dir = os.path.join(root, "backup")
        _build_fixture_export(export_dir)  # exceeds threshold
        before = sorted(os.listdir(export_dir))
        processor = FileProcessor(export_dir, backup_dir)

        with patch.object(fp_module, "ProcessPoolExecutor") as pool_ctor:
            summary = processor.process_all_files(dry_run=True)

        pool_ctor.assert_not_called()
        # Nothing converted, nothing moved: export is untouched, backup absent.
        self.assertEqual(sorted(os.listdir(export_dir)), before)
        self.assertFalse(os.path.exists(os.path.join(backup_dir, "photos")))
        # A dry run is side-effect free: it records no processed/failed files.
        self.assertEqual(summary["files_processed"], 0)
        self.assertEqual(summary["files_failed"], 0)


class TestWorkerFailureHandling(unittest.TestCase):
    """One failing file is recorded and skipped; the batch still lands."""

    def test_corrupt_heic_recorded_others_land(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        export_dir = os.path.join(root, "export")
        backup_dir = os.path.join(root, "backup")
        _build_fixture_export(export_dir)  # includes one BROKEN.heic
        processor = FileProcessor(export_dir, backup_dir)

        summary = processor.process_all_files(dry_run=False)

        failures = _failure_set(summary)
        self.assertIn(
            ("convert_heic", "BROKEN.heic", "HEIC conversion failed"), failures
        )
        self.assertEqual(summary["files_failed"], 1)
        self.assertEqual(summary["heic_conversion_failures"], 1)
        # The nine good files still converted and landed.
        self.assertEqual(summary["files_processed"], 9)
        self.assertEqual(
            len(os.listdir(os.path.join(backup_dir, "photos"))), 9
        )
        # The corrupt original is preserved in export (never deleted).
        self.assertTrue(os.path.exists(os.path.join(export_dir, "BROKEN.heic")))


if __name__ == "__main__":
    unittest.main()
