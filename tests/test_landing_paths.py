"""
End-to-end landing-path tests for the real file-processing pipeline.

Issue #32: the suite verified categorization *counts* and mock call counts but
never that a processed file actually arrived at ``backup/<category>/<timestamp>
.<ext>`` with the right contents. These tests run the real
``FileProcessor.process_all_files(dry_run=False)`` against a temp export tree
populated with real fixtures and assert on the resulting directory tree, with
**no mocking** of ``shutil.move``, ``os.remove``, or the HEIC converter.

Each test is written to fail under the mutations the issue's mutation-testing
audit found uncaught -- most notably ``HeicConverter.is_heic_file -> False`` and
``FileProcessor._process_single_file -> no-op`` -- so the tests prove the
pipeline does its job, not merely that a line of code was reached.
"""

import os
import shutil
import tempfile
import unittest

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from src.file_processor import FileProcessor
from tests.fixtures import make_exif_heic, make_exif_jpeg


class TestLandingPaths(unittest.TestCase):
    """Assert files land at their renamed backup paths with correct contents."""

    def setUp(self):
        """Create isolated temp export/backup dirs and a real FileProcessor."""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        """Remove the temp tree."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Fixture helpers (photo/HEIC come from tests.fixtures; the rest are  #
    # built here because they depend on filename + content, not EXIF      #
    # sub-IFD layout).                                                     #
    # ------------------------------------------------------------------ #

    def _export_path(self, name: str) -> str:
        """Return an absolute path inside the temp export directory."""
        return os.path.join(self.export_dir, name)

    def _backup_path(self, *parts: str) -> str:
        """Return an absolute path inside the temp backup directory."""
        return os.path.join(self.backup_dir, *parts)

    def _make_screenshot_png(
        self, name: str, color: str, size: tuple
    ) -> str:
        """
        Write a real PNG whose name marks it a screenshot and encodes a date.

        Screenshots carry no EXIF timestamp (PNG does not preserve it through
        Pillow), so the pipeline falls back to the date embedded in the
        filename -- making the landing path deterministic.
        """
        path = self._export_path(name)
        Image.new("RGB", size, color=color).save(path, format="PNG")
        return path

    def _make_video(self, name: str, marker: bytes) -> str:
        """
        Write a small ``.mov`` file with a date-bearing name.

        The bytes are not a decodable video, so metadata extraction yields no
        timestamp and the pipeline falls back to the filename date. ``marker``
        gives the file a distinct size so a misfile/swap is detectable.
        """
        path = self._export_path(name)
        with open(path, "wb") as handle:
            handle.write(marker)
        return path

    def _make_generated_png(
        self, name: str, color: str, size: tuple
    ) -> str:
        """
        Write a real PNG tagged with an AI marker so it categorizes as generated.

        The name encodes a date for a deterministic filename-fallback timestamp.
        """
        path = self._export_path(name)
        info = PngInfo()
        info.add_text("Software", "created with openai gpt image tools")
        Image.new("RGB", size, color=color).save(path, format="PNG", pnginfo=info)
        return path

    def _assert_pixel_near(self, path: str, expected: tuple, tol: int = 12):
        """Assert the top-left pixel of ``path`` is within ``tol`` of ``expected``.

        Lossy JPEG encoding nudges channel values by a point or two, so identity
        is checked with a tolerance rather than exact equality.
        """
        with Image.open(path) as img:
            actual = img.getpixel((0, 0))
        for channel, (got, want) in enumerate(zip(actual, expected)):
            self.assertLessEqual(
                abs(got - want),
                tol,
                f"pixel channel {channel} at {path} was {actual}, expected ~{expected}",
            )

    def _tree(self, root: str) -> set:
        """Return every file path under ``root`` relative to ``root``."""
        found = set()
        for dirpath, _dirs, filenames in os.walk(root):
            for filename in filenames:
                abs_path = os.path.join(dirpath, filename)
                found.add(os.path.relpath(abs_path, root))
        return found

    # ------------------------------------------------------------------ #
    # 1. Photo lands at its EXIF-derived path                             #
    # ------------------------------------------------------------------ #

    def test_photo_lands_at_its_exif_timestamp_path(self):
        """A photo's DateTimeOriginal drives its exact renamed backup path."""
        make_exif_jpeg(
            self._export_path("IMG_0001.jpg"),
            date_time_original="2024:01:15 14:30:45",
            color="green",
            size=(48, 48),
        )

        self.processor.process_all_files(dry_run=False)

        expected = self._backup_path("photos", "2024.01.15.14.30.45.jpg")
        self.assertTrue(
            os.path.isfile(expected), f"photo did not land at {expected}"
        )
        # Identity: the landed file is the 48x48 green source, not some other file.
        with Image.open(expected) as landed:
            self.assertEqual(landed.size, (48, 48))
        self._assert_pixel_near(expected, (0, 128, 0))
        # The source is gone from export -- it was moved, not copied.
        self.assertEqual(os.listdir(self.export_dir), [])

    # ------------------------------------------------------------------ #
    # 2. HEIC is converted end-to-end and the original removed            #
    # ------------------------------------------------------------------ #

    def test_heic_is_converted_and_original_removed(self):
        """A real HEIC lands as a JPEG in photos/ and the .heic disappears."""
        heic = make_exif_heic(
            self._export_path("IMG_0002.HEIC"),
            date_time_original="2022:03:04 05:06:07",
            color="blue",
            size=(64, 64),
        )

        results = self.processor.process_all_files(dry_run=False)

        # The converter actually ran.
        self.assertEqual(results["heic_conversions"], 1)
        # The original HEIC is gone from export.
        self.assertFalse(
            os.path.exists(heic), "original HEIC was not removed from export"
        )
        # It arrived as a JPEG at its EXIF-derived renamed path.
        expected = self._backup_path("photos", "2022.03.04.05.06.07.jpg")
        self.assertTrue(
            os.path.isfile(expected), f"converted JPEG not at {expected}"
        )
        # No stray .heic anywhere in the backup tree.
        self.assertFalse(
            any(name.lower().endswith(".heic") for name in self._tree(self.backup_dir)),
            "a .heic file leaked into the backup tree",
        )
        # Identity: contents survived the conversion, not just the name.
        with Image.open(expected) as landed:
            self.assertEqual(landed.format, "JPEG")
            self.assertEqual(landed.size, (64, 64))
            red, green, blue = landed.getpixel((0, 0))
            self.assertTrue(
                blue > 200 and red < 60 and green < 60,
                f"landed pixel {(red, green, blue)} is not the blue source",
            )

    # ------------------------------------------------------------------ #
    # 3. Every processable category lands in its own directory            #
    # ------------------------------------------------------------------ #

    def test_each_category_lands_in_its_own_directory(self):
        """Photo, screenshot, video, and generated each land where they belong."""
        make_exif_jpeg(
            self._export_path("IMG_0003.jpg"),
            date_time_original="2024:01:15 14:30:45",
            color="green",
            size=(48, 48),
        )
        self._make_screenshot_png(
            "Screenshot 2024-01-16-09-08-07.png", color="red", size=(30, 30)
        )
        self._make_video("video_2024-02-20-10-11-12.mov", marker=b"NOTAVIDEO" * 11)
        self._make_generated_png(
            "artwork_2024-03-03-03-03-03.png", color="purple", size=(72, 72)
        )

        self.processor.process_all_files(dry_run=False)

        # Each category's file landed at its exact expected path.
        expected_paths = {
            ("photos", "2024.01.15.14.30.45.jpg"): (48, 48),
            ("screenshots", "2024.01.16.09.08.07.png"): (30, 30),
            ("generated", "2024.03.03.03.03.03.png"): (72, 72),
        }
        for parts, size in expected_paths.items():
            path = self._backup_path(*parts)
            self.assertTrue(os.path.isfile(path), f"missing {path}")
            with Image.open(path) as landed:
                self.assertEqual(landed.size, size, f"wrong image landed at {path}")

        video_path = self._backup_path("videos", "2024.02.20.10.11.12.mov")
        self.assertTrue(os.path.isfile(video_path), f"missing {video_path}")

        # Each directory holds exactly one file -- nothing collapsed together.
        for category in ("photos", "screenshots", "videos", "generated"):
            entries = os.listdir(self._backup_path(category))
            self.assertEqual(
                len(entries), 1, f"{category} held {entries}, expected exactly one"
            )
        # Export is fully drained of processable files.
        self.assertEqual(os.listdir(self.export_dir), [])

    # ------------------------------------------------------------------ #
    # 3b. Identity: two photos are not swapped                            #
    # ------------------------------------------------------------------ #

    def test_photos_are_not_swapped_between_paths(self):
        """Distinct photos land at their own paths, not each other's."""
        make_exif_jpeg(
            self._export_path("A.jpg"),
            date_time_original="2024:05:01 01:02:03",
            color="green",
            size=(40, 40),
        )
        make_exif_jpeg(
            self._export_path("B.jpg"),
            date_time_original="2024:05:02 04:05:06",
            color="red",
            size=(80, 80),
        )

        self.processor.process_all_files(dry_run=False)

        a_path = self._backup_path("photos", "2024.05.01.01.02.03.jpg")
        b_path = self._backup_path("photos", "2024.05.02.04.05.06.jpg")
        with Image.open(a_path) as a_img:
            self.assertEqual(a_img.size, (40, 40))
        self._assert_pixel_near(a_path, (0, 128, 0))
        with Image.open(b_path) as b_img:
            self.assertEqual(b_img.size, (80, 80))
        self._assert_pixel_near(b_path, (255, 0, 0))

    # ------------------------------------------------------------------ #
    # 4. Sidecars deleted; unknowns untouched; tree is exactly expected   #
    # ------------------------------------------------------------------ #

    def test_sidecar_deleted_and_backup_tree_is_exactly_expected(self):
        """.aae sidecars are deleted; the backup tree holds only renamed files."""
        make_exif_jpeg(
            self._export_path("IMG_0004.jpg"),
            date_time_original="2024:07:08 09:10:11",
            color="green",
            size=(48, 48),
        )
        aae_path = self._export_path("IMG_0004.aae")
        with open(aae_path, "wb") as handle:
            handle.write(b"<plist>sidecar</plist>")
        unknown_path = self._export_path("notes.xyz")
        with open(unknown_path, "wb") as handle:
            handle.write(b"unknown blob")

        self.processor.process_all_files(dry_run=False)

        # The sidecar is deleted from export and never appears in backup.
        self.assertFalse(os.path.exists(aae_path), "sidecar was not deleted")
        self.assertFalse(
            any(name.lower().endswith(".aae") for name in self._tree(self.backup_dir)),
            "an .aae sidecar leaked into the backup tree",
        )
        # Unknown files are not processed; they stay in export, out of backup.
        self.assertTrue(
            os.path.exists(unknown_path), "unknown file should remain in export"
        )
        # The backup tree contains exactly the one expected renamed file.
        self.assertEqual(
            self._tree(self.backup_dir),
            {os.path.join("photos", "2024.07.08.09.10.11.jpg")},
        )


if __name__ == "__main__":
    unittest.main()
