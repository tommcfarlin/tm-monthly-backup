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


if __name__ == "__main__":
    unittest.main()
