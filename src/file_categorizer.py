"""
File categorization module for organizing files by type
"""

import os
import re
import uuid
import logging
from pathlib import Path
from typing import Dict, List
from enum import Enum

logger = logging.getLogger(__name__)


class FileCategory(Enum):
    """File category enumeration"""
    PHOTO = "photos"
    VIDEO = "videos"
    SCREENSHOT = "screenshots"
    GENERATED = "generated"  # AI-generated or heavily edited content
    UNKNOWN = "unknown"
    SIDECAR = "sidecar"


class FileCategorizer:
    """Categorizes files by type based on extension and content analysis"""

    # File extension mappings
    PHOTO_EXTENSIONS = {
        '.jpg', '.jpeg', '.png', '.gif', '.heic', '.heif',
        '.tiff', '.tif', '.bmp', '.webp', '.dng', '.raw',
        '.cr2', '.nef', '.arw', '.orf', '.rw2'
    }

    VIDEO_EXTENSIONS = {
        '.mov', '.mp4', '.m4v', '.avi', '.mkv', '.wmv',
        '.flv', '.webm', '.3gp', '.mpg', '.mpeg'
    }

    SCREENSHOT_EXTENSIONS = {
        '.png'  # Screenshots are typically PNG on iOS/macOS
    }

    SIDECAR_EXTENSIONS = {
        '.aae'  # Apple's sidecar files
    }

    # Pointer tag to the Exif sub-IFD. DateTimeOriginal (0x9003) and
    # DateTimeDigitized (0x9004) live behind this pointer, not in IFD0, and are
    # invisible to Image.getexif() at the top level (issue #25).
    EXIF_IFD = 0x8769

    # PNG text-chunk KEYS whose mere presence is itself provenance. C2PA writes
    # its manifest under a 'c2pa' key; Stable Diffusion / AUTOMATIC1111 write the
    # full generation settings under a 'parameters' key. Neither key appears in
    # an ordinary photograph, so matching the key -- rather than scanning free
    # description text -- is both precise and immune to caption bleed (issue #8).
    GENERATED_TEXT_KEYS = frozenset({'c2pa', 'parameters'})

    # High-signal AI tool/format tokens, matched at WORD BOUNDARIES against text
    # values (never as bare substrings). The former bare 'ai' token is dropped
    # entirely: as a substring it matched inside chair, trail, portrait, detail,
    # rain and Spain, routing ordinary photos into backup/generated/ (issue #8).
    # Each token below names a specific product, model, or provenance format:
    #   chatgpt / openai / gpt-4 / gpt-4o -- OpenAI image generation signatures
    #   dall-e / dall·e                   -- OpenAI DALL-E (hyphen or middle dot)
    #   midjourney                        -- Midjourney
    #   stable diffusion                  -- Stable Diffusion
    #   firefly                           -- Adobe Firefly
    #   c2pa                              -- Coalition for Content Provenance
    # gpt-4o precedes gpt-4 so the trailing 'o' is consumed rather than left to
    # break the word boundary. Word boundaries keep these from matching inside
    # longer words; the one common English collision (firefly the insect) is a
    # whole-word, low-frequency risk, unlike the substring bleed 'ai' caused.
    AI_MARKER_PATTERN = re.compile(
        r'\b(?:chatgpt|openai|gpt-4o|gpt-4|dall[-·]?e|midjourney|'
        r'stable diffusion|firefly|c2pa)\b',
        re.IGNORECASE,
    )

    # Editing-software signatures in the EXIF Software tag. Presence alone is not
    # enough to flag content as generated; see _exif_shows_synthetic_edit.
    EDITING_SOFTWARE = (
        'snapseed', 'photoshop', 'lightroom', 'gimp', 'canva',
    )

    def __init__(self):
        self.categorized_files = {
            FileCategory.PHOTO: [],
            FileCategory.VIDEO: [],
            FileCategory.SCREENSHOT: [],
            FileCategory.GENERATED: [],
            FileCategory.UNKNOWN: [],
            FileCategory.SIDECAR: []
        }

        # Convert to lowercase for case-insensitive matching
        self.photo_exts = {ext.lower() for ext in self.PHOTO_EXTENSIONS}
        self.video_exts = {ext.lower() for ext in self.VIDEO_EXTENSIONS}
        self.screenshot_exts = {ext.lower() for ext in self.SCREENSHOT_EXTENSIONS}
        self.sidecar_exts = {ext.lower() for ext in self.SIDECAR_EXTENSIONS}

    def categorize_file(self, file_path: str) -> FileCategory:
        """
        Categorize a single file based on extension and filename patterns.

        Args:
            file_path: Path to file

        Returns:
            FileCategory enum value
        """
        file_path_obj = Path(file_path)
        ext = file_path_obj.suffix.lower()
        filename = file_path_obj.name.lower()

        # Check for sidecar files first
        if ext in self.sidecar_exts:
            return FileCategory.SIDECAR

        # Check for screenshots (PNG files with screenshot patterns)
        if ext in self.screenshot_exts:
            # Additional heuristics for screenshot detection
            if self._is_likely_screenshot(filename):
                return FileCategory.SCREENSHOT
            # Check if it's AI-generated or heavily edited content
            if self._is_generated_content(file_path):
                return FileCategory.GENERATED
            # If PNG but not clearly a screenshot, treat as photo
            return FileCategory.PHOTO

        # Check for photos
        if ext in self.photo_exts:
            # Check if it's AI-generated or heavily edited content
            if self._is_generated_content(file_path):
                return FileCategory.GENERATED
            return FileCategory.PHOTO

        # Check for videos
        if ext in self.video_exts:
            return FileCategory.VIDEO

        # Unknown file type
        logger.warning("Unknown file type: %s", file_path)
        return FileCategory.UNKNOWN

    def _is_likely_screenshot(self, filename: str) -> bool:
        """
        Determine if a file is likely a screenshot based on filename patterns.

        Args:
            filename: Lowercase filename

        Returns:
            True if filename suggests it's a screenshot
        """
        screenshot_patterns = [
            'screenshot',
            'screen shot',
            'img_',  # iOS screenshot pattern
            'simulator screen shot',  # iOS Simulator
            'screen recording',
            'img_3',  # iOS screenshot pattern (IMG_3XXX)
            'screen_',
            'capture',
        ]

        # iOS screenshots often have specific patterns
        # IMG_XXXX.PNG where XXXX is 4+ digits starting with 3
        if filename.startswith('img_3') and filename.endswith('.png'):
            return True

        return any(pattern in filename for pattern in screenshot_patterns)

    def _is_generated_content(self, file_path: str) -> bool:
        """
        Detect AI-generated or heavily edited content using pure Python.

        Detection is deliberately precise rather than broad (issue #8): PNG
        provenance is matched against identifiable text-chunk keys and
        word-boundary tool markers, EXIF editing software is only treated as
        generated when a genuine capture timestamp is absent, and UUID-style
        stems are validated by parsing rather than by counting characters.

        Args:
            file_path: Path to file

        Returns:
            True if file appears to be AI-generated or heavily edited
        """
        try:
            from PIL import Image

            with Image.open(file_path) as img:
                # Check for C2PA/AI provenance in PNG text chunks.
                if file_path.lower().endswith('.png'):
                    if self._png_text_has_ai_provenance(img):
                        logger.info("Detected AI-generated content: %s", file_path)
                        return True

                # Editing software present with no genuine capture timestamp.
                if self._exif_shows_synthetic_edit(img):
                    logger.info("Detected heavily edited content: %s", file_path)
                    return True

                # UUID-style stems are a common convention for generated output.
                if self._has_uuid_stem(file_path):
                    logger.info("Detected UUID filename (likely generated): %s", file_path)
                    return True

        except Exception as e:
            logger.debug("Error checking generated content for %s: %s", file_path, e)

        return False

    def _png_text_has_ai_provenance(self, img) -> bool:
        """
        Report whether a PNG's text chunks carry genuine AI/C2PA provenance.

        A chunk is provenance if its KEY is a known generator key
        (:attr:`GENERATED_TEXT_KEYS`) or if its VALUE contains a high-signal
        tool marker at a word boundary (:attr:`AI_MARKER_PATTERN`). Matching the
        key first is the reliable path -- C2PA and generation tools write to
        identifiable keys -- while the value match is confined to whole-word
        product names so ordinary caption text can no longer trip it (issue #8).

        Reads ``img.info`` rather than ``img.text`` (issue #44). Pillow's
        ``PngImageFile.text`` property calls ``self.load()`` before returning,
        because tEXt/iTXt chunks are legally allowed to follow IDAT and Pillow
        will not report a partial answer -- so merely probing ``.text`` forces
        a full pixel decode of the whole image, at the same cost as an explicit
        ``load()``, purely to read metadata. ``img.info`` is a plain dict
        populated while ``Image.open()`` parses the chunk stream and already
        holds every chunk that precedes IDAT, which is where C2PA manifests and
        generator ``Software``/``parameters`` chunks are actually written by
        every mainstream tool; reading it costs nothing extra because
        ``Image.open()`` performed that parse regardless. The trade-off is a
        chunk written strictly after IDAT would be invisible here -- no
        mainstream generator does that, so detection is unaffected in practice.

        Unlike ``img.text``, ``img.info`` also carries every *non*-text PNG
        ancillary chunk Pillow parses before IDAT -- ``icc_profile``
        (``bytes``), raw ``exif`` (``bytes``), ``transparency``, ``dpi``,
        ``gamma``, ``aspect``, and others. Only entries whose value is a
        ``str`` are scanned below (``PIL.PngImagePlugin.iTXt`` is itself a
        ``str`` subclass, so unicode iTXt values are included too); this
        reconstructs exactly ``img.text``'s value-space without decoding, so
        this is still a how-we-read change, not a what-we-match change --
        a binary chunk like an ICC profile can never widen what gets matched,
        the way scanning ``str(value)`` over every ``info`` entry would.

        Args:
            img: An open :class:`PIL.Image.Image`.

        Returns:
            True if any text chunk indicates AI-generated provenance.
        """
        png_info = getattr(img, 'info', None)
        if not png_info:
            return False

        for key, value in png_info.items():
            if str(key).lower() in self.GENERATED_TEXT_KEYS:
                return True
            if isinstance(value, str) and self.AI_MARKER_PATTERN.search(value):
                return True

        return False

    def _exif_shows_synthetic_edit(self, img) -> bool:
        """
        Report whether EXIF signals a synthetic edit (software, no capture time).

        Editing software alone does not condemn an image: a real photo retouched
        in Lightroom keeps its ``DateTimeOriginal``. The signal is editing
        software *combined with* the absence of any genuine capture timestamp,
        which fits a graphic composed in software rather than captured. Since
        issue #25, ``DateTimeOriginal`` / ``DateTimeDigitized`` are read from the
        Exif sub-IFD as well as IFD0; reading only IFD0 (as this branch used to)
        never found them, so the AND condition silently collapsed into "any
        edited image", misfiling genuinely edited photos. Reading the sub-IFD
        here restores the intended behavior.

        Args:
            img: An open :class:`PIL.Image.Image`.

        Returns:
            True if editing software is present and no capture timestamp exists.
        """
        exif = img.getexif()
        if not exif:
            return False

        from PIL.ExifTags import TAGS

        has_editing_software = any(
            TAGS.get(tag_id, str(tag_id)) == 'Software'
            and any(editor in str(value).lower() for editor in self.EDITING_SOFTWARE)
            for tag_id, value in exif.items()
        )
        if not has_editing_software:
            return False

        return not self._has_original_timestamp(exif)

    def _has_original_timestamp(self, exif) -> bool:
        """
        Report whether a genuine capture timestamp exists in IFD0 or the sub-IFD.

        ``DateTimeOriginal`` (0x9003) and ``DateTimeDigitized`` (0x9004) live in
        the Exif sub-IFD behind pointer tag ``0x8769``, which ``Image.getexif()``
        does not expose at the top level (issue #25). Both directories are
        checked so a real photo's capture time is actually found.

        Args:
            exif: The :class:`PIL.Image.Exif` object from ``Image.getexif()``.

        Returns:
            True if an original/digitized capture timestamp is present.
        """
        from PIL.ExifTags import TAGS

        capture_tags = {'DateTimeOriginal', 'DateTimeDigitized'}

        if any(TAGS.get(tag_id, str(tag_id)) in capture_tags for tag_id in exif):
            return True

        # get_ifd returns {} when the sub-IFD is absent; the guard also covers
        # exif doubles lacking the API and malformed pointers that raise.
        try:
            sub_ifd = exif.get_ifd(self.EXIF_IFD)
        except (AttributeError, KeyError, OSError, ValueError):
            sub_ifd = {}

        return any(TAGS.get(tag_id, str(tag_id)) in capture_tags for tag_id in sub_ifd)

    @staticmethod
    def _has_uuid_stem(file_path: str) -> bool:
        """
        Report whether a file's stem is a valid UUID (a common generator name).

        The stem is parsed with :class:`uuid.UUID`; a :class:`ValueError` means
        it is not a UUID. This replaces the previous shape-only heuristic
        (``len == 36 and four hyphens``), which accepted any 36-character string
        with four hyphens -- validating neither the hex digits nor the segment
        lengths (issue #8).

        Args:
            file_path: Path whose stem is tested.

        Returns:
            True if the stem parses as a UUID.
        """
        stem = Path(file_path).stem
        try:
            uuid.UUID(stem)
        except ValueError:
            return False
        return True

    def batch_categorize(self, file_paths: List[str]) -> Dict[FileCategory, List[str]]:
        """
        Categorize multiple files.

        Args:
            file_paths: List of file paths to categorize

        Returns:
            A dictionary mapping categories to lists of file paths. This is an
            independent snapshot: neither the returned dict nor any of its
            list values alias ``self.categorized_files``, so a subsequent call
            to ``batch_categorize`` (which clears and refills the internal
            lists) or a caller mutating the returned lists cannot affect the
            other (issue #37). ``dict.copy()`` alone is insufficient here --
            it is shallow, so its values would still be the same list objects
            this method clears on its next invocation.
        """
        # Clear previous categorization
        for category in self.categorized_files:
            self.categorized_files[category].clear()

        for file_path in file_paths:
            if not os.path.exists(file_path):
                logger.warning("File not found: %s", file_path)
                continue

            category = self.categorize_file(file_path)
            self.categorized_files[category].append(file_path)

        return {
            category: files.copy()
            for category, files in self.categorized_files.items()
        }

    def get_files_by_category(self, category: FileCategory) -> List[str]:
        """
        Get files for a specific category.

        Args:
            category: FileCategory to retrieve

        Returns:
            List of file paths for the category
        """
        return self.categorized_files[category].copy()

    def get_sidecar_files(self) -> List[str]:
        """
        Get list of sidecar files that should be deleted.

        Returns:
            List of sidecar file paths
        """
        return self.get_files_by_category(FileCategory.SIDECAR)

    def get_processable_files(self) -> Dict[FileCategory, List[str]]:
        """
        Get files that should be processed (every category except sidecar).

        Sidecar files are deleted rather than filed, so they are the only
        category excluded here. Building the mapping by iterating
        :class:`FileCategory` -- rather than listing categories by hand --
        means a newly added category is processed automatically and can never
        be silently dropped the way ``UNKNOWN`` was: it was categorized,
        counted, and rendered against ``backup/unknown/`` yet omitted from this
        hand-maintained list, so unrecognized files were left in ``export/``
        while the destination sat empty (issue #29).

        Returns:
            Dictionary of processable files by category.
        """
        return {
            category: self.get_files_by_category(category)
            for category in FileCategory
            if category is not FileCategory.SIDECAR
        }

    def get_target_directory(self, category: FileCategory, base_backup_dir: str) -> str:
        """
        Get target directory path for a file category.

        Args:
            category: FileCategory
            base_backup_dir: Base backup directory path

        Returns:
            Full path to target directory
        """
        if category == FileCategory.PHOTO:
            return os.path.join(base_backup_dir, "photos")
        elif category == FileCategory.VIDEO:
            return os.path.join(base_backup_dir, "videos")
        elif category == FileCategory.SCREENSHOT:
            return os.path.join(base_backup_dir, "screenshots")
        elif category == FileCategory.GENERATED:
            return os.path.join(base_backup_dir, "generated")
        elif category == FileCategory.UNKNOWN:
            return os.path.join(base_backup_dir, "unknown")
        else:
            raise ValueError(f"No target directory defined for category: {category}")

    def ensure_target_directories(self, base_backup_dir: str) -> List[str]:
        """
        Create target directories for the recognized categories.

        ``UNKNOWN`` is deliberately excluded: ``backup/unknown/`` must exist
        only once an unrecognized file is actually routed into it, never as an
        empty phantom that implies handling which did not occur (issue #29). It
        is created lazily, at the moment a file lands there, by
        :meth:`FileProcessor._process_unknown_file`.

        Args:
            base_backup_dir: Base backup directory path

        Returns:
            List of created directory paths
        """
        directories = []
        for category in [FileCategory.PHOTO, FileCategory.VIDEO, FileCategory.SCREENSHOT, FileCategory.GENERATED]:
            target_dir = self.get_target_directory(category, base_backup_dir)
            os.makedirs(target_dir, exist_ok=True)
            directories.append(target_dir)

        return directories

    def get_categorization_stats(self) -> Dict[str, int]:
        """
        Get statistics about file categorization.

        Returns:
            Dictionary with categorization counts
        """
        return {
            'photos': len(self.categorized_files[FileCategory.PHOTO]),
            'videos': len(self.categorized_files[FileCategory.VIDEO]),
            'screenshots': len(self.categorized_files[FileCategory.SCREENSHOT]),
            'generated': len(self.categorized_files[FileCategory.GENERATED]),
            'unknown': len(self.categorized_files[FileCategory.UNKNOWN]),
            'sidecar': len(self.categorized_files[FileCategory.SIDECAR]),
            'total': sum(len(files) for files in self.categorized_files.values())
        }

    def get_file_summary(self) -> str:
        """
        Get human-readable summary of categorized files.

        Returns:
            Formatted string summary
        """
        stats = self.get_categorization_stats()

        summary_lines = [
            f"File Categorization Summary:",
            f"  Photos: {stats['photos']} files",
            f"  Videos: {stats['videos']} files",
            f"  Screenshots: {stats['screenshots']} files",
            f"  Unknown: {stats['unknown']} files",
            f"  Sidecar (to delete): {stats['sidecar']} files",
            f"  Total: {stats['total']} files"
        ]

        return "\n".join(summary_lines)

    def clear_categorization(self):
        """Clear all categorized files"""
        for category in self.categorized_files:
            self.categorized_files[category].clear()