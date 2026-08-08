"""
Integration tests for the complete file processing workflow
"""

import unittest
import tempfile
import os
import shutil
from datetime import datetime
from unittest.mock import patch

from PIL import Image

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

    def create_test_sidecar_file(self, filename: str) -> str:
        """
        Create a genuine (XML property list) ``.aae`` sidecar in export/.

        Issue #57: a sidecar is now deleted only when its CONTENT validates as
        a plist, not merely because it carries the ``.aae`` extension -- the
        plain-text placeholder ``create_test_file`` writes no longer qualifies.

        Args:
            filename: Name of the sidecar file to create (e.g. "photo.aae").

        Returns:
            Full path to the created file.
        """
        file_path = os.path.join(self.export_dir, filename)
        with open(file_path, 'wb') as f:
            f.write(b'<?xml version="1.0"?><plist version="1.0"><dict/></plist>')
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

    def create_test_png(self, filename: str) -> str:
        """
        Create a real, decodable PNG in the export directory.

        Screenshots carry a ``.png`` extension and are now decode-verified
        before being filed (issue #58), so a screenshot fixture must be genuine
        image bytes rather than a text file renamed ``.png`` -- otherwise it
        would (correctly) be quarantined instead of processed. The PNG carries no
        EXIF; its timestamp comes from the filename or the filesystem fallback.

        Args:
            filename: Name of the PNG to create.

        Returns:
            Full path to the created PNG.
        """
        path = os.path.join(self.export_dir, filename)
        Image.new("RGB", (24, 24), "purple").save(path, format="PNG")
        return path

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
        self.create_test_png("Screenshot 2024-01-15.png")
        self.create_test_png("IMG_1234.png")  # iOS screenshot pattern
        self.create_test_sidecar_file("sidecar1.aae")
        self.create_test_sidecar_file("sidecar2.aae")
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
        # The second source is ``photo2.jpeg``; its output extension is
        # normalized to the canonical ``.jpg`` spelling (issue #55).
        self.assertTrue(os.path.isfile(
            os.path.join(self.backup_dir, "photos", "2024.01.15.14.30.45.jpg")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.backup_dir, "photos", "2024.02.20.10.11.12.jpg")))

        # Sidecars deleted from export; the unknown file is routed to
        # backup/unknown/ under its original name and no longer sits in export.
        self.assertFalse(os.path.exists(os.path.join(self.export_dir, "sidecar1.aae")))
        self.assertFalse(os.path.exists(os.path.join(self.export_dir, "sidecar2.aae")))
        self.assertFalse(os.path.exists(os.path.join(self.export_dir, "unknown.txt")))
        self.assertTrue(os.path.isfile(
            os.path.join(self.backup_dir, "unknown", "unknown.txt")))
        self.assertEqual(results['sidecars_deleted'], 2)

    def test_sidecar_file_deletion_dry_run(self):
        """Test that sidecar files are identified for deletion in dry run"""
        # Create genuine (plist-content) sidecar files (issue #57).
        sidecar_files = [
            self.create_test_sidecar_file("IMG_1234.aae"),
            self.create_test_sidecar_file("photo.aae")
        ]

        results = self.processor.process_all_files(dry_run=True)

        # In dry run, files should be identified but not deleted
        self.assertEqual(results['categorization_stats']['sidecar'], 2)

        # Files should still exist (dry run doesn't delete)
        for file_path in sidecar_files:
            self.assertTrue(os.path.exists(file_path))

        # The dry-run count of "would delete" matches the real-run count for
        # identical input (issue #10 parity; asserted directly in
        # tests/test_sidecar_validation.py).
        self.assertEqual(results['sidecars_deleted'], 2)

    @patch('src.file_processor.os.remove')
    def test_sidecar_file_deletion_real(self, mock_remove):
        """Test actual sidecar file deletion (mocked)"""
        # Create genuine (plist-content) sidecar files (issue #57): a
        # candidate must validate as a plist before os.remove is ever called.
        sidecar_files = [
            self.create_test_sidecar_file("IMG_1234.aae"),
            self.create_test_sidecar_file("photo.aae")
        ]

        results = self.processor.process_all_files(dry_run=False)

        # Verify deletion was attempted
        self.assertEqual(mock_remove.call_count, 2)
        for file_path in sidecar_files:
            mock_remove.assert_any_call(file_path)
        self.assertEqual(results['sidecars_deleted'], 2)

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
        self.create_test_png("Screenshot 2024-01-15-14-30-45.png")

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
        # Create a real JPEG that carries no EXIF, so genuine EXIF extraction
        # returns None and the handler records it as a missing-EXIF file. Using
        # real image bytes (not a text file renamed .jpg) is what exercises this
        # path now: an undecodable file would instead be quarantined before EXIF
        # extraction ever runs (issue #58). This tests the actual code path
        # rather than mocking extract_timestamp.
        self.create_test_image_without_exif("no_exif.jpg")

        # Use a deterministic fallback so processing does not depend on the
        # filesystem clock, but let extract_timestamp run for real.
        with patch.object(self.processor.exif_handler, 'get_fallback_timestamp') as mock_fallback:
            fallback_time = datetime(2024, 1, 15, 12, 0, 0)
            mock_fallback.return_value = fallback_time

            results = self.processor.process_all_files(dry_run=True)

            # The unreadable image should be recorded as a missing-EXIF file
            # and still be processed via the fallback timestamp.
            self.assertEqual(results['missing_exif_files'], 1)

    def test_missing_exif_real_run_reports_final_path_that_exists(self):
        """A real run's missing_exif_list names a FINAL path that exists.

        Before issue #35, ``missing_exif_list`` carried the SOURCE path
        recorded at EXIF-extraction time. By the time a summary is rendered,
        a real run has already moved that source into ``backup/``, so the
        reported path named nothing on disk -- the exact defect the issue's
        "actual files moved : 2 / files_processed reported : 4" and
        "do reported missing_exif paths still exist on disk? {...: False}"
        findings describe. This asserts the reported path is the file's
        genuine final backup/ location (which exists), the original path is
        carried alongside for identification only, and the original no
        longer exists (the file was moved away, as a real run does).
        """
        photo_path = self.create_test_image_without_exif("no_exif_real.jpg")

        with patch.object(
            self.processor.exif_handler, 'get_fallback_timestamp'
        ) as mock_fallback:
            mock_fallback.return_value = datetime(2024, 3, 1, 8, 0, 0)
            results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(len(results['missing_exif_list']), 1)
        record = results['missing_exif_list'][0]
        self.assertEqual(record['original_path'], photo_path)
        self.assertTrue(
            os.path.isfile(record['final_path']),
            f"reported final_path does not exist on disk: {record['final_path']}",
        )
        self.assertFalse(
            os.path.exists(record['original_path']),
            "original export/ path still exists after a real run moved it",
        )

    def test_missing_exif_dry_run_reports_existing_original_path(self):
        """A dry run's missing_exif_list names the still-existing original.

        A dry run moves nothing, so the ORIGINAL export/ path is the one that
        genuinely exists on disk right now; ``final_path`` is ``None`` because
        no destination was actually created for a dry run to report.
        """
        photo_path = self.create_test_image_without_exif("no_exif_dry.jpg")

        with patch.object(
            self.processor.exif_handler, 'get_fallback_timestamp'
        ) as mock_fallback:
            mock_fallback.return_value = datetime(2024, 3, 1, 8, 0, 0)
            results = self.processor.process_all_files(dry_run=True)

        self.assertEqual(len(results['missing_exif_list']), 1)
        record = results['missing_exif_list'][0]
        self.assertEqual(record['original_path'], photo_path)
        self.assertIsNone(record['final_path'])
        self.assertTrue(os.path.isfile(photo_path))

    def test_missing_exif_file_that_fails_to_move_is_not_double_counted(self):
        """A missing-EXIF file that also fails to move is failed, not missing-EXIF.

        Issue #35 changed what ``missing_exif_files``/``missing_exif_list``
        count: they used to count every file whose EXIF extraction failed
        (``ExifHandler.missing_exif_files``), regardless of what happened to
        it afterward; they now count only files that actually LANDED using a
        fallback timestamp (``_missing_exif_records``, populated only at the
        point a file is recorded as successfully processed). A file that
        lacks EXIF and then fails to move is therefore counted in
        ``files_failed`` (and appears in the failure table) but NOT in
        ``missing_exif_files`` -- counting it there too would tell the user
        it "was processed using filesystem timestamps" when it was never
        filed anywhere at all. This pins the new semantics in both
        directions explicitly, since nothing else in the suite does.
        """
        self.create_test_image_without_exif("no_exif_move_fails.jpg")

        real_move = shutil.move

        def flaky_move(src, dst, *args, **kwargs):
            if os.path.basename(src) == "no_exif_move_fails.jpg":
                raise PermissionError("forced failure for the no-EXIF file")
            return real_move(src, dst, *args, **kwargs)

        with patch.object(
            self.processor.exif_handler, 'get_fallback_timestamp'
        ) as mock_fallback:
            mock_fallback.return_value = datetime(2024, 4, 1, 9, 0, 0)
            with patch('src.file_processor.shutil.move', side_effect=flaky_move):
                results = self.processor.process_all_files(dry_run=False)

        # The file failed to move -- it is accounted for as a failure...
        self.assertEqual(results['files_processed'], 0)
        self.assertEqual(results['files_failed'], 1)
        operation, failed_path, _message = results['failed_files'][0]
        self.assertEqual(operation, 'move_file')
        self.assertTrue(failed_path.endswith("no_exif_move_fails.jpg"))

        # ...and NOT counted (or listed) as a missing-EXIF file, since it was
        # never actually filed using a fallback timestamp.
        self.assertEqual(results['missing_exif_files'], 0)
        self.assertEqual(results['missing_exif_list'], [])

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
            photo_path, self.processor.exif_handler.missing_exif_files
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
            photo_path, self.processor.exif_handler.missing_exif_files
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

    def test_second_run_on_same_instance_reports_only_that_run(self):
        """Calling process_all_files twice on one instance never unions runs.

        Issue #35: FileProcessor is reusable, and every per-run accumulator
        is reset at the start of each call, so a second run's summary
        describes ONLY that run's files -- never the union with an earlier
        run on the same instance. Without the reset, this reproduces exactly
        the issue's verified defect: two files in each of two runs, and the
        second summary reporting files_processed=4 (the union) instead of 2.

        This drives two REAL runs on the SAME ``FileProcessor`` instance,
        each with two genuinely distinct files, and asserts the second
        summary's counts and ``processed_files`` list name only run 2's
        files.
        """
        self.create_test_image_with_exif("run1_a.jpg", "2024:01:15 14:30:45")
        self.create_test_image_with_exif("run1_b.jpg", "2024:01:15 14:30:46")

        results1 = self.processor.process_all_files(dry_run=False)
        self.assertEqual(results1['files_processed'], 2)

        # Second run: two DIFFERENT files land in export/ after run 1 already
        # drained it. Same processor instance, same export/backup dirs.
        self.create_test_image_with_exif("run2_c.jpg", "2024:02:20 09:00:00")
        self.create_test_image_with_exif("run2_d.jpg", "2024:02:20 09:00:01")

        results2 = self.processor.process_all_files(dry_run=False)

        # The second summary describes ONLY the two files just processed --
        # never the union with run 1's two files (would be 4 pre-fix).
        self.assertEqual(results2['files_processed'], 2)
        self.assertEqual(results2['files_failed'], 0)
        self.assertEqual(len(results2['processed_files']), 2)
        final_names = {
            os.path.basename(entry['final_path'])
            for entry in results2['processed_files']
        }
        self.assertEqual(
            final_names,
            {"2024.02.20.09.00.00.jpg", "2024.02.20.09.00.01.jpg"},
        )

        # Both runs' output is preserved on disk -- run 2 did not erase run
        # 1's files -- but run 2's SUMMARY counts only its own work.
        photos_dir = os.path.join(self.backup_dir, "photos")
        self.assertEqual(len(os.listdir(photos_dir)), 4)

    def test_second_run_reuses_a_taken_timestamp_and_bumps_safely(self):
        """A same-instance run 2 that collides with an existing file bumps it.

        This is the riskiest edge of the reuse contract: "reusable" would be
        actively dangerous if a second run on the same instance could file a
        collision on top of a file already sitting in ``backup/photos/``
        instead of bumping past it. Two mechanisms are supposed to jointly
        guarantee this never happens: the atomic ``O_CREAT | O_EXCL``
        destination reservation (issue #6), which is filesystem-authoritative
        and does not care what any in-memory bookkeeping believes, and
        ``_used_timestamps`` being reseeded from disk at the start of every
        run (this issue) rather than carried over stale from a prior run on
        the same instance.

        The colliding file here is placed directly on disk -- standing in for
        "a completely different process," per this issue's own reuse-contract
        docstring -- rather than by having run 1 itself write it through this
        instance. That distinction matters for what this test can actually
        prove: if the colliding stem were instead one THIS instance wrote
        during run 1, run 1's own bookkeeping would already carry that stem
        into ``_used_timestamps`` regardless of whether ``clear_processing_state``
        clears the dict at the top of run 2, so a test built that way could not
        tell a working re-seed from a defeated one.

        Even with the collision placed externally, the LANDING PATH and
        ``_used_timestamps``'s content immediately AFTER run 2 turned out not
        to discriminate either, on inspection: ``_reserve_destination``'s
        ``except FileExistsError`` branch calls ``names.add(stem)`` on the
        very stem that just collided, so a stale (un-reseeded) in-memory set
        silently repairs itself the instant it is asked to reserve the taken
        path and fails -- the exact same final state and landing path result
        whether the set was proactively reseeded or reactively patched up
        one failed ``os.open`` later. What DOES discriminate is *how many
        times* ``os.open`` is attempted for run 2's file: a working re-seed
        means ``handle_duplicate_timestamp`` already knows the natural stem
        is taken and never asks the filesystem for it at all, so the FIRST
        ``os.open`` call lands directly on the bumped path; a defeated
        re-seed does not learn about the collision until that first
        ``os.open`` call fails, so it takes two attempts. The spy below pins
        exactly that -- confirmed to discriminate by temporarily deleting the
        ``self._used_timestamps.clear()`` line in ``clear_processing_state``
        and re-running this test: the landing-path and pixel assertions above
        still passed unchanged, but the "collision never attempted" assertion
        below failed, showing run 2 had in fact tried and failed at the
        external file's exact path first.
        """
        # Run 1 through this instance: establishes _used_timestamps[photos_dir]
        # as a real (non-None) entry, the way any first real run would.
        self.create_test_image_with_exif("run1_other.jpg", "2024:05:10 08:00:00")
        results1 = self.processor.process_all_files(dry_run=False)
        self.assertEqual(results1['files_processed'], 1)

        photos_dir = os.path.join(self.backup_dir, "photos")

        # A file lands in backup/photos/ WITHOUT going through this
        # FileProcessor instance at all -- standing in for a separate
        # process/invocation between run 1 and run 2.
        external_stem = "2024.06.01.10.00.00"
        external_path = os.path.join(photos_dir, f"{external_stem}.jpg")
        Image.new("RGB", (8, 8), "red").save(external_path, format="JPEG")

        # Run 2, same instance: a file whose EXIF timestamp resolves to
        # EXACTLY the externally-placed name above. ``make_exif_jpeg`` is
        # called directly (rather than the ``create_test_image_with_exif``
        # helper) so the fixture's ``color`` can be set to something
        # distinguishable from the external file's, letting the pixel checks
        # below tell the two apart.
        make_exif_jpeg(
            os.path.join(self.export_dir, "run2_collide.jpg"),
            date_time_original="2024:06:01 10:00:00",
            color="blue",
        )

        # Spy on os.open (as file_processor.py looks it up) to record every
        # path a reservation attempt is made against, without changing its
        # behavior -- the real syscall still runs via side_effect.
        real_open = os.open
        opened_paths = []

        def spy_open(path, *args, **kwargs):
            opened_paths.append(path)
            return real_open(path, *args, **kwargs)

        with patch('src.file_processor.os.open', side_effect=spy_open):
            results2 = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results2['files_processed'], 1)
        self.assertEqual(results2['files_failed'], 0)

        # The externally-placed file survives untouched -- not overwritten.
        # JPEG re-encoding shifts pixel values slightly (254 vs 255), so the
        # dominant-channel check tolerates lossy compression rather than
        # requiring byte-exact equality.
        with Image.open(external_path) as image:
            r, g, b = image.convert("RGB").getpixel((0, 0))
            self.assertGreater(r, 200)
            self.assertLess(g, 50)
            self.assertLess(b, 50)

        # Run 2's file bumped to the next second rather than landing on it.
        bumped_path = os.path.join(photos_dir, "2024.06.01.10.00.01.jpg")
        self.assertTrue(
            os.path.isfile(bumped_path),
            f"run 2's file did not bump past the collision; photos/ has: "
            f"{sorted(os.listdir(photos_dir))}",
        )
        with Image.open(bumped_path) as image:
            r, g, b = image.convert("RGB").getpixel((0, 0))
            self.assertLess(r, 50)
            self.assertLess(g, 50)
            self.assertGreater(b, 200)

        # Mechanism-level, and the part that actually discriminates (see the
        # docstring): the externally-placed path was never even ATTEMPTED,
        # because a working re-seed means handle_duplicate_timestamp already
        # knew that stem was taken before the first os.open call. A defeated
        # re-seed would show exactly two attempts here: the collision first,
        # then the bump.
        self.assertNotIn(
            external_path, opened_paths,
            f"run 2 attempted the externally-placed path directly, meaning "
            f"_used_timestamps was NOT reseeded from disk before reserving "
            f"-- all attempts: {opened_paths}",
        )
        self.assertEqual(
            opened_paths, [bumped_path],
            "expected exactly one reservation attempt, landing directly on "
            "the bumped path",
        )

    def test_processing_state_cleanup(self):
        """clear_processing_state empties every piece of state the method owns.

        A real run first populates all ten state containers --
        ``_processed_files`` and ``_used_timestamps`` (a moved photo),
        ``_conversion_log`` and the HEIC converter's ``converted_files`` (a
        real HEIC), ``exif_handler``'s ``missing_exif_files`` (a no-EXIF
        file), ``_failed_files`` (a forced move failure), the categorizer's
        per-category lists, issue #57's ``_deleted_sidecars`` (a genuine
        sidecar) / ``_skipped_sidecars`` (a fake one), and issue #30's
        ``_skipped_files`` (a hidden dotted file the scan itself declines to
        collect). Each is asserted NON-empty before clearing and empty
        afterward, so ``clear_processing_state`` degrading to a no-op -- or
        simply forgetting one of the newest containers -- fails this test (it
        never inspected any of these before).
        """
        self.create_test_image_with_exif("photo.jpg", "2024:01:15 14:30:45")
        make_exif_heic(
            os.path.join(self.export_dir, "clip.heic"),
            date_time_original="2022:03:04 05:06:07",
        )
        self.create_test_image_without_exif("bare.jpg")
        self.create_test_sidecar_file("photo.aae")
        self.create_test_file("notes.aae")  # plain text: not a plist
        # A hidden dotted file (issue #30): the scan declines to collect it
        # and records it in _skipped_files instead of dropping it silently.
        with open(os.path.join(self.export_dir, ".hidden_note.jpg"), "wb") as handle:
            handle.write(b"not a real photo")

        real_move = shutil.move

        def flaky_move(src, dst, *args, **kwargs):
            # Force exactly the no-EXIF file's move to fail so failed_files fills.
            if os.path.basename(src) == "bare.jpg":
                raise PermissionError("forced failure to populate failed_files")
            return real_move(src, dst, *args, **kwargs)

        with patch('src.file_processor.shutil.move', side_effect=flaky_move):
            self.processor.process_all_files(dry_run=False)

        # This run forces a move failure, and since the whole-branch review a
        # failure keeps EVERY sidecar candidate rather than deleting any -- one
        # success must not unlock deleting the edit history of photos that never
        # landed. So _deleted_sidecars and _failed_files can no longer both be
        # populated by a single run, and this test's subject is
        # clear_processing_state, not the deletion gate. Seeding the one
        # container directly keeps the emptiness assertions below non-vacuous
        # for every container the method owns, which is the property under test.
        self.processor._deleted_sidecars.append(
            os.path.join(self.export_dir, "seeded-for-reset-coverage.aae")
        )

        # Precondition: every container the method clears is actually populated,
        # otherwise asserting emptiness afterward would be vacuous.
        self.assertTrue(self.processor._processed_files)
        self.assertTrue(self.processor._used_timestamps)
        self.assertTrue(self.processor._failed_files)
        self.assertTrue(self.processor._conversion_log)
        self.assertTrue(self.processor.exif_handler.missing_exif_files)
        self.assertTrue(self.processor.heic_converter.converted_files)
        self.assertTrue(self.processor._deleted_sidecars)
        self.assertTrue(self.processor._skipped_sidecars)
        self.assertTrue(self.processor._skipped_files)
        self.assertTrue(
            any(files for files in self.processor.categorizer.categorized_files.values())
        )

        self.processor.clear_processing_state()

        # Every container is empty after the clear.
        self.assertEqual(self.processor._processed_files, [])
        self.assertEqual(self.processor._used_timestamps, {})
        self.assertEqual(self.processor._failed_files, [])
        self.assertEqual(self.processor._conversion_log, [])
        self.assertEqual(self.processor.exif_handler.missing_exif_files, [])
        self.assertEqual(self.processor.heic_converter.converted_files, [])
        self.assertEqual(self.processor._deleted_sidecars, [])
        self.assertEqual(self.processor._skipped_sidecars, [])
        self.assertEqual(self.processor._skipped_files, [])
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

        def failing_process(file_path, category, target_dir, dry_run, progress=None):
            if "IMG_1001" in file_path:
                raise Exception("Simulated processing error")
            return original_process(file_path, category, target_dir, dry_run, progress)

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
        # Real, decodable JPEGs carrying no EXIF: they survive the decode gate
        # (issue #58) and fall back to filesystem-timestamp naming, so their
        # near-identical timestamps still exercise collision resolution at scale.
        for i in range(50):
            make_no_exif_jpeg(os.path.join(self.export_dir, f"photo_{i:03d}.jpg"))

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results['categorization_stats']['photos'], 50)
        self.assertEqual(results['files_processed'], 50)

        photos_dir = os.path.join(self.backup_dir, "photos")
        self.assertEqual(
            len(os.listdir(photos_dir)), 50,
            "collision resolution must yield 50 distinct files, none overwritten",
        )


class TestHachoirWarningsDoNotReachTerminalDuringARun(unittest.TestCase):
    """
    A full CLI run over several unparseable videos leaves the terminal free
    of hachoir's own bare diagnostic writes (issue #60, acceptance
    criterion 3). Before the fix, ``createParser`` and ``extractMetadata``
    wrote every parser warning straight to ``sys.stderr`` regardless of the
    application's own logging configuration -- interleaving with
    ``CLIInterface.process_with_progress``'s live ``rich`` ``Progress``
    display, which is exactly the corruption this test would catch: a
    stray, unmanaged write landing on the real process stdout/stderr while
    ``rich`` is mid-render. Patches ``sys.stdout``/``sys.stderr`` around the
    whole run (rather than only inspecting the recording ``rich.Console``,
    which hachoir's writes bypass entirely) so a regression back to
    hachoir's default ``use_print=True`` would be caught here even though
    it would leave the ``rich``-rendered output itself unchanged.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        # Force a fresh, unmocked hachoir resolution for this test rather
        # than relying on whichever earlier test in the suite happens to
        # trigger the first real import.
        self._hachoir_patch = patch("src.exif_handler.HACHOIR_AVAILABLE", None)
        self._hachoir_patch.start()

    def tearDown(self):
        self._hachoir_patch.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_garbage_videos_leave_stdout_and_stderr_clean(self):
        import io

        for i, ext in enumerate((".mp4", ".mov", ".mp4")):
            path = os.path.join(self.export_dir, f"garbage_{i}{ext}")
            with open(path, "wb") as f:
                # Several distinct unparseable "videos" -- each one is a
                # separate createParser() call and a separate opportunity
                # for a hachoir diagnostic to leak to the terminal.
                f.write(b"not a real video container, just garbage bytes" * 5)

        cli = CLIInterface(self.export_dir, self.backup_dir)

        captured_out, captured_err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", captured_out), patch("sys.stderr", captured_err):
            results = cli.process_with_progress(dry_run=True)

        self.assertEqual(results.get("status"), "completed")
        # All three landed on the missing-EXIF fallback path (hachoir found
        # no usable metadata in genuine garbage), proving hachoir's real
        # parser genuinely ran for each rather than short-circuiting before
        # ever reaching createParser.
        self.assertEqual(results.get("missing_exif_files"), 3)

        for stream_name, captured in (("stdout", captured_out), ("stderr", captured_err)):
            text = captured.getvalue()
            self.assertNotIn(
                "[warn]", text, f"a bare hachoir warning leaked to {stream_name}"
            )
            self.assertNotIn(
                "[err!]", text, f"a bare hachoir error leaked to {stream_name}"
            )


if __name__ == '__main__':
    # Run all integration tests
    unittest.main(verbosity=2)
