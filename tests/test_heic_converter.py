"""
Test suite for HEIC -> JPEG conversion, verification, and cleanup.

These tests exercise the live HeicConverter paths that issue #7 made
load-bearing: the conversion failure/exception branches, every
``verify_conversion`` rejection reason, the two ``cleanup_original_heic``
modes, and the conversion-stats accounting.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src.heic_converter import HeicConverter
from tests.fixtures import make_corrupt_jpeg, make_exif_heic, make_no_exif_jpeg


class TestConvertHeicToJpeg(unittest.TestCase):
    """convert_heic_to_jpeg success, rejection, and failure paths."""

    def setUp(self):
        self.converter = HeicConverter()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _path(self, name):
        return os.path.join(self.temp_dir, name)

    def test_converts_real_heic_and_records_stats(self):
        """A real HEIC converts to a decodable JPEG and is logged as converted."""
        heic = make_exif_heic(
            self._path("photo.heic"), date_time_original="2024:01:15 14:30:45"
        )
        out_dir = os.path.join(self.temp_dir, "out")

        result = self.converter.convert_heic_to_jpeg(heic, output_dir=out_dir)

        self.assertIsNotNone(result)
        self.assertTrue(os.path.exists(result))
        # The returned path must be honored -- it is a unique mkstemp name, not
        # a fixed {stem}.jpg sibling (issue #26).
        self.assertTrue(result.endswith(".jpg"))
        with Image.open(result) as jpeg:
            self.assertEqual(jpeg.format, "JPEG")
        self.assertIn(
            (heic, result), self.converter.converted_files
        )

    def test_non_rgb_heic_is_converted_to_rgb(self):
        """A HEIC that decodes as RGBA is converted through RGB to a JPEG."""
        rgba = self._path("alpha.heic")
        Image.new("RGBA", (32, 32), (10, 20, 30, 128)).save(rgba, format="HEIF")

        result = self.converter.convert_heic_to_jpeg(rgba)

        self.assertIsNotNone(result)
        with Image.open(result) as jpeg:
            self.assertEqual(jpeg.mode, "RGB")

    def test_temp_cleanup_failure_is_swallowed(self):
        """If removing the reserved temp file also fails, it is swallowed."""
        bad = make_corrupt_jpeg(self._path("broken.heic"), content=b"nope")

        # The decode fails; then the placeholder unlink also raises -- both are
        # caught so the method still returns None rather than propagating.
        with patch("pathlib.Path.unlink", side_effect=OSError("cannot remove")):
            result = self.converter.convert_heic_to_jpeg(bad)

        self.assertIsNone(result)
        self.assertEqual(len(self.converter.failed_conversions), 1)

    def test_non_heic_input_returns_none_without_writing(self):
        """A non-HEIC input is rejected up front and writes no output file."""
        jpeg = make_no_exif_jpeg(self._path("plain.jpg"))
        before = set(os.listdir(self.temp_dir))

        result = self.converter.convert_heic_to_jpeg(jpeg)

        self.assertIsNone(result)
        self.assertEqual(set(os.listdir(self.temp_dir)), before)
        self.assertEqual(self.converter.converted_files, [])

    def test_undecodable_heic_records_failure_and_leaves_no_stub(self):
        """
        A .heic file whose bytes will not decode fails, records the failure,
        and removes the reserved temp JPEG so no 0-byte artifact remains.
        """
        bad = make_corrupt_jpeg(
            self._path("broken.heic"), content=b"not really heic"
        )
        out_dir = os.path.join(self.temp_dir, "out")

        result = self.converter.convert_heic_to_jpeg(bad, output_dir=out_dir)

        self.assertIsNone(result)
        self.assertEqual(len(self.converter.failed_conversions), 1)
        self.assertEqual(self.converter.failed_conversions[0][0], bad)
        # The reserved mkstemp placeholder must have been cleaned up.
        self.assertEqual(
            [p for p in os.listdir(out_dir) if p.endswith(".jpg")], []
        )

    def test_unwritable_output_dir_records_failure(self):
        """
        When the output directory cannot be created, conversion fails cleanly
        (no temp file was reserved yet) and the failure is recorded.
        """
        heic = make_exif_heic(self._path("photo.heic"))
        # A path nested *under a regular file* cannot be created as a directory.
        blocker = self._path("iamafile")
        Path(blocker).write_bytes(b"x")
        doomed_dir = os.path.join(blocker, "sub")

        result = self.converter.convert_heic_to_jpeg(heic, output_dir=doomed_dir)

        self.assertIsNone(result)
        self.assertEqual(len(self.converter.failed_conversions), 1)


class TestOptimizeFlag(unittest.TestCase):
    """The JPEG encode must not pay for optimize=True by default (issue #40)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _path(self, name):
        return os.path.join(self.temp_dir, name)

    def test_default_converter_does_not_request_huffman_optimization(self):
        """
        The default converter passes optimize=False to Image.save, and the
        JPEG it produces still verifies as a good, EXIF-preserving conversion.

        The kwarg assertion is what pins the perf fix: the extra Huffman pass
        (optimize=True) more than doubles the encode step for a ~2% size win,
        so a default that turned it back on would silently reintroduce the cost
        this issue removed. Pairing it with a real convert + verify_conversion
        keeps the test honest -- it fails both if the flag regresses and if the
        cheaper encode ever stopped producing a valid photo.
        """
        converter = HeicConverter()
        heic = make_exif_heic(
            self._path("photo.heic"),
            date_time_original="2024:01:15 14:30:45",
            size=(64, 64),
        )

        real_save = Image.Image.save
        captured = {}

        def capturing_save(self, fp, *args, **kwargs):
            captured.update(kwargs)
            return real_save(self, fp, *args, **kwargs)

        with patch.object(Image.Image, "save", capturing_save):
            result = converter.convert_heic_to_jpeg(heic, output_dir=self._path("out"))

        self.assertIsNotNone(result)
        # The load-bearing assertion: the encode never requests optimization.
        self.assertIn("optimize", captured)
        self.assertFalse(captured["optimize"])
        self.assertNotEqual(captured["optimize"], True)
        # And the cheaper encode still yields a valid, EXIF-preserving JPEG.
        self.assertTrue(converter.verify_conversion(heic, result))

    def test_optimize_can_be_re_enabled_via_constructor(self):
        """optimize=True is still reachable for anyone who wants the smaller file."""
        converter = HeicConverter(optimize=True)
        heic = make_exif_heic(self._path("opt.heic"), size=(64, 64))

        real_save = Image.Image.save
        captured = {}

        def capturing_save(self, fp, *args, **kwargs):
            captured.update(kwargs)
            return real_save(self, fp, *args, **kwargs)

        with patch.object(Image.Image, "save", capturing_save):
            result = converter.convert_heic_to_jpeg(heic, output_dir=self._path("out"))

        self.assertIsNotNone(result)
        self.assertTrue(captured.get("optimize"))


class TestVerifyConversion(unittest.TestCase):
    """Every verify_conversion branch: missing, size, EXIF-loss, error, pass."""

    def setUp(self):
        self.converter = HeicConverter()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _path(self, name):
        return os.path.join(self.temp_dir, name)

    def test_passes_for_a_genuine_conversion(self):
        """A real HEIC and its real converted JPEG verify as good."""
        heic = make_exif_heic(
            self._path("ok.heic"),
            date_time_original="2024:01:15 14:30:45",
            size=(64, 64),
        )
        jpeg = self.converter.convert_heic_to_jpeg(heic)

        self.assertTrue(self.converter.verify_conversion(heic, jpeg))

    def test_missing_converted_file_fails(self):
        """A converted path that does not exist fails verification."""
        heic = make_exif_heic(self._path("src.heic"))

        self.assertFalse(
            self.converter.verify_conversion(heic, self._path("nope.jpg"))
        )

    def test_size_mismatch_fails(self):
        """Differing pixel dimensions fail verification."""
        heic = make_exif_heic(self._path("big.heic"), size=(64, 64))
        # A JPEG of a different size stands in for a bad conversion.
        small = self._path("small.jpg")
        Image.new("RGB", (48, 48), color="red").save(small, format="JPEG")

        self.assertFalse(self.converter.verify_conversion(heic, small))

    def test_exif_loss_fails(self):
        """A source carrying EXIF but a converted file without it fails."""
        heic = make_exif_heic(
            self._path("hasexif.heic"),
            date_time_original="2024:01:15 14:30:45",
            size=(64, 64),
        )
        # Same size, but deliberately stripped of EXIF.
        stripped = self._path("stripped.jpg")
        Image.new("RGB", (64, 64), color="blue").save(stripped, format="JPEG")

        self.assertFalse(self.converter.verify_conversion(heic, stripped))

    def test_unreadable_input_is_caught_and_fails(self):
        """An undecodable-but-present file raises internally and fails safely."""
        heic = make_exif_heic(self._path("real.heic"), size=(64, 64))
        corrupt = make_corrupt_jpeg(self._path("corrupt.jpg"))

        # Both files exist, so the existence gate passes; Image.open then raises
        # on the corrupt file and the exception branch returns False.
        self.assertFalse(self.converter.verify_conversion(heic, corrupt))


class TestCleanupOriginalHeic(unittest.TestCase):
    """cleanup_original_heic verify / no-verify / error paths."""

    def setUp(self):
        self.converter = HeicConverter()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _path(self, name):
        return os.path.join(self.temp_dir, name)

    def test_verify_first_without_converted_path_keeps_original(self):
        """Refuses to delete when verify is requested but no JPEG is supplied."""
        heic = make_exif_heic(self._path("keep.heic"))

        deleted = self.converter.cleanup_original_heic(heic, verify_first=True)

        self.assertFalse(deleted)
        self.assertTrue(os.path.exists(heic))

    def test_verify_first_failed_verification_keeps_original(self):
        """A failed verification keeps the original on disk."""
        heic = make_exif_heic(self._path("keep2.heic"), size=(64, 64))
        wrong = self._path("wrong.jpg")
        Image.new("RGB", (10, 10), color="red").save(wrong, format="JPEG")

        deleted = self.converter.cleanup_original_heic(
            heic, converted_jpeg=wrong, verify_first=True
        )

        self.assertFalse(deleted)
        self.assertTrue(os.path.exists(heic))

    def test_verify_first_good_conversion_deletes_original(self):
        """A verified-good conversion allows the original to be deleted."""
        heic = make_exif_heic(
            self._path("del.heic"),
            date_time_original="2024:01:15 14:30:45",
            size=(64, 64),
        )
        jpeg = self.converter.convert_heic_to_jpeg(heic)

        deleted = self.converter.cleanup_original_heic(
            heic, converted_jpeg=jpeg, verify_first=True
        )

        self.assertTrue(deleted)
        self.assertFalse(os.path.exists(heic))

    def test_no_verify_deletes_original(self):
        """With verify_first False the original is deleted without a JPEG."""
        heic = make_exif_heic(self._path("gone.heic"))

        deleted = self.converter.cleanup_original_heic(heic, verify_first=False)

        self.assertTrue(deleted)
        self.assertFalse(os.path.exists(heic))

    def test_unlink_error_returns_false(self):
        """A delete of a nonexistent path is caught and reported as False."""
        deleted = self.converter.cleanup_original_heic(
            self._path("never_existed.heic"), verify_first=False
        )

        self.assertFalse(deleted)


class TestConversionStats(unittest.TestCase):
    """get_conversion_stats / clear_stats accounting."""

    def setUp(self):
        self.converter = HeicConverter()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _path(self, name):
        return os.path.join(self.temp_dir, name)

    def test_stats_reflect_success_and_failure_then_clear(self):
        """Stats count a success and a failure; clear_stats resets both."""
        good = make_exif_heic(self._path("good.heic"))
        bad = make_corrupt_jpeg(self._path("bad.heic"), content=b"nope")

        self.converter.convert_heic_to_jpeg(good)
        self.converter.convert_heic_to_jpeg(bad)

        stats = self.converter.get_conversion_stats()
        self.assertEqual(stats["successful_conversions"], 1)
        self.assertEqual(stats["failed_conversions"], 1)
        self.assertEqual(stats["total_conversions"], 1)
        # The returned collections are copies, not the live lists.
        self.assertIsNot(stats["converted_files"], self.converter.converted_files)
        self.assertIsNot(stats["failed_files"], self.converter.failed_conversions)

        self.converter.clear_stats()

        cleared = self.converter.get_conversion_stats()
        self.assertEqual(cleared["successful_conversions"], 0)
        self.assertEqual(cleared["failed_conversions"], 0)


if __name__ == "__main__":
    unittest.main()
