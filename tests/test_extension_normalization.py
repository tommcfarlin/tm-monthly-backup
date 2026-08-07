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
collapses ``.jpeg`` -> ``.jpg`` and ``.tiff`` -> ``.tif``; every other
extension (RAW formats, ``.heif``) is lowercased but otherwise left alone.

These tests run the real ``FileProcessor.process_all_files`` pipeline end to
end against temp export/backup trees -- no mocking of the move/rename path --
so they fail if the normalization is removed, narrowed, or misapplied to the
name-preserving quarantine/unknown routes.
"""

import os
import shutil
import tempfile
import unittest

from src.file_categorizer import FileCategorizer
from src.file_processor import FileProcessor
from tests.fixtures import make_exif_heic, make_exif_jpeg


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
        """HEIC conversion output stays .jpg regardless of source case."""
        make_exif_heic(
            self._export_path("IMG_0001.HEIC"),
            date_time_original="2022:03:04 05:06:07",
            color="blue",
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["heic_conversions"], 1)
        expected = self._backup_path("photos", "2022.03.04.05.06.07.jpg")
        self.assertTrue(os.path.isfile(expected))

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
        """
        photos_dir = self._backup_path("photos")
        os.makedirs(photos_dir, exist_ok=True)
        legacy_path = os.path.join(photos_dir, "2024.01.15.14.30.45.JPG")
        with open(legacy_path, "wb") as handle:
            handle.write(b"legacy uppercase-named archive entry")
        with open(legacy_path, "rb") as handle:
            legacy_contents = handle.read()

        make_exif_jpeg(
            self._export_path("new_photo.jpg"),
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


if __name__ == "__main__":
    unittest.main()
