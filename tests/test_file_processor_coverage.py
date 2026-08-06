"""
Targeted coverage tests for FileProcessor branches not hit end-to-end.

These exercise the move-failure recording, dry-run reporting for each category,
quarantine and unknown-file edge paths, the reserve/collision loops, the
overlap guard, and ``clear_processing_state``.
All filesystem work happens under ``tempfile.mkdtemp`` -- no real export/backup
trees are ever created.
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from src.file_categorizer import FileCategory
from src.file_processor import FileProcessor
from tests.fixtures import make_corrupt_jpeg, make_exif_jpeg, make_no_exif_jpeg


class TestScanAndOverlap(unittest.TestCase):
    """Scan of a missing export dir and the overlap guard."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_scan_missing_export_returns_empty(self):
        """Scanning a nonexistent export directory returns an empty list."""
        processor = FileProcessor(
            os.path.join(self.temp_dir, "nope"),
            os.path.join(self.temp_dir, "backup"),
        )
        self.assertEqual(processor._scan_export_directory(), [])

    def test_process_all_files_raises_on_overlap(self):
        """Overlapping export/backup roots raise ValueError before any work."""
        shared = os.path.join(self.temp_dir, "shared")
        os.makedirs(shared)
        processor = FileProcessor(shared, os.path.join(shared, "backup"))

        with self.assertRaises(ValueError):
            processor.process_all_files()

    def test_process_all_files_no_files_returns_summary(self):
        """An empty export dir yields a zeroed summary, not a crash."""
        export = os.path.join(self.temp_dir, "export")
        os.makedirs(export)
        processor = FileProcessor(export, os.path.join(self.temp_dir, "backup"))

        summary = processor.process_all_files()

        self.assertEqual(summary["files_processed"], 0)
        self.assertEqual(summary["files_failed"], 0)


class TestSidecarDeletionFailure(unittest.TestCase):
    """_delete_sidecar_files validation, dry-run, and failure recording.

    Issue #57: a candidate is deleted only when its CONTENT validates as a
    plist; an unreadable or non-plist candidate is kept and reported, and none
    of these outcomes is counted in ``_processed_files``/``_failed_files``
    (issue #31) -- a sidecar was never a processable file.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.processor = FileProcessor(
            os.path.join(self.temp_dir, "export"),
            os.path.join(self.temp_dir, "backup"),
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_plist(self, name: str) -> str:
        """Write a genuine (XML property list) sidecar and return its path."""
        path = os.path.join(self.temp_dir, name)
        with open(path, "wb") as handle:
            handle.write(b'<?xml version="1.0"?><plist version="1.0"><dict/></plist>')
        return path

    def test_dry_run_does_not_delete_a_valid_sidecar(self):
        """A dry run reports a validated sidecar as deleted, but never touches disk."""
        sidecar = self._write_plist("IMG_0001.aae")

        self.processor._delete_sidecar_files([sidecar], dry_run=True)

        self.assertTrue(os.path.exists(sidecar))
        self.assertEqual(self.processor._failed_files, [])
        self.assertEqual(self.processor._processed_files, [])
        self.assertEqual(self.processor._deleted_sidecars, [sidecar])
        self.assertEqual(self.processor._skipped_sidecars, [])

    def test_non_plist_content_is_kept_and_skipped_not_deleted(self):
        """A zero-byte (or otherwise non-plist) ``.aae`` is kept, not deleted.

        Acceptance criterion: ``notes.aae`` whose content is not a plist must
        not be deleted; it must be reported as skipped.
        """
        sidecar = os.path.join(self.temp_dir, "notes.aae")
        open(sidecar, "wb").close()  # zero bytes: matches neither magic prefix

        self.processor._delete_sidecar_files([sidecar], dry_run=False)

        self.assertTrue(os.path.exists(sidecar), "non-plist .aae was deleted")
        self.assertEqual(self.processor._deleted_sidecars, [])
        self.assertEqual(self.processor._skipped_sidecars, [('not_plist', sidecar)])
        # Not counted as processed or failed (issue #31): a sidecar is not a
        # processable file to begin with.
        self.assertEqual(self.processor._failed_files, [])
        self.assertEqual(self.processor._processed_files, [])

    def test_unreadable_candidate_fails_closed(self):
        """A candidate that cannot be read is kept, not treated as validated.

        A missing/unreadable file (a permissions error, or a race between
        scan and delete) must never be deleted on the strength of its
        extension alone -- the whole point of validating content first.
        """
        ghost = "/tmp/ghost-does-not-exist.aae"
        self.assertFalse(os.path.exists(ghost))

        with patch("os.remove") as mock_remove:
            self.processor._delete_sidecar_files([ghost], dry_run=False)
            mock_remove.assert_not_called()

        self.assertEqual(self.processor._deleted_sidecars, [])
        # Distinct from 'not_plist' (issue #57 fix-round-1): content was
        # never actually inspected, so "not a plist" would be a false claim.
        self.assertEqual(self.processor._skipped_sidecars, [('unreadable', ghost)])
        self.assertEqual(self.processor._failed_files, [])
        self.assertEqual(self.processor._processed_files, [])

    def test_delete_failure_after_validation_is_recorded_as_skipped_not_failed(self):
        """A validated sidecar whose os.remove itself fails is kept, not deleted.

        Issue #31/#57: this outcome must not land in ``_failed_files`` -- a
        sidecar is not a processable file, so counting it there would let
        ``files_processed + files_failed`` exceed the number of files
        actually seen as processable.
        """
        sidecar = self._write_plist("IMG_0002.aae")

        with patch("os.remove", side_effect=OSError("locked")):
            self.processor._delete_sidecar_files([sidecar], dry_run=False)

        self.assertTrue(os.path.exists(sidecar), "sidecar removed despite os.remove failing")
        self.assertEqual(self.processor._deleted_sidecars, [])
        self.assertEqual(self.processor._skipped_sidecars, [('delete_failed', sidecar)])
        self.assertEqual(self.processor._failed_files, [])
        self.assertEqual(self.processor._processed_files, [])


class TestMoveFailureRecording(unittest.TestCase):
    """A move failure during _process_single_file is recorded, not raised."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)
        self.processor = FileProcessor(self.export, self.backup)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_move_failure_records_and_discards_placeholder(self):
        """
        When shutil.move fails, the failure is recorded and the reserved
        placeholder is discarded (no 0-byte stub left in backup/).
        """
        photo = make_exif_jpeg(
            os.path.join(self.export, "pic.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        target_dir = os.path.join(self.backup, "photos")
        # process_all_files ensures this via ensure_target_directories before
        # any file is placed (issue #23 removed the redundant per-file
        # os.makedirs that used to paper over its absence here).
        os.makedirs(target_dir)

        with patch("shutil.move", side_effect=OSError("disk full")):
            self.processor._process_single_file(
                photo, FileCategory.PHOTO, target_dir, dry_run=False
            )

        self.assertEqual(len(self.processor._failed_files), 1)
        self.assertEqual(self.processor._failed_files[0][0], "move_file")
        # The reserved placeholder must have been removed on the failed move.
        self.assertFalse(
            any(name.endswith(".jpg") for name in os.listdir(target_dir))
        )

    def test_heic_verify_failure_records_and_survives_cleanup_error(self):
        """
        A HEIC whose conversion fails verification records a failure and keeps
        the original, even when removing the bad artifact itself errors.
        """
        from tests.fixtures import make_exif_heic

        heic = make_exif_heic(
            os.path.join(self.export, "photo.heic"),
            date_time_original="2024:01:15 14:30:45",
        )
        target_dir = os.path.join(self.backup, "photos")

        with patch.object(
            self.processor.heic_converter, "verify_conversion", return_value=False
        ), patch("os.remove", side_effect=OSError("cannot remove")):
            self.processor._process_single_file(
                heic, FileCategory.PHOTO, target_dir, dry_run=False
            )

        self.assertEqual(len(self.processor._failed_files), 1)
        self.assertEqual(self.processor._failed_files[0][0], "convert_heic")
        # The original HEIC is preserved for the user.
        self.assertTrue(os.path.exists(heic))

    def test_dry_run_heic_reports_simulated_jpeg_name(self):
        """A dry-run HEIC logs the simulated .jpg target without converting."""
        from tests.fixtures import make_exif_heic

        heic = make_exif_heic(
            os.path.join(self.export, "photo.heic"),
            date_time_original="2024:01:15 14:30:45",
        )
        target_dir = os.path.join(self.backup, "photos")

        self.processor._process_single_file(
            heic, FileCategory.PHOTO, target_dir, dry_run=True
        )

        self.assertTrue(os.path.exists(heic))  # untouched
        self.assertEqual(self.processor._processed_files, [])

    def test_heic_conversion_returns_none_records_failure(self):
        """A HEIC whose conversion returns no path is recorded as failed."""
        from tests.fixtures import make_exif_heic

        heic = make_exif_heic(os.path.join(self.export, "photo.heic"))
        target_dir = os.path.join(self.backup, "photos")

        with patch.object(
            self.processor.heic_converter,
            "convert_heic_to_jpeg",
            return_value=None,
        ):
            self.processor._process_single_file(
                heic, FileCategory.PHOTO, target_dir, dry_run=False
            )

        self.assertEqual(len(self.processor._failed_files), 1)
        self.assertEqual(self.processor._failed_files[0][0], "convert_heic")

    def test_dry_run_photo_reports_without_moving(self):
        """A dry-run photo reports its would-be path and moves nothing."""
        photo = make_exif_jpeg(
            os.path.join(self.export, "pic.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        target_dir = os.path.join(self.backup, "photos")

        self.processor._process_single_file(
            photo, FileCategory.PHOTO, target_dir, dry_run=True
        )

        self.assertTrue(os.path.exists(photo))  # untouched
        self.assertFalse(os.path.exists(target_dir))  # nothing created
        self.assertEqual(self.processor._processed_files, [])


class TestQuarantinePaths(unittest.TestCase):
    """Quarantine of undecodable image-typed files: real, dry-run, failure."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)
        self.processor = FileProcessor(self.export, self.backup)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_undecodable_photo_is_quarantined_by_name(self):
        """A corrupt .jpg is moved to backup/corrupt/ under its original name."""
        corrupt = make_corrupt_jpeg(os.path.join(self.export, "broken.jpg"))
        target_dir = os.path.join(self.backup, "photos")

        self.processor._process_single_file(
            corrupt, FileCategory.PHOTO, target_dir, dry_run=False
        )

        quarantine = os.path.join(self.backup, "corrupt", "broken.jpg")
        self.assertTrue(os.path.exists(quarantine))
        self.assertEqual(len(self.processor._quarantined_files), 1)
        self.assertEqual(self.processor._processed_files, [])

    def test_dry_run_quarantine_records_decision_without_moving(self):
        """A dry-run quarantine records the decision but moves nothing."""
        corrupt = make_corrupt_jpeg(os.path.join(self.export, "broken.jpg"))
        target_dir = os.path.join(self.backup, "photos")

        self.processor._process_single_file(
            corrupt, FileCategory.PHOTO, target_dir, dry_run=True
        )

        self.assertTrue(os.path.exists(corrupt))
        self.assertFalse(os.path.exists(os.path.join(self.backup, "corrupt")))
        self.assertEqual(len(self.processor._quarantined_files), 1)

    def test_quarantine_move_failure_is_recorded(self):
        """A failed quarantine move is recorded and the placeholder discarded."""
        corrupt = make_corrupt_jpeg(os.path.join(self.export, "broken.jpg"))
        target_dir = os.path.join(self.backup, "photos")

        with patch("shutil.move", side_effect=OSError("denied")):
            self.processor._process_single_file(
                corrupt, FileCategory.PHOTO, target_dir, dry_run=False
            )

        self.assertEqual(len(self.processor._failed_files), 1)
        self.assertEqual(self.processor._failed_files[0][0], "quarantine")
        corrupt_dir = os.path.join(self.backup, "corrupt")
        self.assertFalse(
            any(name == "broken.jpg" for name in os.listdir(corrupt_dir))
        )

    def test_zero_byte_image_is_undecodable(self):
        """A zero-byte .png is treated as undecodable up front."""
        empty = os.path.join(self.export, "empty.png")
        open(empty, "wb").close()

        self.assertFalse(self.processor._is_decodable_image(empty))

    def test_stat_error_treated_as_undecodable(self):
        """A file that cannot be stat'd is treated as undecodable."""
        with patch("os.path.getsize", side_effect=OSError("gone")):
            self.assertFalse(
                self.processor._is_decodable_image("/tmp/whatever.jpg")
            )


class TestUnknownFilePaths(unittest.TestCase):
    """_process_unknown_file: move, dry-run, collision, and failure."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)
        self.processor = FileProcessor(self.export, self.backup)
        self.unknown_dir = os.path.join(self.backup, "unknown")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unknown_file_moved_under_original_name(self):
        """An unknown file keeps its original name in backup/unknown/."""
        mystery = os.path.join(self.export, "mystery.xyz")
        with open(mystery, "wb") as handle:
            handle.write(b"data")

        self.processor._process_single_file(
            mystery, FileCategory.UNKNOWN, self.unknown_dir, dry_run=False
        )

        self.assertTrue(
            os.path.exists(os.path.join(self.unknown_dir, "mystery.xyz"))
        )
        self.assertEqual(len(self.processor._processed_files), 1)

    def test_unknown_dry_run_moves_nothing(self):
        """A dry-run unknown file logs the target and moves nothing."""
        mystery = os.path.join(self.export, "mystery.xyz")
        open(mystery, "wb").close()

        self.processor._process_unknown_file(
            mystery, self.unknown_dir, dry_run=True
        )

        self.assertTrue(os.path.exists(mystery))
        self.assertFalse(os.path.exists(self.unknown_dir))
        self.assertEqual(self.processor._processed_files, [])

    def test_unknown_name_collision_disambiguated(self):
        """A second same-named unknown file is filed as 'name (1).ext'."""
        os.makedirs(self.unknown_dir)
        # Occupy the natural name first.
        open(os.path.join(self.unknown_dir, "dup.bin"), "wb").close()
        src = os.path.join(self.export, "dup.bin")
        with open(src, "wb") as handle:
            handle.write(b"second")

        self.processor._process_unknown_file(
            src, self.unknown_dir, dry_run=False
        )

        self.assertTrue(
            os.path.exists(os.path.join(self.unknown_dir, "dup (1).bin"))
        )

    def test_unknown_dry_run_collision_reports_bumped_name(self):
        """A dry-run resolves a collision to the disambiguated would-be name."""
        os.makedirs(self.unknown_dir)
        open(os.path.join(self.unknown_dir, "dup.bin"), "wb").close()

        path = self.processor._resolve_named_destination_dry_run(
            self.unknown_dir, "dup.bin"
        )

        self.assertEqual(path, os.path.join(self.unknown_dir, "dup (1).bin"))

    def test_unknown_move_failure_is_recorded(self):
        """A failed unknown-file move is recorded and the placeholder discarded."""
        mystery = os.path.join(self.export, "mystery.xyz")
        open(mystery, "wb").close()

        with patch("shutil.move", side_effect=OSError("nope")):
            self.processor._process_unknown_file(
                mystery, self.unknown_dir, dry_run=False
            )

        self.assertEqual(len(self.processor._failed_files), 1)
        self.assertEqual(self.processor._failed_files[0][0], "move_file")
        self.assertEqual(os.listdir(self.unknown_dir), [])


class TestReserveDestinationCollision(unittest.TestCase):
    """The O_EXCL collision loop in _reserve_destination bumps the second."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.processor = FileProcessor(
            os.path.join(self.temp_dir, "export"),
            os.path.join(self.temp_dir, "backup"),
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_on_disk_collision_bumps_timestamp(self):
        """
        A path already on disk (unknown to the in-memory set) forces the
        reservation to the next second rather than overwriting it.
        """
        target_dir = os.path.join(self.temp_dir, "photos")
        os.makedirs(target_dir)
        ts = datetime(2024, 1, 15, 14, 30, 45)
        # Pre-create the natural name so the first O_EXCL create fails.
        natural = os.path.join(target_dir, "2024.01.15.14.30.45.jpg")
        open(natural, "wb").close()

        adjusted, path = self.processor._reserve_destination(
            target_dir, ts, ".jpg"
        )

        self.assertEqual(adjusted, datetime(2024, 1, 15, 14, 30, 46))
        self.assertEqual(
            path, os.path.join(target_dir, "2024.01.15.14.30.46.jpg")
        )

    def test_reservation_races_on_disk_file_bumps_to_next_second(self):
        """
        A file that appears on disk after the name set was seeded forces the
        O_EXCL create to lose the race and bump to the next free second.
        """
        target_dir = os.path.join(self.temp_dir, "photos")
        os.makedirs(target_dir)
        ts = datetime(2024, 1, 15, 14, 30, 45)

        # First reservation seeds the per-directory set and claims :45.
        _, first = self.processor._reserve_destination(target_dir, ts, ".jpg")
        self.assertTrue(first.endswith("2024.01.15.14.30.45.jpg"))

        # Create :46 directly on disk WITHOUT informing the in-memory set, so
        # the next reservation's O_EXCL create hits FileExistsError and bumps.
        sneaky = os.path.join(target_dir, "2024.01.15.14.30.46.jpg")
        open(sneaky, "wb").close()

        adjusted, path = self.processor._reserve_destination(
            target_dir, ts, ".jpg"
        )

        # :45 taken in-memory -> bump to :46 -> occupied on disk -> bump to :47.
        self.assertEqual(adjusted, datetime(2024, 1, 15, 14, 30, 47))
        self.assertTrue(path.endswith("2024.01.15.14.30.47.jpg"))

    def test_discard_reservation_ignores_missing_path(self):
        """Discarding a nonexistent reservation is a silent no-op."""
        # Should not raise even though the path was never created.
        self.processor._discard_reservation(
            os.path.join(self.temp_dir, "never.jpg")
        )


class TestGenerateSummaryIndependence(unittest.TestCase):
    """``_generate_summary``'s returned lists are independent snapshots (#37).

    Pins acceptance criterion 3 of issue #37: a caller that mutates
    ``processed_files`` / ``failed_files`` / ``conversion_log`` from a summary
    dict must not affect the processor's own tracked state, nor should a
    later mutation of the processor's internal lists reach back into an
    already-returned summary.
    """

    def test_returned_lists_do_not_alias_internal_state(self):
        processor = FileProcessor("export", "backup")
        processor._processed_files.append({"path": "a.jpg"})
        processor._failed_files.append(("move_file", "b.jpg", "boom"))
        processor._conversion_log.append(("c.heic", "c.jpg"))

        summary = processor._generate_summary()

        self.assertIsNot(summary['processed_files'], processor._processed_files)
        self.assertIsNot(summary['failed_files'], processor._failed_files)
        self.assertIsNot(summary['conversion_log'], processor._conversion_log)

        # Caller mutates what it received back.
        summary['processed_files'].clear()
        summary['failed_files'].append(("extra", "x.jpg", "not real"))
        summary['conversion_log'].clear()

        # The processor's own tracked state must be untouched.
        self.assertEqual(processor._processed_files, [{"path": "a.jpg"}])
        self.assertEqual(processor._failed_files, [("move_file", "b.jpg", "boom")])
        self.assertEqual(processor._conversion_log, [("c.heic", "c.jpg")])

    def test_later_internal_mutation_does_not_reach_earlier_summary(self):
        processor = FileProcessor("export", "backup")
        processor._processed_files.append({"path": "a.jpg"})

        first_summary = processor._generate_summary()

        # Simulate a later run appending more processed files on the SAME
        # processor instance (no full clear_processing_state in between).
        processor._processed_files.append({"path": "b.jpg"})

        self.assertEqual(
            first_summary['processed_files'], [{"path": "a.jpg"}],
            "a later mutation of processor._processed_files leaked into an "
            "already-returned summary",
        )


class TestClearProcessingState(unittest.TestCase):
    """clear_processing_state resets every tracked collection."""

    def test_clear_resets_all_state(self):
        """Every processing collection is emptied by clear_processing_state."""
        processor = FileProcessor("export", "backup")
        processor._processed_files.append({"x": 1})
        processor._failed_files.append(("op", "f", "e"))
        processor._quarantined_files.append({"x": 1})
        processor._conversion_log.append(("a", "b"))
        processor._used_timestamps["d"] = {"stem"}
        processor._missing_exif_records.append(
            {"original_path": "a.jpg", "final_path": "backup/photos/a.jpg"}
        )
        # Issue #57's two new accumulators: a reset that dropped either of
        # these would let a second run on the same instance silently report
        # the first run's sidecar deletions/skips.
        processor._deleted_sidecars.append("export/a.aae")
        processor._skipped_sidecars.append(("not_plist", "export/notes.aae"))
        processor.exif_handler.missing_exif_files.append("f")
        processor.heic_converter.converted_files.append(("a", "b"))

        processor.clear_processing_state()

        self.assertEqual(processor._processed_files, [])
        self.assertEqual(processor._failed_files, [])
        self.assertEqual(processor._quarantined_files, [])
        self.assertEqual(processor._conversion_log, [])
        self.assertEqual(processor._used_timestamps, {})
        self.assertEqual(processor._missing_exif_records, [])
        self.assertEqual(processor._deleted_sidecars, [])
        self.assertEqual(processor._skipped_sidecars, [])
        self.assertEqual(processor.exif_handler.missing_exif_files, [])
        self.assertEqual(processor.heic_converter.converted_files, [])


if __name__ == "__main__":
    unittest.main()
