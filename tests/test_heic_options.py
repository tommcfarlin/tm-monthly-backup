"""
Tests for issue #41: expose JPEG quality as a real option.

Before this issue, `HeicConverter.__init__` accepted `jpeg_quality` (and,
since issue #40, `optimize`) but `FileProcessor` always constructed
`HeicConverter()` with no arguments -- there was no path from `main.py` down
to that parameter.

Issue #41 originally also added a `--keep-heic` retention flag that left a
converted HEIC's original in `export/` instead of deleting it. Issue #65
found that a retained original is invisible to this tool as "already
archived": the next run rescans it, re-converts it, and files a duplicate
copy under a bumped, non-capture timestamp. The project decided the flag
should be removed entirely rather than patched -- convert-then-delete is the
tool's intended design -- so `--keep-heic`/`Settings.keep_heic` no longer
exist and the tests that existed only to exercise retention are gone. The
original .heic being deleted after a verified conversion is once again
unconditional, exactly as it was before issue #41.

These tests prove, with real HEIC bytes end to end (through `FileProcessor`
and, for the CLI-level tests, through `src.main.main` via `CliRunner`):

* `--jpeg-quality` reaches `HeicConverter` and measurably changes the
  converted JPEG's size.
* The original `.heic` is deleted once its conversion is filed in `backup/`.
* The issue #7 verify-before-delete gate still runs unconditionally: a
  conversion that fails verification is recorded as a failure and the
  original survives. This is the safety-critical case -- anything that let a
  short-circuited verification through would turn a loud failure into a
  silent one.
* `Settings` is frozen (immutable for the duration of a run).
* `jpeg_quality` behaves identically on the sequential and pooled
  (issue #42) HEIC conversion paths, both of which delete verified-converted
  originals identically.
"""

import os
import shutil
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Dict
from unittest import mock

import click
from click.testing import CliRunner
from PIL import Image

from src.file_processor import FileProcessor, Settings
from src.heic_converter import HeicConverter
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
        settings = Settings(jpeg_quality=80)
        with self.assertRaises(FrozenInstanceError):
            settings.jpeg_quality = 10

    def test_settings_defaults(self):
        """Omitting settings entirely yields the documented defaults.

        The quality default was deliberately raised from 95 to 98: the HEIC
        original is deleted after a verified conversion, so the archived JPEG
        is the only surviving copy, and 98 roughly halves the per-channel
        quantization error (0.420 -> 0.238 of 255, measured over real HEIC
        exports) for ~1.34x the bytes. Going on to 100 buys less than half
        that improvement again for ~1.96x, so 98 is the knee of the curve.
        """
        settings = Settings()
        self.assertEqual(settings.jpeg_quality, 98)

    def test_settings_default_agrees_with_heic_converter_default(self):
        """The two independent default literals must not drift apart.

        ``Settings.jpeg_quality`` and ``HeicConverter.__init__``'s own default
        are separate literals -- collapsing them needs ``Settings`` moved to a
        neutral module to avoid a circular import (issue #67). Until then this
        assertion is what makes a drift between them visible: a caller that
        builds a bare ``HeicConverter()`` would otherwise silently encode at a
        different quality than a caller going through ``Settings``.
        """
        self.assertEqual(HeicConverter().jpeg_quality, Settings().jpeg_quality)


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

    def test_constructor_default_reaches_the_converter(self):
        """Omitting jpeg_quality from Settings still reaches HeicConverter."""
        export_dir = os.path.join(self.temp_dir, "export_default")
        os.makedirs(export_dir)
        processor = FileProcessor(export_dir, os.path.join(self.temp_dir, "backup_default"))
        self.assertEqual(processor.heic_converter.jpeg_quality, 98)


class TestHeicOriginalIsDeletedAfterConversion(unittest.TestCase):
    """The original .heic is deleted once its conversion is filed in backup/."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_original_deleted_after_verified_conversion(self):
        heic = make_exif_heic(
            os.path.join(self.export_dir, "IMG_0101.heic"),
            date_time_original="2024:02:02 08:01:00",
        )
        processor = FileProcessor(self.export_dir, self.backup_dir)

        summary = processor.process_all_files(dry_run=False)

        self.assertEqual(summary["files_failed"], 0)
        self.assertEqual(summary["files_processed"], 1)
        # The original is gone...
        self.assertFalse(os.path.exists(heic))
        # ...and the converted JPEG landed in backup/ instead.
        landing = os.path.join(self.backup_dir, "photos", "2024.02.02.08.01.00.jpg")
        self.assertTrue(os.path.isfile(landing))
        with Image.open(landing) as jpeg:
            self.assertEqual(jpeg.format, "JPEG")


class TestVerifyBeforeDelete(unittest.TestCase):
    """
    The issue #7 verify-before-delete gate still runs unconditionally.

    A conversion that reports success but produces an unverifiable JPEG must
    be recorded as a failure, with the original .heic surviving, because the
    delete call is only ever reached after a successful verification. This
    test injects the real #7 bug shape (a converter that writes a zero-byte
    JPEG and reports success) directly against the default path -- the only
    path that exists now that issue #65 removed the `--keep-heic` retention
    flag this test originally ran under two settings (`keep_heic=True` and
    `keep_heic=False`) to prove retention could not short-circuit
    verification. That flag is gone, but the underlying guarantee -- a
    failed verification is a loud, recorded failure, never a silent one --
    outlives it, so this test is retargeted rather than deleted.
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

    def test_failed_verification_recorded_as_failure(self):
        heic = make_exif_heic(
            os.path.join(self.export_dir, "IMG_0200.heic"),
            date_time_original="2024:02:02 09:00:00",
        )
        original_bytes = Path(heic).read_bytes()
        processor = FileProcessor(self.export_dir, self.backup_dir)

        with mock.patch.object(
            processor.heic_converter,
            "convert_heic_to_jpeg",
            side_effect=self._bad_convert,
        ):
            summary = processor.process_all_files(dry_run=False)

        # The failure is recorded -- a genuinely bad conversion never becomes
        # a quiet success.
        self.assertGreaterEqual(summary["files_failed"], 1)
        self.assertTrue(
            any(entry[1] == heic for entry in processor._failed_files),
            f"failure for {heic} not recorded in {processor._failed_files}",
        )
        # Nothing landed in backup/photos.
        photos_dir = os.path.join(self.backup_dir, "photos")
        landed = os.listdir(photos_dir) if os.path.isdir(photos_dir) else []
        self.assertEqual(landed, [])
        # The original survives untouched.
        self.assertEqual(Path(heic).read_bytes(), original_bytes)


class TestPooledVsSequentialWithOptions(unittest.TestCase):
    """jpeg_quality behaves identically on both HEIC paths (#42), and both
    paths delete verified-converted originals identically."""

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
        """
        Run one batch and report which HEIC conversion path actually executed.

        Spies on ``_convert_heic_files_parallel`` (via ``autospec`` +
        ``side_effect=`` the real, unbound method, so the run's behavior is
        completely unchanged) rather than trusting the batch size alone: 10
        files clears ``HEIC_PARALLEL_THRESHOLD`` (8) *today*, but a test that
        only ever inspects the resulting summary would stay green even if that
        threshold were later raised above 10 -- both "paths" would silently
        become sequential and the equivalence assertions would prove nothing
        about the pool at all.

        Returns:
            ``(summary, export_dir, backup_dir, pool_was_used)``.
        """
        export_dir = os.path.join(self.root, f"{tag}_export")
        backup_dir = os.path.join(self.root, f"{tag}_backup")
        self._build_export(export_dir)
        processor = FileProcessor(export_dir, backup_dir, settings=settings)
        original_parallel = FileProcessor._convert_heic_files_parallel
        with mock.patch.object(
            FileProcessor, "_convert_heic_files_parallel", autospec=True
        ) as spy:
            spy.side_effect = original_parallel
            if force_sequential:
                with mock.patch.object(FileProcessor, "HEIC_PARALLEL_THRESHOLD", 10 ** 9):
                    summary = processor.process_all_files(dry_run=False)
            else:
                summary = processor.process_all_files(dry_run=False)
            pool_was_used = spy.called
        return summary, export_dir, backup_dir, pool_was_used

    def test_custom_jpeg_quality_matches_on_both_paths(self):
        settings = Settings(jpeg_quality=60)

        seq_summary, seq_export, seq_backup, seq_pool_used = self._run("seq", True, settings)
        par_summary, par_export, par_backup, par_pool_used = self._run("par", False, settings)

        # Confirm the two runs actually exercised different code paths --
        # otherwise every equivalence assertion below would be comparing a
        # run against itself.
        self.assertFalse(seq_pool_used, "forced-sequential run unexpectedly used the pool")
        self.assertTrue(par_pool_used, "unforced 10-file run should have used the pool")

        # Every original is deleted on both paths (verified conversion).
        self.assertEqual(os.listdir(seq_export), [])
        self.assertEqual(os.listdir(par_export), [])

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

    def test_default_settings_delete_originals_on_both_paths(self):
        settings = Settings()

        seq_summary, seq_export, _, seq_pool_used = self._run("seqdel", True, settings)
        par_summary, par_export, _, par_pool_used = self._run("pardel", False, settings)

        self.assertFalse(seq_pool_used)
        self.assertTrue(par_pool_used)
        self.assertEqual(os.listdir(seq_export), [])
        self.assertEqual(os.listdir(par_export), [])
        self.assertEqual(seq_summary["files_processed"], par_summary["files_processed"])


class TestCliOptions(unittest.TestCase):
    """End-to-end coverage of --jpeg-quality through main()."""

    def setUp(self):
        self.runner = CliRunner()
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_help_documents_jpeg_quality_and_omits_removed_keep_heic(self):
        result = self.runner.invoke(main, ["--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("--jpeg-quality", result.output)
        # --keep-heic was removed entirely (issue #65); pin its absence so a
        # future re-add is a deliberate, visible decision rather than a
        # silent regression.
        self.assertNotIn("--keep-heic", result.output)

    def _invoke_with_quality(self, value: str):
        return self.runner.invoke(
            main,
            [
                "--export-dir", self.export_dir,
                "--backup-dir", self.backup_dir,
                "--jpeg-quality", value,
                "--yes",
            ],
        )

    def test_jpeg_quality_above_range_is_rejected_with_clear_message(self):
        result = self._invoke_with_quality("101")

        # click.UsageError.exit_code is the documented exit code for a
        # command-line parsing failure -- distinct from this tool's own
        # EXIT_PRECONDITION (2, coincidentally the same value, but decided by
        # click before main()'s body ever runs, not by this tool's taxonomy).
        self.assertEqual(result.exit_code, click.UsageError.exit_code)
        self.assertIn("--jpeg-quality", result.output)
        # The message must actually name the valid range, not just reject.
        self.assertIn("1<=x<=100", result.output)

    def test_jpeg_quality_below_range_is_rejected(self):
        result = self._invoke_with_quality("0")

        self.assertEqual(result.exit_code, click.UsageError.exit_code)
        self.assertIn("--jpeg-quality", result.output)
        self.assertIn("1<=x<=100", result.output)

    def test_jpeg_quality_non_integer_is_rejected(self):
        result = self._invoke_with_quality("abc")

        self.assertEqual(result.exit_code, click.UsageError.exit_code)
        self.assertIn("--jpeg-quality", result.output)
        self.assertIn("not a valid integer", result.output)

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
            # Issue #65 removed --keep-heic, so a real run through main() must
            # drain export/ completely. This is the only CLI-level assertion
            # that the original is deleted rather than retained; the processor
            # layer pins it directly, but a regression that reintroduced
            # retention at the CLI would otherwise pass every test here.
            self.assertEqual(
                os.listdir(export_dir), [],
                f"export/ was not drained by a real run: {os.listdir(export_dir)}",
            )
            photos_dir = os.path.join(backup_dir, "photos")
            [landed] = os.listdir(photos_dir)
            return os.path.getsize(os.path.join(photos_dir, landed))

        low_size = _run_with_quality("10")
        high_size = _run_with_quality("95")

        self.assertLess(low_size, high_size)


if __name__ == "__main__":
    unittest.main()
