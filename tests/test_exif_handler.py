"""
Test suite for EXIF timestamp extraction functionality
"""

import unittest
import tempfile
import os
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock
from PIL import Image, ExifTags

from src.exif_handler import ExifHandler
from tests.fixtures import (
    make_corrupt_jpeg,
    make_exif_jpeg,
    make_no_exif_jpeg,
    read_ifds,
)


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

    def _path(self, name: str) -> str:
        """Return an absolute path inside this test's temp directory."""
        return os.path.join(self.temp_dir, name)

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

    def test_fixture_produces_non_empty_exif(self):
        """
        The shared fixture writes real EXIF: a file it produces has a non-empty
        getexif() (acceptance guard against a helper that silently writes none).
        """
        path = make_exif_jpeg(
            self._path("has_exif.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        top, sub = read_ifds(path)

        self.assertTrue(top, "fixture produced an empty top-level getexif()")
        # DateTimeOriginal must be realistic: absent from IFD0, present in the
        # Exif sub-IFD (0x8769), matching how real cameras write it.
        self.assertNotIn(ExifTags.Base.DateTimeOriginal.value, top)
        self.assertIn(ExifTags.Base.DateTimeOriginal.value, sub)

    def test_extract_timestamp_success(self):
        """Timestamp extraction from a real DateTime tag in IFD0."""
        path = make_exif_jpeg(
            self._path("dt.jpg"),
            date_time="2024:01:15 14:30:45",
        )

        result = self.handler.extract_timestamp(path)

        self.assertEqual(result, datetime(2024, 1, 15, 14, 30, 45))
        self.assertEqual(len(self.handler.missing_exif_files), 0)

    def test_extract_timestamp_no_exif(self):
        """A real JPEG with no EXIF resolves to None and is logged as missing."""
        path = make_no_exif_jpeg(self._path("bare.jpg"))

        result = self.handler.extract_timestamp(path)

        self.assertIsNone(result)
        self.assertIn(path, self.handler.missing_exif_files)

    def test_extract_timestamp_no_timestamp_tags(self):
        """
        A real JPEG carrying EXIF but no timestamp tag resolves to None and is
        logged as missing. The Make tag lives in IFD0 and is not a timestamp.
        """
        image = Image.new("RGB", (48, 48), color="green")
        exif = image.getexif()
        exif[ExifTags.Base.Make.value] = "Test Camera"
        path = self._path("make_only.jpg")
        image.save(path, format="JPEG", exif=exif)

        result = self.handler.extract_timestamp(path)

        self.assertIsNone(result)
        self.assertIn(path, self.handler.missing_exif_files)

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

    # Tag-priority (DateTimeOriginal wins over DateTime) is exercised end-to-end
    # against real sub-IFD bytes in TestSubIfdTimestampExtraction below. The old
    # mock version of that test placed DateTimeOriginal in a flat dict (IFD0),
    # which is structurally unlike any real photo and passed against broken code
    # (issue #25 correction), so it was removed rather than converted.

    def test_extract_timestamp_corrupt_file(self):
        """A real, undecodable file resolves to None and is logged as missing."""
        path = make_corrupt_jpeg(self._path("corrupt.jpg"))

        result = self.handler.extract_timestamp(path)

        self.assertIsNone(result)
        self.assertIn(path, self.handler.missing_exif_files)

    @patch('src.exif_handler.Image')
    def test_extract_timestamp_open_error(self, mock_image):
        """
        A low-level open failure is caught, returns None, and logs the file.

        Kept as a mock: this pins the exception handler for I/O errors that are
        awkward to reproduce deterministically on disk (e.g. permission or
        device errors), distinct from the corrupt-header case above.
        """
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

        Delegates to the shared, self-verifying ``make_exif_jpeg`` builder so
        the sub-IFD layout is validated at build time; this method only adapts
        the dict-of-Base-members call convention these tests were written with.

        Returns the file path.
        """
        sub_ifd_tags = sub_ifd_tags or {}
        ifd0_tags = ifd0_tags or {}

        kwargs = {}
        if ExifTags.Base.DateTime in ifd0_tags:
            kwargs["date_time"] = ifd0_tags[ExifTags.Base.DateTime]
        if ExifTags.Base.DateTimeOriginal in sub_ifd_tags:
            kwargs["date_time_original"] = sub_ifd_tags[ExifTags.Base.DateTimeOriginal]
        if ExifTags.Base.DateTimeDigitized in sub_ifd_tags:
            kwargs["date_time_digitized"] = sub_ifd_tags[ExifTags.Base.DateTimeDigitized]

        return make_exif_jpeg(os.path.join(self.temp_dir, name), **kwargs)

    def _assert_in_sub_ifd_only(self, path, tag):
        """
        Guard: verify `tag` round-tripped into the Exif sub-IFD and is NOT in
        top-level getexif(). If this fails the fixture is worthless.
        """
        top, sub = read_ifds(path)
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
        _top, sub = read_ifds(path)
        self.assertNotIn(ExifTags.Base.DateTimeOriginal.value, sub)

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


class TestBoundaryTimestampFixtures(unittest.TestCase):
    """
    Boundary-date fixtures the suite historically lacked. Each builds a real
    JPEG whose DateTimeOriginal sits in the Exif sub-IFD, extracts it, and
    asserts the resulting YYYY.MM.DD.HH.MM.SS filename -- exercising the full
    read + format path across calendar edges.
    """

    def setUp(self):
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _extract_and_format(self, name, exif_datetime):
        """Build a sub-IFD DateTimeOriginal fixture, extract, and format."""
        path = make_exif_jpeg(
            os.path.join(self.temp_dir, name),
            date_time_original=exif_datetime,
        )
        result = self.handler.extract_timestamp(path)
        self.assertIsNotNone(result, f"{exif_datetime} failed to resolve")
        return self.handler.format_timestamp_filename(result)

    def test_leap_day(self):
        """Feb 29 on a leap year round-trips to the expected filename."""
        self.assertEqual(
            self._extract_and_format("leap.jpg", "2024:02:29 12:00:00"),
            "2024.02.29.12.00.00",
        )

    def test_year_end(self):
        """The final second of a year round-trips to the expected filename."""
        self.assertEqual(
            self._extract_and_format("yearend.jpg", "2023:12:31 23:59:59"),
            "2023.12.31.23.59.59",
        )

    def test_month_end(self):
        """The final second of a month round-trips to the expected filename."""
        self.assertEqual(
            self._extract_and_format("monthend.jpg", "2024:01:31 23:59:59"),
            "2024.01.31.23.59.59",
        )

    def test_invalid_exif_datetime_lands_in_missing(self):
        """
        A file whose only timestamp is a malformed EXIF string (impossible
        month/day/time) resolves to None and is recorded as missing EXIF,
        pinning the _parse_exif_datetime failure path against real bytes.
        """
        path = make_exif_jpeg(
            os.path.join(self.temp_dir, "invalid.jpg"),
            date_time_original="2024:13:45 99:99:99",
        )

        result = self.handler.extract_timestamp(path)

        self.assertIsNone(result)
        self.assertIn(path, self.handler.get_missing_exif_files())


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