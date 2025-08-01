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
        Create a test image file with EXIF data.

        Args:
            filename: Name of image file to create
            timestamp_str: EXIF timestamp string

        Returns:
            Full path to created image
        """
        try:
            from PIL import Image
            from PIL.ExifTags import TAGS

            # Create a simple test image
            image = Image.new('RGB', (100, 100), color='red')

            # For testing purposes, we'll use a simpler approach
            # In real tests, proper EXIF would be added
            file_path = os.path.join(self.export_dir, filename)
            image.save(file_path, format='JPEG')

            return file_path
        except ImportError:
            # Fallback if PIL not available in test environment
            return self.create_test_file(filename)

    def test_empty_export_directory(self):
        """Test processing when export directory is empty"""
        results = self.processor.process_all_files(dry_run=True)

        self.assertEqual(results['files_processed'], 0)
        self.assertEqual(results['files_failed'], 0)
        self.assertEqual(results['categorization_stats']['total'], 0)

    def test_single_photo_processing(self):
        """Test processing a single photo file"""
        # Create test photo
        photo_path = self.create_test_image_with_exif("test_photo.jpg")

        # Process files
        results = self.processor.process_all_files(dry_run=True)

        # Verify results
        self.assertEqual(results['categorization_stats']['photos'], 1)
        self.assertEqual(results['categorization_stats']['total'], 1)

    def test_mixed_file_types_processing(self):
        """Test processing multiple file types together"""
        # Create test files of different types
        test_files = [
            self.create_test_image_with_exif("photo1.jpg"),
            self.create_test_image_with_exif("photo2.jpeg"),
            self.create_test_file("video1.mov"),
            self.create_test_file("video2.mp4"),
            self.create_test_file("Screenshot 2024-01-15.png"),
            self.create_test_file("IMG_1234.png"),  # iOS screenshot
            self.create_test_file("sidecar1.aae"),
            self.create_test_file("sidecar2.aae"),
            self.create_test_file("unknown.txt")
        ]

        # Process files in dry run mode
        results = self.processor.process_all_files(dry_run=True)

        # Verify categorization
        stats = results['categorization_stats']
        self.assertEqual(stats['photos'], 2)  # jpg, jpeg
        self.assertEqual(stats['videos'], 2)  # mov, mp4
        self.assertEqual(stats['screenshots'], 2)  # PNG screenshots
        self.assertEqual(stats['sidecar'], 2)  # aae files
        self.assertEqual(stats['unknown'], 1)  # txt file
        self.assertEqual(stats['total'], 9)

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
        """Test handling of files with duplicate timestamps"""
        # This test simulates the scenario where multiple files
        # would have the same timestamp
        with patch.object(self.processor.exif_handler, 'extract_timestamp') as mock_extract:
            # Mock to return same timestamp for multiple files
            duplicate_timestamp = datetime(2024, 1, 15, 14, 30, 45)
            mock_extract.return_value = duplicate_timestamp

            # Create test files
            test_files = [
                self.create_test_image_with_exif("photo1.jpg"),
                self.create_test_image_with_exif("photo2.jpg"),
                self.create_test_image_with_exif("photo3.jpg")
            ]

            results = self.processor.process_all_files(dry_run=True)

            # Should process all files without timestamp conflicts
            self.assertEqual(results['categorization_stats']['photos'], 3)

    def test_error_handling_workflow(self):
        """Test error handling during processing workflow"""
        # Create test file
        test_file = self.create_test_file("test.jpg")

        # Mock file processing to raise an exception
        with patch.object(self.processor, '_process_single_file') as mock_process:
            mock_process.side_effect = Exception("Test error")

            results = self.processor.process_all_files(dry_run=False)

            # Should capture the error
            self.assertGreater(results['files_failed'], 0)
            self.assertGreater(len(results['failed_files']), 0)

    def test_missing_exif_handling(self):
        """Test handling of files with missing EXIF data"""
        # Create test file
        test_file = self.create_test_file("no_exif.jpg")

        # Mock EXIF extraction to return None (missing EXIF)
        with patch.object(self.processor.exif_handler, 'extract_timestamp') as mock_extract:
            mock_extract.return_value = None

            # Mock fallback timestamp
            with patch.object(self.processor.exif_handler, 'get_fallback_timestamp') as mock_fallback:
                fallback_time = datetime(2024, 1, 15, 12, 0, 0)
                mock_fallback.return_value = fallback_time

                results = self.processor.process_all_files(dry_run=True)

                # Should still process the file using fallback
                self.assertEqual(results['missing_exif_files'], 1)

    def test_directory_creation(self):
        """Test that backup directories are created properly"""
        # Process some files to trigger directory creation
        self.create_test_image_with_exif("photo.jpg")
        self.create_test_file("video.mov")

        results = self.processor.process_all_files(dry_run=False)

        # Check that backup directories were created
        expected_dirs = [
            os.path.join(self.backup_dir, "photos"),
            os.path.join(self.backup_dir, "videos"),
            os.path.join(self.backup_dir, "screenshots"),
            os.path.join(self.backup_dir, "unknown")
        ]

        for expected_dir in expected_dirs:
            self.assertTrue(os.path.exists(expected_dir), f"Directory not created: {expected_dir}")

    def test_nested_file_discovery(self):
        """Test discovery of files in nested directories"""
        # Create files in subdirectories
        nested_files = [
            self.create_test_file("photo.jpg", "subfolder1"),
            self.create_test_file("video.mov", "subfolder1/nested"),
            self.create_test_file("screenshot.png", "subfolder2")
        ]

        results = self.processor.process_all_files(dry_run=True)

        # Should find files in nested directories
        stats = results['categorization_stats']
        self.assertGreater(stats['total'], 0)

    def test_processing_state_cleanup(self):
        """Test that processing state is properly cleared between runs"""
        # Create test files
        self.create_test_image_with_exif("photo1.jpg")

        # First run
        results1 = self.processor.process_all_files(dry_run=True)

        # Clear state
        self.processor.clear_processing_state()

        # Add more files
        self.create_test_image_with_exif("photo2.jpg")

        # Second run
        results2 = self.processor.process_all_files(dry_run=True)

        # Second run should only see new files, not accumulated state
        self.assertEqual(results2['categorization_stats']['photos'], 2)  # Both files found


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

    @patch('src.heic_converter.HeicConverter.convert_heic_to_jpeg')
    @patch('src.file_processor.shutil.move')
    @patch('src.file_processor.os.remove')
    def test_full_processing_workflow(self, mock_remove, mock_move, mock_convert_heic):
        """Test complete processing workflow with mocked file operations"""
        # Mock HEIC conversion
        mock_convert_heic.return_value = "/temp/converted.jpg"

        # Create test files
        self.create_realistic_test_files()

        # Process files (not dry run)
        results = self.processor.process_all_files(dry_run=False)

        # Verify sidecar files were deleted
        self.assertGreater(mock_remove.call_count, 0)

        # Verify files were moved to backup directories
        self.assertGreater(mock_move.call_count, 0)

        # Check results summary
        self.assertGreater(results['files_processed'], 0)
        self.assertIn('categorization_stats', results)

    def test_error_recovery_workflow(self):
        """Test workflow continues gracefully when individual files fail"""
        # Create test files
        self.create_realistic_test_files()

        # Mock some operations to fail
        original_process = self.processor._process_single_file

        def failing_process(file_path, category, target_dir, dry_run):
            if "IMG_1001" in file_path:
                raise Exception("Simulated processing error")
            return original_process(file_path, category, target_dir, dry_run)

        with patch.object(self.processor, '_process_single_file', side_effect=failing_process):
            results = self.processor.process_all_files(dry_run=True)

            # Should continue processing other files despite failures
            self.assertGreater(results['categorization_stats']['total'], 0)
            self.assertGreater(results['files_failed'], 0)

    def test_performance_with_many_files(self):
        """Test processing performance with a larger number of files"""
        # Create many test files
        for i in range(50):
            filename = f"photo_{i:03d}.jpg"
            file_path = os.path.join(self.export_dir, filename)
            with open(file_path, 'w') as f:
                f.write(f"Photo {i}")

        # Measure processing time
        import time
        start_time = time.time()

        results = self.processor.process_all_files(dry_run=True)

        end_time = time.time()
        processing_time = end_time - start_time

        # Should process all files
        self.assertEqual(results['categorization_stats']['photos'], 50)

        # Performance check (should complete reasonably quickly)
        # This is a loose check - adjust based on expected performance
        self.assertLess(processing_time, 10.0)  # Should complete within 10 seconds


if __name__ == '__main__':
    # Run all integration tests
    unittest.main(verbosity=2)