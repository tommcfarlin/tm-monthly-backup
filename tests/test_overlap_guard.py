"""
Overlap-guard tests for the export/backup directory precondition (issue #52).

The tool consumes the export tree in place (it deletes ``.aae`` sidecars and the
original ``.heic`` after conversion) and writes sorted output into the backup
tree. If the two roots are the same, or one is nested inside the other, a run
can re-ingest and destroy its own inputs. The guard must refuse such a
configuration before any filesystem mutation.

These tests cover both entry surfaces:

* ``CLIInterface.check_directories`` returns ``False`` (main exits non-zero).
* ``FileProcessor.process_all_files`` raises ``ValueError`` before scanning,
  so a direct/programmatic caller cannot bypass the guard.

They also prove the negative case (a disjoint sibling pair is allowed and
processes for real) and that resolved aliases -- a symlink or a relative path
normalizing to the same directory -- are caught. Whenever a run is rejected, a
sentinel file placed in the export tree must survive untouched.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.cli_interface import CLIInterface
from src.file_processor import FileProcessor
from tests.fixtures import make_exif_jpeg


class TestOverlapGuard(unittest.TestCase):
    """The export and backup roots must never overlap."""

    def setUp(self):
        """Create an isolated temp tree with a real export directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        os.makedirs(self.export_dir, exist_ok=True)

    def tearDown(self):
        """Remove the temp tree."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _sentinel(self, filename: str = "keep_me.aae") -> str:
        """Drop a deletable sentinel file into the export root and return it.

        A ``.aae`` sidecar is used deliberately: it is the first thing a real
        run deletes, so its survival proves the guard fired before any mutation.
        """
        path = os.path.join(self.export_dir, filename)
        with open(path, "w") as handle:
            handle.write("sentinel")
        return path

    def _assert_processor_rejects(self, export_dir: str, backup_dir: str):
        """The processor raises ValueError and mutates nothing.

        The returned exception message is handed back so callers can assert it
        names both paths.
        """
        sentinel = self._sentinel()
        processor = FileProcessor(export_dir, backup_dir)
        with self.assertRaises(ValueError) as ctx:
            processor.process_all_files(dry_run=False)
        # No processing/deletion occurred: the sidecar sentinel survives and no
        # file was recorded as processed.
        self.assertTrue(
            os.path.exists(sentinel),
            "guard must fire before any file is touched",
        )
        self.assertEqual(processor._processed_files, [])
        return str(ctx.exception)

    def _assert_cli_rejects(self, export_dir: str, backup_dir: str):
        """CLI check_directories returns False and mutates nothing."""
        sentinel = self._sentinel()
        cli = CLIInterface(export_dir, backup_dir)
        # Confirm.ask would only be reached on the empty-dir path; the overlap
        # guard fires first, so patching it simply proves we never get there.
        with patch("rich.prompt.Confirm.ask", return_value=True):
            self.assertFalse(cli.check_directories())
        self.assertTrue(
            os.path.exists(sentinel),
            "guard must fire before the backup directory is created",
        )

    # ---------------------------------------------------------------- #
    # Rejected configurations                                          #
    # ---------------------------------------------------------------- #

    def test_identical_directories_rejected(self):
        """Same directory for both roots is refused on both entry paths."""
        message = self._assert_processor_rejects(self.export_dir, self.export_dir)
        self.assertIn(os.path.realpath(self.export_dir), message)
        self._assert_cli_rejects(self.export_dir, self.export_dir)

    def test_backup_nested_inside_export_rejected(self):
        """A backup dir living inside the export tree is refused."""
        backup = os.path.join(self.export_dir, "backup")
        message = self._assert_processor_rejects(self.export_dir, backup)
        self.assertIn(os.path.realpath(backup), message)
        self.assertIn(os.path.realpath(self.export_dir), message)
        self._assert_cli_rejects(self.export_dir, backup)

    def test_export_nested_inside_backup_rejected(self):
        """An export dir living inside the backup tree is refused."""
        backup = os.path.dirname(self.export_dir)  # export sits inside temp_dir
        message = self._assert_processor_rejects(self.export_dir, backup)
        self.assertIn(os.path.realpath(self.export_dir), message)
        self.assertIn(os.path.realpath(backup), message)
        self._assert_cli_rejects(self.export_dir, backup)

    def test_relative_and_absolute_aliases_rejected(self):
        """A relative path that resolves to the export root is refused.

        ``export/./`` and ``export/sub/..`` both normalize to the export root,
        so the guard must catch them even though the raw strings differ from the
        absolute export path.
        """
        alias = os.path.join(self.export_dir, "sub", "..")
        message = self._assert_processor_rejects(self.export_dir, alias)
        self.assertIn(os.path.realpath(self.export_dir), message)
        self._assert_cli_rejects(self.export_dir, alias)

    def test_symlinked_alias_rejected(self):
        """A symlink pointing at the export root is refused (resolve normalizes)."""
        link = os.path.join(self.temp_dir, "export_link")
        os.symlink(self.export_dir, link)
        message = self._assert_processor_rejects(self.export_dir, link)
        self.assertIn(os.path.realpath(self.export_dir), message)
        self._assert_cli_rejects(self.export_dir, link)

    # ---------------------------------------------------------------- #
    # Allowed configuration (no false positive)                        #
    # ---------------------------------------------------------------- #

    def test_disjoint_siblings_allowed(self):
        """A sibling backup dir is allowed and a real run processes normally.

        This guards against a guard that is too eager: two disjoint directories
        under a common parent must NOT be treated as overlapping, and a genuine
        photo must land in the backup tree.
        """
        backup = os.path.join(self.temp_dir, "backup")
        # The guard itself reports no error for this configuration.
        self.assertIsNone(
            FileProcessor.directory_overlap_error(self.export_dir, backup)
        )

        make_exif_jpeg(
            os.path.join(self.export_dir, "photo.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        processor = FileProcessor(self.export_dir, backup)
        results = processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_processed"], 1)
        self.assertTrue(
            os.path.isfile(
                os.path.join(backup, "photos", "2024.01.15.14.30.45.jpg")
            )
        )

        # The CLI likewise accepts the disjoint pair.
        cli = CLIInterface(self.export_dir, backup)
        with patch("rich.prompt.Confirm.ask", return_value=True):
            self.assertTrue(cli.check_directories())


if __name__ == "__main__":
    unittest.main(verbosity=2)
