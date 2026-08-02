"""
Coverage-focused tests for the lower-level EXIF/video paths.

These target branches the end-to-end tests do not reach: the ISO box-scanner
edge cases, the ``moov`` reader's largesize / size-to-end / malformed handling,
the hachoir fallback's field/plaintext/exception branches, the sub-IFD access
guard, the filename-pattern parse failures, the fallback-timestamp mtime branch,
and the duplicate-timestamp exhaustion cap. Buffers are hand-built from the ISO
base-media box grammar so each asserts a concrete parse decision.
"""

import os
import struct
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

from src.exif_handler import (
    ExifHandler,
    QUICKTIME_CREATIONDATE_KEY,
    _creationdate_key_index,
    _ilst_string_value,
    _iter_boxes,
    _meta_children_start,
    _scan_meta_for_creationdate,
    find_quicktime_creationdate,
)
from tests.fixtures import quicktime_creationdate_moov, write_quicktime_mov


def _box(box_type: bytes, payload: bytes) -> bytes:
    """Wrap payload in a 32-bit ISO base-media box header."""
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


class TestIterBoxes(unittest.TestCase):
    """_iter_boxes: 64-bit largesize, size-to-end, and malformed sizes."""

    def test_largesize_box_is_parsed(self):
        """A size==1 box reads its 64-bit largesize and yields the right span."""
        buf = struct.pack(">I", 1) + b"abcd" + struct.pack(">Q", 24) + b"\x00" * 8
        boxes = list(_iter_boxes(buf, 0, len(buf)))
        self.assertEqual(boxes, [(b"abcd", 16, 24)])

    def test_truncated_largesize_stops(self):
        """A largesize header without its 8 length bytes stops iteration."""
        buf = struct.pack(">I", 1) + b"abcd" + b"\x00\x00"  # only 10 bytes
        self.assertEqual(list(_iter_boxes(buf, 0, len(buf))), [])

    def test_size_zero_runs_to_end(self):
        """A size==0 box extends to the end of the enclosing span."""
        buf = struct.pack(">I", 0) + b"abcd" + b"payload"
        boxes = list(_iter_boxes(buf, 0, len(buf)))
        self.assertEqual(boxes, [(b"abcd", 8, len(buf))])

    def test_malformed_size_stops(self):
        """A size smaller than the header is rejected and stops iteration."""
        buf = struct.pack(">I", 4) + b"abcd"  # size 4 < 8-byte header
        self.assertEqual(list(_iter_boxes(buf, 0, len(buf))), [])


class TestMetaChildrenStart(unittest.TestCase):
    """_meta_children_start default when no known child sits at either offset."""

    def test_defaults_to_quicktime_position(self):
        """With no recognizable child atom, it defaults to the body offset."""
        # 16 bytes of non-atom content: neither the mov nor iso probe matches.
        buf = b"\x00" * 24
        self.assertEqual(_meta_children_start(buf, 0, len(buf)), 0)


class TestScanMetaForCreationdate(unittest.TestCase):
    """_scan_meta_for_creationdate: missing keys/ilst, and unmatched key."""

    def test_missing_keys_or_ilst_returns_none(self):
        """A meta span lacking keys/ilst yields None."""
        only_hdlr = _box(b"hdlr", b"\x00" * 8)
        self.assertIsNone(
            _scan_meta_for_creationdate(only_hdlr, 0, len(only_hdlr))
        )

    def test_key_not_found_returns_none(self):
        """keys+ilst present but the creationdate key absent yields None."""
        # keys atom naming a single, non-creationdate key.
        other_key = b"com.apple.quicktime.make"
        entry = struct.pack(">I", 8 + len(other_key)) + b"mdta" + other_key
        keys_payload = struct.pack(">I", 0) + struct.pack(">I", 1) + entry
        keys = _box(b"keys", keys_payload)
        ilst = _box(b"ilst", b"")
        buf = keys + ilst
        self.assertIsNone(_scan_meta_for_creationdate(buf, 0, len(buf)))


class TestCreationdateKeyIndex(unittest.TestCase):
    """_creationdate_key_index: malformed entry size and no-match walk."""

    def test_malformed_entry_size_returns_none(self):
        """An entry claiming size < 8 aborts the key walk."""
        payload = struct.pack(">I", 0) + struct.pack(">I", 1) + struct.pack(">I", 4) + b"mdta"
        self.assertIsNone(_creationdate_key_index(payload, 0, len(payload)))

    def test_walks_past_nonmatching_key_then_none(self):
        """A non-matching key is skipped and the walk ends at None."""
        other = b"com.apple.quicktime.model"
        entry = struct.pack(">I", 8 + len(other)) + b"mdta" + other
        payload = struct.pack(">I", 0) + struct.pack(">I", 1) + entry
        self.assertIsNone(_creationdate_key_index(payload, 0, len(payload)))

    def test_finds_creationdate_index(self):
        """The 1-based index of the creationdate key is returned."""
        key = QUICKTIME_CREATIONDATE_KEY
        entry = struct.pack(">I", 8 + len(key)) + b"mdta" + key
        payload = struct.pack(">I", 0) + struct.pack(">I", 1) + entry
        self.assertEqual(_creationdate_key_index(payload, 0, len(payload)), 1)


class TestIlstStringValue(unittest.TestCase):
    """_ilst_string_value: index mismatch, non-data box, bad utf-8, no data."""

    def _item(self, index: int, inner: bytes) -> bytes:
        """Build an ilst item box whose type is the big-endian key index."""
        return struct.pack(">I", 8 + len(inner)) + struct.pack(">I", index) + inner

    def test_index_mismatch_returns_none(self):
        """An item under a different index is skipped, yielding None."""
        data = _box(b"data", struct.pack(">I", 1) + struct.pack(">I", 0) + b"v")
        buf = self._item(2, data)
        self.assertIsNone(_ilst_string_value(buf, 0, len(buf), index=1))

    def test_non_data_inner_box_returns_none(self):
        """An item that matches but holds no data box yields None."""
        inner = _box(b"nope", b"payload")
        buf = self._item(1, inner)
        self.assertIsNone(_ilst_string_value(buf, 0, len(buf), index=1))

    def test_invalid_utf8_returns_none(self):
        """A data payload that is not valid UTF-8 yields None, not a crash."""
        data = _box(
            b"data", struct.pack(">I", 1) + struct.pack(">I", 0) + b"\xff\xfe"
        )
        buf = self._item(1, data)
        self.assertIsNone(_ilst_string_value(buf, 0, len(buf), index=1))

    def test_valid_value_is_decoded_and_trimmed(self):
        """A valid UTF-8 data payload is decoded and stripped of NULs."""
        data = _box(
            b"data",
            struct.pack(">I", 1) + struct.pack(">I", 0) + b"2026-07-04\x00",
        )
        buf = self._item(1, data)
        self.assertEqual(_ilst_string_value(buf, 0, len(buf), index=1), "2026-07-04")


class TestReadMoovBytes(unittest.TestCase):
    """_read_moov_bytes: largesize, size-to-end, oversize, and malformed tails."""

    def setUp(self):
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write(self, name, data):
        path = os.path.join(self.temp_dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def test_largesize_moov_is_read(self):
        """A moov declared with a 64-bit largesize is read and scanned."""
        payload = quicktime_creationdate_moov("2024-06-15T21:33:03-0400")
        ftyp = _box(b"ftyp", b"qt  ")
        moov = (
            struct.pack(">I", 1)
            + b"moov"
            + struct.pack(">Q", 16 + len(payload))
            + payload
        )
        path = self._write("large.mov", ftyp + moov)

        result = self.handler._read_moov_bytes(path)

        self.assertEqual(find_quicktime_creationdate(result), "2024-06-15T21:33:03-0400")

    def test_size_zero_moov_runs_to_end(self):
        """A moov declared size 0 is read to end of file."""
        payload = quicktime_creationdate_moov("2024-06-15T21:33:03-0400")
        ftyp = _box(b"ftyp", b"qt  ")
        moov = struct.pack(">I", 0) + b"moov" + payload
        path = self._write("tozero.mov", ftyp + moov)

        result = self.handler._read_moov_bytes(path)

        self.assertEqual(find_quicktime_creationdate(result), "2024-06-15T21:33:03-0400")

    def test_oversize_moov_is_rejected(self):
        """A moov whose declared size exceeds the cap returns None."""
        # Declare a size far larger than _MAX_MOOV_BYTES without providing bytes.
        moov = struct.pack(">I", 200_000_000) + b"moov"
        path = self._write("huge.mov", moov)

        self.assertIsNone(self.handler._read_moov_bytes(path))

    def test_size_zero_nonmoov_box_stops(self):
        """A size==0 box that is not moov consumes the rest and yields None."""
        data = struct.pack(">I", 0) + b"free" + b"junk"
        path = self._write("free.mov", data)

        self.assertIsNone(self.handler._read_moov_bytes(path))

    def test_malformed_small_box_stops(self):
        """A box declaring size < 8 aborts the top-level scan."""
        data = struct.pack(">I", 4) + b"free"
        path = self._write("bad.mov", data)

        self.assertIsNone(self.handler._read_moov_bytes(path))

    def test_truncated_largesize_header_stops(self):
        """A size==1 box whose 64-bit largesize is truncated returns None."""
        data = struct.pack(">I", 1) + b"free" + b"\x00\x00"  # only 2 of 8 bytes
        path = self._write("trunc.mov", data)

        self.assertIsNone(self.handler._read_moov_bytes(path))


class TestQuicktimeCreationdateHelperBranches(unittest.TestCase):
    """_extract_quicktime_creationdate: absent key and unparseable value."""

    def setUp(self):
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_moov_without_creationdate_returns_none(self):
        """A moov carrying only mvhd (no meta key) yields None from the helper."""
        payload = quicktime_creationdate_moov(
            "2024-06-15T21:33:03-0400", include_mvhd=True
        )
        # Keep only the leading mvhd box so no creationdate key remains.
        mvhd_size = struct.unpack(">I", payload[:4])[0]
        ftyp = _box(b"ftyp", b"qt  ")
        moov = _box(b"moov", payload[:mvhd_size])
        path = os.path.join(self.temp_dir, "mvhd_only.mov")
        with open(path, "wb") as handle:
            handle.write(ftyp + moov)

        self.assertIsNone(self.handler._extract_quicktime_creationdate(path))

    def test_unparseable_creationdate_returns_none(self):
        """A present-but-garbage creationdate is warned about and yields None."""
        path = write_quicktime_mov(
            os.path.join(self.temp_dir, "bad_date.mov"), "totally-not-a-date"
        )

        self.assertIsNone(self.handler._extract_quicktime_creationdate(path))


class TestHachoirFallbackBranches(unittest.TestCase):
    """_extract_video_timestamp_hachoir: metadata None, field raise, plaintext."""

    def setUp(self):
        self.handler = ExifHandler()

    @patch("src.exif_handler.HACHOIR_AVAILABLE", True)
    @patch("src.exif_handler.extractMetadata", create=True)
    @patch("src.exif_handler.createParser", create=True)
    def test_no_metadata_records_missing(self, mock_parser, mock_extract):
        """When hachoir yields no metadata, the file is logged as missing."""
        mock_parser.return_value = MagicMock()
        mock_extract.return_value = None

        result = self.handler._extract_video_timestamp_hachoir("movie.mov")

        self.assertIsNone(result)
        self.assertIn("movie.mov", self.handler.missing_exif_files)

    @patch("src.exif_handler.HACHOIR_AVAILABLE", True)
    @patch("src.exif_handler.extractMetadata", create=True)
    @patch("src.exif_handler.createParser", create=True)
    def test_field_access_raising_then_no_date(self, mock_parser, mock_extract):
        """A field whose access raises is skipped; no date is found overall."""

        class Raising:
            @property
            def creation_date(self):  # noqa: D401 - test double
                raise ValueError("boom")

            def exportPlaintext(self):
                return ["nothing datelike here"]

        mock_parser.return_value = MagicMock()
        mock_extract.return_value = Raising()

        result = self.handler._extract_video_timestamp_hachoir("movie.mov")

        self.assertIsNone(result)
        self.assertIn("movie.mov", self.handler.missing_exif_files)

    @patch("src.exif_handler.HACHOIR_AVAILABLE", True)
    @patch("src.exif_handler.extractMetadata", create=True)
    @patch("src.exif_handler.createParser", create=True)
    def test_plaintext_regex_extracts_date(self, mock_parser, mock_extract):
        """A creation date found only in plaintext lines is parsed out."""

        class PlaintextOnly:
            def exportPlaintext(self):
                return ["Creation date: 2024-01-15 14:30:45"]

        mock_parser.return_value = MagicMock()
        mock_extract.return_value = PlaintextOnly()

        result = self.handler._extract_video_timestamp_hachoir("movie.mov")

        self.assertEqual(result, datetime(2024, 1, 15, 14, 30, 45))

    @patch("src.exif_handler.HACHOIR_AVAILABLE", True)
    @patch("src.exif_handler.extractMetadata", create=True)
    @patch("src.exif_handler.createParser", create=True)
    def test_plaintext_date_that_fails_strptime_is_skipped(
        self, mock_parser, mock_extract
    ):
        """A regex-matching but calendar-invalid date line is skipped safely."""

        class BadDateLine:
            def exportPlaintext(self):
                # Matches the date regex but is not a real calendar date, so
                # strptime raises and the line is skipped (no date found).
                return ["Creation date: 2024-13-45 25:61:99"]

        mock_parser.return_value = MagicMock()
        mock_extract.return_value = BadDateLine()

        result = self.handler._extract_video_timestamp_hachoir("movie.mov")

        self.assertIsNone(result)
        self.assertIn("movie.mov", self.handler.missing_exif_files)

    @patch("src.exif_handler.HACHOIR_AVAILABLE", True)
    @patch("src.exif_handler.extractMetadata", create=True)
    @patch("src.exif_handler.createParser", create=True)
    def test_extract_exception_records_missing(self, mock_parser, mock_extract):
        """An error during metadata extraction is caught and logged as missing."""
        mock_parser.return_value = MagicMock()
        mock_extract.side_effect = RuntimeError("parser exploded")

        result = self.handler._extract_video_timestamp_hachoir("movie.mov")

        self.assertIsNone(result)
        self.assertIn("movie.mov", self.handler.missing_exif_files)


class TestTimestampCandidatesGuard(unittest.TestCase):
    """_timestamp_candidates tolerates an exif object whose get_ifd raises."""

    def test_sub_ifd_access_error_is_swallowed(self):
        """A get_ifd that raises leaves only the IFD0 tags in the result."""

        class FakeExif:
            def items(self):
                return [(271, "TestCamera")]  # 271 == Make

            def get_ifd(self, _tag):
                raise KeyError("no sub-IFD")

        handler = ExifHandler()
        merged = handler._timestamp_candidates(FakeExif(), "f.jpg")

        self.assertEqual(merged.get("Make"), "TestCamera")


class TestFallbackTimestampBranches(unittest.TestCase):
    """get_fallback_timestamp mtime branch and filename parse failures."""

    def setUp(self):
        self.handler = ExifHandler()

    @patch("os.stat")
    def test_birthtime_equal_ctime_uses_mtime(self, mock_stat):
        """When birthtime == ctime, the modification time is used instead."""
        stat_result = Mock()
        stat_result.st_birthtime = 1000.0
        stat_result.st_ctime = 1000.0  # equal -> not a real creation time
        stat_result.st_mtime = 1600000000.0
        mock_stat.return_value = stat_result

        result = self.handler.get_fallback_timestamp("f.jpg")

        self.assertEqual(result, datetime.fromtimestamp(1600000000.0))

    def test_filename_pattern1_invalid_date_returns_none(self):
        """A dash-pattern filename with an impossible date parses to None."""
        # Month 13 makes datetime() raise; the pattern-1 branch swallows it.
        self.assertIsNone(
            self.handler._extract_timestamp_from_filename(
                "IMG_2024-13-45-25-61-99.jpg"
            )
        )

    def test_filename_pattern2_invalid_date_returns_none(self):
        """A compact-pattern filename with an impossible date parses to None."""
        self.assertIsNone(
            self.handler._extract_timestamp_from_filename(
                "VID_20241345_256199.mov"
            )
        )


class TestDuplicateTimestampExhaustion(unittest.TestCase):
    """handle_duplicate_timestamp returns after exhausting its attempt cap."""

    def test_returns_last_adjusted_when_cap_exhausted(self):
        """With every slot within the cap taken, the last candidate is returned."""
        handler = ExifHandler()
        base = datetime(2024, 1, 15, 14, 30, 45)
        # Fill the base second plus all 3600 one-second increments the loop tries.
        existing = set()
        cursor = base
        for _ in range(3601):
            existing.add(handler.format_timestamp_filename(cursor))
            cursor = cursor + timedelta(seconds=1)

        result = handler.handle_duplicate_timestamp(base, existing)

        # The loop bumps 3600 times and returns the final (still-colliding) value.
        self.assertEqual(result, base + timedelta(seconds=3600))


if __name__ == "__main__":
    unittest.main()
