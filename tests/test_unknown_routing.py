"""
End-to-end tests for how the pipeline handles UNKNOWN (unrecognized) files.

Issue #29: ``FileCategory.UNKNOWN`` was categorized, counted, rendered against
``backup/unknown/``, and the directory was even created -- but
``get_processable_files`` omitted it, so unknown files were never moved. They
silently stayed in ``export/`` while ``backup/unknown/`` sat empty forever, and
the summary/exit code implied a clean, fully drained run.

The fix routes unrecognized files into ``backup/unknown/`` under their original
filename (no metadata exists to derive a timestamp), creating the directory only
when a file actually lands there. These tests run the real
``FileProcessor.process_all_files(dry_run=False)`` against temp trees populated
with real fixtures -- no mocking of ``shutil.move`` or ``os.makedirs`` -- and
assert the honest end state: the file is in ``backup/unknown/``, gone from
``export/``, counted as processed, and never overwriting a name already there.

Each test is written to FAIL against the pre-fix code (silent leave in export /
phantom empty ``unknown/`` / dishonest processed count).
"""

import os
import shutil
import tempfile
import unittest

from src.file_processor import FileProcessor, Settings
from src.file_categorizer import FileCategory
from tests.fixtures import make_exif_jpeg


class TestUnknownRouting(unittest.TestCase):
    """Unrecognized files are routed to backup/unknown/, honestly reported."""

    def setUp(self):
        """Create isolated temp export/backup dirs and a real FileProcessor."""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(Settings(export_dir=self.export_dir, backup_dir=self.backup_dir))

    def tearDown(self):
        """Remove the temp tree."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _export_path(self, name: str) -> str:
        return os.path.join(self.export_dir, name)

    def _backup_path(self, *parts: str) -> str:
        return os.path.join(self.backup_dir, *parts)

    def _write(self, path: str, data: bytes) -> str:
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    # ------------------------------------------------------------------ #
    # 1. An unknown file is routed under its own name; export is drained  #
    # ------------------------------------------------------------------ #

    def test_unknown_file_lands_in_backup_unknown_with_original_name(self):
        """A .xyz file lands at backup/unknown/<original> and leaves export."""
        src = self._write(self._export_path("document.xyz"), b"unknown blob")

        results = self.processor.process_all_files(dry_run=False)

        landed = self._backup_path("unknown", "document.xyz")
        self.assertTrue(
            os.path.isfile(landed), "unknown file was not routed to backup/unknown/"
        )
        with open(landed, "rb") as handle:
            self.assertEqual(handle.read(), b"unknown blob", "contents changed")
        # It was MOVED, not copied: gone from export.
        self.assertFalse(
            os.path.exists(src), "unknown file was left behind in export/"
        )
        self.assertEqual(
            os.listdir(self.export_dir), [], "export/ was not fully drained"
        )
        # The summary honestly counts it as processed with no failures.
        self.assertEqual(results["files_processed"], 1)
        self.assertEqual(results["files_failed"], 0)
        self.assertEqual(results["categorization_stats"]["unknown"], 1)

    # ------------------------------------------------------------------ #
    # 2. No phantom empty unknown/ when there is nothing unrecognized     #
    # ------------------------------------------------------------------ #

    def test_no_unknown_directory_created_when_no_unknown_files(self):
        """backup/unknown/ exists only if something is routed there."""
        make_exif_jpeg(
            self._export_path("IMG_0001.jpg"),
            date_time_original="2024:01:15 14:30:45",
            color="green",
            size=(48, 48),
        )

        self.processor.process_all_files(dry_run=False)

        self.assertFalse(
            os.path.exists(self._backup_path("unknown")),
            "backup/unknown/ was created empty with no unknown files to route",
        )

    # ------------------------------------------------------------------ #
    # 3. A photo and an unknown file both process, each to its own place  #
    # ------------------------------------------------------------------ #

    def test_photo_and_unknown_process_side_by_side(self):
        """A recognized photo and an unknown file each land correctly."""
        make_exif_jpeg(
            self._export_path("IMG_0002.jpg"),
            date_time_original="2024:02:03 04:05:06",
            color="green",
            size=(48, 48),
        )
        self._write(self._export_path("readme.log"), b"log lines")

        results = self.processor.process_all_files(dry_run=False)

        self.assertTrue(
            os.path.isfile(self._backup_path("photos", "2024.02.03.04.05.06.jpg")),
            "photo did not land in backup/photos/",
        )
        self.assertTrue(
            os.path.isfile(self._backup_path("unknown", "readme.log")),
            "unknown did not land in backup/unknown/",
        )
        self.assertEqual(results["files_processed"], 2)
        self.assertEqual(results["files_failed"], 0)
        self.assertEqual(os.listdir(self.export_dir), [])

    # ------------------------------------------------------------------ #
    # 4. A name collision in unknown/ does not overwrite an existing file #
    # ------------------------------------------------------------------ #

    def test_unknown_name_collision_does_not_overwrite(self):
        """An existing backup/unknown/<name> is preserved; the new one is renamed."""
        os.makedirs(self._backup_path("unknown"), exist_ok=True)
        prior = self._backup_path("unknown", "notes.dat")
        self._write(prior, b"PRIOR RUN CONTENT")

        self._write(self._export_path("notes.dat"), b"NEW RUN CONTENT")

        self.processor.process_all_files(dry_run=False)

        # The earlier file is untouched.
        with open(prior, "rb") as handle:
            self.assertEqual(
                handle.read(), b"PRIOR RUN CONTENT", "prior unknown was overwritten"
            )
        # The incoming file is filed under a disambiguated name, not lost.
        disambiguated = self._backup_path("unknown", "notes (1).dat")
        self.assertTrue(
            os.path.isfile(disambiguated),
            "colliding unknown was not preserved under a new name",
        )
        with open(disambiguated, "rb") as handle:
            self.assertEqual(handle.read(), b"NEW RUN CONTENT")
        # Both files coexist; export drained.
        self.assertEqual(os.listdir(self.export_dir), [])

    # ------------------------------------------------------------------ #
    # 5. UNKNOWN is in the processable set, without a hand-maintained list #
    # ------------------------------------------------------------------ #

    def test_get_processable_files_includes_unknown_excludes_sidecar(self):
        """Every category except SIDECAR is processable; UNKNOWN is included."""
        self._write(self._export_path("thing.xyz"), b"x")
        self.processor.categorizer.batch_categorize(
            self.processor._scan_export_directory()
        )

        processable = self.processor.categorizer.get_processable_files()

        self.assertIn(FileCategory.UNKNOWN, processable)
        self.assertNotIn(FileCategory.SIDECAR, processable)
        # Every non-sidecar category is represented, so adding a new category
        # can never silently drop it the way UNKNOWN was dropped.
        expected = {c for c in FileCategory if c is not FileCategory.SIDECAR}
        self.assertEqual(set(processable.keys()), expected)

    # ------------------------------------------------------------------ #
    # 6. No file the summary reported as handled is left in export/       #
    # ------------------------------------------------------------------ #

    def test_no_reported_file_remains_in_export(self):
        """After a run, export holds no non-hidden file the summary counted."""
        make_exif_jpeg(
            self._export_path("IMG_0003.jpg"),
            date_time_original="2024:03:03 03:03:03",
            color="green",
            size=(48, 48),
        )
        self._write(self._export_path("a.bin"), b"aa")
        self._write(self._export_path("b.unknownext"), b"bb")

        results = self.processor.process_all_files(dry_run=False)

        leftovers = [
            name for name in os.listdir(self.export_dir)
            if not name.startswith(".")
        ]
        self.assertEqual(
            leftovers, [], f"files reported as handled were left in export: {leftovers}"
        )
        # All three were counted as processed.
        self.assertEqual(results["files_processed"], 3)
        self.assertEqual(results["files_failed"], 0)


if __name__ == "__main__":
    unittest.main()
