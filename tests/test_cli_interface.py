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

    def test_unreadable_export_directory_is_reported_not_raised(self):
        """A PermissionError from iterdir() is reported, not left to escape.

        Issue #39: before this fix, ``any(export_path.iterdir())`` was the one
        call in ``check_directories`` with no guard of its own, so a directory
        that exists but cannot be listed raised straight out of this method,
        past the caller in ``main.py``, as a raw traceback. Fails against the
        old code: patching ``iterdir`` to raise lets the ``PermissionError``
        propagate out of ``check_directories()`` itself, so ``assertFalse``
        never runs -- the test errors on the exception instead of failing the
        assertion.
        """
        export, backup = self._dirs()
        os.makedirs(export)
        cli = _recording_cli(export, backup)

        with patch(
            "pathlib.Path.iterdir",
            side_effect=PermissionError(13, "Permission denied"),
        ):
            self.assertFalse(cli.check_directories())
        self.assertIn("Cannot read export directory", cli.console.export_text())

    def test_mkdir_bug_is_not_mislabeled_as_a_directory_error(self):
        """A non-OSError bug from mkdir propagates instead of being mislabeled.

        Issue #39: the old ``except Exception`` around ``backup_path.mkdir``
        additionally caught a ``TypeError``/``AttributeError`` from a
        malformed ``--backup-dir`` value and reported it as "cannot create
        backup directory", which is not what actually happened. Narrowed to
        ``OSError`` so a genuine bug surfaces with its real type instead.
        Fails against the old code: it swallows the ``TypeError`` and returns
        ``False``, so ``assertRaises`` never observes an exception.
        """
        export, backup = self._dirs()
        os.makedirs(export)
        open(os.path.join(export, "a.jpg"), "wb").close()
        cli = _recording_cli(export, backup)

        with patch("pathlib.Path.mkdir", side_effect=TypeError("not a real bug, a test one")):
            with self.assertRaises(TypeError):
                cli.check_directories()


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

    def test_generated_files_add_generated_row(self):
        """A batch containing AI/C2PA-provenance content renders a Generated
        row in the discovery table (issue #19) -- before this fix, the
        table had no Generated row at all, so a batch entirely of generated
        content silently vanished from the pre-run discovery display even
        though it categorizes correctly (``get_categorization_stats``
        already carried the count)."""
        from tests.fixtures import make_png_with_text

        generated = make_png_with_text(
            os.path.join(self.temp_dir, "gen.png"),
            {"c2pa": "manifest-stub"},
        )

        self.cli.display_file_scan_results([generated])

        text = self.cli.console.export_text()
        self.assertIn("Generated", text)
        self.assertIn("Total", text)

    def test_no_generated_files_omits_generated_row(self):
        """An all-photo batch renders no Generated row at all (conditional,
        matching the pre-existing Unknown-row style), not a permanent
        "Generated: 0" line."""
        photo = os.path.join(self.temp_dir, "photo.jpg")
        open(photo, "wb").close()

        self.cli.display_file_scan_results([photo])

        text = self.cli.console.export_text()
        self.assertNotIn("Generated", text)


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
        # A genuine (XML property list) sidecar (issue #57): content, not
        # just the extension, is what makes a candidate eligible for deletion.
        with open(sidecar, "wb") as handle:
            handle.write(b'<?xml version="1.0"?><plist version="1.0"><dict/></plist>')
        cli = _recording_cli(self.export, self.backup)

        with patch("src.cli_interface.Confirm.ask", return_value=True):
            result = cli.process_with_progress(dry_run=False)

        self.assertEqual(result.get("status"), "completed")
        self.assertFalse(os.path.exists(sidecar))  # sidecar deleted
        self.assertTrue(os.path.isdir(os.path.join(self.backup, "photos")))
        self.assertEqual(result.get("sidecars_deleted"), 1)


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

    def test_dry_run_title_renders_blue(self):
        """The dry-run title is styled bold blue (issue #49), not left the
        default table-title style. ANSI SGR 1;34 is bold+blue; export_text's
        styles=True is the only way to observe rich's applied style rather
        than just the title's text, which the previous, unwired
        ``title_style`` local left indistinguishable from every other run."""
        self.cli.display_results(
            {"status": "completed", "files_processed": 3}, dry_run=True
        )
        styled = self.cli.console.export_text(styles=True)
        self.assertIn("\x1b[1;34m", styled, "dry-run title was not rendered bold blue")

    def test_clean_success_title_renders_green(self):
        """A clean, unqualified-success run's title is styled bold green."""
        self.cli.display_results(
            {
                "status": "completed",
                "files_processed": 3,
                "files_failed": 0,
                "files_quarantined": 0,
                "categorization_stats": {
                    "photos": 3, "videos": 0, "screenshots": 0,
                    "generated": 0, "unknown": 0,
                },
            }
        )
        styled = self.cli.console.export_text(styles=True)
        self.assertIn("\x1b[1;32m", styled, "success title was not rendered bold green")

    def test_failure_title_renders_yellow_not_green(self):
        """A run with failures is styled bold yellow, distinct from a clean
        run's green -- this is the exact defect issue #49 reports: before the
        fix, ``title_style`` was computed but never passed to ``Table(...)``,
        so a failing run rendered in the identical style as a clean one."""
        self.cli.display_results(
            {
                "status": "completed",
                "files_processed": 1,
                "files_failed": 1,
                "files_quarantined": 0,
                "categorization_stats": {
                    "photos": 1, "videos": 0, "screenshots": 0,
                    "generated": 0, "unknown": 0,
                },
            }
        )
        styled = self.cli.console.export_text(styles=True)
        self.assertIn("\x1b[1;33m", styled, "failure title was not rendered bold yellow")
        self.assertNotIn("\x1b[1;32m", styled, "failure title rendered the success green")

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
            "missing_exif_list": [
                {"original_path": "export/x.jpg", "final_path": "/tmp/x.jpg"}
            ],
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

    def test_failure_carrying_an_exception_shows_its_type(self):
        """An exception-object error cell names the exception's type (issue #39).

        ``FileProcessor`` now stores the caught exception object itself,
        rather than ``str(exc)``, so the type is not discarded before it
        reaches the Error column -- ``str(exc)`` alone (e.g. "[Errno 2] No
        such file or directory: '...'") gives no indication of which
        exception class produced it.

        Fails against the old ``display_failures``: it renders
        ``safe_markup(error)``, i.e. ``str(error)``, with no type name, so
        "FileNotFoundError" never appears in the output.
        """
        self.cli.display_failures(
            [("move_file", "/tmp/a.jpg", FileNotFoundError(2, "No such file or directory"))]
        )
        text = self.cli.console.export_text()
        self.assertIn("FileNotFoundError", text)
        self.assertIn("No such file or directory", text)

    def test_failure_carrying_a_plain_string_renders_unchanged(self):
        """A non-exception error string still renders exactly as before.

        Regression coverage: a descriptive string that was never a caught
        exception (e.g. "HEIC conversion failed") must not grow a spurious
        type prefix.
        """
        self.cli.display_failures(
            [("convert_heic", "/tmp/a.heic", "HEIC conversion failed")]
        )
        text = self.cli.console.export_text()
        self.assertIn("HEIC conversion failed", text)
        self.assertNotIn("str:", text)


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
        self.cli.display_missing_exif_warning([
            {"original_path": "export/a.jpg", "final_path": "/tmp/a.jpg"},
            {"original_path": "export/b.jpg", "final_path": "/tmp/b.jpg"},
        ])
        text = self.cli.console.export_text()
        self.assertIn("Missing EXIF Data Warning", text)
        self.assertIn("a.jpg", text)
        self.assertNotIn("more", text)

    def test_long_list_truncates_with_more_notice(self):
        """More than five files renders the '...and N more' truncation line."""
        files = [
            {"original_path": f"export/file{i}.jpg", "final_path": f"/tmp/file{i}.jpg"}
            for i in range(8)
        ]
        self.cli.display_missing_exif_warning(files)
        text = self.cli.console.export_text()
        self.assertIn("and 3 more", text)


if __name__ == "__main__":
    unittest.main()
