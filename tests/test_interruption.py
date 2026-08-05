"""
Interruption and partial-failure resilience tests (issue #46).

``FileProcessor`` deletes originals, so how it behaves when a run is cut short
matters as much as the happy path. This module pins the pieces of issue #46 that
no other test module already asserts:

* **``KeyboardInterrupt`` propagates through ``_process_category``.** The loop
  in :meth:`FileProcessor._process_category` catches ``except Exception`` -- and
  ``KeyboardInterrupt`` is a ``BaseException``, not an ``Exception``, so a
  Ctrl-C raised mid-batch escapes the per-file loop and unwinds the run rather
  than being swallowed and demoted to a per-file skip. This is load-bearing: if
  that ``except Exception`` were ever "cleaned up" to a bare ``except:`` (or
  ``except BaseException``), Ctrl-C would silently become a per-file skip that
  grinds through the entire library while the user holds the key down. The
  headline test here fails the instant that mutation is made.
* **``ENOSPC`` keeps trying.** A full disk is not a per-file condition, yet the
  loop retries every remaining file and records each as a failure rather than
  aborting. This pins the *current* behavior (the finding in #46 is that it is
  unasserted, not that it is wrong).
* **A file that vanishes after the scan is absorbed without crashing.** A file
  deleted between the scan and processing is skipped by
  ``batch_categorize`` -- the run completes and does not raise.

Scenarios already pinned elsewhere are intentionally *not* duplicated here:
one-file ``PermissionError`` partial failure lives in
``test_integration.py::test_error_handling_workflow`` and
``test_file_processor_coverage.py::TestMoveFailureRecording``;
``clear_processing_state`` fully resetting lives in
``test_integration.py::test_processing_state_cleanup`` and
``test_file_processor_coverage.py::TestClearProcessingState``; and the
top-level ``main.py`` ``KeyboardInterrupt`` handling lives in
``test_main.py::test_keyboard_interrupt_is_cancelled``.

All filesystem I/O happens under ``tempfile.mkdtemp`` -- these tests never touch
a real ``export/`` or ``backup/``.
"""

import errno
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.file_processor import FileProcessor
from src.file_categorizer import FileCategory
from tests.fixtures import make_exif_jpeg


class TestKeyboardInterruptPropagation(unittest.TestCase):
    """A Ctrl-C mid-batch must escape the per-file loop, not be swallowed."""

    def setUp(self):
        """Create an isolated temp export/backup with five real photos."""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)

        # Five real JPEGs with distinct capture seconds so each resolves to its
        # own timestamped destination name -- no collision bookkeeping muddies
        # which file landed where.
        self.photo_names = [f"IMG_{index}.jpg" for index in range(5)]
        for index, name in enumerate(self.photo_names):
            make_exif_jpeg(
                os.path.join(self.export_dir, name),
                date_time_original=f"2024:01:15 14:30:4{index}",
            )

        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        """Remove the temporary tree."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_keyboard_interrupt_propagates_out_of_process_all_files(self):
        """Ctrl-C on the third file unwinds the run; it is NOT swallowed.

        The real per-file worker runs for the first two files (which genuinely
        move to ``backup/``), then the third invocation raises
        ``KeyboardInterrupt`` *before* doing any work. Because
        ``_process_category`` catches ``except Exception`` and not
        ``BaseException``, the interrupt propagates all the way out of
        ``process_all_files``.

        This is the regression guard on that ``except Exception``: changing it
        to a bare ``except:`` (or ``except BaseException``) would swallow the
        interrupt, let the loop continue to files four and five, and make
        ``process_all_files`` return normally -- failing ``assertRaises`` here.
        """
        real_process_single_file = self.processor._process_single_file
        call_count = {"n": 0}

        def interrupt_on_third(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 3:
                # Simulate the user hitting Ctrl-C at the top of the third
                # file's processing, before it is touched.
                raise KeyboardInterrupt("user pressed ctrl-c")
            return real_process_single_file(*args, **kwargs)

        with patch.object(
            self.processor, "_process_single_file", side_effect=interrupt_on_third
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.processor.process_all_files(dry_run=False)

        # Exactly the two files processed before the interrupt already landed in
        # backup/, and no more.
        photos_dir = os.path.join(self.backup_dir, "photos")
        landed = [
            name for name in os.listdir(photos_dir) if name.endswith(".jpg")
        ] if os.path.isdir(photos_dir) else []
        self.assertEqual(
            len(landed), 2, f"expected two files moved before the interrupt, saw {landed}"
        )

        # The remaining three files -- the interrupted one plus the two never
        # reached -- are still sitting in export/. Nothing was lost.
        remaining = sorted(os.listdir(self.export_dir))
        self.assertEqual(
            len(remaining), 3, f"expected three files left in export, saw {remaining}"
        )

        # Conservation: every original is accounted for as either landed or
        # still in export -- the interrupt destroyed nothing.
        self.assertEqual(len(landed) + len(remaining), len(self.photo_names))

        # The interrupt was NOT recorded as a per-file failure -- it escaped the
        # loop rather than being demoted to a swallowed skip.
        self.assertEqual(self.processor._failed_files, [])

    def test_interrupt_propagates_from_process_category_directly(self):
        """Driving ``_process_category`` alone still lets the interrupt escape.

        A tighter guard on the same ``except Exception``: even with the
        surrounding ``process_all_files`` machinery removed, the per-file loop
        must let ``KeyboardInterrupt`` through. The first file lands; the second
        raises; files after it are never attempted.
        """
        target_dir = os.path.join(self.backup_dir, "photos")
        # process_all_files ensures this via ensure_target_directories before
        # any file is placed (issue #23 removed the redundant per-file
        # os.makedirs that used to paper over its absence here, since
        # _process_category is being driven directly, bypassing that step).
        os.makedirs(target_dir)
        files = [os.path.join(self.export_dir, name) for name in self.photo_names]

        real_process_single_file = self.processor._process_single_file
        call_count = {"n": 0}

        def interrupt_on_second(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise KeyboardInterrupt("user pressed ctrl-c")
            return real_process_single_file(*args, **kwargs)

        with patch.object(
            self.processor, "_process_single_file", side_effect=interrupt_on_second
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.processor._process_category(
                    FileCategory.PHOTO, files, dry_run=False
                )

        # Only the first file's worker ran to completion; the loop stopped at
        # the interrupt rather than continuing through files three, four, five.
        self.assertEqual(call_count["n"], 2)
        self.assertEqual(self.processor._failed_files, [])


class TestDiskFullKeepsTrying(unittest.TestCase):
    """A full disk from file N onward records every remaining file as failed."""

    def setUp(self):
        """Create an isolated temp export/backup with five real photos."""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)

        self.photo_names = [f"IMG_{index}.jpg" for index in range(5)]
        for index, name in enumerate(self.photo_names):
            make_exif_jpeg(
                os.path.join(self.export_dir, name),
                date_time_original=f"2024:02:20 10:11:1{index}",
            )

        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        """Remove the temporary tree."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_enospc_from_first_move_fails_every_file_without_aborting(self):
        """Every move hits ENOSPC -> every file recorded failed, run completes.

        ``shutil.move`` is patched where ``file_processor`` looks it up so that
        every move raises ``OSError(ENOSPC)``, the way a full disk surfaces. The
        run does not abort on the first failure: it keeps trying all five files,
        records each in ``failed_files`` with operation ``move_file``, and still
        returns a summary. Nothing lands in ``backup/`` and every original stays
        in ``export/``.
        """
        enospc = OSError(errno.ENOSPC, "No space left on device")

        def full_disk(*args, **kwargs):
            raise enospc

        with patch("src.file_processor.shutil.move", side_effect=full_disk):
            results = self.processor.process_all_files(dry_run=False)

        # The run completed (returned a summary) rather than propagating the
        # OSError, and every file was attempted and recorded as a failure.
        self.assertEqual(results["files_processed"], 0)
        self.assertEqual(results["files_failed"], len(self.photo_names))
        self.assertEqual(len(results["failed_files"]), len(self.photo_names))
        for operation, _path, _message in results["failed_files"]:
            self.assertEqual(operation, "move_file")

        # No file landed; every original is still in export.
        photos_dir = os.path.join(self.backup_dir, "photos")
        landed = [
            name for name in os.listdir(photos_dir) if name.endswith(".jpg")
        ] if os.path.isdir(photos_dir) else []
        self.assertEqual(landed, [])
        self.assertEqual(len(os.listdir(self.export_dir)), len(self.photo_names))

    def test_enospc_from_second_file_still_attempts_all_remaining(self):
        """Disk fills after the first file -> the rest are each tried and fail.

        Proves the loop does not stop at the first ENOSPC: the first file lands,
        then the disk "fills" and the four remaining files are every one
        attempted and recorded failed. On a real library that is thousands of
        doomed attempts -- the behavior #46 documents and this pins.
        """
        real_move = shutil.move
        move_calls = {"n": 0}

        def fills_after_first(src, dst, *args, **kwargs):
            move_calls["n"] += 1
            if move_calls["n"] == 1:
                return real_move(src, dst, *args, **kwargs)
            raise OSError(errno.ENOSPC, "No space left on device")

        with patch("src.file_processor.shutil.move", side_effect=fills_after_first):
            results = self.processor.process_all_files(dry_run=False)

        # One landed, four each attempted and failed -- the loop kept going.
        self.assertEqual(results["files_processed"], 1)
        self.assertEqual(results["files_failed"], len(self.photo_names) - 1)
        # shutil.move was invoked once per file: the loop never short-circuited.
        self.assertEqual(move_calls["n"], len(self.photo_names))


class TestVanishingFileAfterScan(unittest.TestCase):
    """A file deleted between scan and processing is absorbed without crashing."""

    def setUp(self):
        """Create an isolated temp export/backup with five real photos."""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)

        self.photo_names = [f"IMG_{index}.jpg" for index in range(5)]
        for index, name in enumerate(self.photo_names):
            make_exif_jpeg(
                os.path.join(self.export_dir, name),
                date_time_original=f"2024:03:10 08:09:0{index}",
            )

        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        """Remove the temporary tree."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_file_deleted_after_scan_is_skipped_run_completes(self):
        """A file removed post-scan is skipped by categorization, no crash.

        The scan is wrapped so it returns the full five-file list but one of
        those paths is unlinked from disk *after* the scan and *before*
        categorization. ``batch_categorize`` logs a warning and skips the
        missing path, so the run completes over the four survivors without
        raising. This pins the current behavior: a vanished file is neither
        processed nor recorded as failed -- it is silently absorbed.
        """
        vanishing = os.path.join(self.export_dir, self.photo_names[2])
        real_scan = self.processor._scan_export_directory

        def scan_then_delete_one():
            found = real_scan()
            # The user (or the OS) removes a file in the window between the scan
            # and the processing loop.
            os.remove(vanishing)
            return found

        with patch.object(
            self.processor,
            "_scan_export_directory",
            side_effect=scan_then_delete_one,
        ):
            results = self.processor.process_all_files(dry_run=False)

        # The run did not crash and drained the four survivors.
        self.assertEqual(results["files_processed"], 4)
        # The vanished file is genuinely gone and was not resurrected.
        self.assertFalse(os.path.exists(vanishing))

        # The four survivors all landed in backup/photos/.
        photos_dir = os.path.join(self.backup_dir, "photos")
        landed = [name for name in os.listdir(photos_dir) if name.endswith(".jpg")]
        self.assertEqual(len(landed), 4)


if __name__ == "__main__":
    unittest.main()
