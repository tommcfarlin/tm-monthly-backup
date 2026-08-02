"""
Shared, self-verifying image fixtures for the test suite.

Every EXIF-timestamp test in this project depends on real image bytes whose
timestamp tags are laid out the way a camera actually writes them:

* ``DateTime`` lives in IFD0 (the top-level directory ``Image.getexif()``
  exposes).
* ``DateTimeOriginal`` and ``DateTimeDigitized`` live in the Exif sub-IFD,
  reachable only through the ``0x8769`` pointer via ``Image.Exif.get_ifd()``.

A fixture that accidentally writes ``DateTimeOriginal`` into IFD0 is
structurally unlike every file the tool will ever process and produces false
passes (see issue #25). To make that class of mistake impossible, every
builder here reopens the file it wrote and asserts each tag landed in the
correct directory before returning. A caller therefore cannot silently
construct a broken fixture: a bad round-trip raises :class:`FixtureError` at
build time rather than surfacing as a mysterious test result later.
"""

import os
import struct
from typing import Optional, Tuple

import pillow_heif
from PIL import Image
from PIL.ExifTags import Base

# Register the HEIF opener so Image.open / Image.save handle HEIC/HEIF, mirroring
# how src.heic_converter and src.exif_handler enable HEIF support at import time.
pillow_heif.register_heif_opener()

# Pointer tag to the Exif sub-IFD. DateTimeOriginal (0x9003) and
# DateTimeDigitized (0x9004) live behind this pointer, not in IFD0.
EXIF_IFD = 0x8769

# The Apple metadata key that carries the true local wall-clock capture time.
QUICKTIME_CREATIONDATE_KEY = b"com.apple.quicktime.creationdate"


class FixtureError(RuntimeError):
    """Raised when a fixture fails to round-trip to its expected EXIF layout."""


def make_exif_jpeg(
    path: str,
    *,
    date_time_original: Optional[str] = None,
    date_time: Optional[str] = None,
    date_time_digitized: Optional[str] = None,
    color: str = "green",
    size: Tuple[int, int] = (48, 48),
) -> str:
    """
    Write a real JPEG whose EXIF is laid out the way a camera writes it.

    ``date_time`` (when given) is written into IFD0; ``date_time_original`` and
    ``date_time_digitized`` (when given) are written into the Exif sub-IFD
    (``0x8769``). Timestamp strings use the EXIF format ``"YYYY:MM:DD HH:MM:SS"``
    and are stored verbatim -- deliberately malformed strings are allowed so
    callers can exercise parse-failure paths.

    The file is reopened and its layout verified before returning; a mismatch
    raises :class:`FixtureError`.

    Args:
        path: Destination path for the JPEG.
        date_time_original: Value for DateTimeOriginal (Exif sub-IFD), or None.
        date_time: Value for DateTime (IFD0), or None.
        date_time_digitized: Value for DateTimeDigitized (Exif sub-IFD), or None.
        color: Fill color for the generated image.
        size: (width, height) of the generated image in pixels.

    Returns:
        The path that was written (echoes ``path`` for convenient chaining).
    """
    image = Image.new("RGB", size, color=color)
    exif = image.getexif()

    if date_time is not None:
        exif[Base.DateTime.value] = date_time

    if date_time_original is not None or date_time_digitized is not None:
        sub = exif.get_ifd(EXIF_IFD)
        if date_time_original is not None:
            sub[Base.DateTimeOriginal.value] = date_time_original
        if date_time_digitized is not None:
            sub[Base.DateTimeDigitized.value] = date_time_digitized

    image.save(path, format="JPEG", exif=exif)

    _verify_roundtrip(
        path,
        date_time_original=date_time_original,
        date_time=date_time,
        date_time_digitized=date_time_digitized,
    )
    return path


def make_exif_heic(
    path: str,
    *,
    date_time_original: Optional[str] = None,
    date_time_digitized: Optional[str] = None,
    color: str = "blue",
    size: Tuple[int, int] = (64, 64),
) -> str:
    """
    Write a real HEIC/HEIF file whose EXIF is laid out the way a camera writes it.

    This is the HEIC analogue of :func:`make_exif_jpeg`. ``date_time_original``
    and ``date_time_digitized`` (when given) are written into the Exif sub-IFD
    (``0x8769``), exactly where an iPhone stores them, using the EXIF format
    ``"YYYY:MM:DD HH:MM:SS"``. Encoding uses ``Image.save(..., format="HEIF")``
    via the pillow-heif opener, the round-trip confirmed in issue #25.

    The file is reopened and verified before returning: it must decode as a real
    HEIF image of the requested size, and each requested timestamp tag must land
    in the Exif sub-IFD (and not leak into IFD0). Any mismatch raises
    :class:`FixtureError` at build time so a broken HEIC fixture can never
    silently produce a false pass.

    Args:
        path: Destination path for the HEIC file.
        date_time_original: Value for DateTimeOriginal (Exif sub-IFD), or None.
        date_time_digitized: Value for DateTimeDigitized (Exif sub-IFD), or None.
        color: Fill color for the generated image.
        size: (width, height) of the generated image in pixels.

    Returns:
        The path that was written (echoes ``path`` for convenient chaining).
    """
    image = Image.new("RGB", size, color=color)
    exif = image.getexif()

    if date_time_original is not None or date_time_digitized is not None:
        sub = exif.get_ifd(EXIF_IFD)
        if date_time_original is not None:
            sub[Base.DateTimeOriginal.value] = date_time_original
        if date_time_digitized is not None:
            sub[Base.DateTimeDigitized.value] = date_time_digitized

    image.save(path, format="HEIF", exif=exif)

    _verify_heic_roundtrip(
        path,
        size=size,
        date_time_original=date_time_original,
        date_time_digitized=date_time_digitized,
    )
    return path


def make_no_exif_jpeg(
    path: str,
    *,
    color: str = "blue",
    size: Tuple[int, int] = (48, 48),
) -> str:
    """
    Write a real JPEG that carries no EXIF timestamp data.

    Used to exercise the missing-EXIF / filesystem-fallback path with genuine
    image bytes rather than a text file masquerading as a photo. Verifies the
    written file exposes no timestamp tags in either IFD0 or the Exif sub-IFD.

    Args:
        path: Destination path for the JPEG.
        color: Fill color for the generated image.
        size: (width, height) of the generated image in pixels.

    Returns:
        The path that was written.
    """
    image = Image.new("RGB", size, color=color)
    image.save(path, format="JPEG")

    with Image.open(path) as reopened:
        top = reopened.getexif()
        try:
            sub = top.get_ifd(EXIF_IFD)
        except (AttributeError, KeyError, OSError, ValueError):
            sub = {}

    for tag in (Base.DateTime, Base.DateTimeOriginal, Base.DateTimeDigitized):
        if tag.value in top or tag.value in sub:
            raise FixtureError(
                f"{tag.name} unexpectedly present in a no-EXIF fixture at {path}"
            )
    return path


def make_png_with_text(
    path: str,
    text_chunks: dict,
    *,
    color: str = "green",
    size: Tuple[int, int] = (16, 16),
) -> str:
    """
    Write a real PNG carrying the given ``tEXt`` chunks.

    Used to exercise AI/C2PA provenance detection (issue #8) with genuine PNG
    bytes rather than a mock. Each ``key -> value`` pair is written as a text
    chunk via :class:`PIL.PngImagePlugin.PngInfo`. After writing, the file is
    reopened and every chunk is asserted to have round-tripped into
    ``Image.text`` exactly as given; a mismatch raises :class:`FixtureError` so a
    provenance test can never pass against a fixture whose metadata never landed.

    Args:
        path: Destination path for the PNG.
        text_chunks: Mapping of text-chunk key -> value to embed.
        color: Fill color for the generated image.
        size: (width, height) of the generated image in pixels.

    Returns:
        The path that was written.
    """
    from PIL.PngImagePlugin import PngInfo

    image = Image.new("RGB", size, color=color)
    metadata = PngInfo()
    for key, value in text_chunks.items():
        metadata.add_text(key, value)
    image.save(path, format="PNG", pnginfo=metadata)

    with Image.open(path) as reopened:
        landed = dict(getattr(reopened, "text", {}) or {})
    for key, value in text_chunks.items():
        _require(
            landed.get(key) == value,
            f"PNG text chunk {key!r} round-tripped as {landed.get(key)!r}, "
            f"expected {value!r} for {path}",
        )
    return path


def make_corrupt_jpeg(path: str, *, content: bytes = b"this is not a JPEG") -> str:
    """
    Write a file with a ``.jpg`` name but a body Pillow cannot decode.

    Exercises the open/decode-failure branch of ``extract_timestamp`` with a
    real on-disk file. Verifies that ``Image.open`` genuinely rejects it.

    Args:
        path: Destination path (should end in an image extension).
        content: Raw bytes to write. Defaults to non-image bytes.

    Returns:
        The path that was written.
    """
    with open(path, "wb") as handle:
        handle.write(content)

    try:
        with Image.open(path) as image:
            image.load()
    except Exception:
        return path
    raise FixtureError(f"corrupt fixture at {path} was unexpectedly decodable")


def read_ifds(path: str) -> Tuple[dict, dict]:
    """
    Reopen ``path`` and return ``(ifd0, sub_ifd)`` as raw tag-id -> value dicts.

    A convenience for tests that want to assert directly on the EXIF layout of
    a fixture (e.g. that DateTimeOriginal is absent from IFD0 but present in the
    sub-IFD).

    Args:
        path: Path to an image file.

    Returns:
        Tuple of ``(top_level_getexif_dict, exif_sub_ifd_dict)``.
    """
    with Image.open(path) as reopened:
        top = reopened.getexif()
        try:
            sub = top.get_ifd(EXIF_IFD)
        except (AttributeError, KeyError, OSError, ValueError):
            sub = {}
        return dict(top), dict(sub)


def _verify_roundtrip(
    path: str,
    *,
    date_time_original: Optional[str],
    date_time: Optional[str],
    date_time_digitized: Optional[str],
) -> None:
    """
    Reopen ``path`` and assert every requested tag landed in the right IFD.

    Raises :class:`FixtureError` if any tag is missing, stored a different
    value, or leaked into the wrong directory.
    """
    top, sub = read_ifds(path)

    if date_time is not None:
        _require(
            Base.DateTime.value in top,
            f"DateTime did not land in IFD0 for {path}",
        )
        _require(
            top[Base.DateTime.value] == date_time,
            f"DateTime round-tripped as {top.get(Base.DateTime.value)!r}, "
            f"expected {date_time!r} for {path}",
        )

    for tag, expected in (
        (Base.DateTimeOriginal, date_time_original),
        (Base.DateTimeDigitized, date_time_digitized),
    ):
        if expected is None:
            continue
        _require(
            tag.value in sub,
            f"{tag.name} did not land in the Exif sub-IFD for {path}",
        )
        _require(
            tag.value not in top,
            f"{tag.name} leaked into IFD0 for {path}; fixture would pass "
            f"against broken code",
        )
        _require(
            sub[tag.value] == expected,
            f"{tag.name} round-tripped as {sub.get(tag.value)!r}, "
            f"expected {expected!r} for {path}",
        )


def _verify_heic_roundtrip(
    path: str,
    *,
    size: Tuple[int, int],
    date_time_original: Optional[str],
    date_time_digitized: Optional[str],
) -> None:
    """
    Reopen a HEIC fixture and assert it decoded as HEIF with the expected layout.

    Confirms the file is a genuine, decodable HEIF image of the requested size
    and that each requested timestamp tag landed in the Exif sub-IFD without
    leaking into IFD0. Raises :class:`FixtureError` on any mismatch.
    """
    with Image.open(path) as reopened:
        if reopened.format != "HEIF":
            raise FixtureError(
                f"HEIC fixture at {path} reopened as {reopened.format!r}, "
                f"expected 'HEIF'"
            )
        if reopened.size != size:
            raise FixtureError(
                f"HEIC fixture at {path} reopened as {reopened.size}, "
                f"expected {size}"
            )
        reopened.load()  # force a real decode; a truncated file raises here
        top = reopened.getexif()
        try:
            sub = top.get_ifd(EXIF_IFD)
        except (AttributeError, KeyError, OSError, ValueError):
            sub = {}

    for tag, expected in (
        (Base.DateTimeOriginal, date_time_original),
        (Base.DateTimeDigitized, date_time_digitized),
    ):
        if expected is None:
            continue
        _require(
            tag.value in sub,
            f"{tag.name} did not land in the Exif sub-IFD for {path}",
        )
        _require(
            tag.value not in top,
            f"{tag.name} leaked into IFD0 for {path}; fixture would pass "
            f"against broken code",
        )
        _require(
            sub[tag.value] == expected,
            f"{tag.name} round-tripped as {sub.get(tag.value)!r}, "
            f"expected {expected!r} for {path}",
        )


def _box(box_type: bytes, payload: bytes) -> bytes:
    """Wrap ``payload`` in an ISO base-media box header (32-bit size + type)."""
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def quicktime_creationdate_moov(
    creationdate: str,
    *,
    include_mvhd: bool = True,
    mvhd_creation_1904: int = 0,
    meta_style: str = "mov",
) -> bytes:
    """
    Build a synthetic ``moov`` payload carrying an Apple creationdate key.

    Constructs a structurally faithful ``meta`` atom -- a ``keys`` atom naming
    ``com.apple.quicktime.creationdate`` and an ``ilst`` atom holding its value
    in a ``data`` box -- exactly the layout an iPhone writes. Optionally
    prepends an ``mvhd`` atom whose UTC creation seconds deliberately DIFFER
    from the local creationdate, so a test can prove the local key wins over the
    UTC time.

    Args:
        creationdate: ISO-8601 string to store (e.g. ``2024-06-15T21:33:03-0400``).
        include_mvhd: Whether to include a leading ``mvhd`` atom.
        mvhd_creation_1904: ``mvhd`` creation time in seconds since 1904-01-01 UTC.
        meta_style: ``"mov"`` (QuickTime: children follow the header) or
            ``"iso"`` (MP4: a 4-byte version+flags precedes the children).

    Returns:
        The bytes of a ``moov`` box payload (its children, no ``moov`` header).
    """
    if meta_style not in ("mov", "iso"):
        raise ValueError(f"meta_style must be 'mov' or 'iso', got {meta_style!r}")

    key = QUICKTIME_CREATIONDATE_KEY
    key_entry = struct.pack(">I", 8 + len(key)) + b"mdta" + key
    keys_payload = struct.pack(">I", 0) + struct.pack(">I", 1) + key_entry
    keys = _box(b"keys", keys_payload)

    # data box: 4-byte type indicator (1 == UTF-8), 4-byte locale, then payload.
    data_payload = struct.pack(">I", 1) + struct.pack(">I", 0) + creationdate.encode("utf-8")
    data = _box(b"data", data_payload)
    # ilst item: box whose type is the 4-byte big-endian key index (1).
    item = struct.pack(">I", 8 + len(data)) + struct.pack(">I", 1) + data
    ilst = _box(b"ilst", item)

    meta_children = keys + ilst
    if meta_style == "iso":
        meta_children = struct.pack(">I", 0) + meta_children  # version + flags
    meta = _box(b"meta", meta_children)

    moov_payload = b""
    if include_mvhd:
        # mvhd v0 (100-byte body): version+flags(4), creation(4), modification(4),
        # timescale(4), duration(4), then rate/volume/matrix/... The timescale is
        # deliberately non-zero and duration non-empty so hachoir can parse this
        # atom (a zero timescale makes hachoir divide by zero and skip it).
        mvhd_payload = (
            struct.pack(">I", 0)                     # version + flags
            + struct.pack(">I", mvhd_creation_1904)  # creation_time (UTC, 1904)
            + struct.pack(">I", mvhd_creation_1904)  # modification_time
            + struct.pack(">I", 1000)                # timescale (units per second)
            + struct.pack(">I", 1000)                # duration (== 1 second)
            + b"\x00" * 80                           # rate/volume/matrix/next_id
        )
        moov_payload += _box(b"mvhd", mvhd_payload)
    moov_payload += meta

    _verify_creationdate_moov(moov_payload, creationdate)
    return moov_payload


def write_quicktime_mov(
    path: str,
    creationdate: str,
    *,
    include_mvhd: bool = True,
    mvhd_creation_1904: int = 0,
    meta_style: str = "mov",
) -> str:
    """
    Write a minimal ``.mov`` file whose only real content is the Apple metadata.

    The file is an ``ftyp`` box followed by a ``moov`` box built by
    :func:`quicktime_creationdate_moov`. It carries no media samples -- just
    enough box structure for the box scanner under test. After writing, the file
    is reopened and re-scanned; a mismatch raises :class:`FixtureError` so a
    broken fixture can never yield a false pass.

    Args:
        path: Destination path (should end in ``.mov`` or ``.mp4``).
        creationdate: ISO-8601 creationdate string to embed.
        include_mvhd: Whether to include a ``mvhd`` atom with a UTC time.
        mvhd_creation_1904: ``mvhd`` creation seconds since 1904-01-01 UTC.
        meta_style: ``"mov"`` or ``"iso"`` ``meta`` layout.

    Returns:
        The path that was written.
    """
    moov_payload = quicktime_creationdate_moov(
        creationdate,
        include_mvhd=include_mvhd,
        mvhd_creation_1904=mvhd_creation_1904,
        meta_style=meta_style,
    )
    ftyp = _box(b"ftyp", b"qt  " + struct.pack(">I", 512) + b"qt  ")
    with open(path, "wb") as handle:
        handle.write(ftyp + _box(b"moov", moov_payload))

    # Confirm the value survives to disk (the in-memory scan already ran inside
    # quicktime_creationdate_moov); the scanner is exercised end-to-end by the
    # tests that read this file back through ExifHandler.
    with open(path, "rb") as handle:
        raw = handle.read()
    _require(
        creationdate.encode("utf-8") in raw,
        f"creationdate {creationdate!r} not present in written file {path}",
    )
    return path


def _verify_creationdate_moov(moov_payload: bytes, expected: str) -> None:
    """Re-scan a built ``moov`` payload and assert the creationdate round-trips."""
    from src.exif_handler import find_quicktime_creationdate

    found = find_quicktime_creationdate(moov_payload)
    _require(
        found == expected,
        f"built moov scanned back as {found!r}, expected {expected!r}",
    )


def _require(condition: bool, message: str) -> None:
    """Raise :class:`FixtureError` with ``message`` unless ``condition`` holds."""
    if not condition:
        raise FixtureError(message)
