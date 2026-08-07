"""
File categorization module for organizing files by type
"""

import os
import re
import uuid
import logging
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from enum import Enum

from .media_types import (
    VIDEO_EXTENSIONS as _SHARED_VIDEO_EXTENSIONS,
    ifd0_tag_names,
    merge_exif_ifds,
)

logger = logging.getLogger(__name__)


class FileCategory(Enum):
    """File category enumeration"""
    PHOTO = "photos"
    VIDEO = "videos"
    SCREENSHOT = "screenshots"
    GENERATED = "generated"  # AI-generated or heavily edited content
    UNKNOWN = "unknown"
    SIDECAR = "sidecar"


class ImageMetadata(NamedTuple):
    """
    Per-file image metadata, read once via a single ``Image.open`` (issue #24).

    ``FileCategorizer._is_generated_content`` and
    ``ExifHandler.extract_timestamp`` used to each open the same file
    independently within a single processing pass -- doubling Pillow's open
    cost per image (worst for HEIC, where every open is a full libheif
    decode). This record is built once, while :meth:`FileCategorizer.
    categorize_file` decides the file's category, cached on the categorizer
    for the rest of the run, and handed forward to ``extract_timestamp`` so
    neither has to reopen the file.

    Attributes:
        exif: IFD0 and the Exif sub-IFD merged into one tag-name -> value
            mapping (see ``media_types.merge_exif_ifds``).
        png_info: The ``str``-valued entries of the ``Image.info`` dict,
            captured at open time and only for ``.png`` files -- the PNG
            text/provenance chunks written before IDAT (issue #44). Empty for
            every other extension. Pillow's binary ancillary chunks
            (``icc_profile``, raw ``exif``, ``dpi``, ``gamma``) are filtered
            out at capture, so they can never reach the marker regex and can
            never widen what issue #8's precision rules match.
        category: The :class:`FileCategory` this file resolved to.
    """
    exif: Dict[str, Any]
    png_info: Dict[str, Any]
    category: FileCategory


class FileCategorizer:
    """Categorizes files by type based on extension and content analysis"""

    # File extension mappings
    PHOTO_EXTENSIONS = {
        '.jpg', '.jpeg', '.png', '.gif', '.heic', '.heif',
        '.tiff', '.tif', '.bmp', '.webp', '.dng', '.raw',
        '.cr2', '.nef', '.arw', '.orf', '.rw2'
    }

    # Single shared definition, imported from media_types.py (issue #43) so
    # this set can never drift from the copy ExifHandler reads.
    VIDEO_EXTENSIONS = _SHARED_VIDEO_EXTENSIONS

    SCREENSHOT_EXTENSIONS = {
        '.png'  # Screenshots are typically PNG on iOS/macOS
    }

    SIDECAR_EXTENSIONS = {
        '.aae'  # Apple's sidecar files
    }

    # Canonical output spelling for extensions that have more than one common
    # alias (issue #55). This module already treats both spellings of each
    # pair as equivalent for categorization -- both live in ``PHOTO_EXTENSIONS``
    # or ``VIDEO_EXTENSIONS`` above -- and ``HeicConverter`` always writes
    # lowercase ``.jpg`` for its converted output, so the codebase has already
    # decided lowercase-and-one-spelling is canonical everywhere except the one
    # place a human actually sees it: the filename ``FileProcessor`` composes
    # for ``backup/``. This map is that decision made explicit and centralized,
    # rather than inlined in ``FileProcessor._process_single_file``, so every
    # aliased pair collapses to a single on-disk spelling:
    #   .jpeg -> .jpg   (matches the spelling HeicConverter already writes)
    #   .tiff -> .tif   (same short-form convention as .jpg, for consistency)
    #   .mpeg -> .mpg   (same container/codec as .mpg, same short-form pattern;
    #                    both live in VIDEO_EXTENSIONS as the same format, so
    #                    leaving this pair unmapped would leave exactly the
    #                    two-spellings-per-format defect this issue exists to
    #                    remove, just in videos/ instead of photos/)
    # Every other extension is intentionally absent and passes through
    # unchanged apart from lowercasing: ``.heif`` is a distinct format from
    # ``.heic`` (a differently-boxed HEIF still, but not what this tool's HEIC
    # converter produces) and must never be folded into it, and the raw formats
    # (``.cr2``, ``.nef``, ``.arw``, ``.orf``, ``.rw2``) are each a genuinely
    # distinct format with no common alias to collapse.
    EXTENSION_ALIASES = {
        '.jpeg': '.jpg',
        '.tiff': '.tif',
        '.mpeg': '.mpg',
    }

    @classmethod
    def normalize_extension(cls, extension: str) -> str:
        """
        Lowercase an output extension and collapse it to its canonical alias.

        Used by :class:`FileProcessor` when composing the final on-disk
        filename for a photo/screenshot/generated/video file (issue #55), so
        ``backup/photos/`` ends up with one spelling per format instead of the
        source file's raw, arbitrarily-cased suffix (``IMG_1.JPG`` next to
        ``IMG_2.jpg`` next to a HEIC-converted ``.jpg``). Lowercasing is
        unconditional; the alias collapse is limited to :attr:`EXTENSION_ALIASES`
        so formats with no common alias (RAW, ``.heif``) are left alone apart
        from case.

        Args:
            extension: A suffix including the leading dot (e.g. ``".JPG"``),
                in whatever case the source file carried.

        Returns:
            The lowercased, alias-collapsed extension (e.g. ``".jpg"``).
        """
        lowered = extension.lower()
        return cls.EXTENSION_ALIASES.get(lowered, lowered)

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

        # Per-file EXIF + PNG-text metadata, read once per photo/screenshot-
        # extension file inside categorize_file and handed forward to
        # ExifHandler.extract_timestamp (issue #24) so the same file is not
        # opened by Pillow a second time just to read its header. Lifetime is
        # exactly one categorization pass: cleared at the top of every
        # batch_categorize call and by clear_categorization, so it never
        # outlives -- or is silently reused across -- a run.
        self.image_metadata: Dict[str, ImageMetadata] = {}

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
        if ext in self.SIDECAR_EXTENSIONS:
            return FileCategory.SIDECAR

        # Check for screenshots (PNG files with screenshot patterns).
        #
        # Precedence, decided (issue #22): AI/C2PA provenance wins over the
        # screenshot filename convention, not the other way around. A
        # filename match is a guess about how a file was produced; provenance
        # metadata is evidence a generation tool actually wrote. A PNG named
        # like a screenshot can still be genuinely AI-generated -- e.g. a
        # screenshot taken OF a generated image, or a save-as tool that
        # happens to apply screenshot-style naming -- and such a file must
        # land in GENERATED rather than being waved through as SCREENSHOT on
        # name alone. ``_categorize_image_or_generated`` reads this file's
        # metadata exactly once (issue #24) and checks provenance before ever
        # consulting the ``filename`` fallback passed to it below.
        if ext in self.SCREENSHOT_EXTENSIONS:
            return self._categorize_image_or_generated(file_path, filename)

        # Check for photos
        if ext in self.PHOTO_EXTENSIONS:
            return self._categorize_image_or_generated(file_path)

        # Check for videos
        if ext in self.VIDEO_EXTENSIONS:
            return FileCategory.VIDEO

        # Unknown file type
        logger.warning("Unknown file type: %s", file_path)
        return FileCategory.UNKNOWN

    def _categorize_image_or_generated(
        self, file_path: str, filename: Optional[str] = None
    ) -> FileCategory:
        """
        Resolve a photo/screenshot-extension file to GENERATED, SCREENSHOT,
        or PHOTO.

        Reads the file's EXIF + PNG-text metadata exactly once via
        :meth:`_read_image_metadata` (issue #24), hands it to
        :meth:`_is_generated_content` (which performs no I/O of its own), and
        caches the result on :attr:`image_metadata` so
        ``ExifHandler.extract_timestamp`` can reuse it later in the same run
        instead of reopening the file.

        Precedence (issue #22): AI/C2PA provenance is checked FIRST and wins
        outright -- a file with a genuine generator marker is GENERATED
        regardless of its name. Only once provenance says "no marker found"
        does the screenshot filename convention (:meth:`_is_likely_screenshot`)
        get a vote, and only when ``filename`` is supplied at all (the
        non-screenshot-extension photo call site below passes none, since
        those extensions never carry that convention). This is a single
        metadata read either way: the same ``exif``/``ifd0``/``png_info``
        already fetched for the provenance check is reused for the
        filename-fallback branch, never triggering a second
        ``_read_image_metadata`` call or a second ``Image.open``.

        Args:
            file_path: Path to the candidate photo/screenshot file.
            filename: Lowercase filename to test against the screenshot
                naming convention if provenance finds no AI/C2PA marker, or
                ``None`` to skip that fallback entirely (non-screenshot
                extensions have no such convention to fall back to).

        Returns:
            FileCategory.GENERATED, FileCategory.SCREENSHOT (only when
            ``filename`` is given and matches), or FileCategory.PHOTO.
        """
        metadata = self._read_image_metadata(file_path)
        if metadata is None:
            # Unreadable as an image (corrupt, zero-byte, or a format Pillow
            # has no codec for, e.g. RAW) -- nothing to cache, and no
            # provenance evidence to weigh. The filename convention is the
            # only signal left, so it decides alone. Mirrors
            # _is_generated_content's previous try/except-swallows-and-
            # returns-False behavior for the GENERATED side of this.
            if filename is not None and self._is_likely_screenshot(filename):
                return FileCategory.SCREENSHOT
            return FileCategory.PHOTO

        exif, ifd0, png_info = metadata
        if self._is_generated_content(file_path, exif, ifd0, png_info):
            category = FileCategory.GENERATED
        elif filename is not None and self._is_likely_screenshot(filename):
            category = FileCategory.SCREENSHOT
        else:
            category = FileCategory.PHOTO

        self.image_metadata[file_path] = ImageMetadata(
            exif=exif, png_info=png_info, category=category
        )
        return category

    def _read_image_metadata(
        self, file_path: str
    ) -> Optional[Tuple[dict, dict, dict]]:
        """
        Open ``file_path`` once and capture its EXIF + PNG-text metadata.

        This is the single ``Image.open`` for the whole categorize-then-
        timestamp pass (issue #24): it reads only what Pillow already parses
        while opening the file -- ``image.getexif()`` and ``image.info`` --
        and never calls ``load()``, so it forces no pixel decode. The
        separate, deliberate full-decode gate FileProcessor runs before
        trusting an undecodable-image-typed file (issue #58) is untouched by
        this change and still performs its own decode later, exactly once.

        Returns BOTH an IFD0-only view and the merged (IFD0 + Exif sub-IFD)
        view of the same already-read ``Image.Exif`` object -- no extra I/O,
        since it is the identical in-memory object read twice with different
        tag-name resolution. This is fix-round-1 finding 1 (issue #24 review):
        the merged view must never feed the ``Software`` editing-detection
        heuristic (see :func:`media_types.merge_exif_ifds`'s docstring), only
        the capture-timestamp checks issue #25 actually intends to span both
        IFDs.

        ``png_info`` is captured only for ``.png`` files (the only extension
        :meth:`_is_generated_content` ever consults it for) and filtered to
        ``str``-valued entries -- the only entries
        :meth:`_png_info_has_ai_provenance` ever examines -- to avoid
        retaining large binary ancillary chunks (``icc_profile``, raw
        ``exif`` bytes, etc.) that are never read (fix-round-1 finding 5).

        Args:
            file_path: Path to the candidate image file.

        Returns:
            ``(exif, ifd0, png_info)`` on success, or ``None`` if the file
            cannot be opened as an image at all (corrupt, zero-byte, or a
            format Pillow has no codec for -- e.g. RAW).
        """
        from PIL import Image, UnidentifiedImageError

        # This is the only guard between a single bad file and the rest of
        # ``batch_categorize``'s loop, which has no try/except of its own
        # (issue #39): a permission error, a truncated/corrupt image, or a
        # decompression-bomb-sized header must fail THIS file, not the whole
        # categorization pass. The catch is narrowed to the exceptions
        # ``Image.open``/``getexif`` actually raise for those cases --
        # ``UnidentifiedImageError`` (itself an ``OSError`` subclass, listed
        # for clarity) covers "not an image"/"no codec"; ``OSError`` covers
        # permission and truncated-read failures; ``ValueError``/``SyntaxError``
        # cover malformed-but-openable files some Pillow plugins raise for;
        # ``Image.DecompressionBombError`` covers an image whose declared
        # pixel count exceeds Pillow's safety limit, raised by ``open()``
        # itself before any pixel is decoded. A previous bare ``except
        # Exception`` here also caught -- and discarded -- caller bugs (e.g.
        # a test double's ``AssertionError``); narrowing lets those propagate.
        try:
            with Image.open(file_path) as img:
                if file_path.lower().endswith('.png'):
                    png_info = {
                        key: value
                        for key, value in (getattr(img, 'info', None) or {}).items()
                        if isinstance(value, str)
                    }
                else:
                    png_info = {}
                raw_exif = img.getexif()
                ifd0 = ifd0_tag_names(raw_exif)
                exif = merge_exif_ifds(raw_exif, file_path)
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            SyntaxError,
            Image.DecompressionBombError,
        ) as e:
            # Deliberately still DEBUG, not WARNING (issue #39 was filed
            # against an earlier shape of this code, before issue #58 added
            # FileProcessor._is_decodable_image; that gate is now the one that
            # matters for user-visible reporting). Every extension where "this
            # file did not open" is an actionable problem --
            # DECODABLE_IMAGE_EXTENSIONS: .jpg/.jpeg/.png/.gif/.tiff/.tif/
            # .bmp/.webp -- gets independently re-opened by
            # ``_is_decodable_image`` before the file would ever be filed,
            # which ALREADY logs a WARNING naming the file and the exception
            # type and reroutes it to ``backup/corrupt/`` instead of
            # ``backup/photos/``, for every real or dry run. Warning again
            # here would be a second, redundant alarm for the exact same
            # failure. For a RAW extension (``.dng``/``.cr2``/``.nef``/...)
            # that gate deliberately does not apply -- Pillow has no codec
            # for RAW at all, so EVERY valid RAW file would raise
            # ``UnidentifiedImageError`` here on every run, and a WARNING
            # would be a constant false alarm for a library's entire RAW
            # collection, not a diagnosable failure. The exception type is
            # still named in the message for anyone running with
            # ``--verbose``.
            logger.debug(
                "Cannot read image metadata for %s: %s: %s",
                file_path, type(e).__name__, e,
            )
            return None

        return exif, ifd0, png_info

    def get_image_metadata(self, file_path: str) -> Optional[ImageMetadata]:
        """
        Return the cached :class:`ImageMetadata` for ``file_path``, if any.

        Populated by :meth:`categorize_file` for every photo/screenshot-
        extension file it successfully opened during the current
        categorization pass (issue #24). ``FileProcessor`` uses this to hand
        ``ExifHandler.extract_timestamp`` the already-read EXIF instead of
        reopening the file. Returns ``None`` for any file ``categorize_file``
        never opened (video, unknown, sidecar) or could not open
        (corrupt/undecodable-by-Pillow).

        Args:
            file_path: The path passed to categorize_file.

        Returns:
            The cached ImageMetadata, or None if there isn't one.
        """
        return self.image_metadata.get(file_path)

    def _is_likely_screenshot(self, filename: str) -> bool:
        """
        Determine if a file is likely a screenshot based on filename patterns.

        One coherent rule per pattern (issue #22): this used to carry THREE
        overlapping checks for the same ``IMG_`` convention -- a bare
        ``'img_'`` substring, a redundant ``'img_3'`` entry it already
        subsumed, and a ``filename.startswith('img_3') and
        filename.endswith('.png')`` branch that could never fire before the
        bare substring already returned True. There is now exactly one
        ``'img_'`` entry, below, and the stale docstring claim that only an
        ``IMG_3XXX`` numbering was matched is gone -- the implementation, both
        before and after this fix, matches ANY ``img_`` prefix, not just
        stems starting with digit 3.

        ``'img_'`` earns its place in this list for a reason specific to this
        tool's input: this method is only ever consulted for extensions in
        :attr:`SCREENSHOT_EXTENSIONS`, which is ``{'.png'}`` -- so in
        practice the rule this implements is not "any file named ``IMG_``"
        but "a PNG named ``IMG_`` in an Apple Photos export." Apple's
        cameras never emit PNG for a captured photo (camera output is HEIC or
        JPG); the only common source of a camera-style ``IMG_####.PNG`` is
        iOS/macOS's own screenshot pipeline, which reuses the camera's
        ``IMG_`` numbering sequence for its own PNG output. A PNG carrying
        that name is therefore almost certainly a screenshot, not a photo --
        dropping this entry would misfile every one of those as an ordinary
        image.

        Args:
            filename: Lowercase filename

        Returns:
            True if filename suggests it's a screenshot
        """
        screenshot_patterns = [
            'screenshot',
            'screen shot',
            'simulator screen shot',  # iOS Simulator
            'screen recording',
            'screen_',
            'capture',
            'img_',  # Apple export convention -- see docstring above.
        ]

        return any(pattern in filename for pattern in screenshot_patterns)

    def _is_generated_content(
        self,
        file_path: str,
        exif: Dict[str, Any],
        ifd0: Dict[str, Any],
        png_info: Dict[str, Any],
    ) -> bool:
        """
        Detect AI-generated or heavily edited content from pre-read metadata.

        Performs no I/O of its own (issue #24): ``exif``, ``ifd0``, and
        ``png_info`` are read once by the caller (:meth:`_read_image_metadata`)
        rather than this method opening ``file_path`` itself, which --
        combined with ``ExifHandler.extract_timestamp``'s own open later in
        the same pass -- used to open every candidate image file twice just
        for metadata.

        Detection is deliberately precise rather than broad (issue #8): PNG
        provenance is matched against identifiable text-chunk keys and
        word-boundary tool markers, EXIF editing software is only treated as
        generated when a genuine capture timestamp is absent (and that
        software check is scoped to IFD0 only -- fix-round-1 finding 1, see
        ``_exif_shows_synthetic_edit``), and UUID-style stems are validated
        by parsing rather than by counting characters.

        Args:
            file_path: Path to file (used only for its suffix and stem --
                neither touches the filesystem).
            exif: Merged IFD0 + Exif sub-IFD tag-name -> value mapping (see
                :func:`media_types.merge_exif_ifds`), used only for the
                capture-timestamp check.
            ifd0: IFD0-only tag-name -> value mapping (see
                :func:`media_types.ifd0_tag_names`), used only for the
                ``Software`` check.
            png_info: The PNG's ``Image.info`` dict (irrelevant for non-PNG).

        Returns:
            True if file appears to be AI-generated or heavily edited
        """
        # Check for C2PA/AI provenance in PNG text chunks.
        if file_path.lower().endswith('.png'):
            if self._png_info_has_ai_provenance(png_info):
                logger.info("Detected AI-generated content: %s", file_path)
                return True

        # Editing software present with no genuine capture timestamp.
        if self._exif_shows_synthetic_edit(exif, ifd0):
            logger.info("Detected heavily edited content: %s", file_path)
            return True

        # UUID-style stems are a common convention for generated output.
        if self._has_uuid_stem(file_path):
            logger.info("Detected UUID filename (likely generated): %s", file_path)
            return True

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
        return self._png_info_has_ai_provenance(png_info)

    def _png_info_has_ai_provenance(self, png_info: Optional[Dict[str, Any]]) -> bool:
        """
        Report whether a PNG-info-shaped mapping carries AI/C2PA provenance.

        The no-I/O core of :meth:`_png_text_has_ai_provenance` (issue #24):
        operates directly on an already-read ``img.info``-shaped mapping so
        :meth:`_is_generated_content` can call it with
        :class:`FileCategorizer`'s once-per-file cached ``png_info`` instead
        of a live ``Image``. See that method's docstring for why ``img.info``
        (rather than ``img.text``) is the right thing to read (issue #44).

        Args:
            png_info: A mapping shaped like PIL's ``Image.info`` (or falsy).

        Returns:
            True if any entry indicates AI-generated provenance.
        """
        if not png_info:
            return False

        for key, value in png_info.items():
            if str(key).lower() in self.GENERATED_TEXT_KEYS:
                return True
            if isinstance(value, str) and self.AI_MARKER_PATTERN.search(value):
                return True

        return False

    def _exif_shows_synthetic_edit(
        self, exif: Dict[str, Any], ifd0: Dict[str, Any]
    ) -> bool:
        """
        Report whether EXIF signals a synthetic edit (software, no capture time).

        Editing software alone does not condemn an image: a real photo retouched
        in Lightroom keeps its ``DateTimeOriginal``. The signal is editing
        software *combined with* the absence of any genuine capture timestamp,
        which fits a graphic composed in software rather than captured.

        The ``Software`` check below reads ``ifd0`` -- the IFD0-only view --
        NOT the merged ``exif`` view, and this is deliberate (fix-round-1,
        issue #24 review, finding 1 -- CRITICAL). Before this fix,
        ``exif.get('Software')`` read the merged IFD0 + Exif sub-IFD view, so
        a ``Software`` tag written ONLY in the sub-IFD (which some EXIF
        writers do) flipped an ordinary, unedited photo to ``GENERATED`` --
        strictly widening issue #8's detection surface beyond anything the
        pre-#24 code (which read only ``img.getexif()``, i.e. IFD0) ever
        matched. The capture-timestamp check on the next line is the one
        part of this method issue #25 *does* intend to span both IFDs
        (``DateTimeOriginal``/``DateTimeDigitized`` live in the sub-IFD), so
        it still takes the merged ``exif`` view.

        Args:
            exif: Merged IFD0 + Exif sub-IFD tag-name -> value mapping (see
                :func:`media_types.merge_exif_ifds`) -- used only for the
                capture-timestamp check.
            ifd0: IFD0-only tag-name -> value mapping (see
                :func:`media_types.ifd0_tag_names`) -- used only for the
                ``Software`` check.

        Returns:
            True if editing software is present and no capture timestamp exists.
        """
        if not ifd0:
            return False

        software = str(ifd0.get('Software', '')).lower()
        has_editing_software = any(editor in software for editor in self.EDITING_SOFTWARE)
        if not has_editing_software:
            return False

        return not self._has_original_timestamp(exif)

    def _has_original_timestamp(self, exif: Dict[str, Any]) -> bool:
        """
        Report whether a genuine capture timestamp exists in the merged EXIF.

        ``DateTimeOriginal`` (0x9003) and ``DateTimeDigitized`` (0x9004) live in
        the Exif sub-IFD behind pointer tag ``0x8769``, which ``Image.getexif()``
        does not expose at the top level (issue #25). Since issue #24, ``exif``
        is already the merged tag-name view that includes it (see
        :func:`media_types.merge_exif_ifds`), so this is a plain key lookup.

        Args:
            exif: Merged tag-name -> value mapping.

        Returns:
            True if an original/digitized capture timestamp is present.
        """
        return 'DateTimeOriginal' in exif or 'DateTimeDigitized' in exif

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
        # Clear previous categorization. image_metadata is cleared alongside
        # categorized_files (issue #24) so the per-file cache's lifetime is
        # unambiguously "one batch_categorize call" and never accumulates
        # stale entries across repeated calls on a reused FileCategorizer.
        for category in self.categorized_files:
            self.categorized_files[category].clear()
        self.image_metadata.clear()

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

        The directory name is simply ``category.value`` -- every
        ``FileCategory`` member's value IS its target directory's name (e.g.
        ``FileCategory.PHOTO.value == "photos"``), so this needs no
        per-category branch and no directory-name string literals (issue
        #43). Adding a new recognized category to the enum therefore needs no
        edit here: it is handled the moment it is added.

        ``SIDECAR`` is the one member excluded, the same way it is excluded
        from :meth:`get_processable_files` (issue #29) -- sidecar files are
        deleted rather than filed, so they have no target directory.

        Args:
            category: FileCategory
            base_backup_dir: Base backup directory path

        Returns:
            Full path to target directory

        Raises:
            ValueError: If ``category`` is ``FileCategory.SIDECAR``.
        """
        if category is FileCategory.SIDECAR:
            raise ValueError(f"No target directory defined for category: {category}")
        return str(Path(base_backup_dir) / category.value)

    def ensure_target_directories(self, base_backup_dir: str) -> List[str]:
        """
        Create target directories for the recognized categories.

        Derived from :class:`FileCategory` the same way
        :meth:`get_processable_files` is (issue #29): every member except
        ``SIDECAR`` (no target directory, see :meth:`get_target_directory`)
        and ``UNKNOWN``. ``UNKNOWN`` is deliberately excluded: ``backup/
        unknown/`` must exist only once an unrecognized file is actually
        routed into it, never as an empty phantom that implies handling which
        did not occur (issue #29). It is created lazily, at the moment a file
        lands there, by :meth:`FileProcessor._process_unknown_file`. Because
        this iterates the enum rather than a hand-written list, adding a new
        recognized category needs no edit here either (issue #43) -- only an
        explicit exclusion, the same way UNKNOWN's already is, keeps a
        category out of eager creation.

        Args:
            base_backup_dir: Base backup directory path

        Returns:
            List of created directory paths
        """
        directories = []
        for category in FileCategory:
            if category in (FileCategory.SIDECAR, FileCategory.UNKNOWN):
                continue
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
        self.image_metadata.clear()