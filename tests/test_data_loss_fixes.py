"""
Regression tests for the data-loss defects found by the whole-branch review.

Each class here pins one defect that could permanently destroy a photograph or a
user's edit history. Every one was confirmed to FAIL against the code as it stood
immediately before its fix -- see the mutation table in the task report.

The defects, in the order they appear below:

1. ``verify_conversion`` authorized deleting a HEIC original after only reading
   the JPEG's headers, so a truncated JPEG passed and the sole surviving copy of
   the photo was garbage.
2. The sidecar-deletion gate was run-level all-or-nothing, so ONE archived file
   unlocked deleting EVERY ``.aae`` while the failed photos sat un-archived.
3. ``directory_overlap_error`` compared paths as strings, so on case-insensitive
   APFS a case-mismatched pair let a run consume its own archive.
4. ``determine_exit_code`` consulted only ``files_failed``, so a run that told a
   human "the counts above cannot be trusted" told a script it succeeded.
5. Converted JPEGs left in ``export/`` were re-ingested by the next run.
6. A failed ``unlink`` of a HEIC original was reported as unqualified success.
7. ``KeyboardInterrupt`` bypassed the reserved-placeholder cleanup.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageFile

from src.cli_interface import is_unqualified_success
from src.file_processor import FileProcessor, ProgressReporter, Settings
from src.heic_converter import HeicConverter
from src.main import determine_exit_code
from tests.fixtures import make_exif_heic, make_exif_jpeg

PLIST = b'<?xml version="1.0"?><plist version="1.0"><dict/></plist>'


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)
        self.processor = FileProcessor(Settings(export_dir=self.export, backup_dir=self.backup))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _photo(self, name, second=1):
        return make_exif_jpeg(
            os.path.join(self.export, name),
            date_time_original=f"2024:01:15 14:30:{second:02d}",
        )

    def _aae(self, name, content=PLIST):
        path = os.path.join(self.export, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path


class TestVerifyConversionActuallyDecodes(unittest.TestCase):
    """The gate on an irreversible delete must read pixels, not headers."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.source = os.path.join(self.temp_dir, "source.jpg")
        Image.new("RGB", (400, 300), (200, 100, 50)).save(self.source, quality=95)
        self.full = os.path.join(self.temp_dir, "full.jpg")
        Image.new("RGB", (400, 300), (200, 100, 50)).save(self.full, quality=95)
        self.converter = HeicConverter()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _truncate(self, fraction=0.5):
        data = open(self.full, "rb").read()
        path = os.path.join(self.temp_dir, "truncated.jpg")
        with open(path, "wb") as handle:
            handle.write(data[: int(len(data) * fraction)])
        return path

    def test_intact_jpeg_still_verifies(self):
        """The fix must not reject good conversions."""
        self.assertTrue(self.converter.verify_conversion(self.source, self.full))

    def test_truncated_jpeg_is_rejected(self):
        """A half-written JPEG reports correct size and EXIF, and must fail."""
        truncated = self._truncate()
        # Prove the header read the old code relied on is genuinely fooled.
        with Image.open(truncated) as img:
            self.assertEqual(img.size, (400, 300))
        self.assertFalse(
            self.converter.verify_conversion(self.source, truncated),
            "a truncated JPEG must never authorize deleting the original",
        )

    def test_load_truncated_images_global_cannot_defeat_the_gate(self):
        """Pillow's global must not be able to turn the decode into a no-op."""
        truncated = self._truncate()
        previous = ImageFile.LOAD_TRUNCATED_IMAGES
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            self.assertFalse(
                self.converter.verify_conversion(self.source, truncated),
                "the gate must force the flag off rather than trust the default",
            )
            self.assertTrue(
                ImageFile.LOAD_TRUNCATED_IMAGES,
                "the gate must restore the caller's flag, not clobber it",
            )
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous

    def test_original_survives_a_truncated_conversion(self):
        """End to end: cleanup_original_heic must refuse to delete."""
        stand_in_heic = os.path.join(self.temp_dir, "original.heic")
        shutil.copyfile(self.source, stand_in_heic)
        truncated = self._truncate()

        deleted = self.converter.cleanup_original_heic(
            stand_in_heic, converted_jpeg=truncated, verify_first=True
        )

        self.assertFalse(deleted)
        self.assertTrue(
            os.path.exists(stand_in_heic),
            "the only surviving copy of the photo was deleted",
        )


class TestAnyFailureKeepsEverySidecar(_Sandbox):
    """One archived file must not unlock deleting every .aae."""

    def _run_with_one_move_failing(self, victim="photo_b.jpg"):
        real_move = shutil.move

        def flaky(src, dst, *args, **kwargs):
            if os.path.basename(src) == victim:
                raise PermissionError("forced failure")
            return real_move(src, dst, *args, **kwargs)

        with patch("src.file_processor.shutil.move", side_effect=flaky):
            return self.processor.process_all_files(dry_run=False)

    def test_partial_failure_keeps_all_sidecars(self):
        """3 of 4 succeed; all four .aae must survive."""
        for i, name in enumerate(["photo_a.jpg", "photo_b.jpg", "photo_c.jpg"]):
            self._photo(name, second=i + 1)
        sidecars = [self._aae(f"photo_{s}.aae") for s in "abc"]

        results = self._run_with_one_move_failing()

        self.assertEqual(results["files_failed"], 1)
        self.assertEqual(
            results["sidecars_deleted"], 0,
            "a run with a failure must not delete any edit history",
        )
        self.assertEqual(results["sidecars_skipped"], len(sidecars))
        for path in sidecars:
            self.assertTrue(
                os.path.exists(path),
                f"{path} was permanently deleted despite a failed photo",
            )

    def test_the_kept_sidecars_are_reported_with_a_reason(self):
        """Silence would be as bad as deletion -- the user must be told."""
        self._photo("photo_a.jpg", second=1)
        self._photo("photo_b.jpg", second=2)
        self._aae("photo_a.aae")

        results = self._run_with_one_move_failing()

        reasons = {reason for reason, _path in results["skipped_sidecar_files"]}
        self.assertIn("run_archived_nothing", reasons)
        self.assertFalse(is_unqualified_success(results))

    def test_a_fully_clean_run_still_deletes_its_sidecars(self):
        """The conservative gate must not stop ordinary cleanup."""
        self._photo("photo_a.jpg", second=1)
        sidecar = self._aae("photo_a.aae")

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_failed"], 0)
        self.assertEqual(results["sidecars_deleted"], 1)
        self.assertFalse(os.path.exists(sidecar))
        self.assertTrue(is_unqualified_success(results))

    def test_a_landing_discrepancy_also_keeps_every_sidecar(self):
        """The audit must gate deletion, not merely report after it.

        The audit used to run AFTER sidecar deletion, so a run could delete the
        edit history and only then discover its archive did not verify.
        """
        self._photo("photo_a.jpg", second=1)
        sidecar = self._aae("photo_a.aae")

        original_place = self.processor._place_source_content

        def place_then_vanish(source, destination):
            original_place(source, destination)
            os.remove(destination)

        with patch.object(
            self.processor, "_place_source_content", side_effect=place_then_vanish
        ):
            results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_failed"], 0)
        self.assertGreater(results["landing_discrepancies"], 0)
        self.assertEqual(
            results["sidecars_deleted"], 0,
            "edit history was deleted for a run whose archive did not verify",
        )
        self.assertTrue(os.path.exists(sidecar))


class TestOverlapDetectionIgnoresPathCase(unittest.TestCase):
    """On case-insensitive APFS, two spellings are one directory."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.lower = os.path.join(self.temp_dir, "export")
        os.makedirs(self.lower)
        self.upper = os.path.join(self.temp_dir, "Export")
        self.case_insensitive = os.path.isdir(self.upper)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_same_directory_under_two_spellings_is_rejected(self):
        if not self.case_insensitive:
            self.skipTest("filesystem is case-sensitive; the aliasing cannot occur")
        error = FileProcessor.directory_overlap_error(self.upper, self.lower)
        self.assertIsNotNone(
            error, "a case-mismatched alias of one directory was accepted"
        )

    def test_backup_nested_inside_a_case_variant_of_export_is_rejected(self):
        if not self.case_insensitive:
            self.skipTest("filesystem is case-sensitive; the aliasing cannot occur")
        nested = os.path.join(self.lower, "backup")
        os.makedirs(nested)
        error = FileProcessor.directory_overlap_error(self.upper, nested)
        self.assertIsNotNone(
            error,
            "backup/ nested inside a case-variant of export/ was accepted; the "
            "run would consume its own archive",
        )

    def test_genuinely_separate_directories_are_still_accepted(self):
        """The fix must not reject a valid configuration."""
        other = os.path.join(self.temp_dir, "backup")
        os.makedirs(other)
        self.assertIsNone(
            FileProcessor.directory_overlap_error(self.lower, other)
        )

    def test_a_backup_dir_that_does_not_exist_yet_is_accepted(self):
        """A first run creates backup/; samefile raises, and that is not an error."""
        self.assertIsNone(
            FileProcessor.directory_overlap_error(
                self.lower, os.path.join(self.temp_dir, "not-created-yet")
            )
        )


class TestExitCodeTracksTheSuccessPredicate(unittest.TestCase):
    """The exit code is the only signal an unattended caller sees."""

    def test_every_qualified_failure_exits_nonzero(self):
        cases = {
            "landing discrepancy": {"landing_discrepancies": 1},
            "source not drained": {"landing_discrepancies": 1},
            "sidecar kept": {"sidecars_skipped": 1},
            "quarantined": {"files_quarantined": 1},
            "hidden skip": {"skipped_files": [("hidden", ".x.jpg")]},
        }
        for label, extra in cases.items():
            with self.subTest(label):
                results = {"status": "completed", "files_processed": 1,
                           "files_failed": 0, **extra}
                self.assertFalse(is_unqualified_success(results))
                self.assertNotEqual(
                    determine_exit_code(results), 0,
                    f"{label} reported success to a script",
                )

    def test_clean_and_junk_only_runs_still_exit_zero(self):
        """Every macOS export carries a .DS_Store; it must not fail the run."""
        clean = {"status": "completed", "files_processed": 3, "files_failed": 0}
        junk = {**clean, "skipped_files": [("junk", ".DS_Store")]}
        self.assertEqual(determine_exit_code(clean), 0)
        self.assertEqual(determine_exit_code(junk), 0)

    def test_cancelled_still_outranks_everything(self):
        self.assertEqual(
            determine_exit_code({"status": "cancelled"}), 130
        )


class TestNoTransientConversionIsLeftInExport(_Sandbox):
    """A stray converted JPEG is re-ingested by the next run."""

    def test_a_failed_move_leaves_no_transient_jpeg_behind(self):
        """The transient output must be swept when its move fails."""
        self._photo("photo_a.jpg", second=1)
        # Stand in for a conversion: register a transient output, then fail.
        transient = os.path.join(self.export, "photo_a-abcd1234.jpg")
        shutil.copyfile(os.path.join(self.export, "photo_a.jpg"), transient)
        self.processor._transient_outputs.add(transient)

        self.processor._sweep_transient_outputs()

        self.assertFalse(
            os.path.exists(transient),
            "an unplaced transient conversion was left in export/ for the next "
            "run to archive as a duplicate",
        )
        self.assertEqual(self.processor._transient_outputs, set())

    def test_the_sweep_only_touches_paths_it_recorded(self):
        """It must never pattern-match; IMG_x-edited.jpg is a real user file."""
        decoy = os.path.join(self.export, "photo_a-edited.jpg")
        with open(decoy, "wb") as handle:
            handle.write(b"a real user file that merely looks like a temp name")

        self.processor._sweep_transient_outputs()

        self.assertTrue(
            os.path.exists(decoy),
            "the sweep deleted a user file it never created",
        )

    def test_a_missing_transient_does_not_raise(self):
        """Already-moved outputs are the normal case, not an error."""
        self.processor._transient_outputs.add(
            os.path.join(self.export, "never-existed.jpg")
        )
        self.processor._sweep_transient_outputs()  # must not raise
        self.assertEqual(self.processor._transient_outputs, set())

    def test_clear_processing_state_resets_transient_tracking(self):
        self.processor._transient_outputs.add("/tmp/whatever.jpg")
        self.processor.clear_processing_state()
        self.assertEqual(self.processor._transient_outputs, set())


class _InterruptOnFirstConversion(ProgressReporter):
    """Raise the user's Ctrl-C from the first pool-phase progress callback.

    The callback fires inside ``_convert_heic_files_parallel``'s
    ``as_completed`` loop, so this lands the interrupt at the exact point a
    real Ctrl-C hits the pool phase -- after at least one conversion has
    written its transient JPEG into ``export/``.
    """

    def on_heic_converted(self, path: str) -> None:
        raise KeyboardInterrupt("user pressed ctrl-c during the pool phase")


class TestInterruptedRunStillSweepsTransients(_Sandbox):
    """Ctrl-C in either phase must not leave a transient JPEG in export/.

    Phase 8 review: the sweep ran only on the normal-completion path, and
    pool outputs were only recorded when Phase B consumed them -- so both
    interrupt cases the sweep's docstring claimed to close were open. A
    KeyboardInterrupt in Phase B left a TRACKED transient in export/; one
    during the pool phase left a transient that was not even tracked. Either
    way the next run re-ingests it: a duplicate photograph, or a false
    "your photos are corrupt" quarantine.
    """

    def test_interrupt_in_the_place_phase_sweeps_a_tracked_transient(self):
        """A tracked transient survives Ctrl-C unless the sweep is in a finally."""
        self._photo("photo_a.jpg", second=1)
        stray = os.path.join(self.export, "photo_a-deadbeef.jpg")

        def convert_then_interrupt(*args, **kwargs):
            # Stand in for "the conversion happened, then Ctrl-C landed while
            # the file was being placed": the transient exists on disk and is
            # tracked, exactly as after a real inline conversion.
            shutil.copyfile(os.path.join(self.export, "photo_a.jpg"), stray)
            self.processor._transient_outputs.add(stray)
            raise KeyboardInterrupt("user pressed ctrl-c")

        with patch.object(
            self.processor, "_process_single_file",
            side_effect=convert_then_interrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.processor.process_all_files(dry_run=False)

        self.assertFalse(
            os.path.exists(stray),
            "a tracked transient JPEG was left in export/ by an interrupted "
            "run for the next run to re-ingest",
        )

    def test_interrupt_in_the_pool_phase_leaves_no_stray_jpeg(self):
        """Pool outputs must be tracked as they arrive, not at consume time.

        A real pool run (at the parallel threshold) interrupted from the
        first conversion's progress callback: without arrival-time recording
        plus the completed-future harvest plus the finally-sweep, at least
        one converted JPEG stays in export/ next to its original HEIC.
        """
        for index in range(FileProcessor.HEIC_PARALLEL_THRESHOLD):
            make_exif_heic(
                os.path.join(self.export, f"IMG_{index:02d}.heic"),
                date_time_original=f"2024:05:05 05:05:{index:02d}",
            )

        with self.assertRaises(KeyboardInterrupt):
            self.processor.process_all_files(
                dry_run=False, progress=_InterruptOnFirstConversion()
            )

        strays = [
            name for name in os.listdir(self.export) if name.endswith(".jpg")
        ]
        self.assertEqual(
            strays, [],
            "transient conversions from an interrupted pool phase were left "
            "in export/",
        )


class TestSourceDrainageIsAudited(_Sandbox):
    """A photo left in export/ is archived twice by the next run."""

    def test_an_undrained_source_is_reported_as_a_discrepancy(self):
        """The audit covered backup/ only; export/ was never checked."""
        photo = self._photo("photo_a.jpg", second=1)

        real_move = shutil.move

        def move_but_leave_a_copy(src, dst, *args, **kwargs):
            result = real_move(src, dst, *args, **kwargs)
            # Simulate a source that survived: the shape a failed unlink of a
            # HEIC original leaves behind.
            shutil.copyfile(dst, src)
            return result

        with patch("src.file_processor.shutil.move",
                   side_effect=move_but_leave_a_copy):
            results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_failed"], 0)
        self.assertIn(
            ("source_not_drained", photo),
            results["landing_discrepancy_list"],
        )
        self.assertFalse(
            is_unqualified_success(results),
            "a run that did not drain its source reported clean success",
        )
        self.assertNotEqual(determine_exit_code(results), 0)

    def test_a_clean_run_reports_no_drainage_discrepancy(self):
        self._photo("photo_a.jpg", second=1)
        results = self.processor.process_all_files(dry_run=False)
        self.assertEqual(results["landing_discrepancies"], 0)
        self.assertTrue(is_unqualified_success(results))

    def test_dry_run_is_not_flagged_for_undrained_sources(self):
        """A dry run drains nothing by design (#10 parity)."""
        self._photo("photo_a.jpg", second=1)
        results = self.processor.process_all_files(dry_run=True)
        self.assertEqual(results["landing_discrepancies"], 0)


class TestInterruptLeavesNoPlaceholder(_Sandbox):
    """KeyboardInterrupt is not an Exception, and lands in the move window."""

    def test_keyboard_interrupt_between_reserve_and_move_leaves_no_stub(self):
        self._photo("photo_a.jpg", second=1)

        def interrupt_instead_of_moving(source, destination):
            raise KeyboardInterrupt()

        with patch.object(
            self.processor, "_place_source_content",
            side_effect=interrupt_instead_of_moving,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.processor.process_all_files(dry_run=False)

        stubs = []
        for root, _dirs, files in os.walk(self.backup):
            for name in files:
                path = os.path.join(root, name)
                if os.path.getsize(path) == 0:
                    stubs.append(path)
        self.assertEqual(
            stubs, [],
            "Ctrl-C left a 0-byte file in backup/ that is indistinguishable "
            "from an archived photo and permanently claims its timestamp",
        )


if __name__ == "__main__":
    unittest.main()
