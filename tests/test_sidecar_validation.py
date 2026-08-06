"""
Sidecar content-validation and deferred-deletion tests (issue #57).

Before this issue, ``FileProcessor`` deleted any file whose extension was
``.aae``/``.AAE`` unconditionally, with no content check, and did so as the
very FIRST thing ``process_all_files`` did -- before a single photo was safely
in ``backup/``. A file that merely happened to share the extension (a
23-byte ``tax-return-2024.AAE`` in the issue's own reproduction) was destroyed
on the first run, and nothing in the results summary mentioned it.

These tests pin the fix end to end, with no mocking of ``os.remove`` unless a
test is specifically about a delete failure:

* A candidate is deleted only when its content validates as a plist (XML or
  binary); a non-plist candidate, and one that cannot even be read, is kept
  and reported as skipped -- never deleted on the strength of its extension
  alone.
* Deletion happens strictly after every processable file has been filed or
  recorded as failed, not before.
* The results summary reports the number of sidecars deleted, identically for
  a dry run and a real run over the same input (issue #10 parity).
* A run that never reaches the per-file processing loop at all (a directory-
  creation failure, standing in for "fails to process any photo") leaves
  every ``.aae`` candidate on disk, because the deferred delete call is never
  reached.

All filesystem I/O happens under ``tempfile.mkdtemp`` -- never against this
repo's real ``export/`` or ``backup/``.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.file_processor import FileProcessor
from src.cli_interface import CLIInterface
from tests.fixtures import make_exif_jpeg

# Genuine Apple sidecar content shapes (issue #57's SIDECAR_MAGIC prefixes).
XML_PLIST = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
    b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    b'<plist version="1.0"><dict><key>adjustmentBaseVersion</key>'
    b'<integer>1</integer></dict></plist>'
)
# Only the 8-byte magic prefix is actually checked; the trailing bytes are
# deliberately not a fully well-formed binary plist -- the validation is a
# content-sniff, not a parser.
BINARY_PLIST = b"bplist00" + b"\xd1\x01\x02_\x10\x00\x08\x00"

NOT_A_PLIST = b"This is my tax return, not an Apple sidecar.\n"


def _write(path: str, content: bytes) -> str:
    """Write raw bytes to ``path`` and return it, for readable fixture setup."""
    with open(path, "wb") as handle:
        handle.write(content)
    return path


class TestContentValidationBeforeDeletion(unittest.TestCase):
    """A candidate is deleted only when its content validates as a plist."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_non_plist_aae_is_not_deleted_and_is_reported_skipped(self):
        """The issue's own reproduction: a fake ``.AAE`` survives a real run.

        Acceptance criterion: a file named ``notes.aae`` whose contents are
        not a plist is not deleted; it is reported as skipped.
        """
        fake = _write(
            os.path.join(self.export_dir, "notes.aae"), NOT_A_PLIST
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertTrue(os.path.exists(fake), "non-plist .aae was destroyed")
        self.assertEqual(results["sidecars_deleted"], 0)
        self.assertEqual(results["sidecars_skipped"], 1)
        reason, path = results["skipped_sidecar_files"][0]
        self.assertEqual(reason, "not_plist")
        self.assertEqual(path, fake)
        # Not a processable file: neither counted as processed nor failed.
        self.assertEqual(results["files_processed"], 0)
        self.assertEqual(results["files_failed"], 0)

    def test_reproduction_fake_aae_survives_genuine_sidecar_is_deleted(self):
        """The exact issue #57 scenario: a real sidecar, a real photo, and an
        unrelated file merely named ``tax-return-2024.AAE`` in the same run.

        Only the genuine sidecar is deleted; the fake survives; the photo is
        filed; the summary accounts for exactly one deletion.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "IMG_1000.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        _write(
            os.path.join(self.export_dir, "IMG_1000.aae"), XML_PLIST
        )
        fake_tax_return = _write(
            os.path.join(self.export_dir, "tax-return-2024.AAE"),
            b"1234567890123",  # 13 bytes of unrelated user data
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertFalse(
            os.path.exists(os.path.join(self.export_dir, "IMG_1000.aae")),
            "the genuine sidecar was not deleted",
        )
        self.assertTrue(
            os.path.exists(fake_tax_return),
            "tax-return-2024.AAE was destroyed -- the issue's own repro",
        )
        self.assertTrue(
            os.path.isfile(
                os.path.join(self.backup_dir, "photos", "2024.01.15.14.30.45.jpg")
            )
        )
        self.assertEqual(results["sidecars_deleted"], 1)
        self.assertEqual(results["sidecars_skipped"], 1)
        self.assertEqual(results["files_processed"], 1)
        self.assertEqual(results["files_failed"], 0)

    def test_genuine_xml_plist_sidecar_is_deleted(self):
        """A genuine XML-plist ``.aae`` is deleted."""
        sidecar = _write(
            os.path.join(self.export_dir, "photo.aae"), XML_PLIST
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertFalse(os.path.exists(sidecar))
        self.assertEqual(results["sidecars_deleted"], 1)
        self.assertEqual(results["sidecars_skipped"], 0)

    def test_genuine_binary_plist_sidecar_is_deleted(self):
        """A ``bplist00`` binary-plist ``.aae`` is deleted."""
        sidecar = _write(
            os.path.join(self.export_dir, "photo.aae"), BINARY_PLIST
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertFalse(os.path.exists(sidecar))
        self.assertEqual(results["sidecars_deleted"], 1)
        self.assertEqual(results["sidecars_skipped"], 0)

    def test_unreadable_candidate_fails_closed_and_is_not_deleted(self):
        """A candidate that cannot be read is kept -- never treated as valid.

        Simulates a permissions error / a race where the file vanished
        between scan and delete: ``open()`` itself raises. The candidate must
        be kept, not deleted, and reported as skipped -- not silently
        dropped, and not treated as validated just because it could not be
        checked.
        """
        sidecar = _write(
            os.path.join(self.export_dir, "photo.aae"), XML_PLIST
        )

        real_open = open

        def failing_open(path, *args, **kwargs):
            if os.path.abspath(path) == os.path.abspath(sidecar):
                raise PermissionError("simulated permission error")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=failing_open):
            results = self.processor.process_all_files(dry_run=False)

        self.assertTrue(os.path.exists(sidecar), "unreadable candidate was deleted")
        self.assertEqual(results["sidecars_deleted"], 0)
        self.assertEqual(results["sidecars_skipped"], 1)
        reason, path = results["skipped_sidecar_files"][0]
        self.assertEqual(reason, "not_plist")
        self.assertEqual(path, sidecar)


class TestDeletionRunsAfterCategoryProcessing(unittest.TestCase):
    """Sidecar deletion happens after all category processing, not before it."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_delete_sidecar_files_called_after_process_category(self):
        """Call order: every ``_process_category`` call precedes the single
        ``_delete_sidecar_files`` call, for both a real and a dry run.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "IMG_2000.jpg"),
            date_time_original="2024:05:05 05:05:05",
        )
        _write(os.path.join(self.export_dir, "IMG_2000.aae"), XML_PLIST)

        order = []
        real_process_category = self.processor._process_category
        real_delete = self.processor._delete_sidecar_files

        def recording_process_category(*args, **kwargs):
            order.append("process_category")
            return real_process_category(*args, **kwargs)

        def recording_delete(*args, **kwargs):
            order.append("delete_sidecar_files")
            return real_delete(*args, **kwargs)

        with patch.object(
            self.processor, "_process_category", side_effect=recording_process_category
        ), patch.object(
            self.processor, "_delete_sidecar_files", side_effect=recording_delete
        ):
            self.processor.process_all_files(dry_run=False)

        self.assertEqual(order, ["process_category", "delete_sidecar_files"])

    def test_sidecar_still_present_while_photo_is_being_placed(self):
        """The sidecar must still exist on disk while the photo is mid-move --
        proof deletion has not already happened by the time processing runs.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "IMG_3000.jpg"),
            date_time_original="2024:06:06 06:06:06",
        )
        sidecar = _write(
            os.path.join(self.export_dir, "IMG_3000.aae"), XML_PLIST
        )

        seen_during_processing = {}
        real_process_single_file = self.processor._process_single_file

        def spying_process_single_file(*args, **kwargs):
            seen_during_processing["sidecar_exists"] = os.path.exists(sidecar)
            return real_process_single_file(*args, **kwargs)

        with patch.object(
            self.processor,
            "_process_single_file",
            side_effect=spying_process_single_file,
        ):
            self.processor.process_all_files(dry_run=False)

        self.assertTrue(
            seen_during_processing.get("sidecar_exists"),
            "sidecar was already deleted before the photo was processed",
        )
        # And it IS gone by the time the whole run has completed.
        self.assertFalse(os.path.exists(sidecar))


class TestDryRunRealRunParity(unittest.TestCase):
    """The dry-run sidecar count matches the real-run count for identical input."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _build_export(self, export_dir: str) -> None:
        os.makedirs(export_dir, exist_ok=True)
        make_exif_jpeg(
            os.path.join(export_dir, "IMG_4000.jpg"),
            date_time_original="2024:07:07 07:07:07",
        )
        _write(os.path.join(export_dir, "IMG_4000.aae"), XML_PLIST)
        _write(os.path.join(export_dir, "IMG_4001.aae"), BINARY_PLIST)
        _write(os.path.join(export_dir, "notes.aae"), NOT_A_PLIST)

    def test_dry_run_and_real_run_report_the_same_sidecar_count(self):
        dry_export = os.path.join(self.temp_dir, "dry_export")
        dry_backup = os.path.join(self.temp_dir, "dry_backup")
        real_export = os.path.join(self.temp_dir, "real_export")
        real_backup = os.path.join(self.temp_dir, "real_backup")
        self._build_export(dry_export)
        self._build_export(real_export)

        dry_results = FileProcessor(dry_export, dry_backup).process_all_files(
            dry_run=True
        )
        real_results = FileProcessor(real_export, real_backup).process_all_files(
            dry_run=False
        )

        self.assertEqual(dry_results["sidecars_deleted"], 2)
        self.assertEqual(
            dry_results["sidecars_deleted"], real_results["sidecars_deleted"],
            "dry-run and real-run sidecar-deletion counts diverged (#10 parity)",
        )
        self.assertEqual(dry_results["sidecars_skipped"], 1)
        self.assertEqual(
            dry_results["sidecars_skipped"], real_results["sidecars_skipped"]
        )

        # And a dry run genuinely performed zero deletions.
        for name in ("IMG_4000.aae", "IMG_4001.aae", "notes.aae"):
            self.assertTrue(os.path.exists(os.path.join(dry_export, name)))

    def test_dry_run_never_calls_os_remove(self):
        """A dry run performs zero ``os.remove`` calls, even for a validated
        sidecar it correctly counts as "would delete"."""
        export_dir = os.path.join(self.temp_dir, "export")
        self._build_export(export_dir)

        with patch("src.file_processor.os.remove") as mock_remove:
            results = FileProcessor(
                export_dir, os.path.join(self.temp_dir, "backup")
            ).process_all_files(dry_run=True)

        mock_remove.assert_not_called()
        self.assertEqual(results["sidecars_deleted"], 2)


class TestFailedRunLeavesSidecarsIntact(unittest.TestCase):
    """A run that fails to process any photo leaves the .aae files intact.

    Deferring deletion to after category processing (issue #57) means a
    failure severe enough to prevent ANY photo from being processed -- i.e.
    the run never even reaches the per-file processing loop -- also prevents
    the deferred sidecar deletion from ever running, since it sits after that
    loop in ``process_all_files``. Under the OLD ordering (delete first),
    the same failure would still have destroyed the sidecar before the
    failure ever occurred.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_target_directory_creation_failure_leaves_sidecar_untouched(self):
        """``ensure_target_directories`` raising means no photo is even
        attempted, and the .aae candidate survives untouched.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "IMG_5000.jpg"),
            date_time_original="2024:08:08 08:08:08",
        )
        sidecar = _write(
            os.path.join(self.export_dir, "IMG_5000.aae"), XML_PLIST
        )

        with patch.object(
            self.processor.categorizer,
            "ensure_target_directories",
            side_effect=OSError("cannot create backup/photos"),
        ):
            with self.assertRaises(OSError):
                self.processor.process_all_files(dry_run=False)

        self.assertTrue(
            os.path.exists(sidecar),
            "sidecar was deleted even though no photo was ever processed",
        )
        self.assertEqual(self.processor._deleted_sidecars, [])
        self.assertEqual(self.processor._processed_files, [])
        # The photo itself is untouched too -- nothing landed in backup/.
        self.assertFalse(os.path.isdir(os.path.join(self.backup_dir, "photos")))


class TestSkippedSidecarDisplay(unittest.TestCase):
    """display_results renders the new rows, escaping untrusted filenames."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.cli = CLIInterface(self.export_dir, self.backup_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_sidecar_deleted_row_present_for_zero_and_nonzero(self):
        """The 'Sidecar Files Deleted' row renders unconditionally."""
        base = {
            "files_processed": 0,
            "files_failed": 0,
            "files_quarantined": 0,
            "sidecars_deleted": 0,
            "sidecars_skipped": 0,
            "status": "completed",
        }
        with self.cli.console.capture() as capture:
            self.cli.display_results(dict(base), dry_run=False)
        self.assertIn("Sidecar Files Deleted", capture.get())

        with_deletes = dict(base, sidecars_deleted=3)
        with self.cli.console.capture() as capture:
            self.cli.display_results(with_deletes, dry_run=True)
        rendered = capture.get()
        self.assertIn("Sidecar Files Deleted", rendered)
        self.assertIn("3", rendered)

    def test_crafted_sidecar_filename_does_not_abort_rendering(self):
        """A malicious filename in the skipped-sidecar list is escaped, not
        parsed as rich markup (issue #9's precedent: an unmatched markup tag
        in an untrusted filename previously aborted the entire run).
        """
        hostile_name = "notes[/bold red]PWNED[bold].aae"
        results = {
            "files_processed": 0,
            "files_failed": 0,
            "files_quarantined": 0,
            "sidecars_deleted": 0,
            "sidecars_skipped": 1,
            "skipped_sidecar_files": [("not_plist", hostile_name)],
            "status": "completed",
        }

        try:
            with self.cli.console.capture() as capture:
                self.cli.display_results(results, dry_run=False)
        except Exception as error:  # pragma: no cover - failure path
            self.fail(f"rendering a hostile filename raised: {error!r}")

        rendered = capture.get()
        self.assertIn("PWNED", rendered)


if __name__ == "__main__":
    unittest.main()
