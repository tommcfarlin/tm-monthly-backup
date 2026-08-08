"""
End-to-end tests for quarantining undecodable image-typed files (issue #58).

``_process_single_file`` never established that a file carrying a photo
extension was decodable media. It asked ``ExifHandler.extract_timestamp`` --
which returns ``None`` on any failure -- fell back to filesystem mtime, then
renamed and moved. Everything with a recognized extension ended up in the
archive: a zero-byte ``.jpg``, a text file named ``.png``, a JPEG truncated by
an interrupted download. Each was renamed to a fabricated timestamp and filed
into ``backup/photos/`` as if it were a photograph, and the rename is the
damage -- it destroys the original filename, the one clue to what the file was.

The fix decodes the bytes (``Image.open(...).load()``, a full decode that
catches truncation where ``verify()`` does not) before trusting the extension,
and routes what fails to ``backup/corrupt/`` under its ORIGINAL filename. These
tests run the real ``FileProcessor.process_all_files`` against temp trees
populated with real fixtures -- no mocking of ``shutil.move`` -- and assert the
honest end state: the file is in ``backup/corrupt/``, gone from ``export/``,
never in ``backup/photos/``, never overwriting a name already there, and counted
as a distinct quarantined outcome. Valid images alongside must land normally.

Each test is written to FAIL against the pre-fix code (undecodable file lands in
``backup/photos/`` under a timestamp name).
"""

import os
import shutil
import tempfile
import unittest

from PIL import Image

from src.cli_interface import CLIInterface
from src.file_processor import FileProcessor, Settings
from tests.fixtures import make_corrupt_jpeg, make_exif_jpeg


def _truncated_jpeg_bytes() -> bytes:
    """Return the first half of a real JPEG -- header intact, pixel data cut."""
    scratch = tempfile.mkdtemp()
    try:
        path = os.path.join(scratch, "full.jpg")
        Image.new("RGB", (240, 240), "blue").save(path, format="JPEG")
        with open(path, "rb") as handle:
            data = handle.read()
        return data[: len(data) // 2]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


class TestQuarantineUndecodable(unittest.TestCase):
    """Undecodable image-typed files are quarantined, not archived as photos."""

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

    def _photos_dir_files(self):
        photos = self._backup_path("photos")
        return os.listdir(photos) if os.path.isdir(photos) else []

    # ------------------------------------------------------------------ #
    # 1. A truncated JPEG is quarantined, not archived as a healthy photo #
    # ------------------------------------------------------------------ #

    def test_truncated_jpeg_is_quarantined_not_archived(self):
        """A JPEG truncated to ~50% lands in backup/corrupt/, never photos/."""
        payload = _truncated_jpeg_bytes()
        src = make_corrupt_jpeg(self._export_path("IMG_9001.jpg"), content=payload)

        results = self.processor.process_all_files(dry_run=False)

        landed = self._backup_path("corrupt", "IMG_9001.jpg")
        self.assertTrue(
            os.path.isfile(landed),
            "truncated JPEG was not quarantined to backup/corrupt/",
        )
        # Its bytes are preserved exactly -- the user can still recover it.
        with open(landed, "rb") as handle:
            self.assertEqual(handle.read(), payload, "quarantined bytes changed")
        # It never masquerades as a photo.
        self.assertEqual(
            self._photos_dir_files(), [], "undecodable file leaked into photos/"
        )
        # It was MOVED, not copied: gone from export, which is fully drained.
        self.assertFalse(os.path.exists(src), "corrupt file left behind in export/")
        self.assertEqual(os.listdir(self.export_dir), [], "export/ not drained")
        # Honestly reported: quarantined, not counted as processed or failed.
        self.assertEqual(results["files_quarantined"], 1)
        self.assertEqual(results["files_processed"], 0)
        self.assertEqual(results["files_failed"], 0)

    # ------------------------------------------------------------------ #
    # 2. A text file renamed .png is quarantined, not archived as a photo #
    # ------------------------------------------------------------------ #

    def test_text_file_named_png_is_quarantined(self):
        """A non-image mislabeled .png is quarantined, never filed as a photo."""
        self._write(self._export_path("not_an_image.png"), b"just some text, not a PNG")

        results = self.processor.process_all_files(dry_run=False)

        self.assertTrue(
            os.path.isfile(self._backup_path("corrupt", "not_an_image.png")),
            "text-as-.png was not quarantined",
        )
        self.assertEqual(
            self._photos_dir_files(), [], "text-as-.png leaked into photos/"
        )
        self.assertFalse(
            os.path.isdir(self._backup_path("screenshots"))
            and os.listdir(self._backup_path("screenshots")),
            "text-as-.png leaked into screenshots/",
        )
        self.assertEqual(results["files_quarantined"], 1)
        self.assertEqual(os.listdir(self.export_dir), [])

    # ------------------------------------------------------------------ #
    # 3. A zero-byte .jpg is quarantined                                  #
    # ------------------------------------------------------------------ #

    def test_zero_byte_jpg_is_quarantined(self):
        """A 0-byte .jpg is quarantined, not renamed and filed as a photo."""
        self._write(self._export_path("empty.jpg"), b"")

        results = self.processor.process_all_files(dry_run=False)

        self.assertTrue(
            os.path.isfile(self._backup_path("corrupt", "empty.jpg")),
            "zero-byte .jpg was not quarantined",
        )
        self.assertEqual(self._photos_dir_files(), [])
        self.assertEqual(results["files_quarantined"], 1)

    # ------------------------------------------------------------------ #
    # 4. A valid photo alongside a corrupt one still lands correctly      #
    # ------------------------------------------------------------------ #

    def test_valid_photo_lands_while_corrupt_is_quarantined(self):
        """A genuine photo is filed in photos/ even next to an undecodable file."""
        make_exif_jpeg(
            self._export_path("good.jpg"),
            date_time_original="2024:02:03 04:05:06",
            color="green",
            size=(48, 48),
        )
        make_corrupt_jpeg(
            self._export_path("bad.jpg"), content=_truncated_jpeg_bytes()
        )

        results = self.processor.process_all_files(dry_run=False)

        self.assertTrue(
            os.path.isfile(self._backup_path("photos", "2024.02.03.04.05.06.jpg")),
            "valid photo did not land in backup/photos/",
        )
        self.assertTrue(
            os.path.isfile(self._backup_path("corrupt", "bad.jpg")),
            "corrupt file was not quarantined",
        )
        self.assertEqual(results["files_processed"], 1)
        self.assertEqual(results["files_quarantined"], 1)
        self.assertEqual(results["files_failed"], 0)
        self.assertEqual(os.listdir(self.export_dir), [])

    # ------------------------------------------------------------------ #
    # 5. Valid PNG / GIF / TIFF / BMP are NOT falsely quarantined         #
    # ------------------------------------------------------------------ #

    def test_valid_raster_formats_are_not_quarantined(self):
        """Genuine PNG/GIF/TIFF/BMP images decode and are never quarantined."""
        for fmt, name in (
            ("PNG", "pic.png"),
            ("GIF", "pic.gif"),
            ("TIFF", "pic.tiff"),
            ("BMP", "pic.bmp"),
        ):
            Image.new("RGB", (32, 32), "red").save(
                self._export_path(name), format=fmt
            )

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(
            results["files_quarantined"], 0, "a valid image was falsely quarantined"
        )
        self.assertFalse(
            os.path.exists(self._backup_path("corrupt")),
            "backup/corrupt/ created for valid images",
        )
        # All four are genuine images -> each filed as a photo, export drained.
        self.assertEqual(results["files_processed"], 4)
        self.assertEqual(os.listdir(self.export_dir), [])

    # ------------------------------------------------------------------ #
    # 6. No phantom backup/corrupt/ when nothing is undecodable           #
    # ------------------------------------------------------------------ #

    def test_no_corrupt_directory_when_nothing_undecodable(self):
        """backup/corrupt/ exists only if something is actually quarantined."""
        make_exif_jpeg(
            self._export_path("IMG_0001.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )

        self.processor.process_all_files(dry_run=False)

        self.assertFalse(
            os.path.exists(self._backup_path("corrupt")),
            "backup/corrupt/ was created empty with nothing to quarantine",
        )

    # ------------------------------------------------------------------ #
    # 7. A quarantine name collision does not overwrite an existing file  #
    # ------------------------------------------------------------------ #

    def test_quarantine_name_collision_does_not_overwrite(self):
        """An existing backup/corrupt/<name> is preserved; the new one is renamed."""
        os.makedirs(self._backup_path("corrupt"), exist_ok=True)
        prior = self._backup_path("corrupt", "broken.jpg")
        self._write(prior, b"PRIOR RUN CONTENT")

        payload = _truncated_jpeg_bytes()
        make_corrupt_jpeg(self._export_path("broken.jpg"), content=payload)

        self.processor.process_all_files(dry_run=False)

        # The earlier quarantined file is untouched.
        with open(prior, "rb") as handle:
            self.assertEqual(
                handle.read(), b"PRIOR RUN CONTENT", "prior corrupt was overwritten"
            )
        # The incoming file is filed under a disambiguated name, not lost.
        disambiguated = self._backup_path("corrupt", "broken (1).jpg")
        self.assertTrue(
            os.path.isfile(disambiguated),
            "colliding corrupt file was not preserved under a new name",
        )
        with open(disambiguated, "rb") as handle:
            self.assertEqual(handle.read(), payload)
        self.assertEqual(os.listdir(self.export_dir), [])

    # ------------------------------------------------------------------ #
    # 8. The processed/failed/quarantined buckets partition every file    #
    # ------------------------------------------------------------------ #

    def test_processed_failed_quarantined_partition_all_files(self):
        """Every processable file lands in exactly one of the three buckets."""
        make_exif_jpeg(
            self._export_path("a.jpg"), date_time_original="2024:01:01 01:01:01"
        )
        make_exif_jpeg(
            self._export_path("b.jpg"), date_time_original="2024:01:02 02:02:02"
        )
        make_corrupt_jpeg(
            self._export_path("c.jpg"), content=_truncated_jpeg_bytes()
        )
        self._write(self._export_path("d.png"), b"not a png at all")

        results = self.processor.process_all_files(dry_run=False)

        processable = self.processor.categorizer.get_processable_files()
        files_seen = sum(len(paths) for paths in processable.values())

        self.assertEqual(files_seen, 4)
        self.assertEqual(
            results["files_processed"]
            + results["files_failed"]
            + results["files_quarantined"],
            files_seen,
            "every file must be processed, failed, or quarantined",
        )
        self.assertEqual(results["files_processed"], 2)
        self.assertEqual(results["files_quarantined"], 2)
        self.assertEqual(results["files_failed"], 0)

    # ------------------------------------------------------------------ #
    # 9. A dry run reports the quarantine decision but moves nothing       #
    # ------------------------------------------------------------------ #

    def test_dry_run_reports_quarantine_without_moving(self):
        """--dry-run counts the would-be quarantine and touches no files."""
        src = make_corrupt_jpeg(
            self._export_path("IMG_7777.jpg"), content=_truncated_jpeg_bytes()
        )

        results = self.processor.process_all_files(dry_run=True)

        # The decision is surfaced...
        self.assertEqual(results["files_quarantined"], 1)
        self.assertEqual(
            os.path.basename(results["quarantined_files"][0]["final_path"]),
            "IMG_7777.jpg",
        )
        # ...but nothing was moved or created.
        self.assertTrue(os.path.exists(src), "dry run moved the file")
        self.assertFalse(
            os.path.exists(self._backup_path("corrupt")),
            "dry run created backup/corrupt/",
        )

    # ------------------------------------------------------------------ #
    # 10. The banner is not an unqualified "Success!" when files quarantined #
    # ------------------------------------------------------------------ #

    def test_summary_banner_is_not_success_when_files_quarantined(self):
        """A run with quarantined-but-no-failed files does not read "Success!"."""
        make_corrupt_jpeg(
            self._export_path("junk.jpg"), content=_truncated_jpeg_bytes()
        )

        results = self.processor.process_all_files(dry_run=False)
        self.assertEqual(results["files_failed"], 0)
        self.assertEqual(results["files_quarantined"], 1)

        cli = CLIInterface(self.export_dir, self.backup_dir)
        with cli.console.capture() as capture:
            cli.display_results({**results, "status": "completed"}, dry_run=False)
        rendered = capture.get()

        self.assertNotIn("Success!", rendered)
        self.assertIn("Quarantined", rendered)
        self.assertIn("backup/corrupt/", rendered)


if __name__ == "__main__":
    unittest.main()
