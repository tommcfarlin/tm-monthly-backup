"""
Tests that non-regular files are filtered out of the export scan.

Issue #54: ``FileProcessor._scan_export_directory`` appended every non-hidden
entry ``os.walk`` yielded without checking it was a *regular* file. ``os.walk``
excludes directories, but its ``filenames`` list can still contain FIFOs (named
pipes), sockets, and device nodes -- non-regular files an untrusted archive
(``tar``/``cpio``) can materialize in ``export/``. Opening a FIFO for reading
blocks forever until a writer appears, so a later ``Image.open(...).load()``
hangs the entire run (even ``--dry-run``) with no error.

The fix filters at the scan boundary with ``os.path.isfile`` -- a ``stat``-based
check that never opens the file (so it cannot itself block) and returns True
only for regular files and symlinks pointing at regular files. FIFOs, sockets,
device nodes, and broken symlinks are therefore skipped.

Safety note: these tests assert filtering at the **scan** level and, where they
run full processing, do so only after the FIFO has already been filtered out.
No test ever triggers a blocking open on a FIFO -- if the filter regressed, the
scan-level assertions would fail (fast) rather than hang. A symlink pointing at
a real image is deliberately kept here; resolving/naming symlink targets is
issue #63's job, not this one.
"""

import os
import shutil
import socket
import tempfile
import unittest

from src.file_processor import FileProcessor
from tests.fixtures import make_exif_jpeg


class TestNonRegularFileFiltering(unittest.TestCase):
    """Assert FIFOs, sockets, and device nodes never reach the pipeline."""

    def setUp(self):
        """Create isolated temp export/backup dirs and a real FileProcessor."""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)
        # Sockets bound during a test are tracked here so tearDown can close
        # them even if an assertion fails partway through.
        self._sockets = []

    def tearDown(self):
        """Close any bound sockets and remove the temp tree."""
        for sock in self._sockets:
            try:
                sock.close()
            except OSError:
                pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _export_path(self, *parts: str) -> str:
        """Return an absolute path inside the temp export directory."""
        return os.path.join(self.export_dir, *parts)

    def _make_fifo(self, name: str) -> str:
        """Create a named pipe (FIFO) at ``name`` inside export/."""
        path = self._export_path(name)
        os.mkfifo(path)
        return path

    def _make_socket(self, name: str) -> str:
        """Bind a unix-domain socket at ``name`` inside export/."""
        path = self._export_path(name)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        self._sockets.append(sock)
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
    # 1. A FIFO named like an image is filtered out at scan time          #
    # ------------------------------------------------------------------ #

    def test_scan_excludes_fifo_with_image_extension(self):
        """A FIFO named ``pipe.jpg`` never appears in the scan results.

        This is asserted at the scan level precisely so the test cannot block:
        the FIFO is filtered before any ``Image.open`` could reach it.
        """
        self._make_fifo("pipe.jpg")
        photo = make_exif_jpeg(
            self._export_path("real.jpg"),
            date_time_original="2026:06:06 06:06:06",
            color="green",
            size=(48, 48),
        )

        scanned = self.processor._scan_export_directory()

        self.assertEqual(
            set(scanned),
            {photo},
            "scan must return only the regular photo, never the FIFO",
        )

    # ------------------------------------------------------------------ #
    # 2. Full processing completes and the photo lands beside a FIFO      #
    # ------------------------------------------------------------------ #

    def test_processing_completes_with_fifo_present(self):
        """A FIFO beside a real photo does not hang the run; the photo lands.

        Because the FIFO is filtered at scan time it never reaches a blocking
        open, so ``process_all_files`` returns normally. If the filter
        regressed, the run would hang here -- but the earlier scan-level test
        would have already failed fast, so this test only executes against a
        working filter.
        """
        self._make_fifo("pipe.jpg")
        make_exif_jpeg(
            self._export_path("real.jpg"),
            date_time_original="2026:06:06 06:06:06",
            color="green",
            size=(48, 48),
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(
            results["files_processed"],
            1,
            "exactly the one regular photo should have been processed",
        )
        self.assertEqual(
            self._backup_tree(),
            {os.path.join("photos", "2026.06.06.06.06.06.jpg")},
            "backup tree should contain only the legitimate photo",
        )
        # The FIFO was left in place, untouched -- never moved into backup/.
        self.assertTrue(
            os.path.exists(self._export_path("pipe.jpg")),
            "the FIFO should remain in export/, not be relocated",
        )

    def test_dry_run_completes_with_fifo_present(self):
        """``dry_run=True`` also completes -- the FIFO cannot stall it."""
        self._make_fifo("pipe.jpg")
        make_exif_jpeg(
            self._export_path("real.jpg"),
            date_time_original="2026:06:06 06:06:06",
            color="green",
            size=(48, 48),
        )

        results = self.processor.process_all_files(dry_run=True)

        # Dry run makes no changes, so nothing lands in backup/, but the run
        # must still complete rather than block on the FIFO.
        self.assertEqual(self._backup_tree(), set())
        self.assertIn("files_processed", results)

    # ------------------------------------------------------------------ #
    # 3. A unix socket with an image extension is skipped                 #
    # ------------------------------------------------------------------ #

    def test_scan_excludes_unix_socket_with_image_extension(self):
        """A bound unix socket named ``sock.png`` is not returned by the scan."""
        self._make_socket("sock.png")
        photo = make_exif_jpeg(
            self._export_path("real.jpg"),
            date_time_original="2025:05:05 05:05:05",
            color="green",
            size=(48, 48),
        )

        scanned = self.processor._scan_export_directory()

        self.assertEqual(
            set(scanned),
            {photo},
            "scan must skip the unix socket and return only the photo",
        )

    # ------------------------------------------------------------------ #
    # 4. Regular files are still included (guard against over-filtering)  #
    # ------------------------------------------------------------------ #

    def test_scan_includes_ordinary_regular_file(self):
        """A plain regular photo is still returned -- no over-filtering."""
        photo = make_exif_jpeg(
            self._export_path("keep_me.jpg"),
            date_time_original="2024:04:04 04:04:04",
            color="green",
            size=(48, 48),
        )

        scanned = self.processor._scan_export_directory()

        self.assertEqual(
            set(scanned),
            {photo},
            "an ordinary regular file must still be scanned",
        )

    # ------------------------------------------------------------------ #
    # 5. A symlink to a regular image is STILL included (for issue #63)   #
    # ------------------------------------------------------------------ #

    def test_scan_includes_symlink_to_regular_image(self):
        """A symlink pointing at a real image is kept (issue #63 resolves it).

        ``os.path.isfile`` follows the symlink and sees a regular file, so #54
        must NOT skip it. Target resolution/naming is deferred to issue #63.
        """
        target = make_exif_jpeg(
            self._export_path("target.jpg"),
            date_time_original="2023:03:03 03:03:03",
            color="blue",
            size=(48, 48),
        )
        link = self._export_path("link.jpg")
        os.symlink(target, link)

        scanned = self.processor._scan_export_directory()

        self.assertEqual(
            set(scanned),
            {target, link},
            "a symlink to a regular image must remain in the scan for #63",
        )

    def test_scan_excludes_symlink_to_fifo(self):
        """A symlink pointing at a FIFO is skipped -- ``isfile`` follows it.

        Asserted at the scan level: the symlink is filtered before anything
        could open its FIFO target, so the test cannot block.
        """
        fifo_target = self._make_fifo("pipe_target")
        link = self._export_path("link.jpg")
        os.symlink(fifo_target, link)
        photo = make_exif_jpeg(
            self._export_path("real.jpg"),
            date_time_original="2022:02:02 02:02:02",
            color="green",
            size=(48, 48),
        )

        scanned = self.processor._scan_export_directory()

        self.assertEqual(
            set(scanned),
            {photo},
            "a symlink to a FIFO must be skipped, like the FIFO itself",
        )


if __name__ == "__main__":
    unittest.main()
