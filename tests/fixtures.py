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


def _require(condition: bool, message: str) -> None:
    """Raise :class:`FixtureError` with ``message`` unless ``condition`` holds."""
    if not condition:
        raise FixtureError(message)
