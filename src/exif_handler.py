"""
EXIF timestamp extraction and handling module for photos and videos
"""

import os
import struct
import logging
from datetime import date, datetime, time, timedelta
from typing import Iterator, Optional, Tuple, TYPE_CHECKING
from pathlib import Path
from PIL import Image
from PIL.ExifTags import TAGS
import pillow_heif
from dateutil import parser as dateutil_parser

# Video metadata extraction
try:
    from hachoir.parser import createParser
    from hachoir.metadata import extractMetadata
    HACHOIR_AVAILABLE = True
except ImportError:
    HACHOIR_AVAILABLE = False

if TYPE_CHECKING:
    # Type-hint only: FileCategorizer.ImageMetadata is not imported at runtime
    # so this module never depends on file_categorizer (which imports THIS
    # module for merge_exif_ifds -- keeping the edge one-directional avoids a
    # circular import, issue #24).
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


def ifd0_tag_names(exif) -> dict:
    """
    Resolve a single IFD's tag ids to a tag-name -> value mapping.

    Despite the name this works on any single IFD-shaped mapping (IFD0 or a
    sub-IFD); it is named for its primary caller, which always passes IFD0.
    Kept separate from :func:`merge_exif_ifds` so a caller that must NOT see
    sub-IFD tags -- see :meth:`FileCategorizer._exif_shows_synthetic_edit`,
    issue #24's fix-round-1 finding 1 below -- has a way to get an IFD0-only
    view without re-implementing the tag-id -> tag-name resolution.

    Args:
        exif: An ``Image.Exif`` (or sub-IFD) mapping of tag id -> raw value.

    Returns:
        Mapping of resolved tag name -> raw tag value, for this IFD only.
    """
    resolved = {}
    for tag_id, value in exif.items():
        resolved.setdefault(TAGS.get(tag_id, str(tag_id)), value)
    return resolved


def merge_exif_ifds(exif, file_path: str = "") -> dict:
    """
    Flatten IFD0 and the Exif sub-IFD of a PIL ``Image.Exif`` into one
    tag-name -> value mapping.

    ``Image.getexif()`` exposes IFD0 only. The preferred ``DateTimeOriginal``
    (0x9003) and ``DateTimeDigitized`` (0x9004) tags live in the Exif sub-IFD
    behind pointer tag ``0x8769``, reachable only via ``Image.Exif.get_ifd()``
    (issue #25). This is the single place that merge happens: used by
    :meth:`ExifHandler._timestamp_candidates` (the standalone open path) and,
    since issue #24, by :class:`FileCategorizer`'s once-per-file metadata read
    -- both used to build this same view from their own separate
    ``Image.open`` of the same file.

    IFD0 values win over sub-IFD values for any shared tag id (setdefault).

    CAUTION: this merged view is for **timestamp** lookups only (issue #25's
    concern). Do NOT use it to look up ``Software`` or any other tag whose
    presence is meant to be scoped to IFD0 -- issue #24's fix-round-1 finding
    1 found that feeding this merged view to the editing-software heuristic
    (:meth:`FileCategorizer._exif_shows_synthetic_edit`) let a ``Software``
    tag written only in the Exif sub-IFD flip an ordinary photo to
    ``GENERATED``, strictly widening issue #8's detection surface beyond what
    the old, IFD0-only code ever matched. That heuristic now takes
    :func:`ifd0_tag_names` for its ``Software`` check and this merged view
    only for the capture-timestamp check, which #25 does intend to span both
    directories.

    Args:
        exif: The ``Image.Exif`` mapping returned by ``Image.getexif()``.
        file_path: Source path, used only for logging context if the sub-IFD
            lookup raises.

    Returns:
        Mapping of resolved tag name -> raw tag value.
    """
    merged = ifd0_tag_names(exif)

    # get_ifd returns {} when the sub-IFD is absent. The guard also covers
    # exif objects that predate the sub-IFD API (e.g. plain-dict test doubles
    # lack get_ifd) and malformed pointers that raise on access.
    try:
        sub_ifd = exif.get_ifd(ExifHandler.EXIF_IFD)
    except (AttributeError, KeyError, OSError, ValueError) as exc:
        logger.debug("No Exif sub-IFD in %s: %s", file_path, exc)
        sub_ifd = {}

    for tag_id, value in sub_ifd.items():
        merged.setdefault(TAGS.get(tag_id, str(tag_id)), value)

    return merged


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

    # Video file extensions that need special handling
    VIDEO_EXTENSIONS = {
        '.mov', '.mp4', '.m4v', '.avi', '.mkv', '.wmv',
        '.flv', '.webm', '.3gp', '.mpg', '.mpeg'
    }

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

        Delegates to the module-level :func:`merge_exif_ifds`, which is
        shared with :class:`FileCategorizer`'s once-per-file metadata read
        (issue #24) so both build the identical tag-name view from a decoded
        EXIF blob rather than each re-implementing the IFD0/sub-IFD merge.

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

        Prefers Apple's ``com.apple.quicktime.creationdate`` key, which records
        the TRUE local wall-clock capture time with its UTC offset, and names
        the file by that local reading (see issue #28). Only when that key is
        absent does it fall back to hachoir's ``mvhd`` creation_time, which is
        UTC; that fallback can therefore be off by the UTC offset and land a
        late-evening capture on the next calendar day. This is a pre-existing
        limitation preserved here, not one this change introduces.

        Args:
            file_path: Path to video file

        Returns:
            datetime object if found, None if missing/invalid
        """
        # Preferred path: Apple's local wall-clock creationdate. Independent of
        # hachoir, so it works even when hachoir cannot be imported.
        local_creation = self._extract_quicktime_creationdate(file_path)
        if local_creation is not None:
            logger.info(
                f"Extracted local video creation date: {file_path} -> {local_creation}"
            )
            return local_creation

        return self._extract_video_timestamp_hachoir(file_path)

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
                f"Unparseable QuickTime creationdate in {file_path}: {value!r}"
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
        with open(file_path, "rb") as handle:
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
                                f"moov atom too large or invalid in {file_path}"
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
                        import re
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
                    logger.info("Extracted video creation date: %s -> %s", file_path, creation_date)
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
            logger.info("Extracted timestamp from filename: %s -> %s", file_path, filename_timestamp)
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

        import re

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

        # Pattern 3: DD-MM-YYYY-HH-MM-SS, e.g. Facetune's
        # "Facetune_09-02-2024-11-22-33.heic" (issue #51). The two leading
        # two-digit fields are ambiguous with MM-DD-YYYY whenever both are
        # <= 12 (04-07 could be day=4/month=7 or month=4/day=7), so both
        # readings are tried -- day-first first, since that is this pattern's
        # documented convention -- and only a reading that produces a real
        # calendar date is accepted. A filename where NEITHER reading (nor
        # the other way around) produces a valid date returns None rather
        # than guessing, preserving the fail-closed contract of this whole
        # fallback chain. Whichever reading is used is logged at INFO so a
        # wrong guess on a genuinely ambiguous name is auditable, not silent.
        pattern3 = r'(\d{2})[_\-\s](\d{2})[_\-\s](\d{4})[_\-\s](\d{2})[_\-\s](\d{2})[_\-\s](\d{2})'
        match = re.search(pattern3, filename)
        if match:
            first_field, second_field, year, hour, minute, second = map(int, match.groups())
            candidate_readings = (
                ('day-first (DD-MM-YYYY)', first_field, second_field),
                ('month-first (MM-DD-YYYY)', second_field, first_field),
            )
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
                logger.info("Resolved timestamp conflict: %s -> %s", base_format, formatted)
                return adjusted

            attempts += 1

        logger.error("Could not resolve timestamp conflict after %s attempts", max_attempts)
        return adjusted

    def get_missing_exif_files(self) -> list:
        """Return list of files that had missing/invalid EXIF data"""
        return self.missing_exif_files.copy()

    def clear_missing_files_log(self):
        """Clear the missing EXIF files log"""
        self.missing_exif_files.clear()