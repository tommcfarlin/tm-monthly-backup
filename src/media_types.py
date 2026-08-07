"""
Shared, stdlib-only media-type extension constants (issue #43).

``VIDEO_EXTENSIONS`` used to be defined identically -- and independently --
in both ``file_categorizer.py`` and ``exif_handler.py``: two copies with no
link between them, so adding a format to one and forgetting the other would
silently desynchronize how a file is filed (``FileCategorizer``) from how it
is read for a timestamp (``ExifHandler``). ``HEIC_EXTENSIONS`` similarly
existed only as an inline ``list`` literal inside ``HeicConverter.
is_heic_file``, with no link to the ``.heic``/``.heif`` members of
``FileCategorizer.PHOTO_EXTENSIONS``. This module gives each set exactly one
home so both consumers read the same data.

This module is deliberately a *leaf*: it imports nothing from
``file_categorizer`` or ``exif_handler``, and holds no logic, only constants.
That matters for two reasons beyond this issue. First, ``file_categorizer.py``
currently imports ``exif_handler`` at module scope for unrelated helpers
(``ifd0_tag_names``, ``merge_exif_ifds``), which drags Pillow, pillow-heif
(registering the HEIF opener as an import side effect), hachoir, and
dateutil into what is otherwise a stdlib-only module; routing the shared
video/HEIC extension sets through a third, import-light module avoids
deepening that coupling in either direction, which is the setup issue #47
(deferring the hachoir import until a video is actually parsed) needs.
Second, a neutral module with no dependents among the domain modules is
exactly what issue #67 separately asks for to break a different circular-
import shape, so this one module is positioned to serve both.
"""

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
