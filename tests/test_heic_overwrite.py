"""
Regression tests for issue #26: HEIC conversion must never overwrite a sibling.

``HeicConverter.convert_heic_to_jpeg`` used to write the JPEG to a fixed
``{stem}.jpg`` beside the HEIC. When a real sibling with that name already sat
in ``export/`` -- either an exact ``IMG_1234.jpg`` or an uppercase
``IMG_1234.JPG`` on a case-insensitive APFS volume -- the conversion silently
overwrote and destroyed that photo, with ``files_failed: 0`` and no error.

These tests drive the real pipeline (no mocking of ``shutil.move``,
``os.remove``, or the converter) and assert that a pre-existing sibling reaches
``backup/`` intact. The end-to-end cases force the dangerous ``.heic``-first
scan order so they reliably fail against the un-fixed code, matching the
issue's forced-scan-order evidence. A unit-level case pins the guarantee
regardless of filesystem case sensitivity.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from src.file_processor import FileProcessor
from src.heic_converter import HeicConverter
from tests.fixtures import make_exif_heic, make_exif_jpeg


def _fs_is_case_insensitive(directory: str) -> bool:
    """Return True if ``directory`` lives on a case-insensitive filesystem."""
    probe = os.path.join(directory, "cAsE_probe.tmp")
    with open(probe, "w"):
        pass
    try:
        return os.path.exists(os.path.join(directory, "CASE_PROBE.TMP"))
    finally:
        os.remove(probe)


class TestHeicOverwrite(unittest.TestCase):
    """HEIC conversion output must never clobber an existing file."""

    def setUp(self):
        """Create isolated temp export/backup dirs and a real FileProcessor."""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

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

    def _assert_pixel_near(self, path: str, expected: tuple, tol: int = 16):
        """Assert the top-left pixel of ``path`` is within ``tol`` of ``expected``."""
        with Image.open(path) as img:
            actual = img.getpixel((0, 0))
        for channel, (got, want) in enumerate(zip(actual, expected)):
            self.assertLessEqual(
                abs(got - want),
                tol,
                f"pixel channel {channel} at {path} was {actual}, expected ~{expected}",
            )

    def _photos_tree(self) -> list:
        """Return the sorted list of files under ``backup/photos``."""
        photos_dir = self._backup_path("photos")
        if not os.path.isdir(photos_dir):
            return []
        return sorted(os.listdir(photos_dir))

    def _run_with_scan_order(self, names: list):
        """Run the pipeline forcing ``os.walk`` to return ``names`` in order.

        The exact-name overwrite is scan-order dependent (see the issue's
        99/200 measurement); forcing the ``.heic``-first order makes the
        regression deterministic instead of a coin flip.
        """
        walk_result = [(self.export_dir, [], list(names))]
        with mock.patch("src.file_processor.os.walk", return_value=walk_result):
            return self.processor.process_all_files(dry_run=False)

    # ------------------------------------------------------------------ #
    # 1. Exact same-stem collision: IMG_1234.heic + IMG_1234.jpg          #
    # ------------------------------------------------------------------ #

    def test_exact_same_stem_jpeg_is_preserved_end_to_end(self):
        """A real IMG_1234.jpg survives conversion of a sibling IMG_1234.heic."""
        make_exif_heic(
            self._export_path("IMG_1234.heic"),
            date_time_original="2022:03:04 05:06:07",
            color="blue",
            size=(64, 64),
        )
        make_exif_jpeg(
            self._export_path("IMG_1234.jpg"),
            date_time_original="2024:01:15 14:30:45",
            color="green",
            size=(48, 48),
        )

        # Force the dangerous order: HEIC reached (and converted) first.
        self._run_with_scan_order(["IMG_1234.heic", "IMG_1234.jpg"])

        # Both photos must land in backup/photos, distinct and intact.
        heic_landing = self._backup_path("photos", "2022.03.04.05.06.07.jpg")
        jpeg_landing = self._backup_path("photos", "2024.01.15.14.30.45.jpg")

        self.assertTrue(
            os.path.isfile(jpeg_landing),
            "pre-existing IMG_1234.jpg was destroyed by the HEIC conversion",
        )
        with Image.open(jpeg_landing) as landed:
            self.assertEqual(
                landed.size,
                (48, 48),
                "landed JPEG is not the original 48x48 green photo -- it was clobbered",
            )
        self._assert_pixel_near(jpeg_landing, (0, 128, 0))

        self.assertTrue(
            os.path.isfile(heic_landing),
            "converted HEIC did not reach backup/photos",
        )
        with Image.open(heic_landing) as landed:
            self.assertEqual(landed.size, (64, 64))

        # Exactly two distinct photos survived; nothing was silently destroyed.
        self.assertEqual(len(self._photos_tree()), 2)
        # Export is fully consumed -- no leftover temp artifact either.
        self.assertEqual(os.listdir(self.export_dir), [])

    # ------------------------------------------------------------------ #
    # 2. Case-insensitive variant: IMG_1234.heic + IMG_1234.JPG           #
    # ------------------------------------------------------------------ #

    def test_case_insensitive_uppercase_jpeg_is_preserved(self):
        """An uppercase IMG_1234.JPG survives conversion of IMG_1234.heic.

        On a case-insensitive APFS volume the un-fixed converter writes
        ``IMG_1234.jpg`` and truncates ``IMG_1234.JPG`` in place. On a
        case-sensitive volume the two names differ, so this asserts the
        unique-output guarantee directly instead.
        """
        make_exif_heic(
            self._export_path("IMG_1234.heic"),
            date_time_original="2022:03:04 05:06:07",
            color="blue",
            size=(64, 64),
        )
        make_exif_jpeg(
            self._export_path("IMG_1234.JPG"),
            date_time_original="2024:01:15 14:30:45",
            color="green",
            size=(48, 48),
        )
        original_jpeg_bytes = Path(self._export_path("IMG_1234.JPG")).read_bytes()

        if _fs_is_case_insensitive(self.export_dir):
            # The dangerous case: force HEIC-first and assert no data loss.
            self._run_with_scan_order(["IMG_1234.heic", "IMG_1234.JPG"])

            jpeg_landing = self._backup_path("photos", "2024.01.15.14.30.45.jpg")
            self.assertTrue(
                os.path.isfile(jpeg_landing),
                "uppercase IMG_1234.JPG was destroyed on a case-insensitive volume",
            )
            with Image.open(jpeg_landing) as landed:
                self.assertEqual(landed.size, (48, 48))
            self._assert_pixel_near(jpeg_landing, (0, 128, 0))
            self.assertEqual(len(self._photos_tree()), 2)
            self.assertEqual(os.listdir(self.export_dir), [])
        else:
            # Case-sensitive volume: the names never collide, so assert the
            # unique-output guarantee at the converter level directly.
            converter = HeicConverter()
            result = converter.convert_heic_to_jpeg(self._export_path("IMG_1234.heic"))
            self.assertIsNotNone(result)
            self.assertNotEqual(
                os.path.normcase(os.path.abspath(result)),
                os.path.normcase(os.path.abspath(self._export_path("IMG_1234.JPG"))),
                "conversion output collided with the uppercase sibling",
            )
            self.assertEqual(
                Path(self._export_path("IMG_1234.JPG")).read_bytes(),
                original_jpeg_bytes,
                "uppercase sibling bytes changed",
            )

    # ------------------------------------------------------------------ #
    # 3. Unit: convert never writes to an existing path                   #
    # ------------------------------------------------------------------ #

    def test_convert_never_overwrites_existing_target(self):
        """Pre-create the fixed target name; convert must not touch it."""
        heic_path = make_exif_heic(
            self._export_path("IMG_9999.heic"),
            date_time_original="2021:06:07 08:09:10",
            color="blue",
            size=(64, 64),
        )
        # The name the un-fixed code would have written to, holding a real photo.
        sibling = make_exif_jpeg(
            self._export_path("IMG_9999.jpg"),
            date_time_original="2024:01:15 14:30:45",
            color="red",
            size=(100, 100),
        )
        sibling_bytes_before = Path(sibling).read_bytes()

        converter = HeicConverter()
        result = converter.convert_heic_to_jpeg(heic_path)

        self.assertIsNotNone(result, "conversion should still succeed")
        self.assertTrue(os.path.isfile(result), "converter must return a real path")

        # A different path than the pre-existing sibling was written.
        self.assertNotEqual(
            os.path.normcase(os.path.abspath(result)),
            os.path.normcase(os.path.abspath(sibling)),
            "conversion overwrote the pre-existing sibling's path",
        )

        # The sibling is byte-for-byte untouched.
        self.assertEqual(
            Path(sibling).read_bytes(),
            sibling_bytes_before,
            "pre-existing IMG_9999.jpg was modified by the conversion",
        )
        with Image.open(sibling) as untouched:
            self.assertEqual(untouched.size, (100, 100))

        # The returned output is the converted HEIC (64x64), not the sibling.
        with Image.open(result) as converted:
            self.assertEqual(converted.size, (64, 64))


if __name__ == "__main__":
    unittest.main()
