"""
Test suite for file categorization functionality
"""

import unittest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.file_categorizer import FileCategorizer, FileCategory


class TestFileCategory(unittest.TestCase):
    """Test cases for FileCategory enum"""

    def test_category_values(self):
        """Test that all categories have correct values"""
        self.assertEqual(FileCategory.PHOTO.value, "photos")
        self.assertEqual(FileCategory.VIDEO.value, "videos")
        self.assertEqual(FileCategory.SCREENSHOT.value, "screenshots")
        self.assertEqual(FileCategory.GENERATED.value, "generated")
        self.assertEqual(FileCategory.UNKNOWN.value, "unknown")
        self.assertEqual(FileCategory.SIDECAR.value, "sidecar")


class TestFileCategorizer(unittest.TestCase):
    """Test cases for FileCategorizer class"""

    def setUp(self):
        """Set up test fixtures"""
        self.categorizer = FileCategorizer()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up test fixtures"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_test_file(self, filename: str) -> str:
        """
        Create a test file in temp directory.

        Args:
            filename: Name of file to create

        Returns:
            Full path to created file
        """
        file_path = os.path.join(self.temp_dir, filename)
        Path(file_path).touch()
        return file_path

    def test_init(self):
        """Test FileCategorizer initialization"""
        categorizer = FileCategorizer()

        # Check that all categories are initialized as empty lists
        for category in FileCategory:
            self.assertIsInstance(categorizer.categorized_files[category], list)
            self.assertEqual(len(categorizer.categorized_files[category]), 0)

        # Check that extension sets are properly converted to lowercase
        self.assertIn('.jpg', categorizer.photo_exts)
        self.assertIn('.jpeg', categorizer.photo_exts)
        self.assertIn('.mov', categorizer.video_exts)
        self.assertIn('.png', categorizer.screenshot_exts)
        self.assertIn('.aae', categorizer.sidecar_exts)

    def test_categorize_photo_extensions(self):
        """Every photo-extension fixture maps to the exact category the code returns.

        ``logo.png`` is a genuine (non-screenshot) PNG and must resolve to PHOTO:
        if PNG handling ever regresses to classifying every ``.png`` as a
        SCREENSHOT, this assertion fails rather than silently accepting either
        outcome (the previous ``assertIn([PHOTO, SCREENSHOT])`` was a tautology).
        """
        expected = {
            "image.jpg": FileCategory.PHOTO,
            "photo.jpeg": FileCategory.PHOTO,
            "picture.JPEG": FileCategory.PHOTO,  # case insensitive
            "logo.png": FileCategory.PHOTO,      # PNG w/o screenshot pattern -> photo
            "image.gif": FileCategory.PHOTO,
            "photo.heic": FileCategory.PHOTO,
            "picture.tiff": FileCategory.PHOTO,
            "raw.dng": FileCategory.PHOTO,
        }

        for filename, want in expected.items():
            file_path = self.create_test_file(filename)
            category = self.categorizer.categorize_file(file_path)
            self.assertEqual(category, want, f"Failed for {filename}")

    def test_categorize_video_extensions(self):
        """Test categorization of video file extensions"""
        video_files = [
            "movie.mov",
            "video.mp4",
            "clip.m4v",
            "film.avi",
            "recording.MOV"  # Test case insensitive
        ]

        for filename in video_files:
            file_path = self.create_test_file(filename)
            category = self.categorizer.categorize_file(file_path)
            self.assertEqual(category, FileCategory.VIDEO, f"Failed for {filename}")

    def test_categorize_screenshot_patterns(self):
        """Test categorization of screenshot filename patterns"""
        screenshot_files = [
            "Screenshot 2024-01-15 at 2.30.45 PM.png",
            "Screen Shot 2024-01-15 at 2.30.45 PM.png",
            "IMG_1234.png",  # iOS screenshot pattern
            "Simulator Screen Shot - iPhone 15 Pro - 2024-01-15.png"
        ]

        for filename in screenshot_files:
            file_path = self.create_test_file(filename)
            category = self.categorizer.categorize_file(file_path)
            self.assertEqual(category, FileCategory.SCREENSHOT, f"Failed for {filename}")

    def test_categorize_sidecar_files(self):
        """Test categorization of sidecar files"""
        sidecar_files = [
            "IMG_1234.aae",
            "photo.AAE"  # Test case insensitive
        ]

        for filename in sidecar_files:
            file_path = self.create_test_file(filename)
            category = self.categorizer.categorize_file(file_path)
            self.assertEqual(category, FileCategory.SIDECAR, f"Failed for {filename}")

    def test_categorize_unknown_extensions(self):
        """Test categorization of unknown file extensions"""
        unknown_files = [
            "document.txt",
            "data.xml",
            "archive.zip",
            "executable.exe"
        ]

        for filename in unknown_files:
            file_path = self.create_test_file(filename)
            category = self.categorizer.categorize_file(file_path)
            self.assertEqual(category, FileCategory.UNKNOWN, f"Failed for {filename}")

    def test_is_likely_screenshot_positive_cases(self):
        """Test screenshot detection for positive cases"""
        screenshot_patterns = [
            "screenshot_test.png",
            "screen shot test.png",
            "img_1234.png",
            "simulator screen shot test.png",
            "screen recording test.png"
        ]

        for filename in screenshot_patterns:
            result = self.categorizer._is_likely_screenshot(filename.lower())
            self.assertTrue(result, f"Should detect screenshot pattern in {filename}")

    def test_is_likely_screenshot_negative_cases(self):
        """Test screenshot detection for negative cases"""
        non_screenshot_patterns = [
            "regular_image.png",
            "photo.png",
            "document.png",
            "logo.png"
        ]

        for filename in non_screenshot_patterns:
            result = self.categorizer._is_likely_screenshot(filename.lower())
            self.assertFalse(result, f"Should not detect screenshot pattern in {filename}")

    def test_batch_categorize(self):
        """Test batch categorization of multiple files"""
        # Create test files
        test_files = [
            self.create_test_file("photo.jpg"),
            self.create_test_file("video.mov"),
            self.create_test_file("Screenshot.png"),
            self.create_test_file("sidecar.aae"),
            self.create_test_file("unknown.txt")
        ]

        result = self.categorizer.batch_categorize(test_files)

        # Check that files are properly categorized
        self.assertEqual(len(result[FileCategory.PHOTO]), 1)
        self.assertEqual(len(result[FileCategory.VIDEO]), 1)
        self.assertEqual(len(result[FileCategory.SCREENSHOT]), 1)
        self.assertEqual(len(result[FileCategory.SIDECAR]), 1)
        self.assertEqual(len(result[FileCategory.UNKNOWN]), 1)

        # Check specific file assignments
        self.assertIn(test_files[0], result[FileCategory.PHOTO])
        self.assertIn(test_files[1], result[FileCategory.VIDEO])
        self.assertIn(test_files[2], result[FileCategory.SCREENSHOT])
        self.assertIn(test_files[3], result[FileCategory.SIDECAR])
        self.assertIn(test_files[4], result[FileCategory.UNKNOWN])

    @patch('os.path.exists')
    def test_batch_categorize_missing_files(self, mock_exists):
        """Test batch categorization with missing files"""
        # Mock some files as missing
        def exists_side_effect(path):
            return "existing" in path

        mock_exists.side_effect = exists_side_effect

        test_files = [
            "existing_photo.jpg",
            "missing_photo.jpg"
        ]

        result = self.categorizer.batch_categorize(test_files)

        # Should only categorize existing files
        self.assertEqual(len(result[FileCategory.PHOTO]), 1)
        self.assertIn("existing_photo.jpg", result[FileCategory.PHOTO])

    def test_get_files_by_category(self):
        """Test retrieving files by specific category"""
        # Add some test files to categories
        self.categorizer.categorized_files[FileCategory.PHOTO] = ["photo1.jpg", "photo2.jpg"]
        self.categorizer.categorized_files[FileCategory.VIDEO] = ["video1.mov"]

        photo_files = self.categorizer.get_files_by_category(FileCategory.PHOTO)
        video_files = self.categorizer.get_files_by_category(FileCategory.VIDEO)

        self.assertEqual(photo_files, ["photo1.jpg", "photo2.jpg"])
        self.assertEqual(video_files, ["video1.mov"])

        # Should return copies, not original lists
        self.assertIsNot(photo_files, self.categorizer.categorized_files[FileCategory.PHOTO])

    def test_get_sidecar_files(self):
        """Test retrieving sidecar files"""
        sidecar_files = ["file1.aae", "file2.aae"]
        self.categorizer.categorized_files[FileCategory.SIDECAR] = sidecar_files

        result = self.categorizer.get_sidecar_files()

        self.assertEqual(result, sidecar_files)
        self.assertIsNot(result, self.categorizer.categorized_files[FileCategory.SIDECAR])

    def test_get_processable_files(self):
        """Every category except sidecar is processable -- including unknown.

        Unknown files must be returned here so they are actually routed to
        backup/unknown/ instead of being silently left in export/ (issue #29).
        Only sidecar (which is deleted, not filed) is excluded.
        """
        # Setup test data
        self.categorizer.categorized_files[FileCategory.PHOTO] = ["photo.jpg"]
        self.categorizer.categorized_files[FileCategory.VIDEO] = ["video.mov"]
        self.categorizer.categorized_files[FileCategory.SCREENSHOT] = ["screenshot.png"]
        self.categorizer.categorized_files[FileCategory.SIDECAR] = ["sidecar.aae"]
        self.categorizer.categorized_files[FileCategory.UNKNOWN] = ["unknown.txt"]

        result = self.categorizer.get_processable_files()

        # Every category except SIDECAR, derived from the enum so adding a new
        # category can never silently drop it (issue #29).
        expected_categories = {
            category for category in FileCategory
            if category is not FileCategory.SIDECAR
        }
        self.assertEqual(set(result.keys()), expected_categories)
        self.assertNotIn(FileCategory.SIDECAR, result)

        self.assertEqual(result[FileCategory.PHOTO], ["photo.jpg"])
        self.assertEqual(result[FileCategory.VIDEO], ["video.mov"])
        self.assertEqual(result[FileCategory.SCREENSHOT], ["screenshot.png"])
        self.assertEqual(result[FileCategory.UNKNOWN], ["unknown.txt"])

    def test_get_target_directory(self):
        """Test getting target directory paths for categories"""
        base_dir = "/test/backup"

        self.assertEqual(
            self.categorizer.get_target_directory(FileCategory.PHOTO, base_dir),
            "/test/backup/photos"
        )
        self.assertEqual(
            self.categorizer.get_target_directory(FileCategory.VIDEO, base_dir),
            "/test/backup/videos"
        )
        self.assertEqual(
            self.categorizer.get_target_directory(FileCategory.SCREENSHOT, base_dir),
            "/test/backup/screenshots"
        )
        self.assertEqual(
            self.categorizer.get_target_directory(FileCategory.UNKNOWN, base_dir),
            "/test/backup/unknown"
        )

        # Test invalid category
        with self.assertRaises(ValueError):
            self.categorizer.get_target_directory(FileCategory.SIDECAR, base_dir)

    @patch('os.makedirs')
    def test_ensure_target_directories(self, mock_makedirs):
        """Eager directory creation covers every recognized category but unknown.

        backup/unknown/ is deliberately NOT pre-created here: it must exist only
        once an unrecognized file is actually routed into it, never as an empty
        phantom (issue #29). It is created lazily during processing instead.
        """
        base_dir = "/test/backup"

        result = self.categorizer.ensure_target_directories(base_dir)

        expected_dirs = [
            "/test/backup/photos",
            "/test/backup/videos",
            "/test/backup/screenshots",
            "/test/backup/generated",
        ]

        self.assertEqual(result, expected_dirs)
        self.assertNotIn("/test/backup/unknown", result)

        # Check that makedirs was called for each directory
        self.assertEqual(mock_makedirs.call_count, 4)
        for expected_dir in expected_dirs:
            mock_makedirs.assert_any_call(expected_dir, exist_ok=True)

    def test_get_categorization_stats(self):
        """Test getting categorization statistics"""
        # Setup test data
        self.categorizer.categorized_files[FileCategory.PHOTO] = ["p1.jpg", "p2.jpg"]
        self.categorizer.categorized_files[FileCategory.VIDEO] = ["v1.mov"]
        self.categorizer.categorized_files[FileCategory.SCREENSHOT] = ["s1.png"]
        self.categorizer.categorized_files[FileCategory.SIDECAR] = ["sc1.aae", "sc2.aae"]
        self.categorizer.categorized_files[FileCategory.UNKNOWN] = []

        stats = self.categorizer.get_categorization_stats()

        expected = {
            'photos': 2,
            'videos': 1,
            'screenshots': 1,
            'generated': 0,
            'unknown': 0,
            'sidecar': 2,
            'total': 6
        }

        self.assertEqual(stats, expected)

    def test_get_file_summary(self):
        """Test getting human-readable file summary"""
        # Setup test data
        self.categorizer.categorized_files[FileCategory.PHOTO] = ["p1.jpg"]
        self.categorizer.categorized_files[FileCategory.VIDEO] = ["v1.mov"]
        self.categorizer.categorized_files[FileCategory.SCREENSHOT] = []
        self.categorizer.categorized_files[FileCategory.SIDECAR] = ["sc1.aae"]
        self.categorizer.categorized_files[FileCategory.UNKNOWN] = []

        summary = self.categorizer.get_file_summary()

        # Check that summary contains expected information
        self.assertIn("File Categorization Summary:", summary)
        self.assertIn("Photos: 1 files", summary)
        self.assertIn("Videos: 1 files", summary)
        self.assertIn("Screenshots: 0 files", summary)
        self.assertIn("Sidecar (to delete): 1 files", summary)
        self.assertIn("Total: 3 files", summary)

    def test_clear_categorization(self):
        """Test clearing all categorized files"""
        # Setup test data
        for category in FileCategory:
            self.categorizer.categorized_files[category] = ["test_file"]

        self.categorizer.clear_categorization()

        # All categories should be empty
        for category in FileCategory:
            self.assertEqual(len(self.categorizer.categorized_files[category]), 0)

    def test_extension_case_insensitivity(self):
        """Test that file extension matching is case insensitive"""
        test_cases = [
            ("photo.JPG", FileCategory.PHOTO),
            ("video.MOV", FileCategory.VIDEO),
            ("sidecar.AAE", FileCategory.SIDECAR),
            ("photo.Jpeg", FileCategory.PHOTO),
            ("video.Mp4", FileCategory.VIDEO)
        ]

        for filename, expected_category in test_cases:
            file_path = self.create_test_file(filename)
            category = self.categorizer.categorize_file(file_path)
            self.assertEqual(category, expected_category, f"Failed for {filename}")


class TestGeneratedContentDetection(unittest.TestCase):
    """Test cases for FileCategorizer._is_generated_content"""

    def setUp(self):
        """Set up test fixtures"""
        self.categorizer = FileCategorizer()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up test fixtures"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ai_marker_in_png_text(self):
        """PNG text chunks containing AI markers are detected as generated"""
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo

        file_path = os.path.join(self.temp_dir, "ai_image.png")
        image = Image.new('RGB', (10, 10), color='green')
        metadata = PngInfo()
        metadata.add_text("Comment", "Created with ChatGPT / OpenAI")
        image.save(file_path, pnginfo=metadata)

        self.assertTrue(self.categorizer._is_generated_content(file_path))

    def test_plain_png_not_generated(self):
        """A plain PNG with no AI markers is not flagged as generated"""
        from PIL import Image

        file_path = os.path.join(self.temp_dir, "plain.png")
        Image.new('RGB', (10, 10), color='blue').save(file_path)

        self.assertFalse(self.categorizer._is_generated_content(file_path))

    def test_editing_software_without_original_timestamp(self):
        """Editing software with no original timestamp is flagged as generated"""
        from PIL import Image
        from PIL.ExifTags import TAGS

        # Resolve the numeric tag id for 'Software'
        software_tag = next(tag_id for tag_id, name in TAGS.items() if name == 'Software')

        file_path = os.path.join(self.temp_dir, "edited.jpg")
        image = Image.new('RGB', (10, 10), color='red')
        exif = image.getexif()
        exif[software_tag] = "Adobe Photoshop 2024"
        image.save(file_path, exif=exif)

        self.assertTrue(self.categorizer._is_generated_content(file_path))

    def test_uuid_stem_detected(self):
        """A UUID-style filename stem is flagged as generated"""
        from PIL import Image

        # 36-char UUID stem with four dashes
        file_path = os.path.join(
            self.temp_dir, "12345678-1234-1234-1234-123456789abc.jpg"
        )
        Image.new('RGB', (10, 10), color='red').save(file_path)

        self.assertTrue(self.categorizer._is_generated_content(file_path))


class TestFileCategorization_EdgeCases(unittest.TestCase):
    """Test edge cases and special scenarios for file categorization"""

    def setUp(self):
        """Set up test fixtures"""
        self.categorizer = FileCategorizer()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up test fixtures"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_test_file(self, filename: str) -> str:
        """Create a test file in temp directory"""
        file_path = os.path.join(self.temp_dir, filename)
        Path(file_path).touch()
        return file_path

    def test_png_screenshot_vs_photo_distinction(self):
        """Test that PNG files are correctly distinguished between screenshots and photos"""
        # PNG files that should be screenshots
        screenshot_files = [
            "Screenshot 2024-01-15.png",
            "IMG_1234.png",
            "Screen Shot 2024-01-15.png"
        ]

        # PNG files that should be photos
        photo_files = [
            "logo.png",
            "image.png",
            "graphic.png"
        ]

        for filename in screenshot_files:
            file_path = self.create_test_file(filename)
            category = self.categorizer.categorize_file(file_path)
            self.assertEqual(category, FileCategory.SCREENSHOT, f"Should be screenshot: {filename}")

        for filename in photo_files:
            file_path = self.create_test_file(filename)
            category = self.categorizer.categorize_file(file_path)
            self.assertEqual(category, FileCategory.PHOTO, f"Should be photo: {filename}")

    def test_files_without_extensions(self):
        """Test categorization of files without extensions"""
        file_path = self.create_test_file("filename_no_extension")
        category = self.categorizer.categorize_file(file_path)
        self.assertEqual(category, FileCategory.UNKNOWN)

    def test_files_with_multiple_dots(self):
        """Test files with multiple dots in filename"""
        test_cases = [
            ("photo.backup.jpg", FileCategory.PHOTO),
            ("video.final.mov", FileCategory.VIDEO),
            ("file.old.aae", FileCategory.SIDECAR)
        ]

        for filename, expected_category in test_cases:
            file_path = self.create_test_file(filename)
            category = self.categorizer.categorize_file(file_path)
            self.assertEqual(category, expected_category, f"Failed for {filename}")

    def test_empty_filename_handling(self):
        """Edge-case filenames resolve to UNKNOWN and never crash the categorizer.

        None of these carry a recognized extension (``Path('.jpg').suffix`` is
        ``''``), so each must categorize as UNKNOWN. ``categorize_file`` parses
        the path only, so the inputs need not exist on disk and no exception is
        expected -- if the categorizer raises, this test errors instead of
        swallowing it in a bare ``except`` (the previous version could not fail).
        """
        edge_cases = [
            ".jpg",  # Hidden-file name; suffix is '' -> no recognized extension
            ".",     # Current directory
            "..",    # Parent directory
        ]

        for filename in edge_cases:
            category = self.categorizer.categorize_file(filename)
            self.assertEqual(
                category,
                FileCategory.UNKNOWN,
                f"expected UNKNOWN for {filename!r}, got {category}",
            )


if __name__ == '__main__':
    unittest.main()