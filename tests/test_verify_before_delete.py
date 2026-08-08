"""
Regression tests for issue #7: verify a HEIC conversion before deleting the
original -- and never leave the user with neither the original nor a filed copy.

``_process_single_file`` used to delete the original ``.heic`` with
``os.remove`` the instant ``convert_heic_to_jpeg`` returned a non-None path,
with no check that the JPEG was actually valid on disk. Because the converter
returns its output path right after ``image.save`` without inspecting the
result, a truncated write, a zero-byte file, a full disk, or a partial decode
all produced a "successful" return -- after which the only copy of the original
photo was destroyed.

These tests drive the real pipeline (no mocking of ``shutil.move`` or
``os.remove`` except where a specific failure is being simulated) and assert
that:

* a genuinely valid HEIC still converts, verifies, lands in ``backup/photos``,
  and has its original removed (happy path);
* a conversion that *reports success but produces a corrupt JPEG* leaves the
  original ``.heic`` in ``export/``, records the failure, drives a non-zero
  ``files_failed``, and leaves no corrupt artifact where a good photo belongs;
* a move failure after a good, verified conversion still leaves the original
  ``.heic`` recoverable in ``export/`` (defer-until-moved).
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from src.file_processor import FileProcessor, Settings
from tests.fixtures import make_exif_heic


class TestVerifyBeforeDelete(unittest.TestCase):
    """The original HEIC must survive any conversion that is not verified-good."""

    def setUp(self):
        """Create isolated temp export/backup dirs and a real FileProcessor."""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(Settings(export_dir=self.export_dir, backup_dir=self.backup_dir))

    def tearDown(self):
        """Remove the temp tree."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _export_path(self, name: str) -> str:
        """Return an absolute path inside the temp export directory."""
        return os.path.join(self.export_dir, name)

    def _backup_path(self, *parts: str) -> str:
        """Return an absolute path inside the temp backup directory."""
        return os.path.join(self.backup_dir, *parts)

    def _photos_tree(self) -> list:
        """Return the sorted list of files under ``backup/photos``."""
        photos_dir = self._backup_path("photos")
        if not os.path.isdir(photos_dir):
            return []
        return sorted(os.listdir(photos_dir))

    # ------------------------------------------------------------------ #
    # 1. Happy path: valid HEIC converts, verifies, lands, original gone  #
    # ------------------------------------------------------------------ #

    def test_valid_heic_converts_verifies_lands_and_original_removed(self):
        """A genuinely valid HEIC lands as a verified JPEG; the original is gone."""
        heic = make_exif_heic(
            self._export_path("IMG_0007.HEIC"),
            date_time_original="2023:08:09 10:11:12",
            color="blue",
            size=(64, 64),
        )

        results = self.processor.process_all_files(dry_run=False)

        landing = self._backup_path("photos", "2023.08.09.10.11.12.jpg")
        self.assertTrue(os.path.isfile(landing), f"converted JPEG not at {landing}")
        with Image.open(landing) as landed:
            self.assertEqual(landed.format, "JPEG")
            self.assertEqual(landed.size, (64, 64))
        # The original was deleted only after a verified-good landing.
        self.assertFalse(os.path.exists(heic), "verified original should be removed")
        self.assertEqual(results["files_failed"], 0)
        self.assertEqual(self.processor._failed_files, [])
        # Export is fully drained -- no leftover temp artifact.
        self.assertEqual(os.listdir(self.export_dir), [])

    # ------------------------------------------------------------------ #
    # 2. Failure path: conversion reports success but output is corrupt   #
    # ------------------------------------------------------------------ #

    def test_corrupt_conversion_preserves_original_and_records_failure(self):
        """A zero-byte 'successful' conversion must not destroy the original.

        The converter is patched to mimic exactly the reported bug: it returns a
        non-None path (signalling success) while the JPEG on disk is unreadable.
        The original ``.heic`` must survive in ``export/``, the failure must be
        recorded and drive a non-zero ``files_failed``, and no corrupt artifact
        may be left behind in export or backup.
        """
        heic = make_exif_heic(
            self._export_path("IMG_0008.HEIC"),
            date_time_original="2023:08:09 10:11:12",
            color="blue",
            size=(64, 64),
        )
        original_bytes = Path(heic).read_bytes()

        def _bad_convert(heic_path, output_dir=None):
            """Write a zero-byte .jpg and report success, like the real bug."""
            bad = Path(heic_path).with_suffix(".corrupt.jpg")
            bad.write_bytes(b"")  # zero bytes -> Image.open fails to decode
            return str(bad)

        with mock.patch.object(
            self.processor.heic_converter,
            "convert_heic_to_jpeg",
            side_effect=_bad_convert,
        ):
            results = self.processor.process_all_files(dry_run=False)

        # The original is untouched, byte-for-byte, right where the user left it.
        self.assertTrue(
            os.path.exists(heic),
            "original HEIC was destroyed despite an unverifiable conversion",
        )
        self.assertEqual(Path(heic).read_bytes(), original_bytes)

        # The failure surfaced in the results and in failed_files.
        self.assertGreaterEqual(results["files_failed"], 1)
        self.assertTrue(
            any(entry[1] == heic for entry in self.processor._failed_files),
            f"failure for {heic} not recorded in {self.processor._failed_files}",
        )

        # No corrupt artifact was left where a good photo should be, in either
        # export (the bad .jpg was cleaned up) or backup (nothing landed).
        self.assertEqual(self._photos_tree(), [], "a corrupt JPEG leaked into backup")
        leftover_jpgs = [
            name for name in os.listdir(self.export_dir) if name.lower().endswith(".jpg")
        ]
        self.assertEqual(leftover_jpgs, [], "corrupt JPEG left behind in export")

    # ------------------------------------------------------------------ #
    # 3. Failure path (dimension mismatch): output decodes but is wrong   #
    # ------------------------------------------------------------------ #

    def test_dimension_mismatch_conversion_preserves_original(self):
        """A decodable-but-wrong-size JPEG must not qualify to delete the original."""
        heic = make_exif_heic(
            self._export_path("IMG_0009.HEIC"),
            date_time_original="2023:08:09 10:11:12",
            color="blue",
            size=(64, 64),
        )

        def _wrong_size_convert(heic_path, output_dir=None):
            """Produce a valid JPEG whose dimensions do not match the source."""
            bad = Path(heic_path).with_suffix(".mismatch.jpg")
            Image.new("RGB", (8, 8), color="red").save(bad, format="JPEG")
            return str(bad)

        with mock.patch.object(
            self.processor.heic_converter,
            "convert_heic_to_jpeg",
            side_effect=_wrong_size_convert,
        ):
            results = self.processor.process_all_files(dry_run=False)

        self.assertTrue(
            os.path.exists(heic),
            "original HEIC was destroyed despite a dimension-mismatched conversion",
        )
        self.assertGreaterEqual(results["files_failed"], 1)
        self.assertEqual(self._photos_tree(), [])

    # ------------------------------------------------------------------ #
    # 4. Defer-until-moved: a move failure keeps the original recoverable #
    # ------------------------------------------------------------------ #

    def test_move_failure_after_good_conversion_keeps_original(self):
        """If the post-conversion move fails, the original .heic must survive.

        This is the defer-until-moved guarantee: the original is deleted only
        after the verified-good JPEG has actually landed in backup/, so a failure
        during the move can never leave the user with neither copy.
        """
        heic = make_exif_heic(
            self._export_path("IMG_0010.HEIC"),
            date_time_original="2023:08:09 10:11:12",
            color="blue",
            size=(64, 64),
        )

        with mock.patch(
            "src.file_processor.shutil.move",
            side_effect=OSError("disk full during move"),
        ):
            results = self.processor.process_all_files(dry_run=False)

        # The move failed, so nothing landed in backup...
        self.assertEqual(self._photos_tree(), [])
        # ...and the original HEIC is still recoverable in export.
        self.assertTrue(
            os.path.exists(heic),
            "original HEIC was deleted even though its JPEG never reached backup",
        )
        self.assertGreaterEqual(results["files_failed"], 1)


if __name__ == "__main__":
    unittest.main()
