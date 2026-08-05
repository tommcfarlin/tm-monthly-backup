"""
Failure-accounting and exit-code taxonomy tests (issue #31).

These tests pin two related contracts:

* Every processable input file lands in exactly one bucket -- processed or
  failed -- so a run that leaves a file behind can never report success. The
  motivating bug: when ``HeicConverter.convert_heic_to_jpeg`` returned ``None``,
  ``_process_single_file`` returned without recording anything, so an
  unconvertible ``.heic`` was silently dropped while the summary said "Success!"
  and the process exited ``0``.
* The exit codes ``0`` / ``1`` / ``2`` / ``130`` each carry exactly one meaning,
  and a cancelled run is distinguishable from a clean success (it no longer
  signals itself with an empty dict).

All filesystem I/O happens under ``tempfile.mkdtemp`` -- the tests never touch a
real ``export/`` or ``backup/``.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from src.file_processor import FileProcessor
from src.cli_interface import CLIInterface
from src.main import (
    main,
    determine_exit_code,
    EXIT_SUCCESS,
    EXIT_PARTIAL_FAILURE,
    EXIT_PRECONDITION,
    EXIT_CANCELLED,
)
from tests.fixtures import make_exif_heic, make_exif_jpeg


class TestFailureAccounting(unittest.TestCase):
    """Every processable file must be counted as processed or failed."""

    def setUp(self):
        """Create isolated temp export/backup directories for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        """Remove the temporary tree."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unconvertible_heic_is_recorded_as_failure(self):
        """A HEIC whose conversion returns ``None`` is counted, not dropped.

        This is the core bug from issue #31: the returns-``None`` branch of
        ``_process_single_file`` must append to ``failed_files`` so the file is
        reflected in ``files_failed`` and the exit code, instead of vanishing
        while the banner reads "Success!".
        """
        make_exif_heic(
            os.path.join(self.export_dir, "broken.heic"),
            date_time_original="2024:03:01 09:08:07",
        )

        # Force the conversion to report failure the way a truncated write or a
        # decode error would surface it to ``_process_single_file``.
        with patch.object(
            self.processor.heic_converter,
            "convert_heic_to_jpeg",
            return_value=None,
        ):
            results = self.processor.process_all_files(dry_run=False)

        # The failure is recorded with the right operation and path.
        self.assertEqual(results["files_failed"], 1)
        self.assertEqual(len(results["failed_files"]), 1)
        operation, failed_path, _message = results["failed_files"][0]
        self.assertEqual(operation, "convert_heic")
        self.assertTrue(
            failed_path.endswith("broken.heic"),
            f"failure tuple named the wrong file: {failed_path}",
        )

        # The file was not silently counted as processed, and the run does not
        # qualify as a clean success.
        self.assertEqual(results["files_processed"], 0)
        self.assertEqual(
            determine_exit_code({**results, "status": "completed"}),
            EXIT_PARTIAL_FAILURE,
        )

    def test_summary_title_is_not_success_when_a_file_failed(self):
        """The results banner must not read "Success!" when work failed."""
        make_exif_heic(
            os.path.join(self.export_dir, "broken.heic"),
            date_time_original="2024:03:01 09:08:07",
        )

        with patch.object(
            self.processor.heic_converter,
            "convert_heic_to_jpeg",
            return_value=None,
        ):
            results = self.processor.process_all_files(dry_run=False)

        cli = CLIInterface(self.export_dir, self.backup_dir)
        with cli.console.capture() as capture:
            cli.display_results({**results, "status": "completed"}, dry_run=False)
        rendered = capture.get()

        self.assertNotIn("Success!", rendered)
        self.assertIn("With Errors", rendered)

    def test_processed_plus_failed_equals_discovered(self):
        """Invariant: files_processed + files_failed == processable files seen.

        Every discovered processable file must land in exactly one bucket. This
        assertion is what would have caught the silently-dropped HEIC.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "good_a.jpg"),
            date_time_original="2024:01:01 01:01:01",
        )
        make_exif_jpeg(
            os.path.join(self.export_dir, "good_b.jpg"),
            date_time_original="2024:01:02 02:02:02",
        )
        make_exif_heic(
            os.path.join(self.export_dir, "broken.heic"),
            date_time_original="2024:01:03 03:03:03",
        )

        with patch.object(
            self.processor.heic_converter,
            "convert_heic_to_jpeg",
            return_value=None,
        ):
            results = self.processor.process_all_files(dry_run=False)

        processable = self.processor.categorizer.get_processable_files()
        files_seen = sum(len(paths) for paths in processable.values())

        self.assertEqual(files_seen, 3)
        self.assertEqual(
            results["files_processed"] + results["files_failed"],
            files_seen,
            "every processable file must be either processed or failed",
        )
        self.assertEqual(results["files_failed"], 1)
        self.assertEqual(results["files_processed"], 2)


class TestExitCodeTaxonomy(unittest.TestCase):
    """The 0/1/2/130 exit codes each carry exactly one meaning."""

    def setUp(self):
        """Create isolated temp export/backup directories for each test."""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        """Remove the temporary tree."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clean_batch_maps_to_success(self):
        """A batch with zero failures decides exit code 0."""
        make_exif_jpeg(
            os.path.join(self.export_dir, "good.jpg"),
            date_time_original="2024:05:05 05:05:05",
        )
        make_exif_heic(
            os.path.join(self.export_dir, "photo.heic"),
            date_time_original="2024:05:06 06:06:06",
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_failed"], 0)
        self.assertGreaterEqual(results["files_processed"], 1)
        self.assertEqual(
            determine_exit_code({**results, "status": "completed"}),
            EXIT_SUCCESS,
        )

    def test_partial_failure_maps_to_code_one(self):
        """A completed run with a per-file failure decides exit code 1."""
        self.assertEqual(
            determine_exit_code({"status": "completed", "files_failed": 2}),
            EXIT_PARTIAL_FAILURE,
        )

    def test_cancelled_run_is_distinct_from_success(self):
        """Declining the prompt yields a tagged result and code 130, not 0."""
        make_exif_jpeg(
            os.path.join(self.export_dir, "good.jpg"),
            date_time_original="2024:07:07 07:07:07",
        )
        cli = CLIInterface(self.export_dir, self.backup_dir)

        with patch("src.cli_interface.Confirm.ask", return_value=False):
            results = cli.process_with_progress(dry_run=False)

        # No longer an empty dict -- cancellation is signalled explicitly.
        self.assertEqual(results, {"status": "cancelled"})
        self.assertEqual(determine_exit_code(results), EXIT_CANCELLED)
        self.assertNotEqual(EXIT_CANCELLED, EXIT_SUCCESS)

    def test_all_four_codes_are_distinct(self):
        """The taxonomy assigns four different integers."""
        codes = {
            EXIT_SUCCESS,
            EXIT_PARTIAL_FAILURE,
            EXIT_PRECONDITION,
            EXIT_CANCELLED,
        }
        self.assertEqual(len(codes), 4)

    def test_missing_export_dir_exits_precondition(self):
        """A missing export directory is a precondition failure (code 2).

        ``--yes`` is required so this reaches ``check_directories`` at all --
        without it, the issue #33 non-interactive gate short-circuits with its
        own exit ``2`` before the directory is ever inspected, which would
        make this test pass for the wrong reason (it would keep passing even
        if the missing-directory check were deleted outright). The
        ``assertIn`` discriminates the two: only ``check_directories``'s
        failure path prints "Cannot proceed".
        """
        runner = CliRunner()
        missing = os.path.join(self.temp_dir, "does_not_exist")

        result = runner.invoke(
            main,
            ["--export-dir", missing, "--backup-dir", self.backup_dir, "--yes"],
        )

        self.assertEqual(result.exit_code, EXIT_PRECONDITION)
        self.assertIn("Cannot proceed", result.output)

    def test_overlapping_dirs_exit_precondition(self):
        """An export/backup overlap (#52) is a precondition failure (code 2).

        Same rationale as the missing-export-dir test above: ``--yes`` is
        required so the run reaches the #52 overlap check inside
        ``check_directories`` rather than being turned away earlier by the
        issue #33 non-interactive gate, and the ``assertIn`` pins that this
        specific failure -- not the gate's -- produced the exit code.
        """
        runner = CliRunner()

        result = runner.invoke(
            main,
            [
                "--export-dir", self.export_dir,
                "--backup-dir", self.export_dir,
                "--yes",
            ],
        )

        self.assertEqual(result.exit_code, EXIT_PRECONDITION)
        self.assertIn("Cannot proceed", result.output)

    def test_clean_run_exits_zero_end_to_end(self):
        """A real, --yes-confirmed run over a good file exits 0 through
        ``main`` -- the full pipeline, with no ``Confirm.ask`` patch anywhere
        (issue #33's non-interactive path)."""
        make_exif_jpeg(
            os.path.join(self.export_dir, "good.jpg"),
            date_time_original="2024:08:08 08:08:08",
        )
        runner = CliRunner()

        result = runner.invoke(
            main,
            [
                "--export-dir", self.export_dir,
                "--backup-dir", self.backup_dir,
                "--yes",
            ],
        )

        self.assertEqual(result.exit_code, EXIT_SUCCESS)

    def test_partial_failure_exits_one_end_to_end(self):
        """A confirmed run with one unconvertible HEIC exits 1 through ``main``.

        The three end-to-end cases together prove the codes are distinct: a
        missing dir exits 2, this partial failure exits 1, and a clean run
        exits 0.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "good.jpg"),
            date_time_original="2024:09:09 09:09:09",
        )
        make_exif_heic(
            os.path.join(self.export_dir, "broken.heic"),
            date_time_original="2024:09:10 10:10:10",
        )
        runner = CliRunner()

        with patch(
            "src.heic_converter.HeicConverter.convert_heic_to_jpeg",
            return_value=None,
        ):
            result = runner.invoke(
                main,
                [
                    "--export-dir", self.export_dir,
                    "--backup-dir", self.backup_dir,
                    "--yes",
                ],
            )

        self.assertEqual(result.exit_code, EXIT_PARTIAL_FAILURE)
        self.assertNotIn("Success!", result.output)


if __name__ == "__main__":
    unittest.main()
