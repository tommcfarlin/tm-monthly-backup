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

from src.file_processor import FileProcessor, Settings
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
        self.processor = FileProcessor(Settings(export_dir=self.export_dir, backup_dir=self.backup_dir))

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
        # Distinct from 'not_plist' (issue #57 fix-round-1): this candidate's
        # content was never actually inspected, only its openability failed.
        self.assertEqual(reason, "unreadable")
        self.assertEqual(path, sidecar)


class TestDeletionRunsAfterCategoryProcessing(unittest.TestCase):
    """Sidecar deletion happens after all category processing, not before it."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(Settings(export_dir=self.export_dir, backup_dir=self.backup_dir))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_delete_sidecar_files_called_after_process_category(self):
        """Call order: every ``_process_category`` call precedes the single
        ``_delete_sidecar_files`` call, for both a real and a dry run.

        Runs against two independent export/backup trees (one per flag) so
        the real run's actual deletion cannot interfere with the dry run's
        assertions, and vice versa.
        """
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                export_dir = os.path.join(
                    self.temp_dir, f"export_{dry_run}"
                )
                backup_dir = os.path.join(
                    self.temp_dir, f"backup_{dry_run}"
                )
                os.makedirs(export_dir, exist_ok=True)
                make_exif_jpeg(
                    os.path.join(export_dir, "IMG_2000.jpg"),
                    date_time_original="2024:05:05 05:05:05",
                )
                _write(os.path.join(export_dir, "IMG_2000.aae"), XML_PLIST)

                processor = FileProcessor(Settings(export_dir=export_dir, backup_dir=backup_dir))
                order = []
                real_process_category = processor._process_category
                real_delete = processor._delete_sidecar_files

                def recording_process_category(*args, **kwargs):
                    order.append("process_category")
                    return real_process_category(*args, **kwargs)

                def recording_delete(*args, **kwargs):
                    order.append("delete_sidecar_files")
                    return real_delete(*args, **kwargs)

                with patch.object(
                    processor,
                    "_process_category",
                    side_effect=recording_process_category,
                ), patch.object(
                    processor,
                    "_delete_sidecar_files",
                    side_effect=recording_delete,
                ):
                    processor.process_all_files(dry_run=dry_run)

                self.assertEqual(
                    order, ["process_category", "delete_sidecar_files"]
                )

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

        dry_results = FileProcessor(Settings(export_dir=dry_export, backup_dir=dry_backup)).process_all_files(
            dry_run=True
        )
        real_results = FileProcessor(Settings(export_dir=real_export, backup_dir=real_backup)).process_all_files(
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
            results = FileProcessor(Settings(export_dir=export_dir, backup_dir=os.path.join(self.temp_dir, "backup"))).process_all_files(dry_run=True)

        mock_remove.assert_not_called()
        self.assertEqual(results["sidecars_deleted"], 2)


class TestFailedRunLeavesSidecarsIntact(unittest.TestCase):
    """A run that fails to process any photo leaves the .aae files intact.

    This is a RUN-LEVEL outcome, not a mechanism -- fix-round-1 ruling: the
    acceptance criterion is written about an outcome ("a run that fails to
    process any photo"), not about how the failure happens, and deferring the
    delete call to after the per-category loop is NOT sufficient by itself:
    ``_process_category``'s own per-file ``except Exception`` (issue #39's
    sanctioned broad catch) guarantees that loop *completes* even when every
    single processable file failed -- a full disk partway through a real run
    (``shutil.move`` raising ``OSError(ENOSPC)``) is the ordinary way this
    happens, and it does not stop the loop from returning normally. So
    ``process_all_files`` additionally gates the deferred delete itself: it
    computes attempted-minus-failed ("archived") and, when there were
    processable files but none of them archived successfully, keeps every
    sidecar candidate rather than calling ``_delete_sidecar_files`` at all
    (recorded under the ``'run_archived_nothing'`` reason).

    ``test_target_directory_creation_failure_leaves_sidecar_untouched`` is
    kept as a second, independent way to reach the same outcome: a failure so
    severe the per-category loop never even starts (so the deferred delete
    call is never reached at all, regardless of the run-archived-nothing
    gate). ``test_every_file_fails_during_processing_leaves_sidecar_intact``
    is the one the criterion itself actually calls for -- the loop runs,
    reaches, and completes, and the gate is what keeps the sidecar safe.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(Settings(export_dir=self.export_dir, backup_dir=self.backup_dir))

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

    def test_every_file_fails_during_processing_leaves_sidecar_intact(self):
        """The reviewer's own probe: a full disk (ENOSPC) makes every single
        photo fail INSIDE the per-category loop -- the loop still completes
        normally (issue #39's per-file catch-all) -- and the run must still
        leave every validated sidecar candidate on disk, not delete it once
        the loop returns.

        This is the acceptance-box-5 scenario per se: the loop runs and
        completes, files_failed == files attempted, files_processed == 0,
        and the .aae must survive. Fails against the code from before this
        fix round (confirmed by stashing this round's src/ changes): the old
        code deleted the (valid) sidecar unconditionally once the loop
        returned, regardless of how many files inside it failed.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "IMG_1.jpg"),
            date_time_original="2024:01:01 01:01:01",
        )
        make_exif_jpeg(
            os.path.join(self.export_dir, "IMG_2.jpg"),
            date_time_original="2024:01:02 02:02:02",
        )
        sidecar = _write(
            os.path.join(self.export_dir, "IMG_1.aae"), XML_PLIST
        )

        with patch(
            "src.file_processor.shutil.move",
            side_effect=OSError(28, "No space left on device"),
        ):
            results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_processed"], 0)
        self.assertEqual(results["files_failed"], 2)
        self.assertEqual(
            results["sidecars_deleted"], 0,
            "a sidecar was deleted even though the run archived nothing",
        )
        self.assertTrue(
            os.path.exists(sidecar),
            "sidecar was destroyed on a run that archived zero photos",
        )
        self.assertEqual(
            sorted(os.listdir(self.export_dir)),
            ["IMG_1.aae", "IMG_1.jpg", "IMG_2.jpg"],
            "export/ should still hold every original file untouched",
        )
        reason, path = results["skipped_sidecar_files"][0]
        self.assertEqual(reason, "run_archived_nothing")
        self.assertEqual(path, sidecar)

    def test_run_archived_nothing_gate_does_not_fire_for_an_all_sidecar_batch(self):
        """The gate must not fire merely because there were zero PHOTOS --
        only when there were processable files that were ATTEMPTED and all
        of them failed. An export containing only sidecars (no processable
        file at all) is a distinct, ordinary case that must still delete a
        valid sidecar normally.
        """
        sidecar = _write(
            os.path.join(self.export_dir, "only.aae"), XML_PLIST
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_processed"], 0)
        self.assertEqual(results["files_failed"], 0)
        self.assertEqual(results["sidecars_deleted"], 1)
        self.assertFalse(os.path.exists(sidecar))


class TestUnexpectedSkipHoldsSidecarGate(unittest.TestCase):
    """An unexpected (non-junk) scan skip must hold the deletion gate.

    Phase 8 review: the gate consulted failures and landing discrepancies but
    not ``_skipped_files``, and its ``attempted > 0`` guard bypassed it
    entirely when nothing was processable. An ``export/`` holding a real photo
    under a dotted name (the interrupted-sync conflict copy issue #30 warns
    may be real data) plus its valid ``.aae`` therefore skipped the photo and
    permanently deleted its edit history -- issue #57's exact failure mode,
    arriving through the one bucket the gate did not read.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(Settings(export_dir=self.export_dir, backup_dir=self.backup_dir))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_hidden_photo_skip_keeps_sidecar_when_nothing_was_processable(self):
        """The review's reproduction: a dotted photo plus its valid sidecar.

        Nothing is processable (``attempted == 0``), so the old gate never
        even ran and the sidecar was deleted while the photo it describes sat
        skipped in ``export/``.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, ".IMG_1234.jpg"),
            date_time_original="2024:04:04 04:04:04",
        )
        sidecar = _write(
            os.path.join(self.export_dir, "IMG_1234.aae"), XML_PLIST
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_processed"], 0)
        self.assertEqual(results["files_skipped"], 1)
        self.assertEqual(
            results["sidecars_deleted"], 0,
            "edit history deleted while its photo sat skipped in export/",
        )
        self.assertTrue(os.path.exists(sidecar))
        reason, path = results["skipped_sidecar_files"][0]
        # 'unexpected_skip', not 'run_archived_nothing': nothing failed here.
        self.assertEqual(reason, "unexpected_skip")
        self.assertEqual(path, sidecar)

    def test_hidden_skip_keeps_sidecar_even_when_every_attempted_file_archived(self):
        """A skip holds the gate even on a run that archived plenty.

        This pins the reason code too: 'run_archived_nothing' would be a lie
        for this run (it archived every file it attempted), which is why the
        skip cause records 'unexpected_skip' instead.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "IMG_1.jpg"),
            date_time_original="2024:04:04 04:04:04",
        )
        make_exif_jpeg(
            os.path.join(self.export_dir, ".IMG_2.jpg"),
            date_time_original="2024:04:04 04:04:05",
        )
        sidecar = _write(
            os.path.join(self.export_dir, "IMG_2.aae"), XML_PLIST
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_processed"], 1)
        self.assertEqual(results["files_failed"], 0)
        self.assertEqual(results["sidecars_deleted"], 0)
        self.assertTrue(os.path.exists(sidecar))
        self.assertEqual(
            results["skipped_sidecar_files"],
            [("unexpected_skip", sidecar)],
        )

    def test_junk_only_skip_does_not_hold_the_gate(self):
        """Settled policy (#30): every macOS export carries a .DS_Store.

        Blocking on it would stop sidecar cleanup on essentially every run
        forever, so a junk-only skip must still let a valid sidecar delete.
        """
        _write(os.path.join(self.export_dir, ".DS_Store"), b"junk")
        make_exif_jpeg(
            os.path.join(self.export_dir, "IMG_1.jpg"),
            date_time_original="2024:04:04 04:04:04",
        )
        sidecar = _write(
            os.path.join(self.export_dir, "IMG_1.aae"), XML_PLIST
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_processed"], 1)
        self.assertEqual(results["sidecars_deleted"], 1)
        self.assertFalse(os.path.exists(sidecar))
        self.assertEqual(results["skipped_sidecar_files"], [])


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

    def test_unexpected_skip_reason_renders_a_label_not_a_raw_code(self):
        """The 'unexpected_skip' reason (phase 8 review) has a human label."""
        with self.cli.console.capture() as capture:
            self.cli.display_skipped_sidecars(
                [("unexpected_skip", os.path.join(self.export_dir, "a.aae"))]
            )
        rendered = capture.get()
        # A single word, not the full phrase: rich may wrap the table cell at
        # a space, but never mid-word.
        self.assertIn("unexpectedly", rendered)
        self.assertNotIn("unexpected_skip", rendered)

    def test_kept_sidecar_demotes_the_success_banner(self):
        """A run with zero failures/quarantines but a kept sidecar must not
        report an unqualified "Success!" -- fix-round-1 concern: a genuine
        delete failure (or any other kept-sidecar reason) was previously
        invisible to the banner, since it never touched ``_failed_files``.
        """
        results = {
            "files_processed": 1,
            "files_failed": 0,
            "files_quarantined": 0,
            "sidecars_deleted": 0,
            "sidecars_skipped": 1,
            "skipped_sidecar_files": [("delete_failed", "export/photo.aae")],
            "status": "completed",
        }

        with self.cli.console.capture() as capture:
            self.cli.display_results(results, dry_run=False)
        rendered = capture.get()

        self.assertNotIn("Success!", rendered)
        self.assertIn("Sidecars Kept", rendered)

    def test_no_kept_sidecar_still_reports_plain_success(self):
        """The banner is unaffected when nothing was kept -- no false demotion."""
        results = {
            "files_processed": 1,
            "files_failed": 0,
            "files_quarantined": 0,
            "sidecars_deleted": 1,
            "sidecars_skipped": 0,
            "status": "completed",
        }

        with self.cli.console.capture() as capture:
            self.cli.display_results(results, dry_run=False)
        rendered = capture.get()

        self.assertIn("Success!", rendered)

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
