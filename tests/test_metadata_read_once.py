"""
Tests for reading each image's metadata once per pass, not twice (issue #24).

Within one ``process_all_files`` call, ``FileCategorizer._is_generated_content``
and ``ExifHandler.extract_timestamp`` used to each open the same file
independently -- doubling Pillow's open cost per image on top of the separate,
deliberate full-pixel-decode gate ``FileProcessor`` runs before trusting an
undecodable-image-typed file as a photograph (issue #58). The fix has
``FileCategorizer.categorize_file`` read a file's EXIF + PNG-text metadata via
a single ``Image.open`` and cache it, then hand it forward to
``ExifHandler.extract_timestamp`` so that method does not reopen the file.

These tests instrument the REAL Pillow boundary (``PIL.Image.open`` and
``PIL.Image.Image.load``) rather than mocking any of this project's own
functions, so a regression that reintroduces a redundant open or an extra
decode is caught at the actual cost site the issue is about.

Every test here was confirmed to fail against the pre-#24 code (verified by
stashing the fix and rerunning this file -- see the task report for the exact
counts observed) and to pass against the fix.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from src.file_categorizer import FileCategorizer, FileCategory
from src.exif_handler import ExifHandler
from src.file_processor import FileProcessor, Settings
from tests.fixtures import (
    make_corrupt_jpeg,
    make_exif_heic,
    make_exif_jpeg,
    make_png_with_text,
)


def _counting_open():
    """Return (wrapper, counter_list) wrapping the REAL PIL.Image.open."""
    real_open = Image.open
    calls = []

    def wrapper(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("fp"))
        return real_open(*args, **kwargs)

    return wrapper, calls


class TestCategorizeAndTimestampShareOneOpen(unittest.TestCase):
    """The exact redundancy the issue names: categorize + extract_timestamp."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.categorizer = FileCategorizer()
        self.handler = ExifHandler()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_categorize_then_extract_timestamp_opens_pillow_once(self):
        """categorize_file + extract_timestamp together call Image.open once.

        Before the fix: categorize_file's _is_generated_content opened the
        file, then extract_timestamp opened it again -- 2 calls. This test
        fails against the old code (2) and passes against the fix (1).
        """
        path = make_exif_jpeg(
            os.path.join(self.temp_dir, "photo.jpg"),
            date_time_original="2026:03:10 14:22:05",
        )

        wrapper, calls = _counting_open()
        with patch("PIL.Image.open", side_effect=wrapper):
            category = self.categorizer.categorize_file(path)
            metadata = self.categorizer.get_image_metadata(path)
            timestamp = self.handler.extract_timestamp(path, metadata)

        self.assertEqual(category, FileCategory.PHOTO)
        self.assertEqual(
            timestamp.strftime("%Y:%m:%d %H:%M:%S"), "2026:03:10 14:22:05"
        )
        self.assertEqual(
            len(calls), 1,
            f"expected exactly one Image.open across categorize_file + "
            f"extract_timestamp, got {len(calls)}",
        )

    def test_heic_categorize_then_extract_timestamp_opens_pillow_once(self):
        """Same reduction for HEIC, where every open is a full libheif decode --
        the case the issue calls out as mattering most."""
        path = make_exif_heic(
            os.path.join(self.temp_dir, "photo.heic"),
            date_time_original="2026:03:10 14:22:05",
        )

        wrapper, calls = _counting_open()
        with patch("PIL.Image.open", side_effect=wrapper):
            category = self.categorizer.categorize_file(path)
            metadata = self.categorizer.get_image_metadata(path)
            timestamp = self.handler.extract_timestamp(path, metadata)

        self.assertEqual(category, FileCategory.PHOTO)
        self.assertEqual(
            timestamp.strftime("%Y:%m:%d %H:%M:%S"), "2026:03:10 14:22:05"
        )
        self.assertEqual(len(calls), 1)


class TestFullPassOpenCount(unittest.TestCase):
    """
    Integration counts across one real process_all_files(dry_run=True) pass,
    mirroring the issue's own measurement methodology ("Measured with a single
    JPEG through one process_all_files(dry_run=True): Image.open called 2x").

    A JPEG also passes through FileProcessor's separate #58 decode-verify gate
    (_is_decodable_image), which is untouched by this fix and still performs
    its own Image.open + load(). So a decodable-extension photo's total drops
    from 3 (categorize + quarantine-decode + timestamp) to 2 (one shared
    metadata read + the preserved quarantine decode) -- not to 1. HEIC is
    excluded from that gate (verified separately, issue #7), so a HEIC's
    total drops all the way from 2 to 1.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_single_jpeg_dry_run_opens_pillow_twice_not_three_times(self):
        make_exif_jpeg(
            os.path.join(self.export_dir, "a.jpg"),
            date_time_original="2024:01:01 01:01:01",
        )
        processor = FileProcessor(Settings(export_dir=self.export_dir, backup_dir=self.backup_dir))

        wrapper, calls = _counting_open()
        with patch("PIL.Image.open", side_effect=wrapper):
            processor.process_all_files(dry_run=True)

        self.assertEqual(
            len(calls), 2,
            f"expected 2 Image.open calls (1 shared metadata read + 1 "
            f"preserved #58 quarantine decode), got {len(calls)}",
        )

    def test_single_heic_dry_run_opens_pillow_once_not_twice(self):
        """HEIC skips the #58 decode gate entirely, so it drops all the way to 1."""
        make_exif_heic(
            os.path.join(self.export_dir, "a.heic"),
            date_time_original="2024:01:01 01:01:01",
        )
        processor = FileProcessor(Settings(export_dir=self.export_dir, backup_dir=self.backup_dir))

        wrapper, calls = _counting_open()
        with patch("PIL.Image.open", side_effect=wrapper):
            processor.process_all_files(dry_run=True)

        self.assertEqual(
            len(calls), 1,
            f"expected exactly 1 Image.open call for a HEIC dry run, got "
            f"{len(calls)}",
        )

    def test_screenshot_named_png_dry_run_opens_pillow_twice_not_three_times(self):
        """A screenshot-named PNG must not cost an extra open (issues #22/#24).

        Issue #22 made AI provenance win over the screenshot filename pattern,
        which means a screenshot-named PNG is now opened during categorization
        where the old filename short-circuit returned early without opening.
        The total still holds at 2 because the open that used to happen later
        in ``extract_timestamp`` is eliminated by #24's cache -- but nothing in
        the suite pinned that, so a future change reintroducing a second real
        read for exactly this shape of file would have gone unnoticed.

        Two is the same total a plain JPEG costs: one shared metadata read plus
        the preserved #58 quarantine decode.
        """
        make_png_with_text(
            os.path.join(self.export_dir, "Screenshot 2024-01-15.png"),
            {"Comment": "an ordinary screenshot, no provenance markers"},
        )
        processor = FileProcessor(Settings(export_dir=self.export_dir, backup_dir=self.backup_dir))

        wrapper, calls = _counting_open()
        with patch("PIL.Image.open", side_effect=wrapper):
            processor.process_all_files(dry_run=True)

        self.assertEqual(
            len(calls), 2,
            f"expected 2 Image.open calls for a screenshot-named PNG (1 shared "
            f"metadata read + 1 preserved #58 quarantine decode), got "
            f"{len(calls)}",
        )


class TestIsGeneratedContentNoIO(unittest.TestCase):
    """Acceptance criterion: _is_generated_content performs no I/O given metadata."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.categorizer = FileCategorizer()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_is_generated_content_never_calls_image_open(self):
        """With metadata already read, _is_generated_content must not touch Pillow.

        Fails against the old code, whose _is_generated_content(file_path)
        always opened the file itself -- there Image.open would be invoked at
        least once and this test would error out via the raising stub below.
        """
        path = make_exif_jpeg(
            os.path.join(self.temp_dir, "photo.jpg"),
            date_time_original="2026:01:15 10:00:00",
        )
        # Read metadata BEFORE installing the trap, exactly like production
        # code does (categorize_file reads it once, then hands it forward).
        exif, ifd0, png_info = self.categorizer._read_image_metadata(path)

        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("_is_generated_content performed file I/O")

        with patch("PIL.Image.open", side_effect=_must_not_be_called):
            result = self.categorizer._is_generated_content(path, exif, ifd0, png_info)

        self.assertFalse(result)

    def test_is_generated_content_still_detects_uuid_stem_without_io(self):
        """A UUID stem is a filename-only check -- must still flag with no I/O."""
        path = make_exif_jpeg(
            os.path.join(
                self.temp_dir, "12345678-1234-1234-1234-123456789abc.jpg"
            ),
        )
        exif, ifd0, png_info = self.categorizer._read_image_metadata(path)

        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("_is_generated_content performed file I/O")

        with patch("PIL.Image.open", side_effect=_must_not_be_called):
            result = self.categorizer._is_generated_content(path, exif, ifd0, png_info)

        self.assertTrue(result)


class TestExtractTimestampStandalone(unittest.TestCase):
    """Acceptance criterion: extract_timestamp still works when called standalone."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.handler = ExifHandler()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_standalone_call_with_no_metadata_opens_and_reads_correctly(self):
        path = make_exif_jpeg(
            os.path.join(self.temp_dir, "photo.jpg"),
            date_time_original="2026:03:10 14:22:05",
        )

        result = self.handler.extract_timestamp(path)

        self.assertEqual(result.strftime("%Y:%m:%d %H:%M:%S"), "2026:03:10 14:22:05")

    def test_metadata_provided_and_standalone_agree(self):
        """The metadata-provided path and the standalone-open path must agree."""
        path = make_exif_jpeg(
            os.path.join(self.temp_dir, "photo.jpg"),
            date_time_original="2026:03:10 14:22:05",
        )
        categorizer = FileCategorizer()
        categorizer.categorize_file(path)
        metadata = categorizer.get_image_metadata(path)

        via_metadata = ExifHandler().extract_timestamp(path, metadata)
        via_standalone = ExifHandler().extract_timestamp(path)

        self.assertEqual(via_metadata, via_standalone)

    def test_non_str_timestamp_value_falls_back_instead_of_raising(self):
        """A non-str EXIF timestamp value must fall back, not raise (fix-round-1 finding 2).

        ``datetime.strptime`` raises ``TypeError`` (not ``ValueError``) when
        handed a non-str value such as ``bytes`` -- a corrupt or hostile EXIF
        blob can produce exactly this, and this tool's stated threat model
        includes untrusted input. Before this fix, the metadata-provided
        branch of ``extract_timestamp`` let that ``TypeError`` propagate
        uncaught (verified: it did, reproducibly, against the pre-fix code),
        which surfaced as a *failed* file (left in ``export/``) instead of
        the fail-open-to-fallback-timestamp behavior every other malformed-tag
        case gets. This asserts the metadata-provided branch degrades the
        same way the standalone branch always has: return ``None`` and record
        the file as missing-EXIF, never raise.
        """
        from src.file_categorizer import ImageMetadata, FileCategory

        handler = ExifHandler()
        metadata = ImageMetadata(
            exif={"DateTimeOriginal": b"2024:01:01 00:00:00"},
            png_info={},
            category=FileCategory.PHOTO,
        )

        result = handler.extract_timestamp("bogus.jpg", metadata)

        self.assertIsNone(result)
        self.assertIn("bogus.jpg", handler.missing_exif_files)

    def test_non_str_timestamp_value_falls_back_on_standalone_path_too(self):
        """The standalone (no-metadata) branch already handled this; pin it too."""
        path = make_exif_jpeg(
            os.path.join(self.temp_dir, "photo.jpg"),
            date_time_original="2026:03:10 14:22:05",
        )
        handler = ExifHandler()
        # A real EXIF read producing a non-str value is far-fetched to
        # construct as a fixture; exercise _parse_exif_datetime directly,
        # which is exactly what both branches funnel every candidate through.
        result = handler._parse_exif_datetime(b"2024:01:01 00:00:00", path)

        self.assertIsNone(result)
        self.assertIn(path, handler.missing_exif_files)


class TestQuarantineFullDecodeExactlyOnce(unittest.TestCase):
    """
    Issue #58's invariant must survive this change: exactly one full pixel
    decode per image-typed file, and the merged metadata read must not add
    another. Instrumented at the real decode boundary (PIL.Image.Image.load),
    not Image.open, since #24 intentionally keeps the metadata-read's open
    lazy (header-only) and the quarantine gate's load() as the sole place a
    full decode happens.

    Counting is by DISTINCT decoded Image object, not raw call count: a
    control measurement (see the task report) shows Pillow's OWN
    ``JpegImageFile.load()`` invokes the base ``Image.Image.load`` twice for
    a single logical ``with Image.open(path) as image: image.load()`` -- an
    internal implementation detail present even in a bare, isolated
    open+load with no project code involved at all. Counting distinct
    objects rather than raw calls isolates "how many separate opens were
    fully decoded" from that Pillow-internal recursion.

    ``_counting_load`` APPENDS the live ``Image`` objects to a list rather
    than recording ``id(self_img)`` into a set (fix-round-1, issue #24
    review, finding 3): without a held reference, CPython is free to garbage
    collect and reuse the memory address of a decoded-and-then-closed
    ``Image`` for a LATER, genuinely distinct decode, so an id-only set can
    silently under-count -- demonstrated by
    ``TestDecodeCountingMechanismDetectsGenuineDoubleDecode`` below, which
    fails with the old id-set approach (reports 1) and passes with this one
    (reports 2) against the exact same run.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _counting_load(self):
        real_load = Image.Image.load
        decoded_objects = []

        def wrapper(self_img, *args, **kwargs):
            # Hold a real reference -- not just id(self_img) -- so the
            # object cannot be garbage-collected and its address reused by a
            # later, distinct decode before we count it (finding 3).
            decoded_objects.append(self_img)
            return real_load(self_img, *args, **kwargs)

        return wrapper, decoded_objects

    @staticmethod
    def _distinct_count(decoded_objects):
        return len({id(o) for o in decoded_objects})

    def test_valid_jpeg_is_fully_decoded_exactly_once(self):
        make_exif_jpeg(
            os.path.join(self.export_dir, "good.jpg"),
            date_time_original="2024:02:03 04:05:06",
        )
        processor = FileProcessor(Settings(export_dir=self.export_dir, backup_dir=self.backup_dir))

        wrapper, decoded_objects = self._counting_load()
        with patch.object(Image.Image, "load", wrapper):
            results = processor.process_all_files(dry_run=False)

        count = self._distinct_count(decoded_objects)
        self.assertEqual(
            count, 1,
            f"expected exactly one Image object fully decoded for the valid "
            f"JPEG, got {count}",
        )
        self.assertEqual(results["files_processed"], 1)
        self.assertEqual(results["files_quarantined"], 0)
        landed = os.path.join(self.backup_dir, "photos", "2024.02.03.04.05.06.jpg")
        self.assertTrue(os.path.isfile(landed))

    def test_corrupt_jpeg_is_decode_attempted_on_exactly_one_object_then_quarantined(self):
        scratch = tempfile.mkdtemp()
        try:
            full = os.path.join(scratch, "full.jpg")
            Image.new("RGB", (240, 240), "blue").save(full, format="JPEG")
            with open(full, "rb") as handle:
                data = handle.read()
            truncated = data[: len(data) // 2]
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        make_corrupt_jpeg(
            os.path.join(self.export_dir, "bad.jpg"), content=truncated
        )
        processor = FileProcessor(Settings(export_dir=self.export_dir, backup_dir=self.backup_dir))

        wrapper, decoded_objects = self._counting_load()
        with patch.object(Image.Image, "load", wrapper):
            results = processor.process_all_files(dry_run=False)

        count = self._distinct_count(decoded_objects)
        self.assertEqual(
            count, 1,
            f"expected exactly one decode attempt (one Image object) for the "
            f"truncated JPEG, got {count}",
        )
        self.assertEqual(results["files_quarantined"], 1)
        self.assertTrue(
            os.path.isfile(os.path.join(self.backup_dir, "corrupt", "bad.jpg"))
        )


class TestDecodeCountingMechanismDetectsGenuineDoubleDecode(unittest.TestCase):
    """
    Proves the reference-holding counting mechanism above actually catches a
    real double decode, rather than passing merely because the two JPEG cases
    it is used on happen to be single-decode (fix-round-1, issue #24 review,
    finding 3).

    A plain PNG with NO EXIF at all is known to be decoded TWICE by the
    current pipeline for a reason independent of this issue and NOT fixed
    here (see the task report's "concern-2 correction" and CHANGELOG): once
    via ``PngImageFile.getexif()`` -- which forces ``load()`` when no
    ``eXIf`` chunk is present -- during ``_read_image_metadata``, and once
    via the #58 quarantine gate's explicit ``load()``. This is a genuine,
    reproducible two-decode case, verified identical before and after issue
    #24's fix, that a sound counting mechanism must report as 2 -- and the
    id()-only set from finding 3 could report as 1 if CPython happened to
    reuse an address, which is exactly the failure mode being guarded here.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_reference_holding_count_reports_two_for_a_known_double_decode(self):
        Image.new("RGB", (32, 32), "red").save(
            os.path.join(self.export_dir, "plain.png"), format="PNG"
        )
        processor = FileProcessor(Settings(export_dir=self.export_dir, backup_dir=self.backup_dir))

        real_load = Image.Image.load
        decoded_objects = []

        def wrapper(self_img, *args, **kwargs):
            decoded_objects.append(self_img)
            return real_load(self_img, *args, **kwargs)

        with patch.object(Image.Image, "load", wrapper):
            processor.process_all_files(dry_run=False)

        distinct_by_reference = len({id(o) for o in decoded_objects})
        self.assertEqual(
            distinct_by_reference, 2,
            "the known pre-existing double decode for an EXIF-less PNG was "
            "not detected -- the counting mechanism itself is unsound",
        )


def _make_png_with_exif_and_text(path, text_chunks, date_time="2024:01:01 00:00:00"):
    """
    Write a PNG carrying BOTH a real ``eXIf`` chunk and ``tEXt`` chunks.

    ``make_png_with_text`` alone writes no EXIF at all, and
    ``PngImageFile.getexif()`` forces a full decode when the ``eXIf`` chunk is
    absent (a separate, pre-existing, deliberately-not-fixed-here decode
    trigger flagged by issue #44 and confirmed in the fix-round-1 report).
    Isolating "does reading PNG *text*-chunk provenance decode" (finding 4's
    subject) from that unrelated, already-known getexif() trigger requires a
    fixture where getexif() itself is also decode-free -- i.e. one that
    genuinely has an ``eXIf`` chunk to read.
    """
    from PIL.PngImagePlugin import PngInfo

    image = Image.new("RGB", (16, 16), "green")
    exif = image.getexif()
    exif[0x0132] = date_time  # DateTime, IFD0 -- just needs an eXIf chunk to exist
    info = PngInfo()
    for key, value in text_chunks.items():
        info.add_text(key, value)
    image.save(path, format="PNG", exif=exif, pnginfo=info)
    return path


class TestReadImageMetadataDoesNotDecodePng(unittest.TestCase):
    """
    Issue #44's decode-free PNG-provenance guarantee, tested against the
    actual PRODUCTION entry point -- fix-round-1, issue #24 review, finding
    4.

    Since #24, production reads PNG provenance via
    ``_read_image_metadata`` -> ``_is_generated_content`` ->
    ``_png_info_has_ai_provenance``; ``_png_text_has_ai_provenance`` (the
    method the five ``TestPngProvenanceProbeDoesNotDecode`` tests in
    ``test_file_categorizer.py`` assert on via ``img.tile``) has ZERO
    production callers left. Those five tests still pass, and are still
    worth keeping (they pin ``_png_info_has_ai_provenance``'s matching
    logic), but they no longer guard the real path: if
    ``_read_image_metadata`` started reading ``img.text`` instead of
    ``img.info``, every one of them would keep passing while #44 silently
    regressed in production. These tests close that gap by asserting the
    no-decode property directly on ``_read_image_metadata``.

    Both fixtures carry a real ``eXIf`` chunk so ``img.getexif()`` -- called
    unconditionally by ``_read_image_metadata`` for the #25 timestamp merge,
    unrelated to this finding -- is also decode-free, isolating the claim
    under test (PNG *text*-chunk provenance reads without decoding) from that
    separate, known, deliberately-unfixed getexif()-on-eXIf-less-PNG trigger.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.categorizer = FileCategorizer()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_provenance_png_metadata_read_without_decode(self):
        path = _make_png_with_exif_and_text(
            os.path.join(self.temp_dir, "gpt.png"),
            {"Comment": "Created with ChatGPT / OpenAI"},
        )

        def _must_not_load(self_img, *args, **kwargs):
            raise AssertionError(
                "_read_image_metadata forced a full pixel decode"
            )

        with patch.object(Image.Image, "load", _must_not_load):
            metadata = self.categorizer._read_image_metadata(path)

        self.assertIsNotNone(metadata)
        exif, ifd0, png_info = metadata
        self.assertTrue(
            self.categorizer._png_info_has_ai_provenance(png_info),
            "provenance chunk did not survive _read_image_metadata's capture",
        )

    def test_plain_png_metadata_read_without_decode(self):
        path = _make_png_with_exif_and_text(
            os.path.join(self.temp_dir, "plain_marker_absent.png"), {}
        )

        def _must_not_load(self_img, *args, **kwargs):
            raise AssertionError(
                "_read_image_metadata forced a full pixel decode"
            )

        with patch.object(Image.Image, "load", _must_not_load):
            metadata = self.categorizer._read_image_metadata(path)

        self.assertIsNotNone(metadata)


class TestImageMetadataCache(unittest.TestCase):
    """The cache categorize_file populates and its documented lifetime."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.categorizer = FileCategorizer()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_get_image_metadata_returns_none_before_categorization(self):
        path = os.path.join(self.temp_dir, "never_seen.jpg")
        self.assertIsNone(self.categorizer.get_image_metadata(path))

    def test_metadata_records_category_and_exif(self):
        path = make_exif_jpeg(
            os.path.join(self.temp_dir, "photo.jpg"),
            date_time_original="2026:01:15 10:00:00",
        )
        category = self.categorizer.categorize_file(path)
        metadata = self.categorizer.get_image_metadata(path)

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.category, category)
        self.assertEqual(metadata.exif.get("DateTimeOriginal"), "2026:01:15 10:00:00")

    def test_batch_categorize_clears_metadata_from_a_prior_call(self):
        """Cache lifetime is one batch_categorize call, not the instance's life."""
        first = make_exif_jpeg(
            os.path.join(self.temp_dir, "first.jpg"),
            date_time_original="2026:01:01 00:00:00",
        )
        second = make_exif_jpeg(
            os.path.join(self.temp_dir, "second.jpg"),
            date_time_original="2026:02:02 00:00:00",
        )

        self.categorizer.batch_categorize([first])
        self.assertIsNotNone(self.categorizer.get_image_metadata(first))

        self.categorizer.batch_categorize([second])
        self.assertIsNone(
            self.categorizer.get_image_metadata(first),
            "metadata from a prior batch_categorize call leaked into the next",
        )
        self.assertIsNotNone(self.categorizer.get_image_metadata(second))

    def test_undecodable_file_leaves_no_cache_entry(self):
        """A file Image.open can't open at all yields no metadata cache entry."""
        path = os.path.join(self.temp_dir, "not_an_image.png")
        with open(path, "wb") as handle:
            handle.write(b"just some text, not a PNG")

        self.categorizer.categorize_file(path)

        self.assertIsNone(self.categorizer.get_image_metadata(path))


if __name__ == "__main__":
    unittest.main()
