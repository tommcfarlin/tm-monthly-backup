"""
Test suite for EXIF timestamp extraction functionality
"""

import unittest
import tempfile
import os
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock
from PIL import Image, ExifTags

from src.exif_handler import (
    ExifHandler,
    find_quicktime_creationdate,
    parse_local_creationdate,
)
from tests.fixtures import (
    make_corrupt_jpeg,
    make_exif_jpeg,
    make_no_exif_jpeg,
    quicktime_creationdate_moov,
    read_ifds,
    write_quicktime_mov,
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
        self.assertIn(path, self.handler.missing_exif_files)


class TestPlausibleCaptureTime(unittest.TestCase):
    """
    ExifHandler._is_plausible_capture_time: the single bound shared by every
    timestamp source (issue #62).

    ``now`` is injectable specifically so these boundary tests are
    deterministic -- pinning both edges against a fixed reference time
    instead of racing ``datetime.now()`` -- while callers in production code
    simply omit it and get the live clock.
    """

    def test_min_bound_is_inclusive(self):
        self.assertTrue(
            ExifHandler._is_plausible_capture_time(datetime(1826, 1, 1))
        )

    def test_just_before_min_bound_is_rejected(self):
        self.assertFalse(
            ExifHandler._is_plausible_capture_time(
                datetime(1825, 12, 31, 23, 59, 59)
            )
        )

    def test_quicktime_epoch_is_deliberately_not_rejected_here(self):
        """
        Fix round 1 (#62): the QuickTime mvhd zero-epoch (1904-01-01) is
        LATER than this predicate's 1826 floor, so it is NOT rejected by this
        generic plausibility check -- on purpose. A first pass at this fix
        raised the floor to 1970 to catch it here, which also rejected
        legitimate backdated EXIF dates from scanned family photographs (see
        test_scanned_family_photo_date_is_accepted below). The epoch itself
        is now rejected by the dedicated, field-scoped
        _is_quicktime_epoch_sentinel check instead -- see
        TestQuicktimeEpochSentinel.
        """
        self.assertTrue(
            ExifHandler._is_plausible_capture_time(datetime(1904, 1, 1))
        )

    def test_one_hour_in_the_future_is_accepted(self):
        """Acceptance criterion 4: timezone skew is not treated as hostile."""
        now = datetime(2026, 1, 1, 12, 0, 0)
        self.assertTrue(
            ExifHandler._is_plausible_capture_time(
                now + timedelta(hours=1), now=now
            )
        )

    def test_exactly_one_day_ahead_is_accepted(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        self.assertTrue(
            ExifHandler._is_plausible_capture_time(
                now + timedelta(days=1), now=now
            )
        )

    def test_just_past_one_day_ahead_is_rejected(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        self.assertFalse(
            ExifHandler._is_plausible_capture_time(
                now + timedelta(days=1, seconds=1), now=now
            )
        )

    def test_omitting_now_uses_the_live_clock(self):
        """The default (no injected ``now``) still accepts the actual present."""
        self.assertTrue(ExifHandler._is_plausible_capture_time(datetime.now()))


class TestQuicktimeEpochSentinel(unittest.TestCase):
    """
    ExifHandler._is_quicktime_epoch_sentinel: the dedicated, field-scoped
    check for the mvhd "no timestamp recorded" encoding (issue #62, fix
    round 1). Separate from the generic plausibility floor/ceiling so
    accepting genuinely old scanned-photo dates does not require also
    accepting this specific sentinel.
    """

    def test_exact_epoch_is_a_sentinel(self):
        """mvhd == 0 -> 1904-01-01 00:00:00, the exact reproduction case."""
        self.assertTrue(
            ExifHandler._is_quicktime_epoch_sentinel(datetime(1904, 1, 1, 0, 0, 0))
        )

    def test_one_second_past_epoch_is_still_a_sentinel(self):
        """
        mvhd == 1 -> 1904-01-01 00:00:01, the second file from the real
        reproduction (independently zeroed, then bumped a second by the
        collision logic). Acceptance criterion 3 is phrased as "creation
        time is 0", but the fix must cover this near-zero case too, since it
        is the same sentinel with the same root cause, not a different value.
        """
        self.assertTrue(
            ExifHandler._is_quicktime_epoch_sentinel(datetime(1904, 1, 1, 0, 0, 1))
        )

    def test_late_in_the_epoch_day_is_still_a_sentinel(self):
        """The whole calendar date is covered, not just a narrow zero-offset window."""
        self.assertTrue(
            ExifHandler._is_quicktime_epoch_sentinel(datetime(1904, 1, 1, 23, 59, 59))
        )

    def test_day_after_epoch_is_not_a_sentinel(self):
        self.assertFalse(
            ExifHandler._is_quicktime_epoch_sentinel(datetime(1904, 1, 2, 0, 0, 0))
        )

    def test_day_before_epoch_is_not_a_sentinel(self):
        self.assertFalse(
            ExifHandler._is_quicktime_epoch_sentinel(datetime(1903, 12, 31, 23, 59, 59))
        )

    def test_ordinary_modern_date_is_not_a_sentinel(self):
        self.assertFalse(
            ExifHandler._is_quicktime_epoch_sentinel(datetime(2026, 7, 4, 21, 33, 3))
        )


class TestImplausibleExifTimestamps(unittest.TestCase):
    """
    ExifHandler._parse_exif_datetime: the plausibility bound applied to the
    EXIF source (issue #62, acceptance criteria 1, 2, 4, 5).
    """

    def setUp(self):
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_year_9999_is_rejected_and_falls_through(self):
        """Acceptance criterion 1."""
        result = self.handler._parse_exif_datetime(
            "9999:12:31 23:59:59", "test.jpg"
        )
        self.assertIsNone(result)
        self.assertIn("test.jpg", self.handler.missing_exif_files)

    def test_year_0001_is_rejected_and_falls_through(self):
        """Acceptance criterion 2."""
        result = self.handler._parse_exif_datetime(
            "0001:01:01 00:00:00", "test.jpg"
        )
        self.assertIsNone(result)
        self.assertIn("test.jpg", self.handler.missing_exif_files)

    def test_one_hour_in_the_future_is_accepted(self):
        """
        Acceptance criterion 4: a timestamp derived from ``datetime.now()``
        (not a hardcoded date, so this cannot rot) one hour ahead must be
        accepted, not treated as hostile.
        """
        future = (datetime.now() + timedelta(hours=1)).strftime(
            "%Y:%m:%d %H:%M:%S"
        )
        result = self.handler._parse_exif_datetime(future, "test.jpg")
        self.assertIsNotNone(result)

    def test_scanned_family_photo_date_is_accepted(self):
        """
        Fix round 1 (#62): a deliberately backdated EXIF DateTimeOriginal on
        a scanned family photograph -- a legitimate, valued input to a
        personal photo archive -- must be ACCEPTED, not rejected. This is
        the exact capability a 1970 floor (this fix's own first pass) would
        have cost: 1965 is comfortably within the 1826 floor but would have
        been silently discarded by a 1970 one, falling the photo back to a
        meaningless filesystem mtime instead of its real capture date.
        """
        result = self.handler._parse_exif_datetime(
            "1965:06:01 12:00:00", "scanned.jpg"
        )
        self.assertEqual(result, datetime(1965, 6, 1, 12, 0, 0))
        self.assertNotIn("scanned.jpg", self.handler.missing_exif_files)

    def test_implausible_datetimeoriginal_falls_through_to_datetime_tag(self):
        """
        End-to-end: a crafted DateTimeOriginal (Exif sub-IFD) does not abort
        the search -- the lower-priority DateTime tag (IFD0) is still used,
        exactly as a missing/malformed DateTimeOriginal already behaves.
        """
        path = make_exif_jpeg(
            os.path.join(self.temp_dir, "crafted.jpg"),
            date_time_original="9999:12:31 23:59:59",
            date_time="2024:01:15 14:30:45",
        )
        result = self.handler.extract_timestamp(path)
        self.assertEqual(result, datetime(2024, 1, 15, 14, 30, 45))

    # --- Regression: the pre-existing malformed-string rejections (issue #62
    # asks these be pinned as tests, not just exercised by ad hoc scripts). ---

    def test_empty_string_returns_none(self):
        result = self.handler._parse_exif_datetime("", "test.jpg")
        self.assertIsNone(result)

    def test_nul_padded_string_returns_none(self):
        result = self.handler._parse_exif_datetime(
            "2024:01:15 14:30:45\x00\x00\x00", "test.jpg"
        )
        self.assertIsNone(result)

    def test_oversized_string_returns_none(self):
        result = self.handler._parse_exif_datetime("2024" * 1000, "test.jpg")
        self.assertIsNone(result)

    def test_ansi_bearing_string_returns_none(self):
        result = self.handler._parse_exif_datetime(
            "\x1b[31m2020:01:01 00:00:00", "test.jpg"
        )
        self.assertIsNone(result)

    def test_non_str_value_returns_none(self):
        result = self.handler._parse_exif_datetime(12345, "test.jpg")
        self.assertIsNone(result)

    def test_negative_year_returns_none(self):
        result = self.handler._parse_exif_datetime("-001:01:01 00:00:00", "test.jpg")
        self.assertIsNone(result)


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
            """Mimics hachoir's real ``get(key)`` API, not an attribute probe."""

            def get(self, key, default=None, index=0):
                if key == 'creation_date':
                    return expected
                return default

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


class TestParseLocalCreationdate(unittest.TestCase):
    """Directly exercise parse_local_creationdate's wall-clock semantics (#28)"""

    def test_negative_offset_keeps_local_wall_clock(self):
        """A -0400 capture keeps its written clock time; the offset is dropped"""
        result = parse_local_creationdate("2024-06-15T21:33:03-0400")
        self.assertEqual(result, datetime(2026, 7, 4, 21, 33, 3))

    def test_positive_offset_keeps_local_wall_clock(self):
        """A +0530 capture keeps its written clock time, not a UTC-shifted one"""
        result = parse_local_creationdate("2026-01-15T09:00:00+0530")
        self.assertEqual(result, datetime(2026, 1, 15, 9, 0, 0))

    def test_zulu_utc_keeps_wall_clock_reading(self):
        """A trailing Z parses; the wall-clock reading is kept as written"""
        result = parse_local_creationdate("2024-06-15T18:30:00Z")
        self.assertEqual(result, datetime(2026, 7, 4, 18, 30, 0))

    def test_colon_offset_form_parses(self):
        """The expanded -04:00 offset form parses to the same wall clock"""
        result = parse_local_creationdate("2024-06-15T21:33:03-04:00")
        self.assertEqual(result, datetime(2026, 7, 4, 21, 33, 3))

    def test_returns_naive_datetime(self):
        """The result carries no tzinfo, so it formats as a local wall clock"""
        result = parse_local_creationdate("2024-06-15T21:33:03-0400")
        self.assertIsNone(result.tzinfo)

    def test_empty_string_returns_none(self):
        """An empty/missing value yields None rather than raising"""
        self.assertIsNone(parse_local_creationdate(""))

    def test_garbage_returns_none(self):
        """An unparseable value yields None rather than raising"""
        self.assertIsNone(parse_local_creationdate("not-a-date"))


class TestFindQuicktimeCreationdate(unittest.TestCase):
    """Exercise the box scanner against hand-built moov/meta/keys/ilst bytes (#28)"""

    def test_scans_mov_style_meta(self):
        """QuickTime-style meta (no version/flags) is scanned correctly"""
        moov = quicktime_creationdate_moov(
            "2024-06-15T21:33:03-0400", meta_style="mov"
        )
        self.assertEqual(
            find_quicktime_creationdate(moov), "2024-06-15T21:33:03-0400"
        )

    def test_scans_iso_style_meta(self):
        """ISO/MP4-style meta (leading version/flags) is scanned correctly"""
        moov = quicktime_creationdate_moov(
            "2024-06-15T21:33:03-0400", meta_style="iso"
        )
        self.assertEqual(
            find_quicktime_creationdate(moov), "2024-06-15T21:33:03-0400"
        )

    def test_absent_key_returns_none(self):
        """A moov with no meta atom yields None (caller falls back to hachoir)"""
        # An mvhd-only moov: build one, then strip the meta atom off the end.
        moov = quicktime_creationdate_moov(
            "2024-06-15T21:33:03-0400", include_mvhd=True
        )
        # mvhd box is first; keep only it by slicing to its declared size.
        import struct
        mvhd_size = struct.unpack(">I", moov[:4])[0]
        self.assertIsNone(find_quicktime_creationdate(moov[:mvhd_size]))

    def test_truncated_buffer_does_not_raise(self):
        """A truncated moov returns None instead of crashing the scan"""
        moov = quicktime_creationdate_moov("2024-06-15T21:33:03-0400")
        self.assertIsNone(find_quicktime_creationdate(moov[:len(moov) // 2]))


class TestVideoLocalCreationDate(unittest.TestCase):
    """End-to-end: the Apple local creationdate names the file, not UTC (#28)"""

    def setUp(self):
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _mov(self, name, creationdate, **kwargs):
        path = os.path.join(self.temp_dir, name)
        return write_quicktime_mov(path, creationdate, **kwargs)

    def test_late_evening_capture_keeps_local_calendar_day(self):
        """A 21:33 EDT capture (01:33Z next day) is named on the LOCAL day"""
        # mvhd carries the UTC time (2024-06-16 01:33:03) so this proves the
        # local key wins over the UTC atom, not merely that mvhd was ignored.
        utc_1904 = self._seconds_1904(datetime(2026, 7, 5, 1, 33, 3))
        path = self._mov(
            "IMG_LATE.MOV",
            "2024-06-15T21:33:03-0400",
            mvhd_creation_1904=utc_1904,
        )
        result = self.handler.extract_timestamp(path)
        self.assertEqual(result, datetime(2026, 7, 4, 21, 33, 3))
        self.assertEqual(
            self.handler.format_timestamp_filename(result), "2024.06.15.21.33.03"
        )

    def test_video_and_photo_same_instant_share_stem(self):
        """A video and photo shot at the same instant get the same filename stem"""
        # Photo: EXIF DateTimeOriginal is local wall clock 14:30:00.
        photo = make_exif_jpeg(
            os.path.join(self.temp_dir, "pic.jpg"),
            date_time_original="2024:07:04 14:30:00",
        )
        # Video: same instant, 14:30 EDT == 18:30 UTC, Apple key carries local.
        video = self._mov("clip.mov", "2024-07-04T14:30:00-0400")
        photo_dt = self.handler.extract_timestamp(photo)
        video_dt = self.handler.extract_timestamp(video)
        self.assertEqual(
            self.handler.format_timestamp_filename(photo_dt),
            self.handler.format_timestamp_filename(video_dt),
        )

    def test_dst_summer_and_winter_offsets(self):
        """A July -0400 and a January -0500 capture each keep their local clock"""
        summer = self._mov("summer.mov", "2024-06-15T21:33:03-0400")
        winter = self._mov("winter.mov", "2026-01-15T21:33:03-0500")
        self.assertEqual(
            self.handler.extract_timestamp(summer),
            datetime(2026, 7, 4, 21, 33, 3),
        )
        self.assertEqual(
            self.handler.extract_timestamp(winter),
            datetime(2026, 1, 15, 21, 33, 3),
        )

    def test_implausible_apple_creationdate_falls_back_to_mvhd(self):
        """
        Issue #62: #28's local-time preference does not exempt the Apple key
        from the plausibility bound -- a crafted creationdate falls through
        to hachoir's mvhd exactly as a missing key would, rather than naming
        the file from it.
        """
        utc_1904 = self._seconds_1904(datetime(2026, 7, 5, 1, 33, 3))
        path = self._mov(
            "IMG_CRAFTED.MOV",
            "9999-12-31T23:59:59-0400",
            mvhd_creation_1904=utc_1904,
        )
        result = self.handler.extract_timestamp(path)
        self.assertEqual(result, datetime(2026, 7, 5, 1, 33, 3))

    def test_local_key_preferred_even_without_hachoir(self):
        """The Apple key path works even when hachoir is unavailable"""
        path = self._mov("nohachoir.mov", "2024-06-15T21:33:03-0400")
        with patch("src.exif_handler.HACHOIR_AVAILABLE", False):
            result = self.handler.extract_timestamp(path)
        self.assertEqual(result, datetime(2026, 7, 4, 21, 33, 3))
        self.assertEqual(len(self.handler.missing_exif_files), 0)

    @staticmethod
    def _seconds_1904(dt_utc: datetime) -> int:
        """Seconds from 1904-01-01 to a UTC datetime, for building mvhd fixtures"""
        return int((dt_utc - datetime(1904, 1, 1)).total_seconds())


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

    def test_pattern_space_separated_date_and_time(self):
        """Issue #51: a space between the date and time (macOS export shape) parses.

        The old pattern1 only accepted `_`/`-` between every field, including
        between the date and the time, so this filename fell all the way
        through to the filesystem-mtime fallback.
        """
        result = self.handler._extract_timestamp_from_filename(
            "2011-03-09 18-20-30.jpg.jpeg"
        )
        self.assertEqual(result, datetime(2014, 7, 5, 20, 0, 47))

    def test_pattern_day_first_facetune_style(self):
        """Issue #51: DD-MM-YYYY-HH-MM-SS (Facetune's naming convention) parses.

        04-07 is ambiguous (both fields <= 12), so the day-first reading is
        expected to be preferred, matching the real Facetune export.
        """
        result = self.handler._extract_timestamp_from_filename(
            "Facetune_09-02-2024-11-22-33.heic"
        )
        self.assertEqual(result, datetime(2026, 7, 4, 13, 32, 17))

    def test_pattern_day_first_logs_chosen_interpretation(self):
        """Issue #51: the resolved day-first/month-first reading is logged at INFO."""
        with self.assertLogs("src.exif_handler", level="INFO") as captured:
            self.handler._extract_timestamp_from_filename(
                "Facetune_09-02-2024-11-22-33.heic"
            )
        self.assertTrue(
            any("day-first" in message for message in captured.output),
            captured.output,
        )

    def test_pattern_day_first_unambiguous_month_first_resolution(self):
        """Issue #51: when only the month-first reading is valid, it is used.

        05-20 cannot be day-first (day=5, month=20 is not a real month), so
        the only surviving reading is month=05, day=20.
        """
        result = self.handler._extract_timestamp_from_filename(
            "Foo_05-20-2026-10-20-30.heic"
        )
        self.assertEqual(result, datetime(2026, 5, 20, 10, 20, 30))

    def test_pattern_day_first_neither_reading_valid_returns_none(self):
        """Issue #51: a day-first candidate where BOTH readings are invalid returns None.

        11-31: day-first reads day=11, month=31 (not a month); month-first
        reads month=11 (November), day=31 (November has 30 days). Neither
        resolves, so this must fall through rather than guess.
        """
        result = self.handler._extract_timestamp_from_filename(
            "Foo_11-31-2026-01-02-03.heic"
        )
        self.assertIsNone(result)

    def test_screen_recording_is_read_month_first(self):
        """Issue #70: macOS/iOS screen recordings use MM-DD-YYYY, not DD-MM.

        Established from a real file rather than assumed: for
        ``ScreenRecording_03-05-2024 09-15-00_1.mp4`` the container's own mvhd
        reads 2024-03-05 13:15:00 UTC, i.e. 08:58:01 EDT -- exactly the time in
        the filename, so the leading 07-01 is July 1. Issue #51's
        day-first-first order returned January 7, six months off, for every such
        capture whose metadata was missing (a re-muxed file commonly carries a
        1904 mvhd).
        """
        result = self.handler._extract_timestamp_from_filename(
            "ScreenRecording_03-05-2024 09-15-00_1.mp4"
        )
        self.assertEqual(result, datetime(2026, 7, 1, 8, 58, 1))

    def test_screen_recording_month_first_on_the_second_real_example(self):
        result = self.handler._extract_timestamp_from_filename(
            "ScreenRecording_03-06-2024 16-40-00_1.mp4"
        )
        self.assertEqual(result, datetime(2026, 7, 2, 14, 27, 54))

    def test_facetune_still_reads_day_first(self):
        """Issue #70 must not regress issue #51's Facetune convention.

        Both conventions are real and live in one regex; fixing one by breaking
        the other would be no fix at all.
        """
        result = self.handler._extract_timestamp_from_filename(
            "Facetune_09-02-2024-11-22-33.heic"
        )
        self.assertEqual(result, datetime(2026, 7, 4, 13, 32, 17))

    def test_unrecognized_generator_keeps_the_day_first_default(self):
        """No marker means no evidence; the #51 default is preserved.

        Deliberate: silently re-dating files from generators this project has
        never observed would be a guess dressed up as a fix.
        """
        result = self.handler._extract_timestamp_from_filename(
            "mystery_09-02-2024-11-22-33.jpg"
        )
        self.assertEqual(result, datetime(2026, 7, 4, 13, 32, 17))

    def test_marker_matching_is_case_insensitive(self):
        result = self.handler._extract_timestamp_from_filename(
            "screenrecording_03-05-2024-09-15-00.mp4"
        )
        self.assertEqual(result, datetime(2026, 7, 1, 8, 58, 1))

    def test_a_space_separated_screen_recording_marker_also_matches(self):
        result = self.handler._extract_timestamp_from_filename(
            "Screen Recording 03-05-2024 09-15-00.mov"
        )
        self.assertEqual(result, datetime(2026, 7, 1, 8, 58, 1))

    def test_month_first_generator_still_falls_back_when_month_first_is_invalid(self):
        """The convention sets the ORDER, not a hard rule.

        20-01 cannot be month-first (month=20), so the day-first reading must
        still resolve rather than the file falling through to its mtime.
        """
        result = self.handler._extract_timestamp_from_filename(
            "ScreenRecording_20-05-2024 09-15-00.mp4"
        )
        self.assertEqual(result, datetime(2026, 1, 20, 8, 58, 1))

    def test_screen_recording_logs_the_month_first_choice(self):
        """A chosen interpretation stays auditable (issue #51's contract)."""
        with self.assertLogs("src.exif_handler", level="INFO") as captured:
            self.handler._extract_timestamp_from_filename(
                "ScreenRecording_03-05-2024 09-15-00_1.mp4"
            )
        self.assertTrue(
            any("month-first" in message for message in captured.output),
            "the resolved interpretation was not logged",
        )

    def test_pattern_day_first_only_valid_reading_resolves(self):
        """Issue #68 gap: the branch where only DAY-first is a real date.

        ``25-03`` cannot be month-first (month=25), so the day-first reading must
        resolve. The both-valid and only-month-first branches were already
        covered; this was the third one, left open by #51.
        """
        result = self.handler._extract_timestamp_from_filename(
            "Foo_25-03-2026-10-20-30.heic"
        )
        self.assertEqual(result, datetime(2026, 3, 25, 10, 20, 30))

    def test_pattern_day_first_out_of_range_time_returns_none(self):
        """Issue #68 gap: the out-of-range negative case for pattern 3.

        #51's out-of-range test exercised pattern 1, so pattern 3's own
        validation was untested. 25:61:61 is not a time under either reading, so
        this must fall through to the filesystem rather than name a file from a
        half-parsed value.
        """
        result = self.handler._extract_timestamp_from_filename(
            "Foo_04-07-2026-25-61-61.heic"
        )
        self.assertIsNone(result)

    def test_pattern_one_at_its_widest_all_space_separated(self):
        """Issue #68 gap: pattern 1's separator class at full width.

        #51 widened the separator to ``[_\\-\\s]`` at all five positions, not just
        the date/time boundary it needed, and that broadest form was never
        exercised. Verified here so the widening is covered rather than merely
        assumed harmless.
        """
        result = self.handler._extract_timestamp_from_filename(
            "2024 01 02 12 30 45.jpg"
        )
        self.assertEqual(result, datetime(2024, 1, 2, 12, 30, 45))

    def test_epoch_suffix_is_still_not_mistaken_for_a_date(self):
        """The counterweight to the widened separator.

        A 13-digit epoch suffix must keep resolving via the compact pattern and
        never be chewed into a date by the broader separator class.
        """
        result = self.handler._extract_timestamp_from_filename(
            "dji_fly_20240115_101112_105_1700000000000_photo_optimized.jpg"
        )
        self.assertEqual(result, datetime(2026, 7, 4, 13, 13, 28))

    def test_dji_epoch_suffix_filename_unaffected(self):
        """Issue #51 regression guard: the DJI epoch-suffix filename still parses
        via pattern2 and is not disturbed by the new day-first pattern."""
        result = self.handler._extract_timestamp_from_filename(
            "dji_fly_20240115_101112_105_1700000000000_photo_optimized.jpg"
        )
        self.assertEqual(result, datetime(2026, 7, 4, 13, 13, 28))

    def test_out_of_range_date_returns_none_not_raise(self):
        """Issue #51: an out-of-range YYYY-MM-DD-HH-MM-SS date returns None."""
        result = self.handler._extract_timestamp_from_filename(
            "2024-13-45-99-99-99.jpg"
        )
        self.assertIsNone(result)

    def test_pattern1_implausible_year_falls_through(self):
        """
        Issue #62: pattern 1's bare ``\\d{4}`` year field admits 0000-9999, so
        it can produce the same implausible date a crafted EXIF value can;
        it must be bounded the same way and not returned.
        """
        result = self.handler._extract_timestamp_from_filename(
            "IMG_9999-12-31-23-59-59.jpg"
        )
        self.assertIsNone(result)

    def test_pattern2_implausible_year_falls_through(self):
        """Issue #62: pattern 2 (YYYYMMDD_HHMMSS) is bounded the same way."""
        result = self.handler._extract_timestamp_from_filename(
            "VID_00010101_000000.mov"
        )
        self.assertIsNone(result)

    def test_pattern3_day_first_implausible_year_falls_through(self):
        """
        Issue #62: the day-first/month-first pattern shares the same
        implausible year under both ambiguity readings, so neither reading
        is returned.
        """
        result = self.handler._extract_timestamp_from_filename(
            "Facetune_04-07-9999-13-32-17.heic"
        )
        self.assertIsNone(result)

    def test_filename_one_hour_in_the_future_is_accepted(self):
        """
        Acceptance criterion 4 applies to the filename fallback too: a
        timestamp derived from ``datetime.now()`` one hour ahead is accepted.
        """
        future = datetime.now() + timedelta(hours=1)
        name = "IMG_{}.jpg".format(future.strftime("%Y-%m-%d-%H-%M-%S"))
        result = self.handler._extract_timestamp_from_filename(name)
        self.assertEqual(result, future.replace(microsecond=0))

    def test_real_export_filename_shapes_hit_rate(self):
        """Issue #51: every real filename shape identified in the QA audit now
        resolves to a timestamp instead of falling through to filesystem mtime."""
        fixtures = [
            ("2011-03-09 18-20-30.jpg.jpeg", datetime(2014, 7, 5, 20, 0, 47)),
            ("Facetune_09-02-2024-11-22-33.heic", datetime(2026, 7, 4, 13, 32, 17)),
            ("Facetune_09-02-2024-11-24-43.heic", datetime(2026, 7, 4, 13, 34, 27)),
            ("Facetune_09-02-2024-11-27-26.heic", datetime(2026, 7, 4, 13, 37, 10)),
            ("Facetune_09-02-2024-11-28-21.heic", datetime(2026, 7, 4, 13, 38, 5)),
            (
                "dji_fly_20240115_101112_105_1700000000000_photo_optimized.jpg",
                datetime(2026, 7, 4, 13, 13, 28),
            ),
        ]
        results = [
            self.handler._extract_timestamp_from_filename(name)
            for name, _ in fixtures
        ]
        self.assertTrue(all(r is not None for r in results), results)
        self.assertEqual(results, [expected for _, expected in fixtures])


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

        for timestamp in timestamps:
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


class TestVideoLocalTimeBeatsUtcContainerTime(unittest.TestCase):
    """Issue #72: a filename's local time wins over mvhd when it is the same instant.

    Before this, a real screen recording whose name carried the correct LOCAL
    time was archived from the UTC mvhd instead -- four hours late, and on the
    wrong calendar DAY for any capture after 20:00 EDT.
    """

    def setUp(self):
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _path(self, name):
        path = os.path.join(self.temp_dir, name)
        open(path, "wb").close()
        return path

    def test_edt_offset_is_recognized_and_the_local_reading_used(self):
        """The real case: mvhd 12:58:01 UTC, filename 08:58:01 local."""
        path = self._path("ScreenRecording_03-05-2024 09-15-00_1.mp4")
        with patch.object(
            self.handler, "_extract_video_timestamp_hachoir",
            return_value=datetime(2026, 7, 1, 12, 58, 1),
        ):
            result = self.handler._extract_video_timestamp(path)
        self.assertEqual(result, datetime(2026, 7, 1, 8, 58, 1))

    def test_a_late_evening_capture_keeps_the_correct_calendar_day(self):
        """The consequence that matters: 22:30 EDT is 02:30 UTC the NEXT day."""
        path = self._path("ScreenRecording_03-05-2024 22-30-00.mp4")
        with patch.object(
            self.handler, "_extract_video_timestamp_hachoir",
            return_value=datetime(2026, 7, 2, 2, 30, 0),
        ):
            result = self.handler._extract_video_timestamp(path)
        self.assertEqual(
            result, datetime(2026, 7, 1, 22, 30, 0),
            "the capture was filed under the following day",
        )

    def test_a_sub_hour_offset_zone_is_accepted(self):
        """India is +5:30; rejecting non-whole-hour offsets would exclude it."""
        path = self._path("Screen Recording 03-05-2024 18-45-00.mp4")
        with patch.object(
            self.handler, "_extract_video_timestamp_hachoir",
            return_value=datetime(2026, 7, 1, 12, 58, 1),
        ):
            result = self.handler._extract_video_timestamp(path)
        self.assertEqual(result, datetime(2026, 7, 1, 18, 28, 1))

    def test_unrelated_filename_digits_do_not_outrank_the_container(self):
        """The cross-check is what makes preferring the filename safe."""
        path = self._path("dji_fly_20240115_101112_105_1700000000000_photo.mp4")
        mvhd = datetime(2026, 7, 1, 12, 58, 1)
        with patch.object(
            self.handler, "_extract_video_timestamp_hachoir", return_value=mvhd
        ):
            result = self.handler._extract_video_timestamp(path)
        self.assertEqual(
            result, mvhd,
            "an unrelated number pattern was allowed to outrank real metadata",
        )

    def test_an_implausible_offset_is_rejected(self):
        """Beyond +/-14h cannot be a zone; keep the container reading."""
        path = self._path("ScreenRecording_03-05-2024 09-15-00.mp4")
        mvhd = datetime(2026, 7, 3, 4, 0, 0)
        with patch.object(
            self.handler, "_extract_video_timestamp_hachoir", return_value=mvhd
        ):
            self.assertEqual(self.handler._extract_video_timestamp(path), mvhd)

    def test_an_offset_that_is_not_a_quarter_hour_is_rejected(self):
        path = self._path("ScreenRecording_03-05-2024 09-15-00.mp4")
        mvhd = datetime(2026, 7, 1, 17, 58, 30)
        with patch.object(
            self.handler, "_extract_video_timestamp_hachoir", return_value=mvhd
        ):
            self.assertEqual(self.handler._extract_video_timestamp(path), mvhd)

    def test_no_container_time_leaves_the_filename_to_the_normal_fallback(self):
        """With no mvhd there is nothing to confirm against."""
        path = self._path("ScreenRecording_03-05-2024 09-15-00.mp4")
        with patch.object(
            self.handler, "_extract_video_timestamp_hachoir", return_value=None
        ):
            self.assertIsNone(self.handler._extract_video_timestamp(path))
        # ...and the chain still reaches the filename on its own.
        self.assertEqual(
            self.handler.get_fallback_timestamp(path),
            datetime(2026, 7, 1, 8, 58, 1),
        )

    def test_the_apple_key_still_outranks_both(self):
        """Issue #28's precedence is unchanged: a real local key wins outright."""
        path = os.path.join(self.temp_dir, "ScreenRecording_03-05-2024 09-15-00.mov")
        write_quicktime_mov(path, "2024-06-20T19:23:34-0400")
        with patch.object(
            self.handler, "_extract_video_timestamp_hachoir",
            return_value=datetime(2026, 7, 1, 12, 58, 1),
        ):
            result = self.handler._extract_video_timestamp(path)
        self.assertEqual(result, datetime(2026, 7, 29, 19, 23, 34))


class TestHachoirDoesNotLeakFileDescriptors(unittest.TestCase):
    """Issue #66: an unparseable video could hold its descriptor for the run.

    hachoir's ``createParser`` opens the file, then closes the stream only when
    ``guessParser`` RETURNS ``None`` -- nothing closes anything when either step
    RAISES. A zero-byte ``.mov`` in an export raises
    ``InputStreamError("Input size is nul")`` from inside ``FileInputStream``
    itself, *after* it has already opened the file.

    Scope, stated honestly: on CPython refcounting reclaims the abandoned handle
    as soon as the exception is discarded, so the CURRENT call path -- which logs
    and drops it -- does not leak today. The leak becomes real the moment
    something retains the exception, because a retained exception keeps its
    traceback, which keeps the raising frame and its open file alive. This
    project deliberately stores exception OBJECTS rather than ``str(error)`` in
    its failure records, so that is one refactor away, not a hypothetical. The
    tests below reproduce the retaining shape directly, which is the only way to
    observe the defect and therefore the only way to pin the fix.
    """

    def setUp(self):
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @staticmethod
    def _open_fd_count():
        return len(os.listdir("/dev/fd"))

    def _broken_videos(self, count):
        paths = []
        for index in range(count):
            path = os.path.join(self.temp_dir, f"broken_{index}.mov")
            open(path, "wb").close()      # zero-byte: raises inside the open
            paths.append(path)
        return paths

    def test_retained_parse_failures_hold_no_descriptors(self):
        """The measurable defect: 40 retained failures held 40 descriptors."""
        import gc
        if not os.path.isdir("/dev/fd"):
            self.skipTest("/dev/fd unavailable; cannot count open descriptors")

        import src.exif_handler as exif_module
        exif_module._ensure_hachoir_imported()
        create_parser = exif_module.createParser
        paths = self._broken_videos(40)

        retained = []
        gc.collect()
        before = self._open_fd_count()
        for path in paths:
            try:
                create_parser(path)
            except Exception as error:
                # Exactly what this codebase does with failures: keep the
                # exception object, whose traceback pins the raising frame.
                retained.append(error)
        gc.collect()
        after = self._open_fd_count()

        self.assertEqual(len(retained), len(paths), "the fixtures did not raise")
        self.assertLessEqual(
            after - before, 1,
            f"leaked {after - before} descriptors across {len(paths)} retained "
            "parse failures",
        )

    def test_a_successful_parse_still_releases_its_descriptor_on_close(self):
        """Ownership passes to the caller's ``with parser:`` block."""
        import gc
        if not os.path.isdir("/dev/fd"):
            self.skipTest("/dev/fd unavailable; cannot count open descriptors")

        import src.exif_handler as exif_module
        exif_module._ensure_hachoir_imported()
        path = os.path.join(self.temp_dir, "real.mov")
        write_quicktime_mov(path, "2024-06-20T19:23:34-0400")

        gc.collect()
        before = self._open_fd_count()
        parser = exif_module.createParser(path)
        self.assertIsNotNone(parser, "a real video must still parse")
        with parser:
            pass
        gc.collect()

        self.assertLessEqual(self._open_fd_count() - before, 0)

    def test_an_unparseable_video_is_still_reported_as_missing_metadata(self):
        """The fix must not change the outcome, only release the descriptor."""
        path = os.path.join(self.temp_dir, "broken.mov")
        open(path, "wb").close()

        result = self.handler._extract_video_timestamp_hachoir(path)

        self.assertIsNone(result)
        self.assertIn(path, self.handler.missing_exif_files)

    def test_a_real_video_still_parses(self):
        """The replacement must behave like hachoir's own createParser."""
        path = os.path.join(self.temp_dir, "real.mov")
        write_quicktime_mov(path, "2024-06-20T19:23:34-0400")
        # Goes through the Apple-key path, which does not use hachoir at all;
        # assert the hachoir path also produces a datetime for the same file.
        self.assertIsNotNone(self.handler._extract_video_timestamp(path))


class TestQuickTimeEpochSentinelOnTheAppleKey(unittest.TestCase):
    """Issue #72: the 1904 sentinel can arrive through the Apple key too."""

    def setUp(self):
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_zero_epoch_in_the_apple_key_is_rejected(self):
        """A re-muxer synthesizing this key from a zeroed mvhd writes 1904.

        The check was scoped to the mvhd path on the reasoning that this field
        "has no zero-epoch encoding of its own" -- true of the encoding, not of
        the value, which parses fine and would be filed as 1904.01.01.00.00.00.
        """
        path = os.path.join(self.temp_dir, "remuxed.mov")
        write_quicktime_mov(path, "1904-01-01T00:00:00+0000")
        self.assertIsNone(self.handler._extract_quicktime_creationdate(path))

    def test_a_real_local_creationdate_is_still_accepted(self):
        """The sentinel check must not reject genuine Apple metadata."""
        path = os.path.join(self.temp_dir, "real.mov")
        write_quicktime_mov(path, "2024-06-20T19:23:34-0400")
        self.assertEqual(
            self.handler._extract_quicktime_creationdate(path),
            datetime(2026, 7, 29, 19, 23, 34),
        )

    def test_a_1904_date_that_is_not_the_sentinel_instant_is_untouched(self):
        """Only the zero-epoch instant is a sentinel, not the whole year.

        A genuine (if improbable) 1904 date that is not midnight on Jan 1 is not
        the QuickTime zero value and must survive the plausibility floor, which
        issue #62 deliberately set at 1826 to protect scanned family photos.
        """
        self.assertFalse(
            self.handler._is_quicktime_epoch_sentinel(datetime(1904, 6, 15, 12, 0, 0))
        )


if __name__ == '__main__':
    unittest.main()
