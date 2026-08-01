"""
Test suite for EXIF timestamp extraction functionality
"""

import unittest
import tempfile
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from PIL import Image, ExifTags
import io

from src.exif_handler import ExifHandler


class TestExifHandler(unittest.TestCase):
    """Test cases for ExifHandler class"""

    def setUp(self):
        """Set up test fixtures"""
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up test fixtures"""
        # Clean up temp directory
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_test_image_with_exif(self, timestamp_str: str = "2024:01:15 14:30:45") -> str:
        """
        Create a test image file with EXIF timestamp data.

        Args:
            timestamp_str: EXIF timestamp string

        Returns:
            Path to created test image
        """
        # Create a simple test image
        image = Image.new('RGB', (100, 100), color='red')

        # Create EXIF data
        exif_dict = {
            ExifTags.TAGS['DateTimeOriginal']: timestamp_str,
            ExifTags.TAGS['DateTime']: timestamp_str,
        }

        # Convert to EXIF format
        exif_bytes = image._getexif() or {}
        for tag, value in exif_dict.items():
            # Find the numeric tag ID
            for tag_id, tag_name in ExifTags.TAGS.items():
                if tag_name == tag:
                    exif_bytes[tag_id] = value
                    break

        # Save image with EXIF data
        test_file = os.path.join(self.temp_dir, "test_image.jpg")
        image.save(test_file, exif=exif_bytes)

        return test_file

    def create_test_image_without_exif(self) -> str:
        """
        Create a test image file without EXIF data.

        Returns:
            Path to created test image
        """
        image = Image.new('RGB', (100, 100), color='blue')
        test_file = os.path.join(self.temp_dir, "no_exif_image.jpg")
        image.save(test_file)
        return test_file

    def test_init(self):
        """Test ExifHandler initialization"""
        handler = ExifHandler()
        self.assertIsInstance(handler.missing_exif_files, list)
        self.assertEqual(len(handler.missing_exif_files), 0)
        self.assertEqual(handler.TIMESTAMP_TAGS, [
            'DateTimeOriginal',
            'DateTime',
            'DateTimeDigitized'
        ])

    @patch('src.exif_handler.Image')
    def test_extract_timestamp_success(self, mock_image):
        """Test successful timestamp extraction"""
        # Mock image with EXIF data
        mock_img = MagicMock()
        mock_exif = {
            306: "2024:01:15 14:30:45"  # DateTime tag
        }
        mock_img.getexif.return_value = mock_exif
        mock_image.open.return_value.__enter__.return_value = mock_img

        # Mock TAGS mapping
        with patch.dict('src.exif_handler.TAGS', {306: 'DateTime'}):
            result = self.handler.extract_timestamp("test_file.jpg")

        expected = datetime(2024, 1, 15, 14, 30, 45)
        self.assertEqual(result, expected)
        self.assertEqual(len(self.handler.missing_exif_files), 0)

    @patch('src.exif_handler.Image')
    def test_extract_timestamp_no_exif(self, mock_image):
        """Test timestamp extraction when no EXIF data present"""
        # Mock image without EXIF data
        mock_img = MagicMock()
        mock_img.getexif.return_value = {}
        mock_image.open.return_value.__enter__.return_value = mock_img

        result = self.handler.extract_timestamp("test_file.jpg")

        self.assertIsNone(result)
        self.assertIn("test_file.jpg", self.handler.missing_exif_files)

    @patch('src.exif_handler.Image')
    def test_extract_timestamp_no_timestamp_tags(self, mock_image):
        """Test timestamp extraction when EXIF exists but no timestamp tags"""
        # Mock image with EXIF but no timestamp tags
        mock_img = MagicMock()
        mock_exif = {
            271: "Test Camera"  # Make tag (not a timestamp)
        }
        mock_img.getexif.return_value = mock_exif
        mock_image.open.return_value.__enter__.return_value = mock_img

        with patch.dict('src.exif_handler.TAGS', {271: 'Make'}):
            result = self.handler.extract_timestamp("test_file.jpg")

        self.assertIsNone(result)
        self.assertIn("test_file.jpg", self.handler.missing_exif_files)

    def test_parse_exif_datetime_valid(self):
        """Test parsing valid EXIF datetime string"""
        result = self.handler._parse_exif_datetime("2024:01:15 14:30:45", "test.jpg")
        expected = datetime(2024, 1, 15, 14, 30, 45)
        self.assertEqual(result, expected)

    def test_parse_exif_datetime_invalid(self):
        """Test parsing invalid EXIF datetime string"""
        result = self.handler._parse_exif_datetime("invalid_date", "test.jpg")
        self.assertIsNone(result)
        self.assertIn("test.jpg", self.handler.missing_exif_files)

    @patch('os.stat')
    def test_get_fallback_timestamp(self, mock_stat):
        """Test fallback timestamp from filesystem"""
        # Mock a stat result exposing the attributes get_fallback_timestamp reads.
        # st_birthtime != st_ctime so the creation-time branch is exercised.
        mock_timestamp = 1705330245.0  # 2024-01-15 14:30:45 UTC
        stat_result = Mock()
        stat_result.st_birthtime = mock_timestamp
        stat_result.st_ctime = 1000000000.0
        stat_result.st_mtime = 1600000000.0
        mock_stat.return_value = stat_result

        result = self.handler.get_fallback_timestamp("test_file.jpg")

        # Should return datetime object from the birthtime timestamp
        self.assertIsInstance(result, datetime)
        self.assertEqual(result, datetime.fromtimestamp(mock_timestamp))
        mock_stat.assert_called_once_with("test_file.jpg")

    @patch('os.stat')
    def test_get_fallback_timestamp_error(self, mock_stat):
        """Test fallback timestamp when filesystem error occurs"""
        # Mock OSError
        mock_stat.side_effect = OSError("File not found")

        result = self.handler.get_fallback_timestamp("test_file.jpg")

        # Should return current time as ultimate fallback
        self.assertIsInstance(result, datetime)
        # Should be very recent (within last few seconds)
        now = datetime.now()
        self.assertLess(abs((now - result).total_seconds()), 5)

    def test_format_timestamp_filename(self):
        """Test timestamp formatting for filename"""
        dt = datetime(2024, 1, 15, 14, 30, 45)
        result = self.handler.format_timestamp_filename(dt)
        self.assertEqual(result, "2024.01.15.14.30.45")

    def test_handle_duplicate_timestamp_no_conflict(self):
        """Test duplicate handling when no conflict exists"""
        timestamp = datetime(2024, 1, 15, 14, 30, 45)
        existing = set()

        result = self.handler.handle_duplicate_timestamp(timestamp, existing)

        self.assertEqual(result, timestamp)

    def test_handle_duplicate_timestamp_with_conflict(self):
        """Test duplicate handling when conflict exists"""
        timestamp = datetime(2024, 1, 15, 14, 30, 45)
        existing = {"2024.01.15.14.30.45"}

        result = self.handler.handle_duplicate_timestamp(timestamp, existing)

        # Should increment seconds
        expected = datetime(2024, 1, 15, 14, 30, 46)
        self.assertEqual(result, expected)

    def test_handle_duplicate_timestamp_multiple_conflicts(self):
        """Test duplicate handling with multiple conflicts"""
        timestamp = datetime(2024, 1, 15, 14, 30, 45)
        existing = {
            "2024.01.15.14.30.45",
            "2024.01.15.14.30.46",
            "2024.01.15.14.30.47"
        }

        result = self.handler.handle_duplicate_timestamp(timestamp, existing)

        # Should find first available slot
        expected = datetime(2024, 1, 15, 14, 30, 48)
        self.assertEqual(result, expected)

    def test_handle_duplicate_timestamp_second_overflow(self):
        """Test duplicate handling with second overflow"""
        timestamp = datetime(2024, 1, 15, 14, 30, 59)
        existing = {"2024.01.15.14.30.59"}

        result = self.handler.handle_duplicate_timestamp(timestamp, existing)

        # Should increment to next minute
        expected = datetime(2024, 1, 15, 14, 31, 0)
        self.assertEqual(result, expected)

    def test_get_missing_exif_files(self):
        """Test retrieving list of missing EXIF files"""
        # Add some test files to missing list
        self.handler.missing_exif_files.extend(["file1.jpg", "file2.jpg"])

        result = self.handler.get_missing_exif_files()

        self.assertEqual(result, ["file1.jpg", "file2.jpg"])
        # Should return a copy, not the original list
        self.assertIsNot(result, self.handler.missing_exif_files)

    def test_clear_missing_files_log(self):
        """Test clearing missing files log"""
        # Add some test files
        self.handler.missing_exif_files.extend(["file1.jpg", "file2.jpg"])

        self.handler.clear_missing_files_log()

        self.assertEqual(len(self.handler.missing_exif_files), 0)

    @patch('src.exif_handler.Image')
    def test_extract_timestamp_prefers_datetime_original(self, mock_image):
        """Test that DateTimeOriginal is preferred over other timestamp tags"""
        # Mock image with multiple timestamp tags
        mock_img = MagicMock()
        mock_exif = {
            306: "2024:01:15 10:00:00",  # DateTime
            36867: "2024:01:15 14:30:45",  # DateTimeOriginal (should be preferred)
            36868: "2024:01:15 15:00:00"   # DateTimeDigitized
        }
        mock_img.getexif.return_value = mock_exif
        mock_image.open.return_value.__enter__.return_value = mock_img

        # Mock TAGS mapping
        tags_mapping = {
            306: 'DateTime',
            36867: 'DateTimeOriginal',
            36868: 'DateTimeDigitized'
        }

        with patch.dict('src.exif_handler.TAGS', tags_mapping):
            result = self.handler.extract_timestamp("test_file.jpg")

        # Should prefer DateTimeOriginal
        expected = datetime(2024, 1, 15, 14, 30, 45)
        self.assertEqual(result, expected)

    @patch('src.exif_handler.Image')
    def test_extract_timestamp_file_error(self, mock_image):
        """Test timestamp extraction when file cannot be opened"""
        # Mock file open error
        mock_image.open.side_effect = IOError("Cannot open file")

        result = self.handler.extract_timestamp("nonexistent.jpg")

        self.assertIsNone(result)
        self.assertIn("nonexistent.jpg", self.handler.missing_exif_files)


class TestSubIfdTimestampExtraction(unittest.TestCase):
    """
    Real-fixture tests for issue #25: DateTimeOriginal lives in the Exif
    sub-IFD (behind pointer 0x8769), not IFD0. These tests build genuine
    on-disk JPEGs whose timestamp tags are written into the sub-IFD, then
    assert the round-trip landed there before exercising extract_timestamp.

    A fixture that accidentally writes DateTimeOriginal into IFD0 would pass
    against the pre-#25 code and prove nothing, so every fixture asserts the
    tag is present in get_ifd(0x8769) and ABSENT from the top-level getexif().
    """

    EXIF_IFD = 0x8769

    def setUp(self):
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_jpeg(self, name, sub_ifd_tags=None, ifd0_tags=None):
        """
        Write a JPEG whose sub_ifd_tags go into the Exif sub-IFD and whose
        ifd0_tags go into IFD0. Tag keys are ExifTags.Base members.

        Returns the file path.
        """
        image = Image.new('RGB', (16, 16), color='red')
        exif = Image.Exif()

        for tag, value in (ifd0_tags or {}).items():
            exif[tag.value] = value

        if sub_ifd_tags:
            sub = exif.get_ifd(self.EXIF_IFD)
            for tag, value in sub_ifd_tags.items():
                sub[tag.value] = value

        path = os.path.join(self.temp_dir, name)
        image.save(path, format='JPEG', exif=exif)
        return path

    def _assert_in_sub_ifd_only(self, path, tag):
        """
        Guard: verify `tag` round-tripped into the Exif sub-IFD and is NOT in
        top-level getexif(). If this fails the fixture is worthless.
        """
        reopened = Image.open(path)
        top = reopened.getexif()
        sub = top.get_ifd(self.EXIF_IFD)
        self.assertIn(
            tag.value, sub,
            f"{tag.name} did not land in the Exif sub-IFD; fixture is invalid",
        )
        self.assertNotIn(
            tag.value, top,
            f"{tag.name} leaked into IFD0; fixture would pass against broken code",
        )

    def test_fixture_writes_datetimeoriginal_to_sub_ifd(self):
        """The fixture recipe provably targets the sub-IFD, not IFD0."""
        path = self._write_jpeg(
            "sub_only.jpg",
            sub_ifd_tags={ExifTags.Base.DateTimeOriginal: "2018:01:02 03:04:05"},
        )
        self._assert_in_sub_ifd_only(path, ExifTags.Base.DateTimeOriginal)

    def test_datetimeoriginal_in_sub_ifd_is_resolved(self):
        """
        A JPEG whose only timestamp is DateTimeOriginal in the sub-IFD resolves
        to that value and is NOT recorded as missing EXIF (AC #1). This FAILS
        against the pre-#25 reader, which only sees IFD0.
        """
        path = self._write_jpeg(
            "sub_only.jpg",
            sub_ifd_tags={ExifTags.Base.DateTimeOriginal: "2018:01:02 03:04:05"},
        )
        self._assert_in_sub_ifd_only(path, ExifTags.Base.DateTimeOriginal)

        result = self.handler.extract_timestamp(path)

        self.assertEqual(result, datetime(2018, 1, 2, 3, 4, 5))
        self.assertNotIn(path, self.handler.missing_exif_files)

    def test_sub_ifd_original_wins_over_ifd0_datetime(self):
        """
        DateTimeOriginal (sub-IFD) must win over a differing DateTime (IFD0),
        honoring declared priority (AC #2).
        """
        path = self._write_jpeg(
            "both.jpg",
            sub_ifd_tags={ExifTags.Base.DateTimeOriginal: "2018:01:02 03:04:05"},
            ifd0_tags={ExifTags.Base.DateTime: "2019:03:04 05:06:07"},
        )
        self._assert_in_sub_ifd_only(path, ExifTags.Base.DateTimeOriginal)

        result = self.handler.extract_timestamp(path)

        self.assertEqual(result, datetime(2018, 1, 2, 3, 4, 5))

    def test_datetime_only_still_resolves(self):
        """
        Fallback: an image with only DateTime in IFD0 (no sub-IFD) still
        resolves to DateTime.
        """
        path = self._write_jpeg(
            "dt_only.jpg",
            ifd0_tags={ExifTags.Base.DateTime: "2019:03:04 05:06:07"},
        )

        # Confirm there is no sub-IFD DateTimeOriginal to steal priority.
        reopened = Image.open(path)
        self.assertNotIn(
            ExifTags.Base.DateTimeOriginal.value,
            reopened.getexif().get_ifd(self.EXIF_IFD),
        )

        result = self.handler.extract_timestamp(path)

        self.assertEqual(result, datetime(2019, 3, 4, 5, 6, 7))
        self.assertNotIn(path, self.handler.missing_exif_files)

    def test_malformed_original_falls_through_to_datetime(self):
        """
        A malformed higher-priority DateTimeOriginal (sub-IFD) falls through to
        a valid DateTime (IFD0) instead of returning None (AC #3), and the file
        is not left flagged as missing EXIF.
        """
        path = self._write_jpeg(
            "malformed.jpg",
            sub_ifd_tags={ExifTags.Base.DateTimeOriginal: "not-a-date"},
            ifd0_tags={ExifTags.Base.DateTime: "2019:03:04 05:06:07"},
        )
        self._assert_in_sub_ifd_only(path, ExifTags.Base.DateTimeOriginal)

        result = self.handler.extract_timestamp(path)

        self.assertEqual(result, datetime(2019, 3, 4, 5, 6, 7))
        self.assertNotIn(path, self.handler.missing_exif_files)


class TestVideoTimestampExtraction(unittest.TestCase):
    """Test cases for ExifHandler._extract_video_timestamp"""

    def setUp(self):
        """Set up test fixtures"""
        self.handler = ExifHandler()

    @patch('src.exif_handler.HACHOIR_AVAILABLE', False)
    def test_extract_video_timestamp_no_hachoir(self):
        """When hachoir is unavailable, extraction returns None and logs the file"""
        result = self.handler._extract_video_timestamp("movie.mov")

        self.assertIsNone(result)
        self.assertIn("movie.mov", self.handler.missing_exif_files)

    @patch('src.exif_handler.HACHOIR_AVAILABLE', True)
    @patch('src.exif_handler.extractMetadata', create=True)
    @patch('src.exif_handler.createParser', create=True)
    def test_extract_video_timestamp_from_metadata(self, mock_create_parser, mock_extract_metadata):
        """A creation_date in video metadata is returned as a datetime"""
        expected = datetime(2024, 1, 15, 14, 30, 45)

        class FakeMetadata:
            creation_date = expected

        mock_create_parser.return_value = MagicMock()  # truthy context manager
        mock_extract_metadata.return_value = FakeMetadata()

        result = self.handler._extract_video_timestamp("movie.mov")

        self.assertEqual(result, expected)
        self.assertEqual(len(self.handler.missing_exif_files), 0)

    @patch('src.exif_handler.HACHOIR_AVAILABLE', True)
    @patch('src.exif_handler.createParser', create=True)
    def test_extract_video_timestamp_no_parser(self, mock_create_parser):
        """When no parser can be created, extraction returns None and logs the file"""
        mock_create_parser.return_value = None

        result = self.handler._extract_video_timestamp("movie.mov")

        self.assertIsNone(result)
        self.assertIn("movie.mov", self.handler.missing_exif_files)


class TestFilenameTimestampExtraction(unittest.TestCase):
    """Test cases for ExifHandler._extract_timestamp_from_filename"""

    def setUp(self):
        """Set up test fixtures"""
        self.handler = ExifHandler()

    def test_pattern_dash_separated(self):
        """Pattern 1: YYYY-MM-DD-HH-MM-SS style filenames"""
        result = self.handler._extract_timestamp_from_filename(
            "IMG_2024-01-15-14-30-45.jpg"
        )
        self.assertEqual(result, datetime(2024, 1, 15, 14, 30, 45))

    def test_pattern_compact(self):
        """Pattern 2: YYYYMMDD_HHMMSS style filenames"""
        result = self.handler._extract_timestamp_from_filename(
            "VID_20240115_143045.mov"
        )
        self.assertEqual(result, datetime(2024, 1, 15, 14, 30, 45))

    def test_no_match_returns_none(self):
        """Filenames without a recognizable date pattern return None"""
        result = self.handler._extract_timestamp_from_filename("Attachment-1.jpg")
        self.assertIsNone(result)


class TestExifHandlerIntegration(unittest.TestCase):
    """Integration tests for ExifHandler with real file operations"""

    def setUp(self):
        """Set up test fixtures"""
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up test fixtures"""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_timestamp_workflow_integration(self):
        """Test complete timestamp processing workflow"""
        # Test multiple files with different scenarios
        timestamps = [
            datetime(2024, 1, 15, 14, 30, 45),
            datetime(2024, 1, 15, 14, 30, 45),  # Duplicate
            datetime(2024, 1, 15, 14, 30, 47),
        ]

        existing_files = set()
        results = []

        for i, timestamp in enumerate(timestamps):
            # Handle duplicates
            adjusted = self.handler.handle_duplicate_timestamp(timestamp, existing_files)
            formatted = self.handler.format_timestamp_filename(adjusted)
            existing_files.add(formatted)
            results.append(formatted)

        expected = [
            "2024.01.15.14.30.45",
            "2024.01.15.14.30.46",  # Incremented due to duplicate
            "2024.01.15.14.30.47"
        ]

        self.assertEqual(results, expected)


if __name__ == '__main__':
    unittest.main()