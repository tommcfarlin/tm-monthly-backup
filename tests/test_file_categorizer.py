"""
Test suite for file categorization functionality
"""

import unittest
import tempfile
import os
from enum import Enum
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.file_categorizer import FileCategorizer, FileCategory
from tests.fixtures import make_exif_jpeg, make_png_with_text


def _is_generated(categorizer, file_path):
    """
    Test helper: read a file's metadata once, then call the no-I/O
    ``_is_generated_content`` (issue #24 changed its signature from a path
    to pre-read ``(exif, ifd0, png_info)`` -- the ``ifd0`` split is
    fix-round-1 finding 1). Mirrors exactly what
    ``FileCategorizer._categorize_image_or_generated`` does internally.
    """
    metadata = categorizer._read_image_metadata(file_path)
    if metadata is None:
        return False
    exif, ifd0, png_info = metadata
    return categorizer._is_generated_content(file_path, exif, ifd0, png_info)


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

        # Check that the class-level extension sets categorize_file consults
        # are already lowercase (issue #43: the per-instance photo_exts/
        # video_exts/screenshot_exts/sidecar_exts lowercasing comprehensions
        # were removed as no-ops -- every source constant below was already
        # lowercase -- so this now reads the class constants directly).
        self.assertIn('.jpg', FileCategorizer.PHOTO_EXTENSIONS)
        self.assertIn('.jpeg', FileCategorizer.PHOTO_EXTENSIONS)
        self.assertIn('.mov', FileCategorizer.VIDEO_EXTENSIONS)
        self.assertIn('.png', FileCategorizer.SCREENSHOT_EXTENSIONS)
        self.assertIn('.aae', FileCategorizer.SIDECAR_EXTENSIONS)

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

    def test_batch_categorize_result_survives_second_call(self):
        """A dict returned by batch_categorize is unaffected by a later call (#37).

        ``batch_categorize`` used to return ``self.categorized_files.copy()``:
        a shallow copy whose *values* were still the exact list objects the
        categorizer mutates in place. The very next call cleared those same
        lists via ``list.clear()``, so a caller's supposedly-independent
        snapshot silently emptied itself. Capturing the photo/video lists,
        running a second, unrelated ``batch_categorize`` on the same instance,
        and then re-checking the first result pins the fix: the first result
        must still show its original files.
        """
        photo = self.create_test_file("photo.jpg")
        video = self.create_test_file("video.mov")

        first = self.categorizer.batch_categorize([photo, video])
        self.assertEqual(first[FileCategory.PHOTO], [photo])
        self.assertEqual(first[FileCategory.VIDEO], [video])

        # A second, unrelated categorization on the SAME categorizer instance.
        other = self.create_test_file("other_photo.jpg")
        self.categorizer.batch_categorize([other])

        # The first caller's dict -- and its per-category lists -- must be
        # completely unaffected by the second call.
        self.assertEqual(
            first[FileCategory.PHOTO], [photo],
            "the first result's PHOTO list was mutated by a later batch_categorize call",
        )
        self.assertEqual(
            first[FileCategory.VIDEO], [video],
            "the first result's VIDEO list was silently emptied by a later call",
        )

    def test_batch_categorize_caller_mutation_does_not_affect_categorizer(self):
        """Mutating the returned dict/lists must not corrupt internal state (#37).

        The inverse direction of the aliasing bug: a caller that appends to or
        clears a list it received back from ``batch_categorize`` must not
        change what the categorizer itself reports afterward via
        ``get_categorization_stats`` / ``get_file_summary``.
        """
        photo = self.create_test_file("photo.jpg")
        video = self.create_test_file("video.mov")

        result = self.categorizer.batch_categorize([photo, video])

        # Caller mutates its own copy: clears one list, appends to another.
        result[FileCategory.VIDEO].clear()
        result[FileCategory.PHOTO].append("/not/a/real/file.jpg")

        stats = self.categorizer.get_categorization_stats()
        self.assertEqual(stats['photos'], 1, "caller mutation leaked into categorization stats")
        self.assertEqual(stats['videos'], 1, "caller mutation leaked into categorization stats")

    def test_batch_categorize_lists_are_not_internal_state(self):
        """No per-category list in the returned dict is the internal list object."""
        photo = self.create_test_file("photo.jpg")

        result = self.categorizer.batch_categorize([photo])

        for category in FileCategory:
            self.assertIsNot(
                result[category],
                self.categorizer.categorized_files[category],
                f"{category} list in the returned dict aliases internal state",
            )

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

    def test_new_category_needs_no_edit_to_target_directory_methods(self):
        """Pins AC2 (issue #43): adding a FileCategory member must require no
        edit to get_target_directory or ensure_target_directories.

        Python enums cannot be extended by subclassing once they have
        members, so this builds an independent, same-shape Enum with every
        existing FileCategory member plus one new ``ARCHIVE`` member, then
        patches the module-level ``FileCategory`` name inside
        file_categorizer.py to point at it. Both methods under test read
        that name as a global at call time, so this changes what "the enum"
        means to them without editing either method's source at all -- which
        is exactly the scenario AC2 describes (a *future* new category, not
        one either method was written with in mind).

        Before issue #43's fix this pinned a real bug: the old
        get_target_directory was a five-branch if/elif that raised
        ValueError for ARCHIVE (an unrecognized branch), and the old
        ensure_target_directories iterated a hand-written four-item list
        that never mentioned ARCHIVE, so ARCHIVE's directory was silently
        never created. Confirmed by stashing the src/ fix and re-running this
        test: it fails on both counts against the pre-#43 code.
        """
        class _FileCategoryPlusOne(Enum):
            PHOTO = "photos"
            VIDEO = "videos"
            SCREENSHOT = "screenshots"
            GENERATED = "generated"
            UNKNOWN = "unknown"
            SIDECAR = "sidecar"
            ARCHIVE = "archive"  # stand-in for a hypothetical new category

        base_dir = "/test/backup"

        with patch('src.file_categorizer.FileCategory', _FileCategoryPlusOne):
            # get_target_directory resolves the brand-new member via
            # category.value alone -- no ValueError, no branch to add.
            self.assertEqual(
                self.categorizer.get_target_directory(
                    _FileCategoryPlusOne.ARCHIVE, base_dir
                ),
                "/test/backup/archive",
            )
            # The "no target directory" behavior for the enum's SIDECAR-
            # equivalent member is preserved.
            with self.assertRaises(ValueError):
                self.categorizer.get_target_directory(
                    _FileCategoryPlusOne.SIDECAR, base_dir
                )

            # ensure_target_directories iterates the (patched) enum, so the
            # new member is created alongside every pre-existing recognized
            # category with no explicit mention anywhere in the method.
            with patch('os.makedirs') as mock_makedirs:
                result = self.categorizer.ensure_target_directories(base_dir)

        expected_dirs = [
            "/test/backup/photos",
            "/test/backup/videos",
            "/test/backup/screenshots",
            "/test/backup/generated",
            "/test/backup/archive",
        ]
        self.assertEqual(result, expected_dirs)
        self.assertEqual(mock_makedirs.call_count, len(expected_dirs))

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

    def test_categorization_results_order_independent(self):
        """Stats/summary are consistent no matter the order accessors run in (#37).

        ``get_categorization_stats`` and ``get_file_summary`` both read
        ``self.categorized_files`` fresh each call, and ``batch_categorize`` no
        longer hands out an aliased snapshot for a caller to accidentally
        corrupt in between. Interleaving the three calls in different orders
        must produce identical, correct results either way.
        """
        photo = self.create_test_file("photo.jpg")
        video = self.create_test_file("video.mov")

        # Order A: categorize, then stats, then summary.
        self.categorizer.batch_categorize([photo, video])
        stats_a = self.categorizer.get_categorization_stats()
        summary_a = self.categorizer.get_file_summary()

        # Order B: categorize (again, same instance), then summary, then stats.
        self.categorizer.batch_categorize([photo, video])
        summary_b = self.categorizer.get_file_summary()
        stats_b = self.categorizer.get_categorization_stats()

        self.assertEqual(stats_a, stats_b)
        self.assertEqual(summary_a, summary_b)
        self.assertEqual(stats_a['photos'], 1)
        self.assertEqual(stats_a['videos'], 1)

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

        self.assertTrue(_is_generated(self.categorizer, file_path))

    def test_plain_png_not_generated(self):
        """A plain PNG with no AI markers is not flagged as generated"""
        from PIL import Image

        file_path = os.path.join(self.temp_dir, "plain.png")
        Image.new('RGB', (10, 10), color='blue').save(file_path)

        self.assertFalse(_is_generated(self.categorizer, file_path))

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

        self.assertTrue(_is_generated(self.categorizer, file_path))

    def test_uuid_stem_detected(self):
        """A UUID-style filename stem is flagged as generated"""
        from PIL import Image

        # 36-char UUID stem with four dashes
        file_path = os.path.join(
            self.temp_dir, "12345678-1234-1234-1234-123456789abc.jpg"
        )
        Image.new('RGB', (10, 10), color='red').save(file_path)

        self.assertTrue(_is_generated(self.categorizer, file_path))


class TestScreenshotVsProvenancePrecedence(unittest.TestCase):
    """
    Issue #22: routing precedence between screenshot filename detection and
    AI/C2PA provenance detection, exercised through the real
    ``categorize_file`` entry point (not the private helpers directly), so
    these tests pin the actual routing decision a run makes.

    Decided precedence: provenance wins. A PNG carrying a genuine AI/C2PA
    marker resolves to GENERATED even when its name matches the screenshot
    convention -- metadata is evidence a generation tool actually wrote, a
    filename match is only a naming guess. Before this fix,
    ``categorize_file`` checked the screenshot filename pattern first and
    returned immediately on a match, so ``_is_generated_content`` was never
    even consulted for a screenshot-named PNG.
    """

    def setUp(self):
        self.categorizer = FileCategorizer()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ai_provenance_with_screenshot_filename_lands_in_generated(self):
        """A genuine AI marker outranks a screenshot-style filename.

        Fails against the old code: the old ``categorize_file`` matched
        ``'screenshot'`` in the filename and returned FileCategory.SCREENSHOT
        before ``_is_generated_content`` ever ran, so the AI marker below was
        detected but never consulted (confirmed by stashing this fix and
        rerunning -- see the task report).
        """
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo

        file_path = os.path.join(self.temp_dir, "Screenshot 2024-01-15.png")
        image = Image.new('RGB', (10, 10), color='green')
        pnginfo = PngInfo()
        pnginfo.add_text("Software", "ChatGPT / OpenAI DALL-E")
        image.save(file_path, pnginfo=pnginfo)

        category = self.categorizer.categorize_file(file_path)

        self.assertEqual(category, FileCategory.GENERATED)

    def test_ai_provenance_with_img_underscore_filename_lands_in_generated(self):
        """The same precedence holds for the IMG_ screenshot convention, not
        just the literal 'screenshot' pattern."""
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo

        file_path = os.path.join(self.temp_dir, "IMG_1001.png")
        image = Image.new('RGB', (10, 10), color='green')
        pnginfo = PngInfo()
        pnginfo.add_text("parameters", "Steps: 20, Sampler: Euler a, Model: sd-v1")
        image.save(file_path, pnginfo=pnginfo)

        category = self.categorizer.categorize_file(file_path)

        self.assertEqual(category, FileCategory.GENERATED)

    def test_screenshot_style_png_without_provenance_still_lands_in_screenshot(self):
        """No AI marker present: the screenshot filename fallback still applies.

        This is regression coverage, not new-behavior coverage -- it cannot
        fail against the old code, which reached FileCategory.SCREENSHOT for
        this exact input via the same filename check, just earlier in the
        function. It is here to pin that the reordering did not lose the
        ordinary (no-provenance) case.
        """
        from PIL import Image

        file_path = os.path.join(self.temp_dir, "Screenshot 2024-01-15.png")
        Image.new('RGB', (10, 10), color='green').save(file_path)

        category = self.categorizer.categorize_file(file_path)

        self.assertEqual(category, FileCategory.SCREENSHOT)

    def test_img_underscore_png_still_classifies_as_screenshot(self):
        """IMG_1001.PNG-style names still resolve to SCREENSHOT (issue #22
        acceptance criterion), even now that provenance is checked first.

        The owner's real export directory has 21 ``IMG_*.PNG`` files, 13 of
        them at exactly the iPhone 15/15 Pro screen resolution
        (1179x2556) -- unambiguously genuine screenshots. Apple cameras never
        emit PNG for a captured photo (camera output is HEIC/JPG), so a PNG
        carrying that camera-style name is not mistaken for a real photo by
        this rule; dropping the ``img_`` pattern would misfile files like
        this one as photos. This test uses the exact resolution to mirror
        that real-world case.

        This is regression coverage, not new-behavior coverage -- it cannot
        fail against the old code, which already matched this filename via
        the (redundant, three-times-repeated) ``img_`` checks. It pins that
        collapsing those three checks into one did not narrow what matches.
        """
        from PIL import Image

        file_path = os.path.join(self.temp_dir, "IMG_1001.PNG")
        Image.new('RGB', (1179, 2556), color='white').save(file_path)

        category = self.categorizer.categorize_file(file_path)

        self.assertEqual(category, FileCategory.SCREENSHOT)

    def test_img_underscore_camera_jpg_never_reaches_screenshot_logic(self):
        """IMG_1001.JPG (camera-extension, not PNG) never goes through the
        screenshot filename check at all -- SCREENSHOT_EXTENSIONS is PNG-only,
        so this always resolved to PHOTO, before and after this fix."""
        from PIL import Image

        file_path = os.path.join(self.temp_dir, "IMG_1001.JPG")
        Image.new('RGB', (10, 10), color='green').save(file_path, format="JPEG")

        category = self.categorizer.categorize_file(file_path)

        self.assertEqual(category, FileCategory.PHOTO)

    def test_unreadable_screenshot_named_png_falls_back_to_filename_alone(self):
        """A PNG that cannot be opened at all has no provenance evidence, so
        the filename convention is the only signal and must still decide it.
        """
        file_path = os.path.join(self.temp_dir, "IMG_9999.png")
        with open(file_path, "wb") as handle:
            handle.write(b"not actually a png")

        category = self.categorizer.categorize_file(file_path)

        self.assertEqual(category, FileCategory.SCREENSHOT)


class TestGeneratedContentPrecision(unittest.TestCase):
    """Precise AI/generated detection: no caption bleed, real UUID parsing (#8).

    These are the negative cases the original substring matcher lacked. The bare
    ``'ai'`` marker matched inside ordinary English words, so a photo captioned
    "Portrait of a chair on a trail" was filed into ``backup/generated/`` where a
    manual cleanup could delete it. Each false-positive word gets its own
    assertion, and each end-to-end assertion goes through ``categorize_file`` so
    a regression in the PHOTO-vs-GENERATED routing (not just the private helper)
    is caught.
    """

    def setUp(self):
        """Set up test fixtures"""
        self.categorizer = FileCategorizer()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up test fixtures"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Negative cases: ordinary caption words must NOT read as generated.  #
    # ------------------------------------------------------------------ #

    def test_caption_words_do_not_flag_as_generated(self):
        """Words that embed 'ai' as a substring must not flag as generated.

        One assertion per word so a single reintroduced token (e.g. a bare 'ai'
        or 'rain') is pinpointed rather than hidden behind an aggregate failure.
        """
        false_positive_words = [
            "chair", "trail", "portrait", "detail", "rain", "Spain",
        ]
        for word in false_positive_words:
            with self.subTest(word=word):
                path = make_png_with_text(
                    os.path.join(self.temp_dir, f"{word}.png"),
                    {"Description": f"A photo of a {word} in natural light"},
                )
                self.assertFalse(
                    _is_generated(self.categorizer, path),
                    f"caption containing {word!r} was misread as generated",
                )

    def test_caption_words_categorize_as_photo(self):
        """The same captions route to PHOTO through the public entry point."""
        for word in ["chair", "trail", "portrait", "detail", "rain", "Spain"]:
            with self.subTest(word=word):
                path = make_png_with_text(
                    os.path.join(self.temp_dir, f"cat_{word}.png"),
                    {"Comment": f"Portrait of a {word} on a trail"},
                )
                self.assertEqual(
                    self.categorizer.categorize_file(path),
                    FileCategory.PHOTO,
                    f"caption containing {word!r} was categorized GENERATED",
                )

    def test_generated_word_in_caption_does_not_flag(self):
        """The dropped 'generated' token no longer matches ordinary prose."""
        path = make_png_with_text(
            os.path.join(self.temp_dir, "prose.png"),
            {"Description": "Revenue generated by the trail cafe last autumn"},
        )
        self.assertFalse(_is_generated(self.categorizer, path))

    def test_non_uuid_36_char_four_hyphen_stem_not_flagged(self):
        """A 36-char, 4-hyphen NON-UUID stem is rejected by uuid.UUID parsing.

        The old shape-only heuristic (len == 36 and four hyphens) accepted this;
        the segment lengths match a UUID but 'zzzz...' is not hexadecimal.
        """
        stem = "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz"
        self.assertEqual(len(stem), 36)
        self.assertEqual(stem.count('-'), 4)
        path = make_exif_jpeg(
            os.path.join(self.temp_dir, f"{stem}.jpg"),
            date_time_original="2026:01:15 10:00:00",
        )
        self.assertFalse(_is_generated(self.categorizer, path))

    def test_edited_photo_with_capture_timestamp_not_flagged(self):
        """Editing software + a real sub-IFD capture time is NOT generated (#25).

        Before the sub-IFD was read here, ``has_original_timestamp`` only saw
        IFD0 and never found ``DateTimeOriginal`` (which lives in the sub-IFD),
        so any Photoshop-touched photo was misfiled as generated. Reading the
        sub-IFD restores the intended editing-software AND no-capture-time rule.
        """
        path = make_exif_jpeg(
            os.path.join(self.temp_dir, "retouched.jpg"),
            date_time_original="2026:03:10 14:22:05",
        )
        # Add editing software to IFD0 without disturbing the sub-IFD timestamp.
        from PIL import Image
        from PIL.ExifTags import TAGS
        software_tag = next(tid for tid, name in TAGS.items() if name == 'Software')
        with Image.open(path) as img:
            exif = img.getexif()
            exif[software_tag] = "Adobe Photoshop 2024"
            img.save(path, exif=exif)

        self.assertFalse(
            _is_generated(self.categorizer, path),
            "an edited photo that kept its capture timestamp was misfiled",
        )

    def test_software_tag_in_sub_ifd_only_does_not_flag_as_generated(self):
        """A ``Software`` tag written ONLY in the Exif sub-IFD must NOT flag (#24 fix-round-1, finding 1 -- CRITICAL).

        ``merge_exif_ifds`` merges IFD0 and the Exif sub-IFD for the
        capture-timestamp check issue #25 needs, but some EXIF writers place
        ``Software`` in the sub-IFD rather than IFD0. Feeding that merged
        view to the editing-software heuristic let such a tag flip an
        ordinary, undated photo to GENERATED -- something the pre-#24 code
        (which read only ``img.getexif()``, i.e. IFD0) never did, strictly
        widening issue #8's detection surface. The heuristic must see
        ``Software`` in IFD0 only; a sub-IFD-only ``Software`` tag, with no
        capture timestamp anywhere, must categorize as PHOTO.
        """
        from PIL import Image
        from PIL.ExifTags import Base

        path = os.path.join(self.temp_dir, "sub_ifd_software.jpg")
        image = Image.new('RGB', (10, 10), color='red')
        exif = image.getexif()
        # Software written ONLY into the Exif sub-IFD (0x8769), never IFD0.
        sub_ifd = exif.get_ifd(0x8769)
        sub_ifd[Base.Software.value] = "Adobe Photoshop 2024"
        image.save(path, exif=exif)

        # Confirm the fixture's layout matches the claim before asserting on
        # behavior: Software must be absent from IFD0 and present in the
        # sub-IFD, and no capture timestamp anywhere.
        with Image.open(path) as reopened:
            top = reopened.getexif()
            sub = top.get_ifd(0x8769)
        self.assertNotIn(Base.Software.value, top, "fixture leaked Software into IFD0")
        self.assertIn(Base.Software.value, sub, "fixture did not write Software into the sub-IFD")

        self.assertFalse(
            _is_generated(self.categorizer, path),
            "a Software tag written only in the Exif sub-IFD was misfiled as GENERATED",
        )
        self.assertEqual(
            self.categorizer.categorize_file(path),
            FileCategory.PHOTO,
            "a Software tag written only in the Exif sub-IFD was misfiled as GENERATED",
        )

    # ------------------------------------------------------------------ #
    # Positive cases: genuine provenance must still be caught.            #
    # ------------------------------------------------------------------ #

    def test_software_key_chatgpt_value_flags(self):
        """A word-boundary 'ChatGPT' in a text value is still detected."""
        path = make_png_with_text(
            os.path.join(self.temp_dir, "gpt.png"),
            {"Software": "ChatGPT"},
        )
        self.assertTrue(_is_generated(self.categorizer, path))
        self.assertEqual(
            self.categorizer.categorize_file(path), FileCategory.GENERATED
        )

    def test_openai_value_flags(self):
        """An 'openai' token at a word boundary is detected."""
        path = make_png_with_text(
            os.path.join(self.temp_dir, "oa.png"),
            {"Comment": "Generated by openai"},
        )
        self.assertTrue(_is_generated(self.categorizer, path))

    def test_c2pa_text_key_flags(self):
        """A 'c2pa' provenance KEY flags regardless of its value text."""
        path = make_png_with_text(
            os.path.join(self.temp_dir, "c2pa.png"),
            {"c2pa": "irrelevant caption about a chair on a trail"},
        )
        self.assertTrue(_is_generated(self.categorizer, path))

    def test_stable_diffusion_parameters_key_flags(self):
        """A Stable Diffusion 'parameters' KEY flags on presence."""
        path = make_png_with_text(
            os.path.join(self.temp_dir, "sd.png"),
            {"parameters": "a mountain, steps: 20, sampler: Euler a"},
        )
        self.assertTrue(_is_generated(self.categorizer, path))

    def test_real_uuid_stem_flags(self):
        """A genuine uuid4() stem is flagged via uuid.UUID parsing."""
        import uuid as uuid_mod
        stem = str(uuid_mod.uuid4())
        path = make_png_with_text(
            os.path.join(self.temp_dir, f"{stem}.png"),
            {"Comment": "an ordinary caption"},
        )
        self.assertTrue(_is_generated(self.categorizer, path))

    def test_editing_software_without_any_timestamp_still_flags(self):
        """Editing software with no capture timestamp at all remains generated."""
        from PIL import Image
        from PIL.ExifTags import TAGS
        software_tag = next(tid for tid, name in TAGS.items() if name == 'Software')
        path = os.path.join(self.temp_dir, "synthetic.jpg")
        image = Image.new('RGB', (10, 10), color='red')
        exif = image.getexif()
        exif[software_tag] = "GIMP 2.10"
        image.save(path, exif=exif)

        self.assertTrue(_is_generated(self.categorizer, path))


class TestPngProvenanceProbeDoesNotDecode(unittest.TestCase):
    """PNG text-chunk provenance is read without a full pixel decode (#44).

    ``PngImageFile.text`` calls ``self.load()`` before returning, because
    tEXt/iTXt chunks are legally permitted to follow IDAT and Pillow will not
    report a partial answer -- so probing ``.text`` decodes the whole image
    purely to read metadata. ``img.info`` is populated while ``Image.open()``
    parses the chunk stream and already holds every chunk written before
    IDAT, which covers every mainstream generator/C2PA marker, at no extra
    decode cost.

    Pillow's own decode boundary is ``Image.tile``: it starts as a non-empty
    list of pending decode ops and ``Image.load()`` (called directly or via
    any Pillow API documented to force a load) empties it once the pixel data
    has actually been read. Asserting on ``img.tile`` -- rather than mocking
    ``_png_text_has_ai_provenance`` or stubbing Pillow -- means these tests
    exercise the real decode boundary the issue is about.
    """

    def setUp(self):
        self.categorizer = FileCategorizer()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ai_marker_text_chunk_probed_without_decode(self):
        """A PNG whose text chunk carries an AI marker is read undecoded."""
        from PIL import Image

        path = make_png_with_text(
            os.path.join(self.temp_dir, "gpt.png"),
            {"Comment": "Created with ChatGPT / OpenAI"},
        )

        with Image.open(path) as img:
            self.assertTrue(
                img.tile, "fixture PNG was already decoded before the probe"
            )
            result = self.categorizer._png_text_has_ai_provenance(img)
            self.assertTrue(
                img.tile,
                "_png_text_has_ai_provenance forced a full pixel decode "
                "(img.tile was emptied) merely to read a text chunk",
            )

        self.assertTrue(result, "the AI marker should still be detected")

    def test_c2pa_key_probed_without_decode(self):
        """A PNG carrying a bare 'c2pa' provenance key is read undecoded."""
        from PIL import Image

        path = make_png_with_text(
            os.path.join(self.temp_dir, "c2pa.png"),
            {"c2pa": "manifest-stub"},
        )

        with Image.open(path) as img:
            self.assertTrue(img.tile)
            result = self.categorizer._png_text_has_ai_provenance(img)
            self.assertTrue(
                img.tile,
                "_png_text_has_ai_provenance forced a full pixel decode "
                "merely to read the c2pa key",
            )

        self.assertTrue(result)

    def test_plain_png_probed_without_decode(self):
        """A PNG with no provenance markers is also read without decoding."""
        from PIL import Image

        path = make_png_with_text(
            os.path.join(self.temp_dir, "plain.png"),
            {"Comment": "An ordinary caption about a chair on a trail"},
        )

        with Image.open(path) as img:
            self.assertTrue(img.tile)
            result = self.categorizer._png_text_has_ai_provenance(img)
            self.assertTrue(
                img.tile,
                "_png_text_has_ai_provenance forced a full pixel decode "
                "even though no provenance marker was present",
            )

        self.assertFalse(result)

    def test_binary_icc_profile_chunk_does_not_cause_false_positive(self):
        """A marker word embedded in binary ``icc_profile`` bytes must not flag.

        ``img.info`` -- unlike ``img.text`` -- carries every PNG ancillary
        chunk Pillow parses before IDAT, including binary ones: ``icc_profile``
        and raw ``exif`` land there as ``bytes``, never as ``str``. Scanning
        ``str(value)`` for every ``info`` entry (rather than only genuinely
        text-typed values) would search the *byte-repr* of those blobs too --
        a real widening of the match surface beyond what ``img.text`` ever
        exposed, which issue #44 requires this read-path change to avoid. The
        ICC profile below deliberately contains the ASCII bytes "Firefly" at a
        word boundary in its ``str()`` repr, and a genuine ``eXIf`` chunk is
        also present (also binary), alongside an ordinary, non-matching text
        chunk -- the categorization result must depend only on the text
        chunk, not on either binary chunk's contents.
        """
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo
        from PIL.ExifTags import TAGS

        path = os.path.join(self.temp_dir, "icc_false_positive.png")
        image = Image.new("RGB", (16, 16), color="green")

        # Sizeable binary ICC profile whose str() repr contains "Firefly" at
        # a word boundary -- exactly the shape a value-type-blind scan over
        # img.info would mis-detect.
        icc_profile = b"ICC PROFILE WITH Firefly INSIDE" + bytes(512)

        exif = image.getexif()
        software_tag = next(tid for tid, name in TAGS.items() if name == "Software")
        exif[software_tag] = "TestCam 1.0"

        metadata = PngInfo()
        metadata.add_text("Comment", "A family photo from the lake house")

        image.save(path, pnginfo=metadata, icc_profile=icc_profile, exif=exif)

        with Image.open(path) as img:
            self.assertIn("icc_profile", img.info)
            self.assertIsInstance(img.info["icc_profile"], bytes)
            self.assertIn("exif", img.info)
            self.assertIsInstance(img.info["exif"], bytes)
            result = self.categorizer._png_text_has_ai_provenance(img)
            self.assertTrue(
                img.tile,
                "_png_text_has_ai_provenance forced a full pixel decode",
            )

        self.assertFalse(
            result,
            "a marker word embedded in the binary icc_profile chunk was "
            "matched as if it were text provenance",
        )

    def test_genuine_marker_still_detected_alongside_binary_chunks(self):
        """A real text-chunk marker is still caught when binary chunks coexist.

        Mirrors the false-positive test's fixture shape (binary ``icc_profile``
        and ``exif`` chunks alongside a text chunk) but with the text chunk
        actually carrying a marker, confirming the type filter that rejects
        binary values does not also reject the genuine str-typed text chunks
        it is meant to keep scanning.
        """
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo
        from PIL.ExifTags import TAGS

        path = os.path.join(self.temp_dir, "icc_true_positive.png")
        image = Image.new("RGB", (16, 16), color="green")

        # No marker words in this ICC profile -- only the text chunk below
        # should be able to trigger a match.
        icc_profile = b"GENERIC DISPLAY PROFILE" + bytes(512)

        exif = image.getexif()
        software_tag = next(tid for tid, name in TAGS.items() if name == "Software")
        exif[software_tag] = "TestCam 1.0"

        metadata = PngInfo()
        metadata.add_text("Comment", "Created with ChatGPT / OpenAI")

        image.save(path, pnginfo=metadata, icc_profile=icc_profile, exif=exif)

        with Image.open(path) as img:
            self.assertIn("icc_profile", img.info)
            self.assertIn("exif", img.info)
            result = self.categorizer._png_text_has_ai_provenance(img)
            self.assertTrue(
                img.tile,
                "_png_text_has_ai_provenance forced a full pixel decode",
            )

        self.assertTrue(
            result, "the genuine text-chunk marker should still be detected"
        )


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


class TestReadImageMetadataExceptionNarrowing(unittest.TestCase):
    """``_read_image_metadata``'s except clause is narrowed, not bare (issue #39).

    ``batch_categorize``'s loop has no try/except of its own, so
    ``_read_image_metadata`` is the only guard between one bad file and the
    whole categorization pass. Before this fix it caught bare ``Exception``,
    which also caught -- and silently discarded at DEBUG -- a bug in a
    caller-supplied mock (e.g. an ``AssertionError``), making a test double's
    own mistake indistinguishable from a genuinely unreadable file. Narrowing
    to the specific exceptions ``Image.open``/``getexif`` actually raise lets
    a real bug propagate instead of vanishing.
    """

    def setUp(self):
        self.categorizer = FileCategorizer()

    def test_non_pillow_exception_is_not_swallowed(self):
        """A bug surfacing as AssertionError now propagates instead of vanishing.

        Fails against the old code: its bare ``except Exception`` caught this
        AssertionError and returned ``None``, so nothing ever reached
        ``assertRaises`` and the context manager itself raised
        "AssertionError not raised".
        """
        with patch("PIL.Image.open", side_effect=AssertionError("mock misconfigured")):
            with self.assertRaises(AssertionError):
                self.categorizer._read_image_metadata("/tmp/whatever.jpg")

    def test_decompression_bomb_is_still_caught(self):
        """An oversized-image failure still fails this one file, not the batch.

        ``Image.open`` raises ``PIL.Image.DecompressionBombError`` -- a plain
        ``Exception`` subclass, not an ``OSError`` -- for an image whose
        declared pixel count exceeds Pillow's safety limit. Regression
        coverage for the narrowed except clause: pins that this specific type
        is still included even though it is not an ``OSError``.
        """
        from PIL import Image

        with patch(
            "PIL.Image.open",
            side_effect=Image.DecompressionBombError("image too large"),
        ):
            result = self.categorizer._read_image_metadata("/tmp/huge.jpg")
        self.assertIsNone(result)

    def test_permission_error_returns_none_without_raising(self):
        """A permission-denied file is reported as unreadable, not raised.

        Regression coverage: ``OSError`` (and its ``PermissionError``
        subclass) was already covered by the old bare ``except Exception``;
        this pins that the narrowed clause still covers it.
        """
        with patch(
            "PIL.Image.open", side_effect=PermissionError(13, "Permission denied")
        ):
            result = self.categorizer._read_image_metadata("/tmp/locked.jpg")
        self.assertIsNone(result)

    def test_open_failure_logs_debug_message_naming_exception_type(self):
        """The open-failure message fires at DEBUG and names the exception type.

        Pins the actual claim behind the deliberate DEBUG-not-WARNING
        decision (documented inline and in the task report): the message
        still exists, still names the file and the exception type, just at a
        level that does not duplicate issue #58's own WARNING for the
        extensions where that gate independently re-checks and reports. A
        test that only asserts the return value (``None``) proves nothing
        about whether anything was logged at all -- this uses ``assertLogs``
        so a regression that silently drops the message (or moves it to a
        level ``assertLogs(level="DEBUG")`` would not even capture) fails
        here instead of only being caught by eye.

        Fails against the pre-#39 code too, in the opposite direction it
        might seem: the old bare ``except Exception`` DID log at DEBUG, but
        with a message of ``"Error reading image metadata for %s: %s"`` that
        never names the exception TYPE (only ``str(e)``) -- so the
        type-name assertion below fails against it.
        """
        with self.assertLogs("src.file_categorizer", level="DEBUG") as captured:
            with patch(
                "PIL.Image.open",
                side_effect=PermissionError(13, "Permission denied"),
            ):
                result = self.categorizer._read_image_metadata("/tmp/locked.jpg")

        self.assertIsNone(result)
        debug_records = [
            r for r in captured.records if r.name == "src.file_categorizer"
        ]
        self.assertTrue(debug_records, "expected a log record from file_categorizer")
        record = debug_records[0]
        # Deliberately DEBUG, not WARNING (see the inline comment on
        # _read_image_metadata and the task report's "Deviation" section):
        # issue #58's _is_decodable_image already independently WARNs and
        # quarantines for every extension where this is a real, actionable
        # failure, so warning again here would be redundant there and a
        # false alarm for every valid RAW file elsewhere.
        self.assertEqual(record.levelname, "DEBUG")
        message = record.getMessage()
        self.assertIn("/tmp/locked.jpg", message)
        self.assertIn("PermissionError", message)


if __name__ == '__main__':
    unittest.main()