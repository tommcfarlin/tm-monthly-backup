"""
End-to-end tests that hidden directories are pruned from the export scan.

Issue #56: ``FileProcessor._scan_export_directory`` skipped hidden *files* but
walked straight into hidden *directories*. ``os.walk`` therefore descended into
macOS system trees (``.Trashes``, ``.Spotlight-V100``, ``.git``, ...) and any
non-hidden file inside one became a full pipeline participant -- renamed, moved
out of ``export/`` into ``backup/``, and, for ``.aae`` sidecars, permanently
deleted. Hidden system directories are never intended photo input.

These tests run the real ``FileProcessor.process_all_files(dry_run=False)``
against temp export trees, with **no mocking** of ``shutil.move`` or
``os.remove``, and assert that the contents of hidden directories are neither
archived nor deleted, while normal (non-hidden) subdirectories are still walked.
"""

import os
import shutil
import tempfile
import unittest

from src.file_processor import FileProcessor
from tests.fixtures import make_exif_jpeg


class TestHiddenDirectoryPruning(unittest.TestCase):
    """Assert os.walk never descends into hidden directories at any depth."""

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
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _export_path(self, *parts: str) -> str:
        """Return an absolute path inside the temp export directory."""
        return os.path.join(self.export_dir, *parts)

    def _write_aae(self, path: str) -> str:
        """Write a minimal Apple ``.aae`` sidecar at ``path``."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"<plist>sidecar</plist>")
        return path

    def _backup_tree(self) -> set:
        """Return every file path under the backup dir, relative to it."""
        found = set()
        for dirpath, _dirs, filenames in os.walk(self.backup_dir):
            for filename in filenames:
                abs_path = os.path.join(dirpath, filename)
                found.add(os.path.relpath(abs_path, self.backup_dir))
        return found

    # ------------------------------------------------------------------ #
    # 1. Hidden system directories are strip-mined by the buggy walk      #
    # ------------------------------------------------------------------ #

    def test_hidden_directory_contents_are_never_ingested_or_deleted(self):
        """.Trashes/ and nested .git/objects/ are left wholly untouched."""
        # A deletable, decodable image sitting in the macOS trash: the user
        # deliberately threw it away and it must not be resurrected into backup/.
        trash_photo = make_exif_jpeg(
            self._trashed("deleted.jpg"),
            date_time_original="2024:01:15 14:30:45",
            color="red",
            size=(48, 48),
        )
        # An .aae sidecar in the trash: deletion runs before anything else and
        # does not care where a file came from, so pruning is its only guard.
        trash_sidecar = self._write_aae(self._trashed("edits.aae"))
        # Internal VCS data nested several levels deep inside a hidden dir.
        git_blob = make_exif_jpeg(
            self._git_objects("blob.jpg"),
            date_time_original="2020:02:02 02:02:02",
            color="blue",
            size=(48, 48),
        )
        # A normal top-level photo that MUST still process and land.
        make_exif_jpeg(
            self._export_path("IMG_0001.jpg"),
            date_time_original="2026:06:06 06:06:06",
            color="green",
            size=(48, 48),
        )

        results = self.processor.process_all_files(dry_run=False)

        # Only the one legitimate top-level photo was seen and processed.
        self.assertEqual(
            results["files_processed"],
            1,
            "exactly the one non-hidden-dir photo should have been processed",
        )
        # The legitimate photo landed at its EXIF-derived path.
        landed = os.path.join("photos", "2026.06.06.06.06.06.jpg")
        self.assertEqual(
            self._backup_tree(),
            {landed},
            "backup tree should contain only the legitimate top-level photo",
        )
        # Nothing from the hidden trees was archived.
        self.assertNotIn(
            os.path.join("photos", "2024.01.15.14.30.45.jpg"),
            self._backup_tree(),
            ".Trashes/deleted.jpg was resurrected into backup/",
        )
        self.assertNotIn(
            os.path.join("photos", "2020.02.02.02.02.02.jpg"),
            self._backup_tree(),
            ".git/objects/blob.jpg was archived as a photo",
        )
        # The hidden-dir files remain exactly where they were in export/.
        self.assertTrue(
            os.path.isfile(trash_photo), ".Trashes/deleted.jpg was moved out"
        )
        self.assertTrue(
            os.path.isfile(git_blob), ".git/objects/blob.jpg was moved out"
        )
        # The sidecar in the trash was NOT deleted.
        self.assertTrue(
            os.path.isfile(trash_sidecar),
            ".aae inside .Trashes/ was deleted -- irreversible data loss",
        )

    def _trashed(self, name: str) -> str:
        """Return a path inside ``export/.Trashes/`` (created lazily)."""
        path = self._export_path(".Trashes", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def _git_objects(self, name: str) -> str:
        """Return a path inside ``export/.git/objects/`` (created lazily)."""
        path = self._export_path(".git", "objects", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    # ------------------------------------------------------------------ #
    # 2. A hidden directory nested several levels deep is not descended   #
    # ------------------------------------------------------------------ #

    def test_deeply_nested_hidden_directory_is_not_descended(self):
        """A hidden dir under normal parents, at depth, is still pruned."""
        # normal_a/normal_b/.cache/pack/hidden.jpg -- the hidden segment is deep
        # inside otherwise-normal directories, proving pruning applies per-level.
        deep = self._export_path("normal_a", "normal_b", ".cache", "pack")
        os.makedirs(deep, exist_ok=True)
        hidden_deep = make_exif_jpeg(
            os.path.join(deep, "hidden.jpg"),
            date_time_original="2019:09:09 09:09:09",
            color="red",
            size=(48, 48),
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(
            results["files_processed"],
            0,
            "a deeply nested hidden dir must not be walked",
        )
        self.assertEqual(self._backup_tree(), set())
        self.assertTrue(
            os.path.isfile(hidden_deep),
            "file inside a deeply nested hidden dir was moved out",
        )

    # ------------------------------------------------------------------ #
    # 3. Normal (non-hidden) subdirectories ARE still walked              #
    # ------------------------------------------------------------------ #

    def test_normal_subdirectory_is_still_processed(self):
        """Photos in a non-hidden subdirectory are processed (no over-pruning)."""
        subdir = self._export_path("2024_vacation")
        os.makedirs(subdir, exist_ok=True)
        make_exif_jpeg(
            os.path.join(subdir, "IMG_0002.jpg"),
            date_time_original="2024:08:08 08:08:08",
            color="green",
            size=(48, 48),
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(
            results["files_processed"],
            1,
            "a photo in a normal subdirectory should be processed",
        )
        self.assertEqual(
            self._backup_tree(),
            {os.path.join("photos", "2024.08.08.08.08.08.jpg")},
        )
        # The source was moved, not copied.
        self.assertFalse(os.path.exists(os.path.join(subdir, "IMG_0002.jpg")))

    # ------------------------------------------------------------------ #
    # 4. Scan results reflect only the non-hidden-dir files               #
    # ------------------------------------------------------------------ #

    def test_scan_excludes_hidden_directory_contents(self):
        """_scan_export_directory returns only files outside hidden dirs."""
        # Mix of hidden-dir files and legitimate files.
        make_exif_jpeg(
            self._trashed("trashed.jpg"),
            date_time_original="2024:01:15 14:30:45",
            color="red",
            size=(48, 48),
        )
        self._write_aae(self._trashed("trashed.aae"))
        make_exif_jpeg(
            self._git_objects("packed.jpg"),
            date_time_original="2020:02:02 02:02:02",
            color="blue",
            size=(48, 48),
        )
        top = make_exif_jpeg(
            self._export_path("keep_me.jpg"),
            date_time_original="2026:06:06 06:06:06",
            color="green",
            size=(48, 48),
        )
        normal_sub = self._export_path("album")
        os.makedirs(normal_sub, exist_ok=True)
        sub_photo = make_exif_jpeg(
            os.path.join(normal_sub, "also_keep.jpg"),
            date_time_original="2025:05:05 05:05:05",
            color="green",
            size=(48, 48),
        )

        scanned = self.processor._scan_export_directory()

        self.assertEqual(
            set(scanned),
            {top, sub_photo},
            "scan should return only files outside hidden directories",
        )


if __name__ == "__main__":
    unittest.main()
