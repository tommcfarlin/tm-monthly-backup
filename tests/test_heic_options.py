"""
Tests for issue #41: expose JPEG quality and HEIC retention as real options.

Before this issue, `HeicConverter.__init__` accepted `jpeg_quality` (and,
since issue #40, `optimize`) but `FileProcessor` always constructed
`HeicConverter()` with no arguments -- there was no path from `main.py` down
to that parameter, and no way at all to keep an original `.heic` after
conversion; `_process_single_file` deleted it unconditionally.

These tests prove, with real HEIC bytes end to end (through `FileProcessor`
and, for the CLI-level tests, through `src.main.main` via `CliRunner`):

* `--jpeg-quality` reaches `HeicConverter` and measurably changes the
  converted JPEG's size.
* `--keep-heic` leaves the original `.heic` in place while still filing the
  converted JPEG in `backup/`.
* Retention gates ONLY the delete step, never the issue #7 verify step: a
  conversion that fails verification is still recorded as a failure with
  `keep_heic=True`, exactly as it is with the default `keep_heic=False`. This
  is the safety-critical case -- a retention flag that short-circuited
  verification would turn a loud failure into a silent one.
* `Settings` is frozen (immutable for the duration of a run).
* Both options behave identically on the sequential and pooled (issue #42)
  HEIC conversion paths.
"""

import os
import shutil
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Dict
from unittest import mock

from click.testing import CliRunner
from PIL import Image

from src.file_processor import FileProcessor, Settings
from src.main import main
from tests.fixtures import make_exif_heic


def _noisy_heic(path: str, size=(200, 200)) -> str:
    """
    Write a detail-rich (not flat-color) HEIC file.

    JPEG quality differences are invisible on a flat-color image -- a solid
    color compresses to a handful of DC-only blocks at any quality setting.
    `Image.effect_noise` generates real per-pixel variance, which is exactly
    what a JPEG quality setting trades off against file size, so the quality
    knob has something to measurably act on.
    """
    noisy = Image.effect_noise(size, 60).convert("RGB")
    noisy.save(path, format="HEIF")
    return path


def _landing_map(summary: Dict) -> Dict[str, str]:
    """Map each source basename to its landed backup-relative path."""
    result = {}
    for record in summary["processed_files"]:
        original = os.path.basename(record["original_path"])
        final = record["final_path"]
        parts = final.replace(os.sep, "/").split("/")
        result[original] = "/".join(parts[-2:])
    return result


def _failure_set(summary: Dict) -> set:
    """Failure records reduced to (op, basename, msg) for comparison."""
    return {
        (op, os.path.basename(path), msg)
        for op, path, msg in summary["failed_files"]
    }


class TestSettingsImmutable(unittest.TestCase):
    """Settings are immutable for the duration of a run."""

    def test_settings_is_frozen(self):
        settings = Settings(jpeg_quality=80, keep_heic=True)
        with self.assertRaises(FrozenInstanceError):
            settings.jpeg_quality = 10

    def test_settings_defaults_match_pre_41_behavior(self):
        """Omitting settings entirely reproduces the old, unconfigurable defaults."""
        settings = Settings()
        self.assertEqual(settings.jpeg_quality, 95)
        self.assertFalse(settings.keep_heic)


class TestJpegQualityReachesConverter(unittest.TestCase):
    """--jpeg-quality (via Settings) reaches HeicConverter and changes output."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, quality: int) -> str:
        export_dir = os.path.join(self.temp_dir, f"export_{quality}")
        backup_dir = os.path.join(self.temp_dir, f"backup_{quality}")
        os.makedirs(export_dir)
        _noisy_heic(os.path.join(export_dir, "noisy.heic"))
        processor = FileProcessor(
            export_dir, backup_dir, settings=Settings(jpeg_quality=quality)
        )
        summary = processor.process_all_files(dry_run=False)
        self.assertEqual(summary["files_failed"], 0)
        photos_dir = os.path.join(backup_dir, "photos")
        [landed] = os.listdir(photos_dir)
        return os.path.join(photos_dir, landed)

    def test_low_quality_produces_a_measurably_smaller_file(self):
        low_path = self._run(quality=10)
        high_path = self._run(quality=95)

        low_size = os.path.getsize(low_path)
        high_size = os.path.getsize(high_path)

        self.assertLess(
            low_size,
            high_size,
            f"quality=10 ({low_size} bytes) was not smaller than "
            f"quality=95 ({high_size} bytes) -- --jpeg-quality is not "
            "reaching the encoder",
        )

    def test_constructor_default_still_matches_heic_converter_default(self):
        """Omitting jpeg_quality from Settings still yields the historical q95."""
        export_dir = os.path.join(self.temp_dir, "export_default")
        os.makedirs(export_dir)
        processor = FileProcessor(export_dir, os.path.join(self.temp_dir, "backup_default"))
        self.assertEqual(processor.heic_converter.jpeg_quality, 95)


class TestKeepHeicRetention(unittest.TestCase):
    """--keep-heic leaves the original in place but still files the JPEG."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_keep_heic_true_retains_original_and_files_jpeg(self):
        heic = make_exif_heic(
            os.path.join(self.export_dir, "IMG_0100.heic"),
            date_time_original="2024:02:02 08:00:00",
        )
        processor = FileProcessor(
            self.export_dir, self.backup_dir, settings=Settings(keep_heic=True)
        )

        summary = processor.process_all_files(dry_run=False)

        self.assertEqual(summary["files_failed"], 0)
        self.assertEqual(summary["files_processed"], 1)
        # The original survives...
        self.assertTrue(os.path.exists(heic), "original .heic was deleted despite --keep-heic")
        # ...and the converted JPEG landed in backup/ as usual.
        landing = os.path.join(self.backup_dir, "photos", "2024.02.02.08.00.00.jpg")
        self.assertTrue(os.path.isfile(landing))
        with Image.open(landing) as jpeg:
            self.assertEqual(jpeg.format, "JPEG")

    def test_keep_heic_false_default_still_deletes_original(self):
        """Regression guard: the default (unset) behavior is unchanged."""
        heic = make_exif_heic(
            os.path.join(self.export_dir, "IMG_0101.heic"),
            date_time_original="2024:02:02 08:01:00",
        )
        processor = FileProcessor(self.export_dir, self.backup_dir)

        summary = processor.process_all_files(dry_run=False)

        self.assertEqual(summary["files_failed"], 0)
        self.assertFalse(os.path.exists(heic))


class TestKeepHeicVerifyStillRuns(unittest.TestCase):
    """
    Retention gates ONLY the delete, never the issue #7 verify step.

    A conversion that reports success but produces an unverifiable JPEG must
    still be recorded as a failure -- with the original .heic surviving either
    way, since neither branch (verify failed) ever reaches the delete call.
    A regression that let ``keep_heic`` short-circuit verification (treating
    "keep the original" as "trust the conversion") would turn this loud
    failure into a silent success instead.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _bad_convert(self, heic_path, output_dir=None):
        """Write a zero-byte .jpg and report success, like the real #7 bug."""
        bad = Path(heic_path).with_suffix(".corrupt.jpg")
        bad.write_bytes(b"")
        return str(bad)

    def test_failed_verification_recorded_as_failure_with_keep_heic_true(self):
        heic = make_exif_heic(
            os.path.join(self.export_dir, "IMG_0200.heic"),
            date_time_original="2024:02:02 09:00:00",
        )
        original_bytes = Path(heic).read_bytes()
        processor = FileProcessor(
            self.export_dir, self.backup_dir, settings=Settings(keep_heic=True)
        )

        with mock.patch.object(
            processor.heic_converter,
            "convert_heic_to_jpeg",
            side_effect=self._bad_convert,
        ):
            summary = processor.process_all_files(dry_run=False)

        # The failure is recorded -- retention does not launder a genuinely
        # bad conversion into a quiet success.
        self.assertGreaterEqual(summary["files_failed"], 1)
        self.assertTrue(
            any(entry[1] == heic for entry in processor.failed_files),
            f"failure for {heic} not recorded in {processor.failed_files}",
        )
        # Nothing landed in backup/photos.
        photos_dir = os.path.join(self.backup_dir, "photos")
        landed = os.listdir(photos_dir) if os.path.isdir(photos_dir) else []
        self.assertEqual(landed, [])
        # The original survives untouched -- true both because retention was
        # requested AND because verification failing keeps it regardless.
        self.assertEqual(Path(heic).read_bytes(), original_bytes)

    def test_failed_verification_recorded_as_failure_with_keep_heic_false(self):
        """Same scenario with the default retention setting, for contrast."""
        heic = make_exif_heic(
            os.path.join(self.export_dir, "IMG_0201.heic"),
            date_time_original="2024:02:02 09:01:00",
        )
        processor = FileProcessor(self.export_dir, self.backup_dir)

        with mock.patch.object(
            processor.heic_converter,
            "convert_heic_to_jpeg",
            side_effect=self._bad_convert,
        ):
            summary = processor.process_all_files(dry_run=False)

        self.assertGreaterEqual(summary["files_failed"], 1)
        self.assertTrue(os.path.exists(heic))


class TestPooledVsSequentialWithOptions(unittest.TestCase):
    """jpeg_quality and keep_heic behave identically on both HEIC paths (#42)."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _build_export(self, export_dir: str) -> None:
        os.makedirs(export_dir, exist_ok=True)
        # Exceeds HEIC_PARALLEL_THRESHOLD (8) so the unforced run uses the pool.
        for index in range(10):
            make_exif_heic(
                os.path.join(export_dir, f"IMG_{index:04d}.heic"),
                date_time_original=f"2024:03:10 09:15:{index:02d}",
            )

    def _run(self, tag: str, force_sequential: bool, settings: Settings):
        export_dir = os.path.join(self.root, f"{tag}_export")
        backup_dir = os.path.join(self.root, f"{tag}_backup")
        self._build_export(export_dir)
        processor = FileProcessor(export_dir, backup_dir, settings=settings)
        if force_sequential:
            with mock.patch.object(FileProcessor, "HEIC_PARALLEL_THRESHOLD", 10 ** 9):
                summary = processor.process_all_files(dry_run=False)
        else:
            summary = processor.process_all_files(dry_run=False)
        return summary, export_dir, backup_dir

    def test_keep_heic_retains_originals_on_both_paths(self):
        settings = Settings(jpeg_quality=60, keep_heic=True)

        seq_summary, seq_export, seq_backup = self._run("seq", True, settings)
        par_summary, par_export, par_backup = self._run("par", False, settings)

        # Every original survives on both paths.
        self.assertEqual(len(os.listdir(seq_export)), 10)
        self.assertEqual(len(os.listdir(par_export)), 10)

        # Landings and failures agree between the two paths.
        self.assertEqual(_landing_map(seq_summary), _landing_map(par_summary))
        self.assertEqual(_failure_set(seq_summary), _failure_set(par_summary))
        self.assertEqual(seq_summary["files_processed"], par_summary["files_processed"])
        self.assertEqual(seq_summary["files_failed"], par_summary["files_failed"])

        # The same jpeg_quality was used for both paths' encode, so the
        # resulting JPEG sizes match file-for-file (same deterministic input).
        seq_sizes = sorted(
            os.path.getsize(os.path.join(seq_backup, "photos", name))
            for name in os.listdir(os.path.join(seq_backup, "photos"))
        )
        par_sizes = sorted(
            os.path.getsize(os.path.join(par_backup, "photos", name))
            for name in os.listdir(os.path.join(par_backup, "photos"))
        )
        self.assertEqual(seq_sizes, par_sizes)

    def test_default_keep_heic_false_still_deletes_on_both_paths(self):
        settings = Settings()  # keep_heic=False

        seq_summary, seq_export, _ = self._run("seqdel", True, settings)
        par_summary, par_export, _ = self._run("pardel", False, settings)

        self.assertEqual(os.listdir(seq_export), [])
        self.assertEqual(os.listdir(par_export), [])
        self.assertEqual(seq_summary["files_processed"], par_summary["files_processed"])


class TestCliOptions(unittest.TestCase):
    """End-to-end coverage of --jpeg-quality and --keep-heic through main()."""

    def setUp(self):
        self.runner = CliRunner()
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_help_documents_both_new_options(self):
        result = self.runner.invoke(main, ["--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("--jpeg-quality", result.output)
        self.assertIn("--keep-heic", result.output)

    def test_jpeg_quality_out_of_range_is_rejected(self):
        result = self.runner.invoke(
            main,
            [
                "--export-dir", self.export_dir,
                "--backup-dir", self.backup_dir,
                "--jpeg-quality", "101",
                "--yes",
            ],
        )
        self.assertNotEqual(result.exit_code, 0)

    def test_keep_heic_flag_leaves_original_after_a_real_run(self):
        heic = make_exif_heic(
            os.path.join(self.export_dir, "IMG_0300.heic"),
            date_time_original="2024:02:02 10:00:00",
        )

        result = self.runner.invoke(
            main,
            [
                "--export-dir", self.export_dir,
                "--backup-dir", self.backup_dir,
                "--keep-heic",
                "--yes",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(os.path.exists(heic), "original .heic missing after --keep-heic run")
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.backup_dir, "photos", "2024.02.02.10.00.00.jpg")
            )
        )

    def test_jpeg_quality_flag_changes_output_size_end_to_end(self):
        def _run_with_quality(quality: str) -> int:
            export_dir = os.path.join(self.temp_dir, f"export_q{quality}")
            backup_dir = os.path.join(self.temp_dir, f"backup_q{quality}")
            os.makedirs(export_dir)
            _noisy_heic(os.path.join(export_dir, "noisy.heic"))

            result = self.runner.invoke(
                main,
                [
                    "--export-dir", export_dir,
                    "--backup-dir", backup_dir,
                    "--jpeg-quality", quality,
                    "--yes",
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            photos_dir = os.path.join(backup_dir, "photos")
            [landed] = os.listdir(photos_dir)
            return os.path.getsize(os.path.join(photos_dir, landed))

        low_size = _run_with_quality("10")
        high_size = _run_with_quality("95")

        self.assertLess(low_size, high_size)


if __name__ == "__main__":
    unittest.main()
