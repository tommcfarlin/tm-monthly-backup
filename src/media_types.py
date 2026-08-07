"""
Shared media-type extension constants and EXIF-IFD helpers (issues #43, #47).

``VIDEO_EXTENSIONS`` used to be defined identically -- and independently --
in both ``file_categorizer.py`` and ``exif_handler.py``: two copies with no
link between them, so adding a format to one and forgetting the other would
silently desynchronize how a file is filed (``FileCategorizer``) from how it
is read for a timestamp (``ExifHandler``). ``HEIC_EXTENSIONS`` similarly
existed only as an inline ``list`` literal inside ``HeicConverter.
is_heic_file``, with no link to the ``.heic``/``.heif`` members of
``FileCategorizer.PHOTO_EXTENSIONS``. This module gives each set exactly one
home so both consumers read the same data.

``ifd0_tag_names`` and ``merge_exif_ifds`` moved here from ``exif_handler.py``
(issue #47). ``file_categorizer.py`` used to import those two helpers from
``exif_handler`` at module scope (added by issue #24); since
``file_categorizer`` was otherwise stdlib-only, that one import dragged in
Pillow, pillow-heif (which registers the HEIF opener as an import side
effect), python-dateutil, and -- because ``exif_handler`` imported hachoir
at its own module scope -- hachoir too, regardless of whether the batch
being categorized contained a single video. Deferring hachoir's import
inside ``exif_handler`` (the primary fix for #47) does not by itself change
that: categorizing a batch would still eagerly pay for pillow-heif and
dateutil it does not need. Routing the two helpers through this module
instead breaks that edge entirely -- ``file_categorizer.py`` no longer
imports ``exif_handler`` at all -- while keeping ``exif_handler.py`` as the
single caller that actually uses them for its own timestamp extraction.

This module is still leaf-shaped in the sense that matters: it imports
nothing from ``file_categorizer`` or ``exif_handler``, so it cannot
reintroduce the circular import issue #24 avoided. It is no longer
stdlib-only -- ``ifd0_tag_names``/``merge_exif_ifds`` need
``PIL.ExifTags.TAGS`` to resolve a tag id to its name -- but that import
alone is a few milliseconds (it does not pull in ``PIL.Image``'s C
extension, pillow-heif, dateutil, or hachoir), so the video/HEIC extension
constants below stay cheap to reach even for a module that otherwise never
touches Pillow. A neutral module with no dependents among the domain
modules is also what issue #67 separately wants for ``Settings``, for the
same circular-import reason, so this one module is positioned to serve both.
"""

import logging

from PIL.ExifTags import TAGS

logger = logging.getLogger(__name__)

# Video file extensions. Read by FileCategorizer to route a file to
# backup/videos/ and by ExifHandler to decide whether a file's timestamp is
# extracted via the hachoir video path rather than Pillow/EXIF.
VIDEO_EXTENSIONS = {
    '.mov', '.mp4', '.m4v', '.avi', '.mkv', '.wmv',
    '.flv', '.webm', '.3gp', '.mpg', '.mpeg'
}

# HEIC/HEIF extensions. Read by HeicConverter.is_heic_file to decide whether
# a file needs HEIC->JPEG conversion before it can be filed and to gate the
# process-pool worker path (issues #7, #42).
HEIC_EXTENSIONS = {'.heic', '.heif'}

# Pointer tag (PIL.ExifTags.IFD.Exif) to the Exif sub-IFD. The preferred
# timestamp tags DateTimeOriginal (0x9003) and DateTimeDigitized (0x9004)
# live behind this pointer, NOT in IFD0, so Image.getexif() does not expose
# them at the top level. They are only reachable via getexif().get_ifd().
# Formerly ExifHandler.EXIF_IFD; moved here alongside merge_exif_ifds (#47),
# which is this constant's only reader. ExifHandler.EXIF_IFD itself is left
# in place as a documented, independent public constant of the same value.
_EXIF_SUB_IFD_TAG = 0x8769


def ifd0_tag_names(exif) -> dict:
    """
    Resolve a single IFD's tag ids to a tag-name -> value mapping.

    Despite the name this works on any single IFD-shaped mapping (IFD0 or a
    sub-IFD); it is named for its primary caller, which always passes IFD0.
    Kept separate from :func:`merge_exif_ifds` so a caller that must NOT see
    sub-IFD tags -- see :meth:`FileCategorizer._exif_shows_synthetic_edit`,
    issue #24's fix-round-1 finding 1 -- has a way to get an IFD0-only view
    without re-implementing the tag-id -> tag-name resolution.

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
        sub_ifd = exif.get_ifd(_EXIF_SUB_IFD_TAG)
    except (AttributeError, KeyError, OSError, ValueError) as exc:
        logger.debug("No Exif sub-IFD in %s: %s", file_path, exc)
        sub_ifd = {}

    for tag_id, value in sub_ifd.items():
        merged.setdefault(TAGS.get(tag_id, str(tag_id)), value)

    return merged
