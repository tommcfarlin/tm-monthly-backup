"""
Tests for removing the redundant per-file ``os.makedirs`` call (issue #23).

``_process_single_file``'s real-run branch called
``os.makedirs(target_dir, exist_ok=True)`` for every single file it placed,
but ``process_all_files`` already calls
``FileCategorizer.ensure_target_directories`` once, up front, which creates
all four recognized-category directories (``photos``, ``videos``,
``screenshots``, ``generated``) before any file is processed. Every one of
those per-file calls was therefore a guaranteed no-op syscall: the directory
already existed, so ``exist_ok=True`` just made the kernel confirm that and
return.

These tests instrument the REAL ``os.makedirs`` (not a mock of any of this
project's own helpers), so a regression that reintroduces a per-file call at
the real filesystem boundary is caught regardless of which internal method it
is added to. ``backup_dir`` itself is pre-created in ``setUp`` so the counts
below are not muddied by ``os.makedirs``'s own recursive parent-creation --
CPython's real implementation recurses into the (patched) global ``makedirs``
name to create a missing parent, which would otherwise add an extra observed
call unrelated to anything this project's code does.

Confirmed to fail against the pre-fix code and pass against the fix by
stashing the change to ``src/file_processor.py`` and rerunning this file --
see the task report for the exact counts observed (7 calls before, 4 after,
for three photos across four ensured categories).
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.file_processor import FileProcessor
from tests.fixtures import make_exif_jpeg


class TestMakedirsCalledOncePerCategoryNotPerFile(unittest.TestCase):
    """Acceptance criterion 1: os.makedirs called once per category per run."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)
        # Pre-create backup_dir itself (see module docstring): otherwise
        # os.makedirs's own recursive parent-creation would add an extra
        # observed call the first time a category subdir is created, which
        # has nothing to do with this project's code.
        os.makedirs(self.backup_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_three_photos_call_makedirs_exactly_four_times(self):
        """4 ensured category dirs, 0 additional per-file calls.

        Before the fix: 4 calls from ``ensure_target_directories`` (photos,
        videos, screenshots, generated) PLUS 1 more per photo from
        ``_process_single_file`` -- 7 total for 3 photos. After the fix: the
        4 up-front calls only.
        """
        for index in range(3):
            make_exif_jpeg(
                os.path.join(self.export_dir, f"photo{index}.jpg"),
                date_time_original=f"2024:01:0{index + 1} 01:01:01",
            )
        processor = FileProcessor(self.export_dir, self.backup_dir)

        with patch("os.makedirs", wraps=os.makedirs) as spy:
            results = processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_processed"], 3)
        self.assertEqual(
            spy.call_count, 4,
            f"expected exactly 4 os.makedirs calls (one per ensured "
            f"category, none per file), got {spy.call_count}: "
            f"{[c.args for c in spy.call_args_list]}",
        )

    def test_one_photo_calls_makedirs_exactly_four_times_not_five(self):
        """Smallest case: even a single file must not add a 5th call."""
        make_exif_jpeg(
            os.path.join(self.export_dir, "solo.jpg"),
            date_time_original="2024:05:06 07:08:09",
        )
        processor = FileProcessor(self.export_dir, self.backup_dir)

        with patch("os.makedirs", wraps=os.makedirs) as spy:
            results = processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_processed"], 1)
        self.assertEqual(spy.call_count, 4)

    def test_file_still_lands_correctly_without_the_per_file_makedirs(self):
        """The removed call must not be load-bearing: the file still lands."""
        make_exif_jpeg(
            os.path.join(self.export_dir, "solo.jpg"),
            date_time_original="2024:05:06 07:08:09",
        )
        processor = FileProcessor(self.export_dir, self.backup_dir)

        results = processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_processed"], 1)
        landed = os.path.join(self.backup_dir, "photos", "2024.05.06.07.08.09.jpg")
        self.assertTrue(os.path.isfile(landed))


class TestDryRunStillCreatesNoDirectories(unittest.TestCase):
    """
    Acceptance criterion 2: dry-run behavior is unchanged and still creates
    no directories.

    This invariant already held before this fix -- the dry-run branch of
    ``_process_single_file`` never called ``os.makedirs`` in the first place,
    only the real-run branch (the one this fix touches) did. This test is
    therefore REGRESSION COVERAGE locking in a pre-existing property, not
    proof that this fix changed dry-run behavior; it would pass against both
    the old and the new code.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)
        # Deliberately NOT pre-creating backup_dir here (unlike the sibling
        # class above): the assertion below is that it never gets created at
        # all during a dry run, so its absence beforehand is the point.

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_dry_run_creates_no_backup_directory_at_all(self):
        make_exif_jpeg(
            os.path.join(self.export_dir, "photo.jpg"),
            date_time_original="2024:01:01 01:01:01",
        )
        processor = FileProcessor(self.export_dir, self.backup_dir)

        with patch("os.makedirs", wraps=os.makedirs) as spy:
            results = processor.process_all_files(dry_run=True)

        # A dry run never actually files anything, so files_processed stays 0
        # (issue #10 dry-run/real-run parity: this counts what a REAL run
        # would touch, not this plan-only pass) -- files_failed likewise 0
        # confirms the file was at least seen and planned, not skipped/errored.
        self.assertEqual(results["files_processed"], 0)
        self.assertEqual(results["files_failed"], 0)
        spy.assert_not_called()
        self.assertFalse(os.path.exists(self.backup_dir))


class TestAbsentTargetDirectoryRecordsRealCause(unittest.TestCase):
    """
    Fix round 1 regression test: an absent ``target_dir`` must be recorded as
    a ``move_file`` failure carrying the real ``FileNotFoundError``, not an
    ``UnboundLocalError`` raised out of the ``except`` handler itself.

    Removing the per-file ``os.makedirs`` (this issue) made "target directory
    absent" a live way to reach ``_process_single_file``'s ``except Exception
    as e:`` handler with ``target_path`` never assigned -- that handler
    references ``target_path`` (for the log message), which was previously
    bound unconditionally by the removed call's side effect (every prior
    caller went through ``ensure_target_directories`` too, so in practice this
    path was never actually exercised, but the local variable's binding
    depended on it regardless). ``target_path = None`` is now bound before the
    ``try:`` -- mirroring ``_process_unknown_file``/``_quarantine_file``,
    which already did this -- so the handler can always reference it.

    Drives ``_process_category`` directly, deliberately never creating (or
    removing) ``target_dir``, mirroring how the reviewer reproduced this.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)
        # Deliberately NOT creating self.backup_dir or any category
        # subdirectory -- that absence is exactly what is under test.

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_missing_target_dir_records_move_file_failure_with_real_cause(self):
        from src.file_categorizer import FileCategory

        photo = make_exif_jpeg(
            os.path.join(self.export_dir, "photo.jpg"),
            date_time_original="2024:01:01 01:01:01",
        )
        target_dir = os.path.join(self.backup_dir, "photos")
        processor = FileProcessor(self.export_dir, self.backup_dir)

        # Drive the category loop directly -- process_all_files is not
        # involved, so ensure_target_directories never runs and target_dir
        # genuinely does not exist on disk.
        processor._process_category(FileCategory.PHOTO, [photo], dry_run=False)

        self.assertEqual(len(processor._failed_files), 1)
        kind, path, error = processor._failed_files[0]
        self.assertEqual(kind, "move_file")
        self.assertEqual(path, photo)
        # The exception object itself is stored, not a stringified message
        # (issue #39), so the type survives to render time.
        self.assertIsInstance(error, FileNotFoundError)
        # The real cause -- not the UnboundLocalError that leaked out of the
        # except handler itself before this fix.
        message = str(error)
        self.assertIn("No such file or directory", message)
        self.assertNotIn("target_path", message)
        self.assertNotIn("cannot access local variable", message)

        # No data lost: the original is still sitting in export/ untouched.
        self.assertTrue(os.path.isfile(photo))
        self.assertFalse(os.path.isdir(target_dir))

    def test_missing_target_dir_through_real_entry_point_mid_run(self):
        """
        Same failure mode, reproduced through the real public entry point:
        the category directory is deleted between two files' placements, so
        whichever of the two is placed SECOND hits the absent-directory path
        while the other succeeds normally.

        Deliberately does not assume which of the two files the categorizer
        hands to ``_process_category`` first -- directory enumeration order
        (``os.walk``) is not guaranteed to match creation order on every
        filesystem, and the property under test (one succeeds, one fails with
        the REAL cause, nothing is lost) holds regardless of which is which.
        """
        photo_a = make_exif_jpeg(
            os.path.join(self.export_dir, "photo_a.jpg"),
            date_time_original="2024:01:01 01:01:01",
        )
        photo_b = make_exif_jpeg(
            os.path.join(self.export_dir, "photo_b.jpg"),
            date_time_original="2024:01:02 02:02:02",
        )
        processor = FileProcessor(self.export_dir, self.backup_dir)

        real_process_single_file = processor._process_single_file
        photos_dir = os.path.join(self.backup_dir, "photos")
        call_count = {"n": 0}

        def delete_dir_before_second_call(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                shutil.rmtree(photos_dir)
            return real_process_single_file(*args, **kwargs)

        with patch.object(
            processor,
            "_process_single_file",
            side_effect=delete_dir_before_second_call,
        ):
            results = processor.process_all_files(dry_run=False)

        self.assertEqual(call_count["n"], 2)
        self.assertEqual(results["files_processed"], 1)
        self.assertEqual(results["files_failed"], 1)
        kind, failed_path, error = processor._failed_files[0]
        self.assertEqual(kind, "move_file")
        self.assertIn(failed_path, {photo_a, photo_b})
        self.assertIsInstance(error, FileNotFoundError)
        message = str(error)
        self.assertIn("No such file or directory", message)
        self.assertNotIn("cannot access local variable", message)

        # No data lost for the failed file specifically: its original is
        # still sitting in export/ at its original path -- the move never
        # happened, so nothing was destroyed by the failure itself. (The
        # succeeded file's original is correctly gone from export/ -- it was
        # moved before photos_dir was rmtree'd out from under the run; that
        # rmtree deleting the already-landed copy along with the directory is
        # an external action outside the tool's control, not something this
        # test needs -- or is able -- to guard against.)
        self.assertTrue(
            os.path.isfile(failed_path),
            "the failed file's original must remain in export/, untouched",
        )


if __name__ == "__main__":
    unittest.main()
