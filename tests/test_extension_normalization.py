"""
Output-extension normalization tests (issue #55).

``_process_single_file`` used to compose the final backup filename from a
normalized timestamp and the source file's raw suffix (``Path(file_path)
.suffix``), copied verbatim -- case and spelling included. A photo library
therefore accumulated every spelling a source app happened to use:
``2024.01.15.14.30.40.JPG`` next to ``2024.01.15.14.30.41.jpg`` next to
``2024.01.15.14.30.42.jpeg``, defeating the entire point of imposing one
deterministic naming scheme. ``FileCategorizer.normalize_extension`` (a new
module-level mapping, per the issue) now lowercases every output extension and
collapses ``.jpeg`` -> ``.jpg``, ``.tiff`` -> ``.tif``, and ``.mpeg`` -> ``.mpg``
(the third pair authorized during review round 1: ``.mpg``/``.mpeg`` are the
same container/codec and both live in ``VIDEO_EXTENSIONS``, so leaving them
unmapped would reproduce the exact two-spellings-per-format defect this issue
exists to remove, just in ``videos/`` instead of ``photos/``); every other
extension (RAW formats, ``.heif``) is lowercased but otherwise left alone.

These tests run the real ``FileProcessor.process_all_files`` pipeline end to
end against temp export/backup trees -- no mocking of the move/rename path --
so they fail if the normalization is removed, narrowed, or misapplied to the
name-preserving quarantine/unknown routes.
"""

import logging
import os
import shutil
import tempfile
import unittest
from typing import List, Set

from PIL import Image
from PIL.ExifTags import Base

from src.file_categorizer import FileCategorizer
from src.file_processor import FileProcessor
from tests.fixtures import make_exif_heic, make_exif_jpeg


def _make_exif_tiff(path: str, date_time: str, color: str = "green") -> str:
    """
    Write a real TIFF carrying an IFD0 ``DateTime`` tag.

    Unlike the JPEG/HEIC fixtures in ``tests/fixtures.py``, Pillow's TIFF
    writer does not round-trip a ``DateTimeOriginal`` written into the Exif
    sub-IFD (confirmed empirically: it comes back empty on reopen), so this
    writes the IFD0 ``DateTime`` tag instead, which DOES round-trip for TIFF
    and which ``ExifHandler.extract_timestamp`` falls back to when
    ``DateTimeOriginal`` is absent.
    """
    image = Image.new("RGB", (16, 16), color=color)
    exif = image.getexif()
    exif[Base.DateTime.value] = date_time
    image.save(path, format="TIFF", exif=exif)
    return path


class _PlanCapture(logging.Handler):
    """Records the destinations a dry run reports, mirroring
    ``tests.test_dry_run_parity._PlanCapture`` (kept as an independent copy
    here rather than imported, so this file has no cross-file coupling to
    another issue's test module).
    """

    def __init__(self) -> None:
        super().__init__()
        self.targets: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("[DRY RUN]") and " -> " in message:
            self.targets.append(message.rsplit(" -> ", 1)[1].strip())


class TestNormalizeExtensionMapping(unittest.TestCase):
    """Unit-level pins for the mapping itself, independent of the pipeline."""

    def test_common_aliases_collapse_to_canonical_spelling(self):
        cases = {
            '.JPG': '.jpg',
            '.jpg': '.jpg',
            '.JPEG': '.jpg',
            '.jpeg': '.jpg',
            '.TIFF': '.tif',
            '.tiff': '.tif',
            '.TIF': '.tif',
            '.tif': '.tif',
            '.MPEG': '.mpg',
            '.mpeg': '.mpg',
            '.MPG': '.mpg',
            '.mpg': '.mpg',
        }
        for source, expected in cases.items():
            self.assertEqual(
                FileCategorizer.normalize_extension(source),
                expected,
                f"normalize_extension({source!r}) should be {expected!r}",
            )

    def test_heif_is_not_folded_into_heic(self):
        """.heif is a distinct format from .heic; only case is normalized."""
        self.assertEqual(FileCategorizer.normalize_extension('.HEIF'), '.heif')
        self.assertEqual(FileCategorizer.normalize_extension('.heif'), '.heif')
        self.assertEqual(FileCategorizer.normalize_extension('.HEIC'), '.heic')

    def test_raw_extensions_round_trip_lowercased_but_unaliased(self):
        """RAW formats are distinct; each lowercases to itself, never to another."""
        raw_exts = ['.CR2', '.NEF', '.ARW', '.ORF', '.RW2']
        for ext in raw_exts:
            expected = ext.lower()
            self.assertEqual(FileCategorizer.normalize_extension(ext), expected)
        # And no two distinct raw formats collapse onto the same spelling.
        normalized = {FileCategorizer.normalize_extension(e) for e in raw_exts}
        self.assertEqual(len(normalized), len(raw_exts))

    def test_video_extension_only_lowercased(self):
        self.assertEqual(FileCategorizer.normalize_extension('.MOV'), '.mov')
        self.assertEqual(FileCategorizer.normalize_extension('.mov'), '.mov')


class TestExtensionNormalizationEndToEnd(unittest.TestCase):
    """Real pipeline runs proving the archive lands consistently spelled."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _export_path(self, name: str) -> str:
        return os.path.join(self.export_dir, name)

    def _backup_path(self, *parts: str) -> str:
        return os.path.join(self.backup_dir, *parts)

    # -- Acceptance criterion 1 -------------------------------------- #

    def test_four_jpeg_spellings_all_land_as_dot_jpg(self):
        """a.JPG, b.jpg, c.jpeg, d.JPEG at distinct timestamps all end .jpg."""
        make_exif_jpeg(
            self._export_path("a.JPG"),
            date_time_original="2024:01:15 14:30:40",
            color="green",
        )
        make_exif_jpeg(
            self._export_path("b.jpg"),
            date_time_original="2024:01:15 14:30:41",
            color="green",
        )
        make_exif_jpeg(
            self._export_path("c.jpeg"),
            date_time_original="2024:01:15 14:30:42",
            color="green",
        )
        make_exif_jpeg(
            self._export_path("d.JPEG"),
            date_time_original="2024:01:15 14:30:43",
            color="green",
        )

        self.processor.process_all_files(dry_run=False)

        for second in ("40", "41", "42", "43"):
            expected = self._backup_path("photos", f"2024.01.15.14.30.{second}.jpg")
            self.assertTrue(
                os.path.isfile(expected), f"expected canonical landing at {expected}"
            )

        landed = os.listdir(self._backup_path("photos"))
        self.assertEqual(len(landed), 4)
        self.assertTrue(all(name.endswith(".jpg") for name in landed), landed)

    # -- Acceptance criterion 2 -------------------------------------- #

    def test_mov_case_variants_both_land_as_dot_mov(self):
        """x.MOV and y.mov (filename-dated fallback) both end in .mov."""
        with open(self._export_path("VID_2021-02-03-04-05-06.MOV"), "wb") as handle:
            handle.write(b"not a real video" * 4)
        with open(self._export_path("VID_2021-02-03-04-05-07.mov"), "wb") as handle:
            handle.write(b"not a real video, either" * 4)

        self.processor.process_all_files(dry_run=False)

        videos = os.listdir(self._backup_path("videos"))
        self.assertEqual(len(videos), 2)
        self.assertTrue(all(name.endswith(".mov") for name in videos), videos)

    # -- Acceptance criterion 3 -------------------------------------- #

    def test_raw_extension_round_trips_lowercased_only(self):
        """A .CR2 (uppercase) source lands as .cr2, not renamed to another raw format."""
        # RAW bytes are not Pillow-decodable, so this file is neither
        # EXIF-timestamped nor decode-checked (the quarantine gate excludes RAW
        # extensions -- see DECODABLE_IMAGE_EXTENSIONS); it falls back to a
        # filesystem timestamp and still lands in photos/ under a lowercased,
        # un-aliased .cr2 suffix.
        with open(self._export_path("RAW_0001.CR2"), "wb") as handle:
            handle.write(b"not a real raw file, just bytes")

        self.processor.process_all_files(dry_run=False)

        landed = os.listdir(self._backup_path("photos"))
        self.assertEqual(len(landed), 1)
        self.assertTrue(landed[0].endswith(".cr2"), landed)

    # -- .tiff/.tif alias, end to end (review round 1, item 3) --------- #

    def test_tiff_alias_lands_as_dot_tif(self):
        """A .TIFF source is decoded, timestamped, and lands as .tif.

        .tiff -> .tif was previously pinned only at the unit-mapping level;
        this exercises it through the real decode-then-rename pipeline, since
        both spellings sit in ``DECODABLE_IMAGE_EXTENSIONS`` and a future
        refactor could silently drop the alias without any test catching it.
        """
        _make_exif_tiff(
            self._export_path("scan_0001.TIFF"), date_time="2019:11:03 08:15:00"
        )

        self.processor.process_all_files(dry_run=False)

        expected = self._backup_path("photos", "2019.11.03.08.15.00.tif")
        self.assertTrue(
            os.path.isfile(expected), f"expected canonical .tif landing at {expected}"
        )
        landed = os.listdir(self._backup_path("photos"))
        self.assertEqual(landed, ["2019.11.03.08.15.00.tif"])

    # -- .mpeg/.mpg alias, end to end (authorized in review round 1) -- #

    def test_mpeg_alias_lands_as_dot_mpg(self):
        """A .mpeg source (filename-dated fallback) lands as .mpg.

        Authorized during review as a scope expansion beyond the issue's own
        two named aliases: .mpg and .mpeg are the same container/codec and
        both live in ``VIDEO_EXTENSIONS``, so leaving them unmapped would
        reproduce the exact defect this issue targets, just in videos/.
        """
        with open(
            self._export_path("VID_2020-09-09-10-11-12.mpeg"), "wb"
        ) as handle:
            handle.write(b"not a real mpeg video" * 4)

        self.processor.process_all_files(dry_run=False)

        expected = self._backup_path("videos", "2020.09.09.10.11.12.mpg")
        self.assertTrue(
            os.path.isfile(expected), f"expected canonical .mpg landing at {expected}"
        )
        landed = os.listdir(self._backup_path("videos"))
        self.assertEqual(landed, ["2020.09.09.10.11.12.mpg"])

    # -- Acceptance criterion 4 -------------------------------------- #

    def test_no_format_appears_in_two_spellings_in_one_directory(self):
        """set(ext.lower()) has the same cardinality as the unlowered set."""
        make_exif_jpeg(
            self._export_path("p1.JPG"),
            date_time_original="2024:03:01 08:00:00",
            color="green",
        )
        make_exif_jpeg(
            self._export_path("p2.jpeg"),
            date_time_original="2024:03:01 08:00:01",
            color="green",
        )
        make_exif_jpeg(
            self._export_path("p3.JPEG"),
            date_time_original="2024:03:01 08:00:02",
            color="green",
        )

        self.processor.process_all_files(dry_run=False)

        names = os.listdir(self._backup_path("photos"))
        raw_exts = {os.path.splitext(n)[1] for n in names}
        lowered_exts = {ext.lower() for ext in raw_exts}
        self.assertEqual(
            len(raw_exts),
            len(lowered_exts),
            f"a format appears in more than one spelling: {raw_exts}",
        )
        # And there is exactly one spelling in play: '.jpg'.
        self.assertEqual(raw_exts, {".jpg"})

    # -- HEIC invariant is undisturbed -------------------------------- #

    def test_heic_and_heif_sources_still_force_dot_jpg(self):
        """HEIC/HEIF conversion output stays .jpg regardless of source case.

        Covers three source spellings ``is_heic_file`` treats as equivalent
        (``.HEIC``, lowercase ``.heic``, and ``.heif``) at three distinct
        timestamps, so this actually exercises "regardless of source case"
        rather than only the single uppercase-.HEIC case the name promised
        but the original version of this test did not cover.
        """
        make_exif_heic(
            self._export_path("IMG_0001.HEIC"),
            date_time_original="2022:03:04 05:06:07",
            color="blue",
        )
        make_exif_heic(
            self._export_path("img_0002.heic"),
            date_time_original="2022:03:04 05:06:08",
            color="blue",
        )
        make_exif_heic(
            self._export_path("img_0003.heif"),
            date_time_original="2022:03:04 05:06:09",
            color="blue",
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["heic_conversions"], 3)
        for second in ("07", "08", "09"):
            expected = self._backup_path("photos", f"2022.03.04.05.06.{second}.jpg")
            self.assertTrue(os.path.isfile(expected), f"missing {expected}")
        landed = os.listdir(self._backup_path("photos"))
        self.assertEqual(len(landed), 3)
        self.assertTrue(all(name.endswith(".jpg") for name in landed), landed)

    # -- Quarantine / unknown routes are deliberately NOT normalized -- #

    def test_quarantine_preserves_original_name_and_case(self):
        """An undecodable image-typed file keeps its exact original filename."""
        with open(self._export_path("broken.JPG"), "wb") as handle:
            handle.write(b"this is definitely not a JPEG")

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_quarantined"], 1)
        expected = os.path.join(
            self.processor.get_quarantine_directory(), "broken.JPG"
        )
        self.assertTrue(
            os.path.isfile(expected),
            "quarantined file must keep its exact original name/case",
        )

    def test_unknown_file_preserves_original_name_and_case(self):
        """An unrecognized extension keeps its exact original filename."""
        with open(self._export_path("mystery.WEIRD"), "wb") as handle:
            handle.write(b"who knows")

        self.processor.process_all_files(dry_run=False)

        expected = self._backup_path("unknown", "mystery.WEIRD")
        self.assertTrue(
            os.path.isfile(expected),
            "unknown-routed file must keep its exact original name/case",
        )

    # -- Cross-run collision safety with a pre-existing mixed-case name - #

    def test_second_run_does_not_collide_with_prior_uppercase_landing(self):
        """
        A legacy archive entry filed under the old, un-normalized code (an
        uppercase ``.JPG``) must never be overwritten by a later, normalized
        run that resolves to the exact same timestamp second. The per-directory
        seed (``_taken_names``) keys on the timestamp STEM, independent of
        extension, so the legacy stem is recognized as taken and the new
        file's timestamp is bumped a second -- exactly the existing #6
        cross-run collision behavior, now proven to also hold when the two
        runs disagree on extension spelling.

        The incoming source is deliberately ``.JPEG`` (an aliased spelling),
        not plain ``.jpg``: this is what makes the test actually exercise
        #55's interaction with #6, rather than merely re-proving #6 in
        isolation (an already-canonical ``.jpg`` source would pass identically
        against the pre-#55 code, since there would be nothing for this issue
        to normalize). The bumped landing must be BOTH resolved to the next
        second AND alias-collapsed to ``.jpg``.
        """
        photos_dir = self._backup_path("photos")
        os.makedirs(photos_dir, exist_ok=True)
        legacy_path = os.path.join(photos_dir, "2024.01.15.14.30.45.JPG")
        with open(legacy_path, "wb") as handle:
            handle.write(b"legacy uppercase-named archive entry")
        with open(legacy_path, "rb") as handle:
            legacy_contents = handle.read()

        make_exif_jpeg(
            self._export_path("new_photo.JPEG"),
            date_time_original="2024:01:15 14:30:45",
            color="green",
        )

        self.processor.process_all_files(dry_run=False)

        # The legacy file is untouched: same path, same bytes.
        self.assertTrue(os.path.isfile(legacy_path))
        with open(legacy_path, "rb") as handle:
            self.assertEqual(handle.read(), legacy_contents)

        # The new file did NOT land at the legacy stem (that would silently
        # shadow it on a case-insensitive volume or collide outright on a
        # case-sensitive one); it was bumped to the next second, lowercased.
        bumped = os.path.join(photos_dir, "2024.01.15.14.30.46.jpg")
        self.assertTrue(
            os.path.isfile(bumped),
            f"expected the new file bumped to {bumped}; dir has "
            f"{os.listdir(photos_dir)}",
        )
        self.assertEqual(len(os.listdir(photos_dir)), 2)


class TestExtensionNormalizationDryRunParity(unittest.TestCase):
    """
    Dry-run / real-run parity for the normalized extension (issues #10 x #55).

    ``tests/test_dry_run_parity.py``'s mixed-export fixture is all-lowercase,
    already-canonical for every non-HEIC input (``IMG_0001.jpg``,
    ``Screenshot_....png``, ``VID_....mov``, ``mystery.xyz``, ``broken.jpg``,
    two more ``.jpg``s), so ``FileCategorizer.normalize_extension`` is a no-op
    for every single one of them and that suite's parity test cannot detect a
    divergence THIS issue's normalization could introduce. ``planned_extension``
    (``src/file_processor.py``) is currently computed once, before the
    ``dry_run``/real-run branch splits, and read identically by both --
    but nothing pins that the normalization call specifically stays on that
    shared line rather than migrating into only the real-run ``else:``
    branch, which would make dry run and real run silently disagree on
    extension while staying green everywhere else.
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.dry_export = os.path.join(self.temp_dir, "dry_export")
        self.dry_backup = os.path.join(self.temp_dir, "dry_backup")
        self.real_export = os.path.join(self.temp_dir, "real_export")
        self.real_backup = os.path.join(self.temp_dir, "real_backup")
        for path in (self.dry_export, self.real_export):
            os.makedirs(path, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _capture_dry_run_plan(self, export_dir: str, backup_dir: str) -> Set[str]:
        processor = FileProcessor(export_dir, backup_dir)
        capture = _PlanCapture()
        logger = logging.getLogger("src.file_processor")
        previous_level = logger.level
        logger.addHandler(capture)
        logger.setLevel(logging.INFO)
        try:
            processor.process_all_files(dry_run=True)
        finally:
            logger.removeHandler(capture)
            logger.setLevel(previous_level)
        return {os.path.relpath(target, backup_dir) for target in capture.targets}

    def _relative_backup_tree(self, backup_dir: str) -> Set[str]:
        found: Set[str] = set()
        for dirpath, _dirs, filenames in os.walk(backup_dir):
            for filename in filenames:
                abs_path = os.path.join(dirpath, filename)
                found.add(os.path.relpath(abs_path, backup_dir))
        return found

    def _populate_mixed_case_export(self, export_dir: str) -> None:
        """Fill ``export_dir`` with only aliased/mixed-case, non-HEIC sources.

        HEIC is deliberately excluded here -- its dry/real extension parity
        was already pinned before this fix
        (``test_heic_dry_run_reports_the_jpeg_a_real_run_lands``); this
        fixture targets the gap that test cannot see: non-HEIC sources whose
        raw suffix needs lowercasing/alias-collapsing.
        """
        make_exif_jpeg(
            os.path.join(export_dir, "IMG_0001.JPG"),
            date_time_original="2024:01:15 14:30:45",
            color="green",
        )
        make_exif_jpeg(
            os.path.join(export_dir, "IMG_0002.jpeg"),
            date_time_original="2024:01:16 09:00:00",
            color="green",
        )
        _make_exif_tiff(
            os.path.join(export_dir, "scan_0003.TIFF"),
            date_time="2019:11:03 08:15:00",
        )
        with open(
            os.path.join(export_dir, "VID_2020-09-09-10-11-12.mpeg"), "wb"
        ) as handle:
            handle.write(b"not a real mpeg video" * 4)

    def test_dry_run_reports_normalized_extensions_matching_real_run(self):
        """Dry-run plan and real-run landings agree, both fully normalized."""
        self._populate_mixed_case_export(self.dry_export)
        self._populate_mixed_case_export(self.real_export)

        dry_plan = self._capture_dry_run_plan(self.dry_export, self.dry_backup)

        real_processor = FileProcessor(self.real_export, self.real_backup)
        real_processor.process_all_files(dry_run=False)
        real_tree = self._relative_backup_tree(self.real_backup)

        expected = {
            os.path.join("photos", "2024.01.15.14.30.45.jpg"),
            os.path.join("photos", "2024.01.16.09.00.00.jpg"),
            os.path.join("photos", "2019.11.03.08.15.00.tif"),
            os.path.join("videos", "2020.09.09.10.11.12.mpg"),
        }
        self.assertEqual(
            real_tree, expected, "real run did not land the expected normalized set"
        )
        self.assertEqual(
            dry_plan,
            real_tree,
            "dry-run plan diverged from the real run's normalized landings",
        )

    def test_dry_run_predicts_bumped_and_normalized_name_over_legacy_archive(self):
        """
        A dry run over an archive already holding a legacy mixed-case name
        must predict the SAME bumped, normalized landing a real run produces
        -- not the legacy spelling, and not the un-bumped second.
        """
        for backup_dir in (self.dry_backup, self.real_backup):
            photos_dir = os.path.join(backup_dir, "photos")
            os.makedirs(photos_dir, exist_ok=True)
            with open(
                os.path.join(photos_dir, "2024.01.15.14.30.45.JPG"), "wb"
            ) as handle:
                handle.write(b"legacy uppercase-named archive entry")

        for export_dir in (self.dry_export, self.real_export):
            make_exif_jpeg(
                os.path.join(export_dir, "new_photo.JPEG"),
                date_time_original="2024:01:15 14:30:45",
                color="green",
            )

        dry_plan = self._capture_dry_run_plan(self.dry_export, self.dry_backup)

        real_processor = FileProcessor(self.real_export, self.real_backup)
        real_processor.process_all_files(dry_run=False)
        real_new_file = os.path.join(
            self.real_backup, "photos", "2024.01.15.14.30.46.jpg"
        )
        self.assertTrue(
            os.path.isfile(real_new_file),
            f"real run did not bump+normalize as expected; photos/ has "
            f"{os.listdir(os.path.join(self.real_backup, 'photos'))}",
        )

        expected_relative = os.path.join("photos", "2024.01.15.14.30.46.jpg")
        self.assertEqual(
            dry_plan,
            {expected_relative},
            "dry run must predict the bumped, normalized name the real run "
            "actually lands, not the legacy spelling or the un-bumped second",
        )


if __name__ == "__main__":
    unittest.main()
