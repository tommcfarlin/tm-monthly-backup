"""
Test suite for the rich CLI rendering and directory-check logic.

Rendering tests capture output through a recording ``rich.Console`` and assert
both that the render does not raise and that the key content is present. The
directory-check tests exercise the overlap rejection, the missing-export and
unwritable-backup branches, and the empty-export prompt (accepted / declined).
"""

import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from rich.console import Console

from src.cli_interface import CLIInterface
from src.file_categorizer import FileCategory


def _recording_cli(export_dir, backup_dir):
    """Return a CLIInterface whose console records to an in-memory buffer."""
    cli = CLIInterface(export_dir, backup_dir)
    cli.console = Console(file=io.StringIO(), record=True, width=120)
    return cli


class TestCheckDirectories(unittest.TestCase):
    """check_directories: overlap, missing export, empty export, backup fail."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _dirs(self, export="export", backup="backup"):
        return (
            os.path.join(self.temp_dir, export),
            os.path.join(self.temp_dir, backup),
        )

    def test_overlap_is_rejected(self):
        """Export and backup pointing at the same directory is rejected."""
        same = os.path.join(self.temp_dir, "shared")
        os.makedirs(same)
        cli = _recording_cli(same, same)

        self.assertFalse(cli.check_directories())
        self.assertIn("Error", cli.console.export_text())

    def test_missing_export_directory_fails(self):
        """A nonexistent export directory is reported and rejected."""
        export, backup = self._dirs()
        cli = _recording_cli(export, backup)

        self.assertFalse(cli.check_directories())
        self.assertIn("does not exist", cli.console.export_text())

    def test_empty_export_declined_returns_false(self):
        """Declining the empty-export prompt aborts."""
        export, backup = self._dirs()
        os.makedirs(export)
        cli = _recording_cli(export, backup)

        with patch("src.cli_interface.Confirm.ask", return_value=False):
            self.assertFalse(cli.check_directories())
        self.assertIn("empty", cli.console.export_text())

    def test_empty_export_accepted_creates_backup(self):
        """Accepting the empty-export prompt proceeds and creates backup/."""
        export, backup = self._dirs()
        os.makedirs(export)
        cli = _recording_cli(export, backup)

        with patch("src.cli_interface.Confirm.ask", return_value=True):
            self.assertTrue(cli.check_directories())
        self.assertTrue(os.path.isdir(backup))

    def test_populated_export_and_new_backup_succeeds(self):
        """A non-empty export with a creatable backup dir is ready."""
        export, backup = self._dirs()
        os.makedirs(export)
        open(os.path.join(export, "a.jpg"), "wb").close()
        cli = _recording_cli(export, backup)

        self.assertTrue(cli.check_directories())
        self.assertIn("Backup directory ready", cli.console.export_text())

    def test_unwritable_backup_directory_fails(self):
        """When backup/ cannot be created, the check reports and rejects."""
        export, _ = self._dirs()
        os.makedirs(export)
        open(os.path.join(export, "a.jpg"), "wb").close()
        # Point backup at a path whose parent is a regular file: mkdir raises.
        blocker = os.path.join(self.temp_dir, "afile")
        with open(blocker, "wb") as handle:
            handle.write(b"x")
        backup = os.path.join(blocker, "backup")
        cli = _recording_cli(export, backup)

        self.assertFalse(cli.check_directories())
        self.assertIn("Cannot create backup directory", cli.console.export_text())


class TestDisplayFileScanResults(unittest.TestCase):
    """display_file_scan_results: empty and unknown-row branches."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cli = _recording_cli(
            os.path.join(self.temp_dir, "export"),
            os.path.join(self.temp_dir, "backup"),
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_empty_file_list_prints_nothing_to_process(self):
        """An empty file list renders the no-files message and returns."""
        self.cli.display_file_scan_results([])
        self.assertIn("No files found", self.cli.console.export_text())

    def test_unknown_files_add_unknown_row(self):
        """A batch containing an unrecognized file renders the Unknown row."""
        # The categorizer inspects real files, so write them to disk. A .xyz
        # file categorizes as UNKNOWN, driving the unknown-row branch.
        photo = os.path.join(self.temp_dir, "photo.jpg")
        mystery = os.path.join(self.temp_dir, "mystery.xyz")
        open(photo, "wb").close()
        open(mystery, "wb").close()

        self.cli.display_file_scan_results([photo, mystery])

        text = self.cli.console.export_text()
        self.assertIn("Unknown", text)
        self.assertIn("Total", text)


class TestProcessWithProgress(unittest.TestCase):
    """process_with_progress: the no-files short-circuit and a sidecar run."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_no_files_returns_no_files_status(self):
        """An empty export yields a 'no_files'-tagged summary and a notice."""
        cli = _recording_cli(self.export, self.backup)

        result = cli.process_with_progress(dry_run=True)

        self.assertEqual(result.get("status"), "no_files")
        self.assertIn("No files found", cli.console.export_text())

    def test_confirmed_run_deletes_sidecar_and_processes_photo(self):
        """A confirmed run drains a sidecar and files a photo, reporting done."""
        from tests.fixtures import make_exif_jpeg

        make_exif_jpeg(
            os.path.join(self.export, "pic.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        sidecar = os.path.join(self.export, "pic.aae")
        open(sidecar, "wb").close()
        cli = _recording_cli(self.export, self.backup)

        with patch("src.cli_interface.Confirm.ask", return_value=True):
            result = cli.process_with_progress(dry_run=False)

        self.assertEqual(result.get("status"), "completed")
        self.assertFalse(os.path.exists(sidecar))  # sidecar deleted
        self.assertTrue(os.path.isdir(os.path.join(self.backup, "photos")))


class TestDisplayResults(unittest.TestCase):
    """display_results: dry-run, quarantine, failure, and category branches."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cli = _recording_cli(
            os.path.join(self.temp_dir, "export"),
            os.path.join(self.temp_dir, "backup"),
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_empty_or_cancelled_results_render_nothing(self):
        """A cancelled result short-circuits without rendering a table."""
        self.cli.display_results({"status": "cancelled"})
        self.assertEqual(self.cli.console.export_text().strip(), "")

    def test_dry_run_title(self):
        """A dry run renders the dry-run titled results table."""
        self.cli.display_results(
            {"status": "completed", "files_processed": 3}, dry_run=True
        )
        self.assertIn("Dry Run Results", self.cli.console.export_text())

    def test_quarantine_title_and_rows(self):
        """Quarantined-but-no-failure renders the quarantine banner and rows."""
        results = {
            "status": "completed",
            "files_processed": 2,
            "files_failed": 0,
            "files_quarantined": 1,
            "categorization_stats": {
                "photos": 2,
                "videos": 0,
                "screenshots": 0,
                "generated": 0,
                "unknown": 0,
            },
        }
        self.cli.display_results(results)
        text = self.cli.console.export_text()
        # The title wraps across lines in a narrow table; assert on its words
        # plus the quarantine metric row and destination.
        self.assertIn("Quarantined", text)
        self.assertIn("undecodable", text)
        self.assertIn("backup/corrupt/", text)

    def test_failure_and_conversion_failure_and_category_rows(self):
        """Failures, HEIC failures, generated/unknown categories all render."""
        results = {
            "status": "completed",
            "files_processed": 1,
            "files_failed": 1,
            "files_quarantined": 0,
            "heic_conversions": 0,
            "heic_conversion_failures": 2,
            "missing_exif_files": 1,
            "failed_files": [("move_file", "/tmp/x.jpg", "disk full")],
            "missing_exif_list": ["/tmp/x.jpg"],
            "categorization_stats": {
                "photos": 1,
                "videos": 0,
                "screenshots": 0,
                "generated": 3,
                "unknown": 2,
            },
        }
        self.cli.display_results(results)
        text = self.cli.console.export_text()
        self.assertIn("With Errors", text)
        self.assertIn("HEIC Conversion Failures", text)
        self.assertIn("Generated", text)
        self.assertIn("Unknown", text)
        self.assertIn("disk full", text)


class TestDisplayFailures(unittest.TestCase):
    """display_failures: empty short-circuit and populated table."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cli = _recording_cli(
            os.path.join(self.temp_dir, "export"),
            os.path.join(self.temp_dir, "backup"),
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_empty_list_renders_nothing(self):
        """No failures renders no output."""
        self.cli.display_failures([])
        self.assertEqual(self.cli.console.export_text().strip(), "")

    def test_failures_render_operation_file_and_error(self):
        """Each failure tuple renders its operation, file, and error."""
        self.cli.display_failures(
            [("convert_heic", "/tmp/a.heic", "verification failed")]
        )
        text = self.cli.console.export_text()
        self.assertIn("convert_heic", text)
        self.assertIn("a.heic", text)
        self.assertIn("verification failed", text)


class TestDisplayMissingExifWarning(unittest.TestCase):
    """display_missing_exif_warning: short list and the '...and N more' branch."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cli = _recording_cli(
            os.path.join(self.temp_dir, "export"),
            os.path.join(self.temp_dir, "backup"),
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_short_list_lists_each_file(self):
        """A short list renders each file and no truncation notice."""
        self.cli.display_missing_exif_warning(["/tmp/a.jpg", "/tmp/b.jpg"])
        text = self.cli.console.export_text()
        self.assertIn("Missing EXIF Data Warning", text)
        self.assertIn("a.jpg", text)
        self.assertNotIn("more", text)

    def test_long_list_truncates_with_more_notice(self):
        """More than five files renders the '...and N more' truncation line."""
        files = [f"/tmp/file{i}.jpg" for i in range(8)]
        self.cli.display_missing_exif_warning(files)
        text = self.cli.console.export_text()
        self.assertIn("and 3 more", text)


if __name__ == "__main__":
    unittest.main()
