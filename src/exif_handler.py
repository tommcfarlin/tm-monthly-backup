"""
EXIF timestamp extraction and handling module for photos and videos
"""

import os
import re
import struct
import logging
from datetime import date, datetime, time, timedelta
from typing import Iterator, Optional, Tuple, TYPE_CHECKING
from pathlib import Path
from PIL import Image
import pillow_heif
from dateutil import parser as dateutil_parser

from .media_types import VIDEO_EXTENSIONS as _SHARED_VIDEO_EXTENSIONS, merge_exif_ifds

# Video metadata extraction (issue #47): hachoir.parser's __init__ eagerly
# imports every parser it ships -- audio, video, container, archive, network,
# win32 -- making it the single largest item in this module's import graph,
# larger than rich or Pillow+pillow-heif combined, yet a batch with zero
# video files never needs any of it. HACHOIR_AVAILABLE starts as None ("not
# yet probed") instead of eagerly resolving True/False here; the actual
# import happens lazily, in _ensure_hachoir_imported below, the first time
# _extract_video_timestamp_hachoir needs it -- i.e. only once a video file's
# Apple local-creationdate key (issue #28) is absent and hachoir's mvhd
# fallback is actually reached.
HACHOIR_AVAILABLE = None

# The two hachoir internals ``_create_parser_closing_stream_on_error`` needs
# (issue #66). Bound lazily by ``_ensure_hachoir_imported`` alongside everything
# else, so a photo-only run still never pays hachoir's import cost (issue #47).
_FileInputStream = None
_guessParser = None


def _forward_hachoir_log(level, prefix, text, context) -> None:
    """
    Route one hachoir diagnostic into the application logger (issue #60).

    hachoir maintains its own private logging system (``hachoir.core.log``)
    that, left unconfigured, writes every parser warning straight to
    ``sys.stderr`` -- bypassing this application's logger, the
    ``RichHandler``, and the sanitizing filter issue #9 installs, entirely
    outside anything ``setup_logging`` or ``logging.disable`` can affect.
    Two problems follow from that: ``_extract_video_timestamp_hachoir`` runs
    while ``CLIInterface`` has a live ``rich`` ``Progress`` display open, so
    a bare stderr write for every malformed video in a library interleaves
    with and visibly scrambles the progress bars; and the message content
    is derived from the untrusted video container's own bytes, so writing it
    straight to the terminal outside the one path issue #9 already sanitizes
    is the wrong default even though no concrete escape-sequence injection
    through it was demonstrated when this issue was filed. Registered as
    ``hachoir.core.log.log.on_new_message`` -- the documented redirect hook
    -- once ``log.use_print`` is turned off, so nothing reaches stderr and
    everything worth keeping instead reaches ``--verbose`` (DEBUG) exactly
    like every other diagnostic this module logs.

    Args:
        level: hachoir's own severity (``LOG_INFO``/``LOG_WARN``/``LOG_ERROR``);
            not used here, since this application does not mirror hachoir's
            severity taxonomy -- every forwarded message lands at DEBUG.
        prefix: hachoir's own rendered severity tag (e.g. ``"[warn]"``).
        text: The message body, already formatted by hachoir.
        context: The hachoir parser/field instance that raised the message;
            not used here.
    """
    logger.debug("hachoir: %s %s", prefix, text)


def _create_parser_closing_stream_on_error(file_path: str):
    """
    hachoir's ``createParser``, but without leaking a file descriptor (issue #66).

    hachoir's own implementation is::

        stream = FileInputStream(filename, ...)   # opens the file internally
        guess = guessParser(stream)
        if guess is None:
            stream.close()
        return guess

    It closes the stream when ``guessParser`` *returns* ``None``, but nothing
    closes anything when either call *raises* -- and they do raise, routinely, on
    the input this tool is pointed at: a zero-byte ``.mov`` in an export is
    enough, and produces ``InputStreamError("Input size is nul")`` from inside
    ``FileInputStream`` itself, *after* it has already called
    ``open(real_filename, 'rb')``.

    That detail dictates the shape of this function, and was established by
    measurement rather than assumed. Wrapping only ``guessParser`` in a
    try/except fixes nothing, because the common failure happens one step
    earlier, before any ``stream`` local exists to close. The file handle is
    therefore opened HERE and passed in (``FileInputStream`` accepts a file
    object, deriving its ``source`` from ``.name``), so this function owns the
    descriptor on every path and can guarantee its release.

    Why the descriptor survives at all: on CPython, refcounting reclaims an
    abandoned stream immediately, so a discarded exception leaks nothing. But
    this project deliberately retains exception OBJECTS (not ``str(error)``) in
    its failure records, and a retained exception keeps its traceback, which
    keeps the raising frame alive, which keeps that frame's open file alive --
    for the rest of the run. Measured: 40 unparseable videos with their
    exceptions retained held 40 extra descriptors open; with this function, zero.

    On success, ownership passes to the caller, whose ``with parser:`` block
    closes the stream and with it this handle.

    Bound onto the module-level ``createParser`` name by
    :func:`_ensure_hachoir_imported`, deliberately: that name is the seam the
    test suite patches, so replacing what it points at fixes production without
    silently defanging any test that patches it.

    Args:
        file_path: Path to the media file to parse.

    Returns:
        A hachoir parser, or ``None`` when the format is unrecognized.
    """
    handle = open(file_path, 'rb')
    try:
        stream = _FileInputStream(handle)
        parser = _guessParser(stream)
    except BaseException:
        # BaseException so a KeyboardInterrupt mid-parse also releases the
        # descriptor rather than leaking it on the way out.
        handle.close()
        raise
    if parser is None:
        # Mirrors hachoir's own None handling. Closing the stream closes the
        # handle it was built from.
        stream.close()
    return parser


def _ensure_hachoir_imported() -> None:
    """
    Resolve ``HACHOIR_AVAILABLE`` and bind ``createParser``/``extractMetadata``.

    Called once, from :meth:`ExifHandler._extract_video_timestamp_hachoir`,
    immediately before those two names are used. A no-op whenever
    ``HACHOIR_AVAILABLE`` is already something other than ``None`` -- either
    because a previous call already resolved it for real, or because a test
    has patched it directly (every test in ``tests/test_exif_handler*.py``
    that patches ``createParser``/``extractMetadata`` also patches
    ``HACHOIR_AVAILABLE`` to ``True`` in the same decorator stack). That
    short-circuit matters: those tests patch ``createParser``/
    ``extractMetadata`` with ``create=True`` precisely because the two names
    do not exist at module scope until hachoir is actually probed. If this
    function ignored an already-resolved ``HACHOIR_AVAILABLE`` and re-ran the
    real import whenever hachoir happens to be installed, it would clobber
    those mocks with the genuine hachoir callables and silently defang every
    one of those tests -- the exact trap this issue's brief warns about.
    """
    global HACHOIR_AVAILABLE, createParser, extractMetadata
    global _FileInputStream, _guessParser
    if HACHOIR_AVAILABLE is not None:
        return
    try:
        from hachoir.metadata import extractMetadata as _extractMetadata
        from hachoir.core.log import log as _hachoir_log
        # The two pieces hachoir's own createParser is built from. Imported
        # directly so the stream can be closed when guessParser raises, which
        # hachoir's version does not do (issue #66).
        from hachoir.stream import FileInputStream as _stream_factory
        from hachoir.parser.guess import guessParser as _guess_parser
    except ImportError:
        HACHOIR_AVAILABLE = False
        return
    _FileInputStream = _stream_factory
    _guessParser = _guess_parser
    # Bound to the leak-free equivalent rather than hachoir's createParser --
    # same contract, same return values, minus the descriptor leak (issue #66).
    createParser = _create_parser_closing_stream_on_error
    extractMetadata = _extractMetadata
    # Silence hachoir's own stderr writes and redirect them through this
    # module's logger instead (issue #60). Done here, at the same lazy
    # first-use point issue #47 already established, rather than at module
    # import time: configuring hachoir's logger still requires importing
    # hachoir.core.log, and doing that eagerly at module scope would defeat
    # #47's entire point of never paying hachoir's import cost on a
    # photo-only run.
    _hachoir_log.use_print = False
    _hachoir_log.on_new_message = _forward_hachoir_log
    HACHOIR_AVAILABLE = True


if TYPE_CHECKING:
    # Type-hint only: FileCategorizer.ImageMetadata is not imported at runtime
    # so this module never depends on file_categorizer. Before issue #47,
    # file_categorizer imported THIS module for ifd0_tag_names/merge_exif_ifds,
    # so this guard was also what kept that edge one-directional (issue #24).
    # Issue #47 moved those two helpers to media_types.py, so file_categorizer
    # no longer imports exif_handler at all, at runtime or otherwise -- this
    # TYPE_CHECKING guard is now belt-and-suspenders rather than load-bearing,
    # but is kept since a live import here would still be wrong on its own
    # terms (this module has no runtime need for FileCategorizer).
    from .file_categorizer import ImageMetadata

# Enable HEIF support in Pillow
pillow_heif.register_heif_opener()

logger = logging.getLogger(__name__)

# --- QuickTime / MP4 metadata box scanning ---------------------------------
#
# Apple writes the TRUE LOCAL wall-clock capture time (with its UTC offset)
# into the ``com.apple.quicktime.creationdate`` metadata key, e.g.
# ``2024-06-15T21:33:03-0400``. That key lives in a ``moov/meta`` structure
# made of a ``keys`` atom (which names each key) and an ``ilst`` atom (which
# holds each key's value, addressed by the key's 1-based index). hachoir does
# NOT surface this key -- it only reports the ``mvhd`` creation_time, which is
# defined as UTC and therefore lands late-evening captures on the wrong
# calendar day. So we scan the box structure ourselves (see issue #28).

# The Apple metadata key carrying the true local capture time.
QUICKTIME_CREATIONDATE_KEY = b"com.apple.quicktime.creationdate"

# Child atoms that appear directly inside a ``meta`` atom. Used to tell the
# QuickTime ``meta`` layout (children immediately follow the header) from the
# ISO/MP4 layout (a 4-byte version+flags precedes the children).
_META_CHILD_ATOMS = frozenset((b"hdlr", b"keys", b"ilst"))

# Upper bound on how many bytes of a ``moov`` atom we will read into memory.
# Real ``moov`` atoms (sample tables + metadata) are small relative to the
# media data; this cap keeps a malformed or hostile file from exhausting RAM.
_MAX_MOOV_BYTES = 128 * 1024 * 1024

# Earliest surviving photograph is 1826 (Niepce's "View from the Window at Le
# Gras"). This is deliberately generous, NOT tightened to the digital-camera
# era: a scanned family photograph carrying a deliberately backdated EXIF
# DateTimeOriginal (a 1950s/1960s print digitized into the library) is a
# legitimate, valued input to a personal photo archive, and a tighter floor
# would silently discard exactly the dates that matter most and are hardest
# to reconstruct by falling them back to a meaningless filesystem mtime. A
# datetime that parses cleanly but falls outside [MIN_PLAUSIBLE_CAPTURE, "now"
# + 1 day] is corrupt/fabricated metadata rather than a real capture time --
# a crafted EXIF year of 9999 or 0001 -- and must be bounded before it names
# an archive path.
#
# NOTE: this floor deliberately does NOT catch the QuickTime ``mvhd``
# zero-epoch sentinel (1904-01-01, issue #62's confirmed real-world defect,
# see backup/videos/1904.01.01.00.00.0{0,1}.mp4) -- 1904 is LATER than 1826,
# so it passes this floor untouched. That is intentional, not an oversight:
# 1904 is not "implausibly old" in the general sense this floor polices, it
# is a sentinel meaning "this specific field never recorded a value", and is
# rejected separately by _is_quicktime_epoch_sentinel below, scoped to the
# one field whose encoding produces it. A first pass at this fix folded both
# concerns into a single, tighter floor (1970) and it silently broke the
# scanned-photo case described above; keep them separate.
MIN_PLAUSIBLE_CAPTURE = datetime(1826, 1, 1)

# Which way to read an ambiguous ``NN-NN-YYYY`` filename, keyed by a marker in
# the filename that identifies the generator (issue #70).
#
# Two real conventions collide inside one regex, and the digits alone cannot
# separate them whenever both leading fields are <= 12:
#
#   ScreenRecording_03-05-2024 09-15-00_1.mp4   is MONTH-first (July 1)
#   Facetune_09-02-2024-11-22-33.heic           is DAY-first (4 July)
#
# The macOS/iOS screen-recording reading is not a guess: for a real file of that
# name the container's own ``mvhd`` says 2024-03-05 13:15:00 UTC, which is
# 08:58:01 EDT -- exactly the time in the filename, confirming the leading
# ``07-01`` is month-first. Issue #51 introduced this pattern for Facetune and
# tried day-first first for everything, so every ScreenRecording_ capture whose
# metadata was missing (common: a re-muxed file often carries a 1904 ``mvhd``)
# was filed up to eleven months off.
#
# Markers are matched case-insensitively anywhere in the filename. A name with
# no known marker keeps the day-first-first order issue #51 established, because
# there is genuinely no evidence either way for an unrecognized generator and
# changing that default would silently re-date files this project has never
# observed.
_DAY_FIRST = 'day-first (DD-MM-YYYY)'
_MONTH_FIRST = 'month-first (MM-DD-YYYY)'
FILENAME_DATE_CONVENTIONS: Tuple[Tuple[str, str], ...] = (
    ('screenrecording', _MONTH_FIRST),
    ('screen recording', _MONTH_FIRST),
    ('screen_recording', _MONTH_FIRST),
    ('facetune', _DAY_FIRST),
)
DEFAULT_AMBIGUOUS_DATE_ORDER = _DAY_FIRST


def filename_date_order(filename: str) -> str:
    """
    Return which reading of an ambiguous ``NN-NN-YYYY`` filename to try first.

    Args:
        filename: Basename to inspect (matched case-insensitively).

    Returns:
        ``_MONTH_FIRST`` or ``_DAY_FIRST`` -- the convention of the first
        matching marker in :data:`FILENAME_DATE_CONVENTIONS`, or
        :data:`DEFAULT_AMBIGUOUS_DATE_ORDER` when the generator is unrecognized.
    """
    lowered = filename.lower()
    for marker, convention in FILENAME_DATE_CONVENTIONS:
        if marker in lowered:
            return convention
    return DEFAULT_AMBIGUOUS_DATE_ORDER


def _iter_boxes(buf: bytes, start: int, end: int) -> Iterator[Tuple[bytes, int, int]]:
    """
    Yield ``(type, body_offset, box_end)`` for each ISO base-media box in a span.

    Walks the flat list of boxes between ``start`` and ``end`` in ``buf``,
    honoring 32-bit sizes, the 64-bit ``largesize`` escape (size field == 1)
    and the "extends to end" escape (size field == 0). ``body_offset`` points
    at the first byte after the box header; ``box_end`` at the first byte after
    the whole box. Iteration stops on any malformed or out-of-range size rather
    than raising, so a truncated tail cannot crash the scan.

    Args:
        buf: Buffer containing the boxes.
        start: Offset of the first box to consider.
        end: Exclusive offset at which to stop.

    Yields:
        ``(type, body_offset, box_end)`` for each well-formed box.
    """
    off = start
    while off + 8 <= end:
        size = struct.unpack(">I", buf[off:off + 4])[0]
        typ = buf[off + 4:off + 8]
        if size == 1:  # 64-bit largesize follows the type
            if off + 16 > end:
                return
            size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
            body = off + 16
        elif size == 0:  # box runs to the end of the enclosing span
            size = end - off
            body = off + 8
        else:
            body = off + 8
        if size < (body - off) or off + size > end:
            return
        yield typ, body, off + size
        off += size


def _meta_children_start(buf: bytes, body: int, box_end: int) -> int:
    """
    Return the offset of the first child atom inside a ``meta`` box.

    QuickTime (.mov) writes ``meta`` as a plain container whose children start
    immediately; ISO/MP4 writes ``meta`` as a full box with a leading 4-byte
    version+flags. We disambiguate by checking whether a known child atom type
    sits at the QuickTime position or the ISO position, defaulting to the
    QuickTime layout.

    Args:
        buf: Buffer containing the ``meta`` box.
        body: Offset of the first byte after the ``meta`` header.
        box_end: Exclusive offset at which the ``meta`` box ends.

    Returns:
        Offset at which to begin iterating the ``meta`` box's children.
    """
    if body + 8 <= box_end and buf[body + 4:body + 8] in _META_CHILD_ATOMS:
        return body
    if body + 12 <= box_end and buf[body + 8:body + 12] in _META_CHILD_ATOMS:
        return body + 4
    return body


def find_quicktime_creationdate(moov: bytes) -> Optional[str]:
    """
    Find the ``com.apple.quicktime.creationdate`` string in a ``moov`` payload.

    Scans ``moov`` for a ``meta`` atom, resolves the 1-based index of the
    creationdate key in its ``keys`` atom, then reads that index's value from
    the ``ilst`` atom's ``data`` box. Returns the raw ISO-8601 string (e.g.
    ``2024-06-15T21:33:03-0400``) or ``None`` if any piece is absent.

    Args:
        moov: The payload (children) of a ``moov`` box.

    Returns:
        The creationdate string if present, otherwise ``None``.
    """
    for typ, body, box_end in _iter_boxes(moov, 0, len(moov)):
        if typ != b"meta":
            continue
        start = _meta_children_start(moov, body, box_end)
        value = _scan_meta_for_creationdate(moov, start, box_end)
        if value is not None:
            return value
    return None


def _scan_meta_for_creationdate(buf: bytes, start: int, end: int) -> Optional[str]:
    """Locate ``keys``/``ilst`` in a ``meta`` box and return the creationdate value."""
    keys_span = None
    ilst_span = None
    for typ, body, box_end in _iter_boxes(buf, start, end):
        if typ == b"keys":
            keys_span = (body, box_end)
        elif typ == b"ilst":
            ilst_span = (body, box_end)
    if keys_span is None or ilst_span is None:
        return None
    index = _creationdate_key_index(buf, keys_span[0], keys_span[1])
    if index is None:
        return None
    return _ilst_string_value(buf, ilst_span[0], ilst_span[1], index)


def _creationdate_key_index(buf: bytes, start: int, end: int) -> Optional[int]:
    """
    Return the 1-based index of the creationdate key within a ``keys`` atom.

    A ``keys`` atom is a 4-byte version+flags, a 4-byte entry count, then one
    entry per key: a 4-byte size, a 4-byte namespace (e.g. ``mdta``) and the
    key name. Keys are numbered from 1 in declaration order; that number is how
    the matching ``ilst`` item is addressed.
    """
    pos = start + 8  # skip version/flags (4) + entry_count (4)
    index = 0
    while pos + 8 <= end:
        entry_size = struct.unpack(">I", buf[pos:pos + 4])[0]
        if entry_size < 8 or pos + entry_size > end:
            return None
        key_name = buf[pos + 8:pos + entry_size]
        index += 1
        if key_name == QUICKTIME_CREATIONDATE_KEY:
            return index
        pos += entry_size
    return None


def _ilst_string_value(buf: bytes, start: int, end: int, index: int) -> Optional[str]:
    """
    Read the UTF-8 string stored under ``index`` in an ``ilst`` atom.

    Each ``ilst`` item is a box whose type is the 4-byte big-endian key index.
    Inside sits a ``data`` box: a 4-byte type indicator, a 4-byte locale and
    then the payload bytes. Returns the decoded, trimmed string or ``None``.
    """
    for typ, body, box_end in _iter_boxes(buf, start, end):
        if struct.unpack(">I", typ)[0] != index:
            continue
        for data_typ, data_body, data_end in _iter_boxes(buf, body, box_end):
            if data_typ != b"data":
                continue
            payload = buf[data_body + 8:data_end]  # skip type indicator + locale
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                return None
            return text.strip("\x00").strip() or None
    return None


def parse_local_creationdate(value: str) -> Optional[datetime]:
    """
    Parse an Apple creationdate string to a naive LOCAL wall-clock datetime.

    Apple stamps the local capture time together with its UTC offset, e.g.
    ``2024-06-15T21:33:03-0400``. The backup tool names files by the local
    wall-clock reading, so we keep the clock time exactly as written and drop
    the offset (``21:33:03`` stays ``21:33:03``) rather than converting to UTC.
    ``python-dateutil`` handles the compact ``-0400`` / ``+0530`` offsets and a
    trailing ``Z`` that ``datetime.fromisoformat`` rejects on older Pythons.

    Args:
        value: An ISO-8601 datetime string, optionally carrying an offset.

    Returns:
        A timezone-naive ``datetime`` in local wall-clock terms, or ``None`` if
        the string cannot be parsed.
    """
    try:
        parsed = dateutil_parser.isoparse(value)
    except (ValueError, OverflowError, TypeError):
        return None
    # Keep the wall-clock reading; discard the offset so the file is named by
    # the local time Apple recorded, not a UTC-shifted value.
    return parsed.replace(tzinfo=None)


def _hachoir_creation_date(metadata) -> Optional[datetime]:
    """
    Read hachoir's ``creation_date`` field via its real ``get`` API (issue #16).

    hachoir's ``Metadata`` object has no ``creation_date`` attribute -- it
    exposes values through ``get(key)`` -- so the previous ``hasattr``/
    ``getattr`` probe never fired and every video paid for the
    ``exportPlaintext()`` fallback. ``get`` also raises ``ValueError``,
    rather than returning ``None``, when the key has no value, so that is
    guarded here rather than at each call site.

    The registered ``creation_date`` key's declared type is
    ``(datetime, date)``. A bare ``date`` (no time-of-day) is promoted to
    midnight so this always returns a real ``datetime`` or ``None`` --
    never the raw hachoir value -- closing the type hazard where a non-
    ``datetime`` result reached ``format_timestamp_filename``'s
    ``dt.strftime(...)`` unchecked.

    Args:
        metadata: A hachoir ``Metadata`` object (or compatible test double).

    Returns:
        A ``datetime``, or ``None`` if the key is absent or not a usable type.
    """
    try:
        value = metadata.get('creation_date')
    except ValueError:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time())
    return None


class ExifHandler:
    """Handles EXIF data extraction and timestamp processing for photos and videos"""

    # EXIF timestamp tags in order of preference
    TIMESTAMP_TAGS = [
        'DateTimeOriginal',    # Camera capture time (preferred)
        'DateTime',            # File modification time
        'DateTimeDigitized'    # Digitization time
    ]

    # Pointer tag (ExifTags.IFD.Exif) to the Exif sub-IFD. The preferred
    # timestamp tags DateTimeOriginal (0x9003) and DateTimeDigitized (0x9004)
    # live behind this pointer, NOT in IFD0, so Image.getexif() does not expose
    # them at the top level. They are only reachable via getexif().get_ifd().
    EXIF_IFD = 0x8769

    # Video file extensions that need special handling. Single shared
    # definition, imported from media_types.py (issue #43) so this set can
    # never drift from the copy FileCategorizer uses to route to videos/.
    VIDEO_EXTENSIONS = _SHARED_VIDEO_EXTENSIONS

    def __init__(self):
        self.missing_exif_files = []

    def _is_video_file(self, file_path: str) -> bool:
        """Check if file is a video file based on extension"""
        return Path(file_path).suffix.lower() in self.VIDEO_EXTENSIONS

    @staticmethod
    def _is_plausible_capture_time(dt: datetime, now: Optional[datetime] = None) -> bool:
        """
        Bound a candidate capture time to a plausible real-world range (#62).

        A value can parse cleanly as a ``datetime`` and still be nonsense as a
        capture time: a crafted EXIF ``DateTime`` of ``9999:12:31 23:59:59`` or
        ``0001:01:01 00:00:00``. This is the single generic predicate shared
        by every timestamp source in this module -- EXIF
        (:meth:`_parse_exif_datetime`), both video paths (Apple's local
        ``creationdate`` in :meth:`_extract_quicktime_creationdate` and
        hachoir's ``mvhd`` in :meth:`_extract_video_timestamp_hachoir`), and
        the filename fallback (:meth:`_extract_timestamp_from_filename`) --
        rather than repeated ad hoc range checks.

        Deliberately NOT covered here: the QuickTime ``mvhd`` zero-epoch
        sentinel (1904-01-01). That value is later than ``MIN_PLAUSIBLE_
        CAPTURE`` and passes this check -- on purpose. It is a sentinel
        meaning "this field never recorded a value", not an implausibly old
        capture, and folding it into this generic floor would also reject the
        genuine 1950s/60s dates a scanned family photograph can legitimately
        carry. See :meth:`_is_quicktime_epoch_sentinel` for that check.

        Callers never raise on a ``False`` result; they treat it exactly like
        a missing value and fall through to the next timestamp source, which
        keeps this module's existing fail-safe shape (a file is still archived
        under a filesystem timestamp, never abandoned).

        The upper bound allows one day past ``now`` so a capture made across a
        timezone boundary -- e.g. one hour in the future from this machine's
        clock -- is accepted rather than treated as hostile.

        Args:
            dt: Candidate capture time to validate.
            now: Reference "current" time for the upper bound. Defaults to
                ``datetime.now()``; overridable so tests can pin both bounds
                deterministically instead of racing a live clock, and so a
                test asserting "one hour in the future is accepted" does not
                rot as real time passes.

        Returns:
            True if ``dt`` falls within ``[MIN_PLAUSIBLE_CAPTURE, now + 1 day]``.
        """
        if now is None:
            now = datetime.now()
        return MIN_PLAUSIBLE_CAPTURE <= dt <= now + timedelta(days=1)

    @staticmethod
    def _is_quicktime_epoch_sentinel(dt: datetime) -> bool:
        """
        True if ``dt`` lands on the QuickTime epoch's calendar date (#62).

        QuickTime's ``mvhd`` ``creation_time`` field is a count of seconds
        since 1904-01-01 00:00:00 UTC. A video whose capture time was never
        stamped into that field -- stripped by a re-muxer, or never written
        by an AI generator -- reports as exactly that epoch (``mvhd == 0`` ->
        1904-01-01 00:00:00) or a handful of seconds past it (the real-world
        reproduction that opened this issue showed a second, independently
        zeroed file land one second later at 00:00:01, which the existing
        collision-bump logic then made look like a deliberate burst pair shot
        in 1904). This checks the whole calendar date, not the exact zero
        instant, so both are caught by one condition rather than an
        enumeration of near-zero offsets.

        This is intentionally NOT folded into
        :meth:`_is_plausible_capture_time`'s generic floor: 1904-01-01 is a
        perfectly plausible real-world date in the abstract (it is well
        after that floor's 1826 bound), and
        widening the generic floor to exclude it costs the ability to accept
        a genuinely backdated capture from a scanned photograph -- exactly
        the regression a first pass at this fix introduced by raising the
        floor to 1970 instead of adding this dedicated, field-scoped check.

        Scope: this check is for the ``mvhd``-derived reading only. Apple's
        ``com.apple.quicktime.creationdate`` key (issue #28) is a distinct
        field with no zero-epoch encoding of its own -- an implausible value
        there is caught by the generic floor/ceiling alone.

        Args:
            dt: Candidate capture time read from the ``mvhd`` field (directly
                or via hachoir's rendered-text last resort).

        Returns:
            True if ``dt``'s calendar date is 1904-01-01.
        """
        return dt.date() == date(1904, 1, 1)

    def extract_timestamp(
        self, file_path: str, metadata: Optional["ImageMetadata"] = None
    ) -> Optional[datetime]:
        """
        Extract timestamp from EXIF data (photos) or metadata (videos).

        Args:
            file_path: Path to image or video file
            metadata: Optional pre-read ``FileCategorizer.ImageMetadata``
                (issue #24). When given, its ``exif`` mapping is used directly
                and ``file_path`` is NOT opened again here -- this is how
                ``FileProcessor`` hands forward the metadata
                ``FileCategorizer.categorize_file`` already read once for this
                same file, closing the second Pillow open this method used to
                perform on every image in a run. ``None`` (the default) is the
                standalone path: every direct caller, the tests, and the video
                path (which never has Pillow metadata to reuse) get exactly
                the pre-#24 behavior of opening ``file_path`` here.

        Returns:
            datetime object if found, None if missing/invalid
        """
        # Handle video files differently
        if self._is_video_file(file_path):
            return self._extract_video_timestamp(file_path)

        if metadata is not None:
            # Pre-read by FileCategorizer while it categorized this same file
            # (issue #24) -- reuse it rather than opening file_path again.
            if not metadata.exif:
                logger.warning("No EXIF data found in %s", file_path)
                self.missing_exif_files.append(file_path)
                return None
            return self._select_timestamp(metadata.exif, file_path)

        # Handle image files with EXIF data
        try:
            with Image.open(file_path) as image:
                exif_data = image.getexif()

                if not exif_data:
                    logger.warning("No EXIF data found in %s", file_path)
                    self.missing_exif_files.append(file_path)
                    return None

                # Flatten IFD0 and the Exif sub-IFD into one name -> value
                # lookup so the priority walk can actually see the preferred
                # DateTimeOriginal / DateTimeDigitized tags.
                candidates = self._timestamp_candidates(exif_data, file_path)

                return self._select_timestamp(candidates, file_path)

        except Exception as e:
            logger.error("Error reading EXIF data from %s: %s", file_path, e)
            self.missing_exif_files.append(file_path)
            return None

    def _select_timestamp(self, candidates: dict, file_path: str) -> Optional[datetime]:
        """
        Walk TIMESTAMP_TAGS in priority order and return the first valid parse.

        Shared by both branches of :meth:`extract_timestamp` -- the
        metadata-provided path and the standalone-open path (issue #24) -- so
        the priority-walk logic lives in exactly one place regardless of
        where ``candidates`` came from.

        Args:
            candidates: Tag-name -> value mapping (see
                :func:`merge_exif_ifds`).
            file_path: File path, used for logging and ``missing_exif_files``.

        Returns:
            The first tag's parsed datetime in TIMESTAMP_TAGS priority order,
            or None if no declared tag is present and valid.
        """
        # Walk TIMESTAMP_TAGS in declared priority order and take the
        # first tag that is present AND parses to a valid datetime. A
        # malformed higher-priority value falls through to the next
        # candidate rather than aborting the search.
        for tag_name in self.TIMESTAMP_TAGS:
            if tag_name not in candidates:
                continue
            parsed = self._parse_exif_datetime(candidates[tag_name], file_path)
            if parsed is not None:
                # A malformed higher-priority tag may have recorded this
                # file as missing; a successful parse supersedes that.
                while file_path in self.missing_exif_files:
                    self.missing_exif_files.remove(file_path)
                return parsed

        logger.warning("No timestamp tags found in EXIF data for %s", file_path)
        self.missing_exif_files.append(file_path)
        return None

    def _timestamp_candidates(self, exif: "Image.Exif", file_path: str) -> dict:
        """
        Flatten IFD0 and the Exif sub-IFD into a tag-name -> value mapping.

        Delegates to :func:`media_types.merge_exif_ifds` (moved there from
        this module by issue #47), which is shared with
        :class:`FileCategorizer`'s once-per-file metadata read (issue #24) so
        both build the identical tag-name view from a decoded EXIF blob
        rather than each re-implementing the IFD0/sub-IFD merge.

        Args:
            exif: The Image.Exif object returned by Image.getexif()
            file_path: File path, used for logging only

        Returns:
            Mapping of resolved tag name -> raw tag value
        """
        return merge_exif_ifds(exif, file_path)

    def _extract_video_timestamp(self, file_path: str) -> Optional[datetime]:
        """
        Extract creation timestamp from video metadata.

        Three sources, in descending order of authority:

        1. Apple's ``com.apple.quicktime.creationdate`` key, which records the
           TRUE local wall-clock capture time with its UTC offset (issue #28).
        2. The filename, when it can be *confirmed* to be a local rendering of
           the same instant the container reports (issue #70, issue #72).
        3. hachoir's ``mvhd`` creation_time, which is UTC.

        Rule 2 exists because ``mvhd`` alone silently mis-times files that DO
        carry their local time, just not in a metadata key. A real screen
        recording named ``ScreenRecording_03-05-2024 09-15-00_1.mp4`` has
        ``mvhd`` = 2024-03-05 13:15:00 UTC and was archived as ``12.58.01`` --
        four hours late, and for any capture after 20:00 EDT that lands on the
        WRONG CALENDAR DAY. The local time was available all along, in the name.

        The cross-check is what makes preferring the name safe. The filename is
        used only when its reading differs from the ``mvhd`` reading by a
        plausible UTC offset -- a whole number of quarter-hours within +/-14h,
        which covers every real zone including the :30 and :45 ones. That is
        strong evidence the two describe one moment in two zones, so the local
        rendering is the better name. A filename whose digits are unrelated to
        the capture (an ID, a resolution, an epoch suffix) will not satisfy it
        and ``mvhd`` is kept. Without that check, blindly preferring the
        filename would let any number pattern outrank real container metadata.

        Args:
            file_path: Path to video file

        Returns:
            datetime object if found, None if missing/invalid
        """
        # Preferred path: Apple's local wall-clock creationdate. Independent of
        # hachoir, so it works even when hachoir cannot be imported.
        local_creation = self._extract_quicktime_creationdate(file_path)
        if local_creation is not None:
            logger.debug(
                "Extracted local video creation date: %s -> %s", file_path, local_creation
            )
            return local_creation

        utc_reading = self._extract_video_timestamp_hachoir(file_path)
        if utc_reading is None:
            # No container time at all. The caller's fallback chain reaches the
            # filename on its own from here (``get_fallback_timestamp``), so
            # there is nothing to cross-check against and nothing to prefer.
            return None

        local_reading = self._local_reading_confirming_utc(file_path, utc_reading)
        if local_reading is not None:
            logger.info(
                "Video filename carries the local capture time for the same "
                "instant as the UTC container time; using the local reading: "
                "%s -> %s (mvhd reported %s)",
                file_path, local_reading, utc_reading,
            )
            return local_reading

        return utc_reading

    def _local_reading_confirming_utc(
        self, file_path: str, utc_reading: datetime
    ) -> Optional[datetime]:
        """
        Return the filename's timestamp if it is the same instant as ``utc_reading``.

        "Same instant" means the two differ by a plausible UTC offset: a whole
        number of quarter-hours, no more than 14 hours either way. Real zones run
        from -12:00 to +14:00 and the only sub-hour ones are :30 and :45, so this
        admits every genuine offset while rejecting an unrelated number that
        merely happens to parse as a date.

        Args:
            file_path: Path whose basename is parsed for a timestamp.
            utc_reading: The UTC capture time read from the container.

        Returns:
            The filename's local reading, or ``None`` when the filename has no
            timestamp or the two cannot be the same moment.
        """
        from_name = self._extract_timestamp_from_filename(file_path)
        if from_name is None:
            return None

        offset_seconds = (from_name - utc_reading).total_seconds()
        if abs(offset_seconds) > 14 * 3600:
            return None
        if offset_seconds % (15 * 60) != 0:
            return None
        return from_name

    def _extract_quicktime_creationdate(self, file_path: str) -> Optional[datetime]:
        """
        Return the local wall-clock creation time from Apple metadata, if any.

        Reads the file's ``moov`` atom and scans it for the
        ``com.apple.quicktime.creationdate`` value, parsing it to a naive local
        datetime. Any I/O or structural problem yields ``None`` so the caller
        falls back to the hachoir/mvhd path; this helper never records a file
        as missing on its own.

        Args:
            file_path: Path to the video file.

        Returns:
            A naive local ``datetime``, or ``None`` if the key is absent or the
            file cannot be scanned.
        """
        try:
            moov = self._read_moov_bytes(file_path)
        except OSError as exc:
            logger.debug("Could not read video boxes from %s: %s", file_path, exc)
            return None
        if moov is None:
            return None

        value = find_quicktime_creationdate(moov)
        if not value:
            return None

        parsed = parse_local_creationdate(value)
        if parsed is None:
            logger.warning(
                "Unparseable QuickTime creationdate in %s: %r", file_path, value
            )
            return None

        # The zero-epoch sentinel can appear HERE too (issue #72). This check was
        # scoped to the mvhd path on the reasoning that Apple's key "has no
        # zero-epoch encoding of its own" -- true of the encoding, but not of the
        # value: a re-muxer that synthesizes this key from a zeroed mvhd writes
        # the literal string "1904-01-01T00:00:00+0000", which parses fine and
        # would be archived as videos/1904.01.01.00.00.00.mov -- exactly the
        # outcome issue #62 exists to prevent, arriving through the sibling
        # field. Returning None drops to the mvhd path and then to the filename
        # and filesystem fallbacks, which is what #62 does for the same value.
        if self._is_quicktime_epoch_sentinel(parsed):
            logger.warning(
                "Implausible QuickTime creationdate (zero-epoch sentinel) in "
                "%s: %r", file_path, value
            )
            return None

        # #28's local-time preference does not exempt this source from the
        # plausibility bound (#62) -- the Apple key can carry a nonsense date
        # just as easily as hachoir's mvhd can. Reject and let the caller fall
        # back to the mvhd path rather than naming a file from it.
        if not self._is_plausible_capture_time(parsed):
            logger.warning(
                "Implausible QuickTime creationdate in %s: %s", file_path, value
            )
            return None

        return parsed

    def _read_moov_bytes(self, file_path: str) -> Optional[bytes]:
        """
        Read the payload of the top-level ``moov`` atom into memory.

        Walks only the top-level box headers, seeking past large boxes such as
        ``mdat`` without reading them, and reads the ``moov`` payload once it is
        found (bounded by ``_MAX_MOOV_BYTES``). ``moov`` may appear before or
        after the media data, so the whole top level is scanned.

        Args:
            file_path: Path to the video file.

        Returns:
            The bytes of the ``moov`` box payload, or ``None`` if there is no
            ``moov`` atom or it exceeds the size cap.
        """
        with Path(file_path).open("rb") as handle:
            offset = 0
            while True:
                header = handle.read(8)
                if len(header) < 8:
                    return None
                size = struct.unpack(">I", header[:4])[0]
                typ = header[4:8]
                body_offset = offset + 8
                if size == 1:  # 64-bit largesize
                    ext = handle.read(8)
                    if len(ext) < 8:
                        return None
                    size = struct.unpack(">Q", ext)[0]
                    body_offset = offset + 16
                elif size == 0:  # box extends to end of file
                    size = None

                if typ == b"moov":
                    if size is not None:
                        payload_len = offset + size - body_offset
                        if payload_len < 0 or payload_len > _MAX_MOOV_BYTES:
                            logger.debug(
                                "moov atom too large or invalid in %s", file_path
                            )
                            return None
                        return handle.read(payload_len)
                    payload = handle.read(_MAX_MOOV_BYTES + 1)
                    if len(payload) > _MAX_MOOV_BYTES:
                        logger.debug("moov atom too large in %s", file_path)
                        return None
                    return payload

                if size is None:
                    return None  # a size==0 non-moov box runs to EOF; nothing after
                if size < 8:
                    return None  # malformed header; stop scanning
                offset += size
                handle.seek(offset)

    def _extract_video_timestamp_hachoir(self, file_path: str) -> Optional[datetime]:
        """
        Fall back to hachoir's ``mvhd`` creation_time (UTC) extraction.

        Used only when Apple's local ``com.apple.quicktime.creationdate`` key is
        absent. The value hachoir reports is UTC, so a file named from it may be
        off by the local UTC offset -- a pre-existing limitation retained here.

        Looks up hachoir's ``creation_date`` field via its real ``get(key)`` API
        (issue #16 -- the previous ``hasattr``/``getattr`` probe named a field
        hachoir does not expose as an attribute, so it never fired and every
        video paid for the ``exportPlaintext()`` scan below regardless). That
        scan remains as a genuine last resort, reached only when the direct
        lookup finds nothing usable.

        Args:
            file_path: Path to video file

        Returns:
            datetime object if found, None if missing/invalid
        """
        _ensure_hachoir_imported()
        if not HACHOIR_AVAILABLE:
            logger.warning("Hachoir not available for video metadata extraction: %s", file_path)
            self.missing_exif_files.append(file_path)
            return None

        try:
            parser = createParser(file_path)
            if not parser:
                logger.warning("Could not create parser for video file: %s", file_path)
                self.missing_exif_files.append(file_path)
                return None

            with parser:
                metadata = extractMetadata(parser)
                if not metadata:
                    logger.warning("No metadata found in video file: %s", file_path)
                    self.missing_exif_files.append(file_path)
                    return None

                creation_date = _hachoir_creation_date(metadata)

                # A zeroed (or near-zeroed) mvhd box -- landing on the
                # QuickTime epoch calendar date, 1904-01-01, e.g. a video
                # stripped by a re-muxer or never stamped by an AI generator
                # -- parses cleanly as a datetime but is a sentinel, not a
                # real capture time; the generic plausibility floor does NOT
                # catch this (1904 is later than that floor's 1826, on
                # purpose -- see _is_quicktime_epoch_sentinel's docstring), so
                # it is checked explicitly here. Reject it exactly like a
                # missing value, so the plaintext scan below gets a chance
                # and -- failing that -- the caller falls back to the
                # filesystem timestamp instead of naming the file 1904 (#62).
                if creation_date is not None and (
                    not self._is_plausible_capture_time(creation_date)
                    or self._is_quicktime_epoch_sentinel(creation_date)
                ):
                    logger.warning(
                        "Implausible video creation date in %s: %s", file_path, creation_date
                    )
                    creation_date = None

                # Last resort only: reached when the direct lookup found nothing
                # usable. Iterates hachoir's rendered metadata lines and regex-
                # scans them for a date, which materializes every field as text.
                if creation_date is None:
                    for line in metadata.exportPlaintext():
                        line_lower = line.lower()
                        if not any(keyword in line_lower for keyword in ('creation', 'date', 'time')):
                            continue
                        date_match = re.search(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line)
                        if not date_match:
                            continue
                        date_str = date_match.group(1).replace('T', ' ')
                        try:
                            candidate = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
                        except Exception:
                            continue
                        if not self._is_plausible_capture_time(candidate) or self._is_quicktime_epoch_sentinel(candidate):
                            logger.warning(
                                "Implausible video creation date in %s: %s", file_path, candidate
                            )
                            continue
                        creation_date = candidate
                        break

                if creation_date:
                    logger.debug("Extracted video creation date: %s -> %s", file_path, creation_date)
                    return creation_date
                else:
                    logger.warning("No creation date found in video metadata: %s", file_path)
                    self.missing_exif_files.append(file_path)
                    return None

        except Exception as e:
            logger.error("Error extracting video metadata from %s: %s", file_path, e)
            self.missing_exif_files.append(file_path)
            return None

    def _parse_exif_datetime(self, datetime_str, file_path: str) -> Optional[datetime]:
        """
        Parse EXIF datetime string to datetime object.

        Args:
            datetime_str: EXIF datetime value (format: "YYYY:MM:DD HH:MM:SS").
                Declared as ``str`` in the EXIF spec, but a corrupt or hostile
                blob can hand this a non-str value (e.g. ``bytes``, an int) --
                ``datetime.strptime`` raises ``TypeError`` for those, not
                ``ValueError``, which fix-round-1 (issue #24 review, finding
                2) found propagated straight through the metadata-provided
                branch of ``extract_timestamp`` uncaught, failing the file
                instead of falling back to the filesystem timestamp the way
                every other malformed-tag case does.
            file_path: File path for logging

        Returns:
            datetime object if valid, None if invalid
        """
        try:
            # EXIF datetime format: "YYYY:MM:DD HH:MM:SS"
            parsed = datetime.strptime(datetime_str, "%Y:%m:%d %H:%M:%S")
        except (ValueError, TypeError) as e:
            logger.error("Invalid EXIF datetime format in %s: %s - %s", file_path, datetime_str, e)
            self.missing_exif_files.append(file_path)
            return None

        # A well-formed date that is not a plausible capture time (e.g. a
        # crafted year of 9999 or 0001) falls through exactly like a
        # malformed one -- the file still gets archived under a filesystem
        # timestamp instead of naming its own archive directory year (#62).
        if not self._is_plausible_capture_time(parsed):
            logger.warning("Implausible EXIF timestamp in %s: %s", file_path, datetime_str)
            self.missing_exif_files.append(file_path)
            return None

        return parsed

    def get_fallback_timestamp(self, file_path: str) -> datetime:
        """
        Get fallback timestamp from filename patterns or file system metadata.

        Args:
            file_path: Path to file

        Returns:
            datetime object from filename pattern or file creation/modification time
        """
        # Try to extract date from filename first
        filename_timestamp = self._extract_timestamp_from_filename(file_path)
        if filename_timestamp:
            logger.debug("Extracted timestamp from filename: %s -> %s", file_path, filename_timestamp)
            return filename_timestamp

        try:
            # Use file creation time if available (macOS/Windows), otherwise modification time
            stat_info = os.stat(file_path)

            # Try creation time first (macOS: st_birthtime, Windows: st_ctime)
            if hasattr(stat_info, 'st_birthtime') and stat_info.st_birthtime != stat_info.st_ctime:
                timestamp = stat_info.st_birthtime
            else:
                # Fall back to modification time
                timestamp = stat_info.st_mtime

            return datetime.fromtimestamp(timestamp)
        except OSError as e:
            logger.error("Error getting file timestamp for %s: %s", file_path, e)
            # Ultimate fallback: current time
            return datetime.now()

    def _extract_timestamp_from_filename(self, file_path: str) -> Optional[datetime]:
        """
        Try to extract timestamp from filename patterns.

        Args:
            file_path: Path to file

        Returns:
            datetime object if pattern found, None otherwise
        """
        filename = Path(file_path).name

        # Pattern 1: IMG_YYYY-MM-DD-HH-MM-SS or similar. The date/time
        # separator also accepts whitespace (`\s`) alongside `_`/`-`, which
        # covers the standard macOS screenshot/export shape
        # "2011-03-09 18-20-30.jpg.jpeg" (issue #51) where a space, not a
        # dash, joins the date and the time.
        pattern1 = r'(\d{4})[_\-\s](\d{2})[_\-\s](\d{2})[_\-\s](\d{2})[_\-\s](\d{2})[_\-\s](\d{2})'
        match = re.search(pattern1, filename)
        if match:
            try:
                year, month, day, hour, minute, second = map(int, match.groups())
                candidate = datetime(year, month, day, hour, minute, second)
                # The bare \d{4} year field admits 0000-9999, so this pattern
                # can produce the same implausible dates as a crafted EXIF
                # value (#62); bound it the same way and try the next pattern
                # instead of naming a file from it.
                if self._is_plausible_capture_time(candidate):
                    return candidate
                logger.warning(
                    "Implausible timestamp from filename pattern, trying next pattern: %s -> %s",
                    filename, candidate,
                )
            except ValueError:
                pass

        # Pattern 2: YYYYMMDD_HHMMSS
        pattern2 = r'(\d{8})[_-](\d{6})'
        match = re.search(pattern2, filename)
        if match:
            try:
                date_str, time_str = match.groups()
                year = int(date_str[:4])
                month = int(date_str[4:6])
                day = int(date_str[6:8])
                hour = int(time_str[:2])
                minute = int(time_str[2:4])
                second = int(time_str[4:6])
                candidate = datetime(year, month, day, hour, minute, second)
                if self._is_plausible_capture_time(candidate):
                    return candidate
                logger.warning(
                    "Implausible timestamp from filename pattern, trying next pattern: %s -> %s",
                    filename, candidate,
                )
            except ValueError:
                pass

        # Pattern 3: an ambiguous NN-NN-YYYY-HH-MM-SS date, which is DD-MM-YYYY
        # for Facetune ("Facetune_09-02-2024-11-22-33.heic", issue #51) but
        # MM-DD-YYYY for macOS/iOS screen recordings
        # ("ScreenRecording_03-05-2024 09-15-00_1.mp4", issue #70). The two
        # leading two-digit fields cannot be told apart from the digits alone
        # whenever both are <= 12 (04-07 could be day=4/month=7 or
        # month=4/day=7), so the ORDER the two readings are tried in comes from
        # the generator named in the filename -- see `filename_date_order` for
        # why the screen-recording convention is established fact rather than a
        # guess. Only a reading that produces a real, plausible calendar date is
        # accepted, and trying the second reading when the first is invalid is
        # what lets an unrecognized generator still resolve. A filename where
        # NEITHER reading produces a valid date returns None rather than
        # guessing, preserving the fail-closed contract of this whole fallback
        # chain. Whichever reading is used is logged at INFO so a wrong guess on
        # a genuinely ambiguous name is auditable, not silent.
        pattern3 = r'(\d{2})[_\-\s](\d{2})[_\-\s](\d{4})[_\-\s](\d{2})[_\-\s](\d{2})[_\-\s](\d{2})'
        match = re.search(pattern3, filename)
        if match:
            first_field, second_field, year, hour, minute, second = map(int, match.groups())
            day_first_reading = (_DAY_FIRST, first_field, second_field)
            month_first_reading = (_MONTH_FIRST, second_field, first_field)
            if filename_date_order(filename) is _MONTH_FIRST:
                candidate_readings = (month_first_reading, day_first_reading)
            else:
                candidate_readings = (day_first_reading, month_first_reading)
            for reading, day, month in candidate_readings:
                try:
                    parsed = datetime(year, month, day, hour, minute, second)
                except ValueError:
                    continue
                # As with patterns 1 and 2, the bare \d{4} year field admits
                # 0000-9999; an implausible reading is rejected the same way
                # a malformed one is, trying the other reading before giving
                # up on this pattern entirely (#62).
                if not self._is_plausible_capture_time(parsed):
                    logger.warning(
                        "Implausible timestamp from filename using %s interpretation, "
                        "trying next: %s -> %s",
                        reading, filename, parsed,
                    )
                    continue
                # Kept at INFO, not demoted with this module's other per-file
                # success lines (issue #48): this narrates WHICH of two
                # genuinely ambiguous day-first/month-first readings of the
                # filename was chosen (issue #51) -- a wrong choice silently
                # misfiles the photo under the other valid date, and unlike
                # an ordinary successful move/conversion, the archived
                # filename alone does not announce that a *choice* was made
                # between two plausible interpretations. Pinned by an
                # existing test (test_pattern_day_first_logs_chosen_interpretation).
                logger.info(
                    "Extracted timestamp from filename using %s interpretation: %s -> %s",
                    reading, filename, parsed,
                )
                return parsed

        # Pattern 4: IMG_XXXX with iOS patterns (these don't contain dates)
        # Pattern 5: Attachment-1, FullSizeRender, etc. (no dates)

        return None

    def format_timestamp_filename(self, dt: datetime) -> str:
        """
        Format datetime object to filename format: YYYY.MM.DD.HH.MM.SS

        Args:
            dt: datetime object

        Returns:
            Formatted timestamp string
        """
        return dt.strftime("%Y.%m.%d.%H.%M.%S")

    def handle_duplicate_timestamp(self, timestamp: datetime, existing_files: set) -> datetime:
        """
        Handle duplicate timestamps by incrementing seconds.

        Args:
            timestamp: Original timestamp
            existing_files: Set of existing formatted timestamp strings

        Returns:
            Adjusted timestamp that doesn't conflict
        """
        base_format = self.format_timestamp_filename(timestamp)

        if base_format not in existing_files:
            return timestamp

        # Increment seconds until we find a unique timestamp
        adjusted = timestamp
        attempts = 0
        max_attempts = 3600  # Max 1 hour of adjustments

        while attempts < max_attempts:
            # timedelta handles second/minute/hour/day/month/year rollover
            adjusted = adjusted + timedelta(seconds=1)

            formatted = self.format_timestamp_filename(adjusted)
            if formatted not in existing_files:
                logger.debug("Resolved timestamp conflict: %s -> %s", base_format, formatted)
                return adjusted

            attempts += 1

        logger.error("Could not resolve timestamp conflict after %s attempts", max_attempts)
        return adjusted

    def clear_missing_files_log(self):
        """Clear the missing EXIF files log"""
        self.missing_exif_files.clear()
