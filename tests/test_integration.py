"""
Integration tests for the complete file processing workflow
"""

import unittest
import tempfile
import os
import shutil
import json
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, MagicMock

from src.file_processor import FileProcessor
from src.file_categorizer import FileCategory
from src.cli_interface import CLIInterface
from tests.fixtures import make_exif_heic, make_exif_jpeg, make_no_exif_jpeg


class TestWorkflowIntegration(unittest.TestCase):
    """Integration tests for complete file processing workflow"""

    def setUp(self):
        """Set up test environment with temporary directories"""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")

        # Create export directory
        os.makedirs(self.export_dir, exist_ok=True)

        # Initialize file processor
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        """Clean up test environment"""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_test_file(self, filename: str, subdir: str = None) -> str:
        """
        Create a test file in export directory.

        Args:
            filename: Name of file to create
            subdir: Optional subdirectory within export

        Returns:
            Full path to created file
        """
        if subdir:
            target_dir = os.path.join(self.export_dir, subdir)
            os.makedirs(target_dir, exist_ok=True)
            file_path = os.path.join(target_dir, filename)
        else:
            file_path = os.path.join(self.export_dir, filename)

        # Create file with some content
        with open(file_path, 'w') as f:
            f.write("test file content")

        return file_path

    def create_test_image_with_exif(self, filename: str, timestamp_str: str = "2024:01:15 14:30:45") -> str:
        """
        Create a real JPEG in the export directory with a genuine EXIF timestamp.

        The timestamp is written into the Exif sub-IFD (0x8769) as
        DateTimeOriginal, exactly the way a camera lays it out, via the shared
        self-verifying fixture builder. Unlike the previous version, the file it
        produces actually carries EXIF the tool can extract.

        Args:
            filename: Name of image file to create.
            timestamp_str: EXIF DateTimeOriginal string ("YYYY:MM:DD HH:MM:SS").

        Returns:
            Full path to created image.
        """
        return make_exif_jpeg(
            os.path.join(self.export_dir, filename),
            date_time_original=timestamp_str,
        )

    def create_test_image_without_exif(self, filename: str) -> str:
        """
        Create a real JPEG in the export directory that carries no EXIF data.

        Exercises the missing-EXIF / filesystem-fallback path with genuine image
        bytes rather than a text file renamed to ``.jpg``.

        Args:
            filename: Name of image file to create.

        Returns:
            Full path to created image.
        """
        return make_no_exif_jpeg(os.path.join(self.export_dir, filename))

    def test_empty_export_directory(self):
        """Test processing when export directory is empty"""
        results = self.processor.process_all_files(dry_run=True)

        self.assertEqual(results['files_processed'], 0)
        self.assertEqual(results['files_failed'], 0)
        self.assertEqual(results['categorization_stats']['total'], 0)

    def test_single_photo_processing(self):
        """A single photo is renamed by its EXIF timestamp and lands in photos/.

        A real (non-dry) run must move the file to
        ``backup/photos/<EXIF-timestamp>.jpg`` and record that destination -- not
        merely count it. This fails if the move is skipped or the timestamp
        formatting breaks.
        """
        self.create_test_image_with_exif("test_photo.jpg", "2024:01:15 14:30:45")

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results['categorization_stats']['photos'], 1)
        self.assertEqual(results['categorization_stats']['total'], 1)
        self.assertEqual(results['files_processed'], 1)

        landed = os.path.join(self.backup_dir, "photos", "2024.01.15.14.30.45.jpg")
        self.assertTrue(os.path.isfile(landed), f"photo did not land at {landed}")
        # The source was moved, not copied.
        self.assertFalse(
            os.path.exists(os.path.join(self.export_dir, "test_photo.jpg"))
        )
        # The processed-file record names the real destination and category.
        record = results['processed_files'][0]
        self.assertEqual(record['final_path'], landed)
        self.assertEqual(record['category'], 'photos')

    def test_mixed_file_types_processing(self):
        """Every file type lands in its own backup dir; sidecars go, unknown routed.

        A real run must actually route each processable file into its category
        directory, delete sidecars, and file unknown files into backup/unknown/
        under their original name (issue #29) -- not merely tally categorization
        counts.
        """
        self.create_test_image_with_exif("photo1.jpg", "2024:01:15 14:30:45")
        self.create_test_image_with_exif("photo2.jpeg", "2024:02:20 10:11:12")
        self.create_test_file("video1.mov")
        self.create_test_file("video2.mp4")
        self.create_test_file("Screenshot 2024-01-15.png")
        self.create_test_file("IMG_1234.png")  # iOS screenshot pattern
        self.create_test_file("sidecar1.aae")
        self.create_test_file("sidecar2.aae")
        self.create_test_file("unknown.txt")

        results = self.processor.process_all_files(dry_run=False)

        # Verify categorization
        stats = results['categorization_stats']
        self.assertEqual(stats['photos'], 2)  # jpg, jpeg
        self.assertEqual(stats['videos'], 2)  # mov, mp4
        self.assertEqual(stats['screenshots'], 2)  # PNG screenshots
        self.assertEqual(stats['sidecar'], 2)  # aae files
        self.assertEqual(stats['unknown'], 1)  # txt file
        self.assertEqual(stats['total'], 9)

        # Real landing: each processable category directory holds exactly its
        # files. Non-photo fixtures fall back to filesystem/filename timestamps,
        # so their exact names are not asserted -- only that they arrived.
        def _count(category: str) -> int:
            path = os.path.join(self.backup_dir, category)
            return len(os.listdir(path)) if os.path.isdir(path) else 0

        self.assertEqual(_count("photos"), 2)
        self.assertEqual(_count("videos"), 2)
        self.assertEqual(_count("screenshots"), 2)
        self.assertEqual(_count("unknown"), 1)  # unknown.txt routed here
        self.assertEqual(results['files_processed'], 7)  # 2+2+2 + 1 unknown

        # The two EXIF photos are renamed to their exact DateTimeOriginal values.
        self.assertTrue(os.path.isfile(
            os.path.join(self.backup_dir, "photos", "2024.01.15.14.30.45.jpg")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.backup_dir, "photos", "2024.02.20.10.11.12.jpeg")))

        # Sidecars deleted from export; the unknown file is routed to
        # backup/unknown/ under its original name and no longer sits in export.
        self.assertFalse(os.path.exists(os.path.join(self.export_dir, "sidecar1.aae")))
        self.assertFalse(os.path.exists(os.path.join(self.export_dir, "sidecar2.aae")))
        self.assertFalse(os.path.exists(os.path.join(self.export_dir, "unknown.txt")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.backup_dir, "unknown", "unknown.txt")))

    def test_sidecar_file_deletion_dry_run(self):
        """Test that sidecar files are identified for deletion in dry run"""
        # Create sidecar files
        sidecar_files = [
            self.create_test_file("IMG_1234.aae"),
            self.create_test_file("photo.aae")
        ]

        results = self.processor.process_all_files(dry_run=True)

        # In dry run, files should be identified but not deleted
        self.assertEqual(results['categorization_stats']['sidecar'], 2)

        # Files should still exist (dry run doesn't delete)
        for file_path in sidecar_files:
            self.assertTrue(os.path.exists(file_path))

    @patch('src.file_processor.os.remove')
    def test_sidecar_file_deletion_real(self, mock_remove):
        """Test actual sidecar file deletion (mocked)"""
        # Create sidecar files
        sidecar_files = [
            self.create_test_file("IMG_1234.aae"),
            self.create_test_file("photo.aae")
        ]

        results = self.processor.process_all_files(dry_run=False)

        # Verify deletion was attempted
        self.assertEqual(mock_remove.call_count, 2)
        for file_path in sidecar_files:
            mock_remove.assert_any_call(file_path)

    def test_duplicate_timestamp_handling(self):
        """Three photos sharing one EXIF timestamp land at .45/.46/.47 distinctly.

        This exercises the real collision machinery end-to-end: three genuine
        fixtures carrying the SAME DateTimeOriginal, processed for real, must
        produce three distinct files at consecutive-second landing paths -- none
        overwritten. Making ``handle_duplicate_timestamp`` the identity function
        collapses them onto one path and fails this test.
        """
        for name in ("photo1.jpg", "photo2.jpg", "photo3.jpg"):
            self.create_test_image_with_exif(name, "2024:01:15 14:30:45")

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results['categorization_stats']['photos'], 3)
        self.assertEqual(results['files_processed'], 3)

        photos_dir = os.path.join(self.backup_dir, "photos")
        landed = set(os.listdir(photos_dir))
        self.assertEqual(
            landed,
            {
                "2024.01.15.14.30.45.jpg",
                "2024.01.15.14.30.46.jpg",
                "2024.01.15.14.30.47.jpg",
            },
            f"collision resolution did not yield three distinct files: {landed}",
        )

    def test_duplicate_timestamp_across_categories(self):
        """A photo and a screenshot sharing a timestamp both keep it (#21).

        Collision tracking is scoped per target directory, so a screenshot that
        resolves to the same second as an already-placed photo is NOT bumped:
        the two land in different directories and can never collide on disk.
        Both keep their true ``.45`` timestamp. Before the #21 fix the shared
        global set spuriously pushed the screenshot to ``.46``.
        """
        self.create_test_image_with_exif("IMG_9999.jpg", "2024:01:15 14:30:45")
        # The screenshot carries no EXIF; its filename yields the same 14:30:45.
        self.create_test_file("Screenshot 2024-01-15-14-30-45.png")

        self.processor.process_all_files(dry_run=False)

        self.assertTrue(
            os.path.isfile(os.path.join(
                self.backup_dir, "photos", "2024.01.15.14.30.45.jpg")),
            "photo should keep the original .45 slot",
        )
        self.assertTrue(
            os.path.isfile(os.path.join(
                self.backup_dir, "screenshots", "2024.01.15.14.30.45.png")),
            "screenshot must keep its true .45 timestamp (different directory)",
        )
        # And the spurious cross-category bump is gone entirely.
        self.assertFalse(
            os.path.exists(os.path.join(
                self.backup_dir, "screenshots", "2024.01.15.14.30.46.png")),
            "screenshot must not be bumped by a photo in another directory",
        )

    def test_error_handling_workflow(self):
        """A real move failure is captured while the other files still land.

        Rather than mocking ``_process_single_file`` away (which would test only
        the surrounding try/except), this induces a genuine failure by making the
        real ``shutil.move`` raise ``PermissionError`` for one specific target.
        The run must continue, the good file must land, and the failure tuple
        must name the right file and operation.
        """
        self.create_test_image_with_exif("good.jpg", "2024:01:15 14:30:45")
        self.create_test_image_with_exif("bad.jpg", "2024:02:20 10:11:12")

        real_move = shutil.move

        def failing_move(src, dst, *args, **kwargs):
            if os.path.basename(dst) == "2024.02.20.10.11.12.jpg":
                raise PermissionError("simulated read-only target")
            return real_move(src, dst, *args, **kwargs)

        with patch('src.file_processor.shutil.move', side_effect=failing_move):
            results = self.processor.process_all_files(dry_run=False)

        # Exactly one file failed, and it is recorded with op + path.
        self.assertEqual(results['files_failed'], 1)
        self.assertEqual(len(results['failed_files']), 1)
        operation, failed_path, _message = results['failed_files'][0]
        self.assertEqual(operation, 'move_file')
        self.assertTrue(
            failed_path.endswith("bad.jpg"),
            f"failure tuple named the wrong file: {failed_path}",
        )

        # The good file still processed and landed despite the sibling failure.
        self.assertEqual(results['files_processed'], 1)
        self.assertTrue(os.path.isfile(os.path.join(
            self.backup_dir, "photos", "2024.01.15.14.30.45.jpg")))
        # The failed file did not land anywhere in backup.
        self.assertFalse(os.path.exists(os.path.join(
            self.backup_dir, "photos", "2024.02.20.10.11.12.jpg")))

    def test_missing_exif_handling(self):
        """Test handling of files with missing EXIF data"""
        # Create a file that is not a real image, so real EXIF extraction
        # fails and the handler records it as a missing-EXIF file. This tests
        # the actual code path rather than mocking extract_timestamp (which
        # would bypass the logic that appends to missing_exif_files).
        self.create_test_file("no_exif.jpg")

        # Use a deterministic fallback so processing does not depend on the
        # filesystem clock, but let extract_timestamp run for real.
        with patch.object(self.processor.exif_handler, 'get_fallback_timestamp') as mock_fallback:
            fallback_time = datetime(2024, 1, 15, 12, 0, 0)
            mock_fallback.return_value = fallback_time

            results = self.processor.process_all_files(dry_run=True)

            # The unreadable image should be recorded as a missing-EXIF file
            # and still be processed via the fallback timestamp.
            self.assertEqual(results['missing_exif_files'], 1)

    def test_exif_helper_embeds_real_exif(self):
        """
        The EXIF helper embeds real EXIF: extract_timestamp returns the embedded
        value rather than None, and the file is not flagged as missing EXIF.
        """
        photo_path = self.create_test_image_with_exif(
            "embedded.jpg", timestamp_str="2024:01:15 14:30:45"
        )

        result = self.processor.exif_handler.extract_timestamp(photo_path)

        self.assertEqual(result, datetime(2024, 1, 15, 14, 30, 45))
        self.assertNotIn(
            photo_path, self.processor.exif_handler.get_missing_exif_files()
        )

    def test_no_exif_helper_falls_back(self):
        """
        The without-EXIF helper produces a real JPEG carrying no timestamp: it
        resolves to None and lands in the missing-EXIF log (fallback path).
        """
        photo_path = self.create_test_image_without_exif("bare.jpg")

        result = self.processor.exif_handler.extract_timestamp(photo_path)

        self.assertIsNone(result)
        self.assertIn(
            photo_path, self.processor.exif_handler.get_missing_exif_files()
        )

    def test_directory_creation(self):
        """A real run creates the category directories AND lands files in them.

        Directory creation alone is not enough -- the files must actually arrive
        inside the created directories, so this asserts both the directories and
        their contents.
        """
        self.create_test_image_with_exif("photo.jpg", "2024:01:15 14:30:45")
        self.create_test_file("video.mov")

        results = self.processor.process_all_files(dry_run=False)

        # Check that backup directories were created
        expected_dirs = [
            os.path.join(self.backup_dir, "photos"),
            os.path.join(self.backup_dir, "videos"),
            os.path.join(self.backup_dir, "screenshots"),
        ]

        for expected_dir in expected_dirs:
            self.assertTrue(os.path.exists(expected_dir), f"Directory not created: {expected_dir}")

        # backup/unknown/ must NOT be created when no unrecognized file exists:
        # it is no longer an empty phantom that implies handling (issue #29).
        self.assertFalse(
            os.path.exists(os.path.join(self.backup_dir, "unknown")),
            "backup/unknown/ was created empty with no unknown files present",
        )

        # The directories are not merely created -- the files land inside them.
        self.assertTrue(os.path.isfile(os.path.join(
            self.backup_dir, "photos", "2024.01.15.14.30.45.jpg")))
        self.assertEqual(
            len(os.listdir(os.path.join(self.backup_dir, "videos"))), 1)
        self.assertEqual(results['files_processed'], 2)

    def test_nested_file_discovery(self):
        """All three nested files are discovered and categorized by real path.

        Asserts the exact total and per-category counts and that each specific
        nested path appears in the categorized output. A recursion bug that
        dropped even one file (e.g. ``_scan_export_directory`` returning a
        truncated list) fails this test instead of slipping by an ``> 0`` check.
        """
        photo = self.create_test_file("photo.jpg", "subfolder1")
        video = self.create_test_file("video.mov", "subfolder1/nested")
        screenshot = self.create_test_file("screenshot.png", "subfolder2")

        results = self.processor.process_all_files(dry_run=True)

        stats = results['categorization_stats']
        self.assertEqual(stats['total'], 3)
        self.assertEqual(stats['photos'], 1)
        self.assertEqual(stats['videos'], 1)
        self.assertEqual(stats['screenshots'], 1)

        # Each nested file is present in the categorized output, keyed by path.
        categorizer = self.processor.categorizer
        self.assertIn(photo, categorizer.get_files_by_category(FileCategory.PHOTO))
        self.assertIn(video, categorizer.get_files_by_category(FileCategory.VIDEO))
        self.assertIn(
            screenshot,
            categorizer.get_files_by_category(FileCategory.SCREENSHOT),
        )

    def test_processing_state_cleanup(self):
        """clear_processing_state empties every piece of state the method owns.

        A real run first populates all seven state containers -- ``processed_files``
        and ``used_timestamps`` (a moved photo), ``conversion_log`` and the HEIC
        converter's ``converted_files`` (a real HEIC), ``exif_handler``'s
        ``missing_exif_files`` (a no-EXIF file), ``failed_files`` (a forced move
        failure), and the categorizer's per-category lists. Each is asserted
        NON-empty before clearing and empty afterward, so ``clear_processing_state``
        degrading to a no-op fails this test (it never inspected any of these
        before).
        """
        self.create_test_image_with_exif("photo.jpg", "2024:01:15 14:30:45")
        make_exif_heic(
            os.path.join(self.export_dir, "clip.heic"),
            date_time_original="2022:03:04 05:06:07",
        )
        self.create_test_image_without_exif("bare.jpg")

        real_move = shutil.move

        def flaky_move(src, dst, *args, **kwargs):
            # Force exactly the no-EXIF file's move to fail so failed_files fills.
            if os.path.basename(src) == "bare.jpg":
                raise PermissionError("forced failure to populate failed_files")
            return real_move(src, dst, *args, **kwargs)

        with patch('src.file_processor.shutil.move', side_effect=flaky_move):
            self.processor.process_all_files(dry_run=False)

        # Precondition: every container the method clears is actually populated,
        # otherwise asserting emptiness afterward would be vacuous.
        self.assertTrue(self.processor.processed_files)
        self.assertTrue(self.processor.used_timestamps)
        self.assertTrue(self.processor.failed_files)
        self.assertTrue(self.processor.conversion_log)
        self.assertTrue(self.processor.exif_handler.missing_exif_files)
        self.assertTrue(self.processor.heic_converter.converted_files)
        self.assertTrue(
            any(files for files in self.processor.categorizer.categorized_files.values())
        )

        self.processor.clear_processing_state()

        # Every container is empty after the clear.
        self.assertEqual(self.processor.processed_files, [])
        self.assertEqual(self.processor.used_timestamps, {})
        self.assertEqual(self.processor.failed_files, [])
        self.assertEqual(self.processor.conversion_log, [])
        self.assertEqual(self.processor.exif_handler.get_missing_exif_files(), [])
        self.assertEqual(self.processor.heic_converter.converted_files, [])
        for category, files in self.processor.categorizer.categorized_files.items():
            self.assertEqual(files, [], f"{category} not cleared")


class TestCLIIntegration(unittest.TestCase):
    """Integration tests for CLI interface"""

    def setUp(self):
        """Set up test environment"""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")

        # Create export directory with some test files
        os.makedirs(self.export_dir, exist_ok=True)

        # Initialize CLI interface
        self.cli = CLIInterface(self.export_dir, self.backup_dir)

    def tearDown(self):
        """Clean up test environment"""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_test_file(self, filename: str) -> str:
        """Create a test file in export directory"""
        file_path = os.path.join(self.export_dir, filename)
        with open(file_path, 'w') as f:
            f.write("test content")
        return file_path

    def test_directory_check_success(self):
        """Test successful directory validation"""
        # Create a test file to make export directory non-empty
        self.create_test_file("test.jpg")

        # Should pass directory checks
        with patch('rich.prompt.Confirm.ask', return_value=True):
            result = self.cli.check_directories()
            self.assertTrue(result)

    def test_directory_check_missing_export(self):
        """Test directory check when export directory is missing"""
        # Remove export directory
        shutil.rmtree(self.export_dir)

        result = self.cli.check_directories()
        self.assertFalse(result)

    def test_directory_check_empty_export(self):
        """Test directory check when export directory is empty"""
        # Export directory exists but is empty
        with patch('rich.prompt.Confirm.ask', return_value=False):
            result = self.cli.check_directories()
            self.assertFalse(result)

    def test_display_file_scan_results(self):
        """Test file scan results display"""
        # Create test files
        test_files = [
            self.create_test_file("photo.jpg"),
            self.create_test_file("video.mov"),
            self.create_test_file("screenshot.png")
        ]

        # This should not raise an exception
        self.cli.display_file_scan_results(test_files)

    def test_process_with_progress_dry_run(self):
        """Test progress processing in dry run mode"""
        # Create test files
        self.create_test_file("photo.jpg")
        self.create_test_file("video.mov")

        # Run with dry run mode
        results = self.cli.process_with_progress(dry_run=True)

        # Should return results without errors
        self.assertIsInstance(results, dict)
        self.assertIn('categorization_stats', results)


class TestEndToEndWorkflow(unittest.TestCase):
    """End-to-end workflow tests simulating real usage scenarios"""

    def setUp(self):
        """Set up realistic test environment"""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")

        os.makedirs(self.export_dir, exist_ok=True)

        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        """Clean up test environment"""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_realistic_test_files(self):
        """Create a realistic set of test files similar to iCloud export"""
        files = []

        # Photos with various extensions
        photo_files = [
            "IMG_1001.jpg",
            "IMG_1002.JPEG",
            "IMG_1003.heic",
            "photo_edited.png"
        ]

        # Videos
        video_files = [
            "VID_20240115_143045.mov",
            "movie.mp4",
            "recording.m4v"
        ]

        # Screenshots
        screenshot_files = [
            "Screenshot 2024-01-15 at 2.30.45 PM.png",
            "IMG_1234.png",
            "Screen Shot 2024-01-15 at 3.15.22 PM.png"
        ]

        # Sidecar files
        sidecar_files = [
            "IMG_1001.aae",
            "IMG_1002.AAE",
            "photo_edited.aae"
        ]

        # Unknown files that might be in export
        unknown_files = [
            "readme.txt",
            "metadata.xml"
        ]

        all_files = photo_files + video_files + screenshot_files + sidecar_files + unknown_files

        for filename in all_files:
            file_path = os.path.join(self.export_dir, filename)
            with open(file_path, 'w') as f:
                f.write(f"Content for {filename}")
            files.append(file_path)

        return files

    def test_realistic_icloud_export_processing(self):
        """Test processing a realistic iCloud export scenario"""
        # Create realistic test files
        test_files = self.create_realistic_test_files()

        # Process in dry run mode first
        dry_run_results = self.processor.process_all_files(dry_run=True)

        # Verify dry run results
        stats = dry_run_results['categorization_stats']
        self.assertEqual(stats['photos'], 4)  # jpg, jpeg, heic, png
        self.assertEqual(stats['videos'], 3)  # mov, mp4, m4v
        self.assertEqual(stats['screenshots'], 3)  # screenshot patterns
        self.assertEqual(stats['sidecar'], 3)  # aae files
        self.assertEqual(stats['unknown'], 2)  # txt, xml

        # Verify files still exist after dry run
        for file_path in test_files:
            self.assertTrue(os.path.exists(file_path))

    # NOTE: The former ``test_full_processing_workflow`` was retired. It mocked
    # ``shutil.move``, ``os.remove``, and the HEIC converter, then asserted only
    # ``mock_move.call_count > 0`` / ``mock_remove.call_count > 0`` -- a
    # call-count-only test that passed whether or not any file actually landed.
    # Its real end-to-end intent (files converted, moved to renamed backup
    # paths, sidecars deleted, no .heic left behind) is now covered against real
    # fixtures with no mocking by tests/test_landing_paths.py, specifically
    # ``test_each_category_lands_in_its_own_directory``,
    # ``test_heic_is_converted_and_original_removed``, and
    # ``test_sidecar_deleted_and_backup_tree_is_exactly_expected``.

    def test_error_recovery_workflow(self):
        """The pipeline continues past a single failing file and records it exactly.

        Only IMG_1001 is made to fail; the real ``_process_single_file`` still
        runs for every other file (this injects a failure, it does not mock the
        method away). The run must complete, categorize all 15 files, and record
        exactly one failure that names IMG_1001 -- not abort, and not silently
        pass an unchecked ``> 0`` count.
        """
        self.create_realistic_test_files()

        original_process = self.processor._process_single_file

        def failing_process(file_path, category, target_dir, dry_run):
            if "IMG_1001" in file_path:
                raise Exception("Simulated processing error")
            return original_process(file_path, category, target_dir, dry_run)

        with patch.object(self.processor, '_process_single_file',
                          side_effect=failing_process):
            results = self.processor.process_all_files(dry_run=True)

        # All 15 realistic files are still discovered and categorized.
        self.assertEqual(results['categorization_stats']['total'], 15)
        # Exactly one file failed, and the failure tuple names IMG_1001.
        self.assertEqual(results['files_failed'], 1)
        operation, failed_path, _message = results['failed_files'][0]
        self.assertEqual(operation, 'process_file')
        self.assertIn("IMG_1001", failed_path)

    def test_correctness_with_many_files(self):
        """Fifty files are all processed and land as fifty distinct renamed files.

        The former version asserted only wall-clock ``processing_time < 10.0`` --
        a ~500x margin that could only fail on a pathologically slow CI box (a
        future flake, not a performance guard). It is replaced by a real-run
        correctness check at scale: all fifty files must land as fifty distinct
        files, which exercises collision resolution across many identical
        filesystem-fallback timestamps. No test asserts on wall-clock time.
        """
        for i in range(50):
            file_path = os.path.join(self.export_dir, f"photo_{i:03d}.jpg")
            with open(file_path, 'w') as f:
                f.write(f"Photo {i}")

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results['categorization_stats']['photos'], 50)
        self.assertEqual(results['files_processed'], 50)

        photos_dir = os.path.join(self.backup_dir, "photos")
        self.assertEqual(
            len(os.listdir(photos_dir)), 50,
            "collision resolution must yield 50 distinct files, none overwritten",
        )


if __name__ == '__main__':
    # Run all integration tests
    unittest.main(verbosity=2)