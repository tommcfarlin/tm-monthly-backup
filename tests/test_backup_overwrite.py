"""
Destination-safety tests for cross-run and cross-category collisions.

Issue #6: ``FileProcessor`` guarded filename collisions with an in-memory set
populated only during the current process, and ``shutil.move`` overwrites its
destination silently. Two photos processed in *different runs* (e.g. two months)
that resolve to the same ``YYYY.MM.DD.HH.MM.SS`` name caused the second run to
overwrite -- and destroy -- the first run's file, with a clean summary and exit
0. Collision detection is now filesystem-authoritative and scoped per target
directory: occupancy is judged by the actual on-disk path in each category
directory, reserved atomically with ``O_CREAT | O_EXCL`` so an existing file is
never chosen as a move target.

Issue #21 falls out of the same change: because occupancy is per-directory, a
photo and a video resolving to the same second no longer bump each other -- they
can never share a path on disk.

Every filesystem interaction runs against a ``tempfile.mkdtemp`` tree; no real
``export/`` or ``backup/`` directory is ever touched.
"""

import os
import shutil
import tempfile
import unittest

from PIL import Image

from src.file_processor import FileProcessor
from tests.fixtures import make_exif_jpeg


class TestBackupOverwriteSafety(unittest.TestCase):
    """A run must never overwrite a file already filed in ``backup/``."""

    def setUp(self):
        """Create an isolated temp tree with a shared backup directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.temp_dir, "backup")

    def tearDown(self):
        """Remove the temp tree."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _fresh_export(self, name: str) -> str:
        """Create and return a uniquely named empty export directory."""
        export_dir = os.path.join(self.temp_dir, name)
        os.makedirs(export_dir, exist_ok=True)
        return export_dir

    def _make_video(self, directory: str, name: str, marker: bytes) -> str:
        """
        Write a small ``.mov`` whose name encodes a date.

        The bytes are not a decodable video, so metadata extraction yields no
        timestamp and the pipeline falls back to the filename date. ``marker``
        gives the file a distinct size so a swap/overwrite is detectable.
        """
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(marker)
        return path

    def _photos_dir(self) -> str:
        return os.path.join(self.backup_dir, "photos")

    def _pixel(self, path: str) -> tuple:
        with Image.open(path) as img:
            return img.getpixel((0, 0))

    def _size(self, path: str) -> tuple:
        with Image.open(path) as img:
            return img.size

    # ------------------------------------------------------------------ #
    # 1. Cross-run overwrite (#6): two separate runs, shared backup       #
    # ------------------------------------------------------------------ #

    def test_two_runs_same_timestamp_do_not_overwrite(self):
        """Two months' distinct photos with one timestamp both survive."""
        # Run 1 (e.g. January's dump): a green 40x40 photo.
        export1 = self._fresh_export("export_jan")
        make_exif_jpeg(
            os.path.join(export1, "IMG_JAN.jpg"),
            date_time_original="2024:05:05 05:05:05",
            color="green",
            size=(40, 40),
        )
        FileProcessor(export1, self.backup_dir).process_all_files(dry_run=False)

        first = os.path.join(self._photos_dir(), "2024.05.05.05.05.05.jpg")
        self.assertTrue(os.path.isfile(first), "run 1 photo did not land")

        # Run 2 (e.g. February's dump): a DIFFERENT red 80x80 photo that resolves
        # to the SAME timestamp. A brand-new FileProcessor -- no shared memory.
        export2 = self._fresh_export("export_feb")
        make_exif_jpeg(
            os.path.join(export2, "IMG_FEB.jpg"),
            date_time_original="2024:05:05 05:05:05",
            color="red",
            size=(80, 80),
        )
        FileProcessor(export2, self.backup_dir).process_all_files(dry_run=False)

        # BOTH files survive as distinct files; neither was overwritten.
        second = os.path.join(self._photos_dir(), "2024.05.05.05.05.06.jpg")
        self.assertTrue(os.path.isfile(first), "run 1 photo was overwritten (#6)")
        self.assertTrue(os.path.isfile(second), "run 2 photo was not bumped")

        # Identity: run 1 kept the green 40x40, run 2 landed the red 80x80.
        self.assertEqual(self._size(first), (40, 40))
        r, g, b = self._pixel(first)
        self.assertTrue(g > 100 and r < 60 and b < 60, f"run 1 pixel wrong: {(r, g, b)}")
        self.assertEqual(self._size(second), (80, 80))
        r, g, b = self._pixel(second)
        self.assertTrue(r > 200 and g < 60 and b < 60, f"run 2 pixel wrong: {(r, g, b)}")

        # Exactly two files in photos/ -- nothing collapsed.
        self.assertEqual(len(os.listdir(self._photos_dir())), 2)

    # ------------------------------------------------------------------ #
    # 2. Cross-category (#21): photo and video keep the same timestamp    #
    # ------------------------------------------------------------------ #

    def test_photo_and_video_same_timestamp_both_keep_it(self):
        """A photo and a video resolving to one second both keep it (#21)."""
        export = self._fresh_export("export_mixed")
        make_exif_jpeg(
            os.path.join(export, "IMG_5000.jpg"),
            date_time_original="2024:03:03 03:03:03",
            color="green",
            size=(48, 48),
        )
        # Video has no decodable metadata; the filename yields 2024-03-03 03:03:03.
        self._make_video(export, "clip_2024-03-03-03-03-03.mov", marker=b"NOTAVIDEO" * 7)

        FileProcessor(export, self.backup_dir).process_all_files(dry_run=False)

        photo = os.path.join(self._photos_dir(), "2024.03.03.03.03.03.jpg")
        video = os.path.join(self.backup_dir, "videos", "2024.03.03.03.03.03.mov")
        self.assertTrue(os.path.isfile(photo), "photo lost its true .03 timestamp")
        self.assertTrue(
            os.path.isfile(video),
            "video was spuriously bumped -- cross-category false collision (#21)",
        )
        # No bumped video exists.
        self.assertFalse(
            os.path.exists(
                os.path.join(self.backup_dir, "videos", "2024.03.03.03.03.04.mov")
            ),
            "video must not be bumped by a photo in a different directory",
        )

    # ------------------------------------------------------------------ #
    # 3. Same directory still bumps                                       #
    # ------------------------------------------------------------------ #

    def test_two_photos_same_timestamp_same_directory_still_bump(self):
        """Two photos, one timestamp, one directory -> .05 and .06."""
        export = self._fresh_export("export_dupe")
        make_exif_jpeg(
            os.path.join(export, "A.jpg"),
            date_time_original="2024:05:05 05:05:05",
            color="green",
            size=(40, 40),
        )
        make_exif_jpeg(
            os.path.join(export, "B.jpg"),
            date_time_original="2024:05:05 05:05:05",
            color="red",
            size=(80, 80),
        )

        FileProcessor(export, self.backup_dir).process_all_files(dry_run=False)

        landed = set(os.listdir(self._photos_dir()))
        self.assertEqual(
            landed,
            {"2024.05.05.05.05.05.jpg", "2024.05.05.05.05.06.jpg"},
            f"same-directory collision did not bump to two files: {landed}",
        )

    # ------------------------------------------------------------------ #
    # 4. Pre-existing backup file on disk is never touched                #
    # ------------------------------------------------------------------ #

    def test_pre_existing_backup_file_is_not_overwritten(self):
        """A file already in backup/photos survives; the new one is bumped."""
        photos_dir = self._photos_dir()
        os.makedirs(photos_dir, exist_ok=True)

        # A hand-placed archive file at the exact name the new photo resolves to.
        existing = os.path.join(photos_dir, "2024.01.15.14.30.45.jpg")
        make_exif_jpeg(
            existing,
            date_time_original="2024:01:15 14:30:45",
            color="blue",
            size=(99, 99),
        )
        with open(existing, "rb") as handle:
            existing_bytes = handle.read()

        # A brand-new, distinct photo resolving to that same timestamp.
        export = self._fresh_export("export_preexist")
        make_exif_jpeg(
            os.path.join(export, "NEW.jpg"),
            date_time_original="2024:01:15 14:30:45",
            color="green",
            size=(48, 48),
        )

        FileProcessor(export, self.backup_dir).process_all_files(dry_run=False)

        # The pre-existing archive file is byte-for-byte untouched.
        with open(existing, "rb") as handle:
            self.assertEqual(
                handle.read(),
                existing_bytes,
                "pre-existing backup file was overwritten (#6)",
            )
        self.assertEqual(self._size(existing), (99, 99))

        # The new photo landed at the bumped path.
        bumped = os.path.join(photos_dir, "2024.01.15.14.30.46.jpg")
        self.assertTrue(os.path.isfile(bumped), "new photo was not bumped to .46")
        self.assertEqual(self._size(bumped), (48, 48))
        self.assertEqual(len(os.listdir(photos_dir)), 2)


if __name__ == "__main__":
    unittest.main()
