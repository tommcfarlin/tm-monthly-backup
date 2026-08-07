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
    _hachoir_creation_date,
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


def _mvhd_only_moov(creation_1904: int) -> bytes:
    """
    Build a real, minimal ``moov`` payload containing only a v0 ``mvhd`` box.

    hachoir's MP4 parser reads ``creation_time`` (seconds since 1904-01-01
    UTC) straight out of this box, giving a genuinely hachoir-populated
    ``creation_date`` field with no Apple ``meta``/``keys``/``ilst`` atoms
    involved at all -- this exercises hachoir's OWN fast path, not issue #28's
    Apple-key path.
    """
    mvhd_payload = (
        struct.pack(">I", 0)                    # version + flags
        + struct.pack(">I", creation_1904)      # creation_time
        + struct.pack(">I", creation_1904)      # modification_time
        + struct.pack(">I", 1000)               # timescale
        + struct.pack(">I", 1000)               # duration (== 1 second)
        + b"\x00" * 80                          # rate/volume/matrix/next_id
    )
    return _box(b"mvhd", mvhd_payload)


def _tkhd_only_moov() -> bytes:
    """
    Build a real, minimal ``moov`` payload containing a ``trak``/``tkhd`` only.

    A track header carries no creation-time field hachoir's MP4Metadata reads,
    so real, unmocked hachoir extraction genuinely produces metadata with no
    ``creation_date`` value -- unlike an empty ``moov``, whose ``Metadata`` is
    falsy and short-circuits before ``get()`` is ever called. ``tkhd`` must be
    wrapped in its enclosing ``trak`` box: hachoir's MP4 parser only descends
    into ``tkhd`` there, and a bare top-level ``tkhd`` is not recognized at all
    (metadata comes back empty/falsy rather than "present but dateless").
    """
    tkhd_payload = (
        struct.pack(">I", 0)         # version + flags
        + struct.pack(">I", 0)       # creation_time (unused by hachoir here)
        + struct.pack(">I", 0)       # modification_time
        + struct.pack(">I", 1)       # track_id
        + struct.pack(">I", 0)       # reserved
        + struct.pack(">I", 1000)    # duration
        + b"\x00" * 8                # reserved
        + b"\x00" * 2                # layer
        + b"\x00" * 2                # alternate group
        + b"\x00" * 2                # volume
        + b"\x00" * 2                # reserved
        + b"\x00" * 36               # matrix
        + struct.pack(">I", 640 << 16)  # width (16.16 fixed point)
        + struct.pack(">I", 480 << 16)  # height (16.16 fixed point)
    )
    tkhd = _box(b"tkhd", tkhd_payload)
    return _box(b"trak", tkhd)


def _write_minimal_mp4(path: str, moov_children: bytes) -> None:
    """Write a minimal, real ``ftyp`` + ``moov`` file hachoir can parse."""
    ftyp = _box(b"ftyp", b"qt  " + struct.pack(">I", 512) + b"qt  ")
    with open(path, "wb") as handle:
        handle.write(ftyp + _box(b"moov", moov_children))


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
    """_extract_video_timestamp_hachoir: metadata None, get() raise, plaintext (#16)."""

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
    def test_creation_date_from_get_skips_plaintext_entirely(
        self, mock_parser, mock_extract
    ):
        """
        When get('creation_date') succeeds, exportPlaintext is never invoked.

        This is the fast path issue #16 fixes: the old code probed for a
        ``creation_date`` attribute via ``hasattr``/``getattr``, which hachoir's
        ``Metadata`` object never has (it only exposes ``get(key)``), so this
        double -- which deliberately has no ``creation_date`` attribute, only
        ``get`` -- was unreachable under the old code and fell through to
        ``exportPlaintext``, which raises here to prove it.
        """

        expected = datetime(2024, 1, 15, 14, 30, 45)

        class HasCreationDate:
            def get(self, key, default=None, index=0):
                if key == 'creation_date':
                    return expected
                return default

            def exportPlaintext(self):
                raise AssertionError("exportPlaintext() should not be reached")

        mock_parser.return_value = MagicMock()
        mock_extract.return_value = HasCreationDate()

        result = self.handler._extract_video_timestamp_hachoir("movie.mov")

        self.assertEqual(result, expected)

    @patch("src.exif_handler.HACHOIR_AVAILABLE", True)
    @patch("src.exif_handler.extractMetadata", create=True)
    @patch("src.exif_handler.createParser", create=True)
    def test_missing_creation_date_key_then_plaintext_also_empty(
        self, mock_parser, mock_extract
    ):
        """
        get('creation_date') raising ValueError (hachoir's real "no such key"
        contract) is guarded rather than propagating; when the plaintext
        fallback also finds nothing, the overall result is None.
        """

        class NoCreationDate:
            def get(self, key, default=None, index=0):
                raise ValueError(
                    "Metadata has no value '%s' (index %s)" % (key, index)
                )

            def exportPlaintext(self):
                return ["nothing datelike here"]

        mock_parser.return_value = MagicMock()
        mock_extract.return_value = NoCreationDate()

        result = self.handler._extract_video_timestamp_hachoir("movie.mov")

        self.assertIsNone(result)
        self.assertIn("movie.mov", self.handler.missing_exif_files)

    @patch("src.exif_handler.HACHOIR_AVAILABLE", True)
    @patch("src.exif_handler.extractMetadata", create=True)
    @patch("src.exif_handler.createParser", create=True)
    def test_plaintext_regex_extracts_date(self, mock_parser, mock_extract):
        """A creation date found only in plaintext lines is parsed out."""

        class PlaintextOnly:
            def get(self, key, default=None, index=0):
                raise ValueError("Metadata has no value '%s'" % key)

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
            def get(self, key, default=None, index=0):
                raise ValueError("Metadata has no value '%s'" % key)

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


class TestHachoirCreationDateHelper(unittest.TestCase):
    """
    _hachoir_creation_date: the get() guard, date->datetime promotion, and
    rejection of any other type (#16).

    hachoir's registered ``creation_date`` key declares its type as
    ``(datetime, date)`` (see ``hachoir/metadata/register.py``), so a bare
    ``date`` is a real possibility, not a hypothetical -- this is what closes
    the type hazard flagged in the issue, where an unchecked value could
    previously have reached ``dt.strftime(...)`` and raised.
    """

    def test_returns_datetime_value_unchanged(self):
        """A datetime value round-trips exactly, read via the real get() shape."""
        metadata = Mock()
        metadata.get.return_value = datetime(2024, 1, 15, 14, 30, 45)

        result = _hachoir_creation_date(metadata)

        self.assertEqual(result, datetime(2024, 1, 15, 14, 30, 45))
        metadata.get.assert_called_once_with('creation_date')

    def test_bare_date_is_promoted_to_midnight_datetime(self):
        """A date-only value (hachoir's declared alternate type) becomes midnight."""
        from datetime import date

        metadata = Mock()
        metadata.get.return_value = date(2024, 1, 15)

        result = _hachoir_creation_date(metadata)

        self.assertEqual(result, datetime(2024, 1, 15, 0, 0, 0))

    def test_missing_key_value_error_returns_none(self):
        """get() raising ValueError (hachoir's real "no value" contract) yields None."""
        metadata = Mock()
        metadata.get.side_effect = ValueError(
            "Metadata has no value 'creation_date' (index 0)"
        )

        self.assertIsNone(_hachoir_creation_date(metadata))

    def test_unexpected_type_is_rejected_not_returned_raw(self):
        """A value that is neither datetime nor date is rejected, not passed through."""
        metadata = Mock()
        metadata.get.return_value = "2024-01-15"  # a str, not datetime/date

        self.assertIsNone(_hachoir_creation_date(metadata))


class TestHachoirFastPathRealBoundary(unittest.TestCase):
    """
    _extract_video_timestamp_hachoir against REAL hachoir parsing (#16).

    Every other hachoir test in this module mocks ``createParser``/
    ``extractMetadata``. These two build genuine MP4/MOV box bytes and let
    hachoir's actual parser and ``Metadata.get(...)`` run unmocked, so a
    regression back to the dead ``hasattr``/``getattr`` probe is caught
    against the real library boundary the fast path depends on, not a mock of
    this module's own code.
    """

    def setUp(self):
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_real_mvhd_creation_date_skips_plaintext_scan(self):
        """A real mvhd creation_time is read via get(); exportPlaintext never runs."""
        from hachoir.metadata.metadata import RootMetadata

        utc_1904 = self._seconds_1904(datetime(2026, 7, 5, 1, 33, 3))
        path = os.path.join(self.temp_dir, "real_mvhd.mov")
        _write_minimal_mp4(path, _mvhd_only_moov(utc_1904))

        with patch.object(
            RootMetadata, "exportPlaintext", wraps=RootMetadata.exportPlaintext
        ) as spy:
            result = self.handler._extract_video_timestamp_hachoir(path)

        self.assertEqual(result, datetime(2026, 7, 5, 1, 33, 3))
        spy.assert_not_called()

    def test_real_metadata_without_creation_date_reaches_plaintext(self):
        """
        A real file whose only box is a track header (no mvhd, so hachoir's
        Metadata carries no ``creation_date``) genuinely reaches
        exportPlaintext -- proving that scan is a real last resort, not
        permanently disabled -- and still yields no date, recording the file
        as missing.
        """
        from hachoir.metadata.metadata import RootMetadata

        path = os.path.join(self.temp_dir, "real_tkhd_only.mov")
        _write_minimal_mp4(path, _tkhd_only_moov())

        with patch.object(
            RootMetadata, "exportPlaintext", wraps=RootMetadata.exportPlaintext
        ) as spy:
            result = self.handler._extract_video_timestamp_hachoir(path)

        self.assertIsNone(result)
        self.assertIn(path, self.handler.missing_exif_files)
        spy.assert_called_once()

    @staticmethod
    def _seconds_1904(dt_utc: datetime) -> int:
        """Seconds from 1904-01-01 to a UTC datetime, for building mvhd fixtures."""
        return int((dt_utc - datetime(1904, 1, 1)).total_seconds())

    def test_zeroed_mvhd_is_rejected_not_named_1904(self):
        """
        Issue #62, acceptance criterion 3: a real, unmocked mvhd whose
        creation_time is the QuickTime epoch (0 -> 1904-01-01) is exactly the
        defect found on the project owner's real export -- two AI-generated
        mp4s with no Apple creationdate key and a zeroed mvhd were archived as
        ``1904.01.01.00.00.00.mp4`` / ``...00.01.mp4``. hachoir's ``get()``
        genuinely returns ``datetime(1904, 1, 1)`` here (this is not mocked),
        so the rejection must happen in this module, not in hachoir --
        specifically via ``_is_quicktime_epoch_sentinel`` (fix round 1): the
        generic plausibility floor (1826) does NOT reject 1904 on its own,
        by design, so this pins the actual rejecting mechanism, not just the
        end result. The plaintext last-resort scan is exercised too (via the
        real ``exportPlaintext``) and also finds nothing plausible, so the
        overall result is None -- never 1904.
        """
        path = os.path.join(self.temp_dir, "zeroed_mvhd.mov")
        _write_minimal_mp4(path, _mvhd_only_moov(0))

        result = self.handler._extract_video_timestamp_hachoir(path)

        self.assertIsNone(result)
        self.assertNotEqual(result, datetime(1904, 1, 1))
        self.assertIn(path, self.handler.missing_exif_files)

    def test_near_zero_mvhd_is_also_rejected_not_named_1904(self):
        """
        Issue #62, fix round 1: the real reproduction's SECOND file had an
        independently zeroed mvhd that the collision logic then bumped a
        second forward, but its own raw mvhd value (1, not 0) must be
        rejected on its own merits, not merely because of the bump -- this
        builds a real, unmocked mvhd == 1 (1904-01-01 00:00:01) directly, the
        exact case an exact-instant-only sentinel check would miss.
        """
        path = os.path.join(self.temp_dir, "near_zero_mvhd.mov")
        _write_minimal_mp4(path, _mvhd_only_moov(1))

        result = self.handler._extract_video_timestamp_hachoir(path)

        self.assertIsNone(result)
        self.assertIn(path, self.handler.missing_exif_files)

    def test_zeroed_mvhd_end_to_end_falls_through_to_filesystem_time(self):
        """
        The full chain the real defect exercised: extract_timestamp resolves
        to None for a zeroed-mvhd video (not 1904), and get_fallback_timestamp
        -- which is what FileProcessor calls next -- names the file from the
        filesystem instead, closing the path that produced
        ``backup/videos/1904.01.01.00.00.0{0,1}.mp4`` on the real export.
        """
        path = os.path.join(self.temp_dir, "zeroed_end_to_end.mov")
        _write_minimal_mp4(path, _mvhd_only_moov(0))

        extracted = self.handler.extract_timestamp(path)
        fallback = self.handler.get_fallback_timestamp(path)

        self.assertIsNone(extracted)
        self.assertNotEqual(fallback.year, 1904)
        # A file just written to disk has an mtime in the present, not 1904.
        self.assertGreater(fallback.year, 2000)


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


class TestHachoirLogRoutedThroughApplicationLogger(unittest.TestCase):
    """
    hachoir's own private logger is silenced and redirected through the
    application logger instead (issue #60), against a real, unmocked
    hachoir invocation -- not a mock of this module's own forwarding
    function, which would prove nothing about whether hachoir's real
    ``log.use_print``/``log.on_new_message`` were actually configured.

    Forces a fresh resolution of ``_ensure_hachoir_imported()`` for every
    test here (rather than relying on whichever earlier test in the suite
    happens to trigger the first real import and thus the first real
    configuration of hachoir's logger) by resetting ``HACHOIR_AVAILABLE``
    to ``None`` for the duration of each test.
    """

    def setUp(self):
        self.handler = ExifHandler()
        self.temp_dir = tempfile.mkdtemp()
        self._hachoir_patch = patch("src.exif_handler.HACHOIR_AVAILABLE", None)
        self._hachoir_patch.start()

    def tearDown(self):
        self._hachoir_patch.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_garbage_video(self, name="garbage.mp4"):
        """
        A deliberately malformed "video": not a real MP4/MOV container at
        all, so ``createParser`` cannot identify a format and hachoir
        writes its own ``[warn] Skip parser ...`` diagnostic -- the exact
        real-world shape the issue's own reproduction used
        ("garbage.mp4 -> None").
        """
        path = os.path.join(self.temp_dir, name)
        with open(path, "wb") as f:
            f.write(b"not a real video container, just garbage bytes" * 5)
        return path

    def test_malformed_video_writes_nothing_to_stdout_or_stderr(self):
        """createParser's own diagnostic must not reach either stream
        directly (acceptance criterion 1). Checks for hachoir's own
        characteristic markers specifically, rather than asserting the
        streams are empty outright: this module's own
        ``logger.warning("Could not create parser...")`` call, a few lines
        below the ``createParser`` call under test, legitimately reaches
        stderr through Python's own unconfigured-logging fallback in this
        bare unittest environment (no ``setup_logging`` is running here) --
        that is this application's own diagnostic, not hachoir bypassing
        it, and is not what this test exists to catch.
        """
        import io

        path = self._make_garbage_video()

        captured_out, captured_err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", captured_out), patch("sys.stderr", captured_err):
            self.handler._extract_video_timestamp_hachoir(path)

        for stream_name, captured in (("stdout", captured_out), ("stderr", captured_err)):
            text = captured.getvalue()
            self.assertNotIn(
                "[warn]", text, f"a bare hachoir warning leaked to {stream_name}"
            )
            self.assertNotIn(
                "[err!]", text, f"a bare hachoir error leaked to {stream_name}"
            )
            self.assertNotIn(
                "hachoir", text.lower(),
                f"hachoir's own diagnostic text leaked to {stream_name}",
            )

    def test_diagnostic_reaches_the_application_logger_at_debug(self):
        """The same diagnostic reaches the application logger at DEBUG
        (i.e. surfaces under ``--verbose``) instead of vanishing along with
        the direct stderr write (acceptance criterion 2)."""
        path = self._make_garbage_video()

        with self.assertLogs("src.exif_handler", level="DEBUG") as captured:
            self.handler._extract_video_timestamp_hachoir(path)

        self.assertTrue(
            any("hachoir:" in message for message in captured.output),
            "no forwarded hachoir diagnostic reached the application logger",
        )

    def test_diagnostic_is_absent_at_the_default_info_level(self):
        """At the default (non-verbose) level, the forwarded hachoir
        message is silent -- it is deliberately DEBUG-only, matching every
        other verbose-only diagnostic in this module, not merely relocated
        from one always-visible channel to another."""
        path = self._make_garbage_video()

        with self.assertLogs("src.exif_handler", level="INFO") as captured:
            self.handler._extract_video_timestamp_hachoir(path)

        self.assertFalse(
            any("hachoir:" in message for message in captured.output),
            "the forwarded hachoir diagnostic leaked into INFO-level output",
        )


if __name__ == "__main__":
    unittest.main()
