"""
End-to-end tests that a symlink in ``export/`` archives the file it points at.

Issue #63: a symlink to a real image is intentionally kept by the scan (#54's
``os.path.isfile`` follows the link and sees a regular file). But
``_process_single_file`` filed it with ``shutil.move``, which falls through to
``os.rename`` on a same-filesystem move -- relocating the *symlink itself*. The
archive then held ``backup/photos/<timestamp>.jpg`` as a symlink pointing back
at the original target, so the "backup" was a hollow pointer: delete the target
and the archived entry is a dead link. The behaviour was even filesystem
dependent (a cross-filesystem ``shutil.move`` copies real bytes instead).

The fix copies the resolved target's real bytes onto the reserved destination
and removes only the link from ``export/``. Two safety invariants are load
bearing and asserted throughout:

* The archived entry is a REGULAR FILE containing the target's bytes -- never a
  symlink -- so it survives the target being deleted afterwards.
* The target file (which may live entirely outside ``export/``) is NEVER moved,
  deleted, or modified. Only the link is unlinked from ``export/``.

Every fixture puts the symlink target OUTSIDE the export tree, exactly the case
the security review flagged: a symlink is how a path escapes the export root,
and the tool must archive a copy without ever touching the escaped target.
"""

import os
import shutil
import tempfile
import unittest

from PIL import Image

from src.file_processor import FileProcessor
from tests.fixtures import make_corrupt_jpeg, make_exif_heic, make_exif_jpeg


class TestSymlinkTargetArchiving(unittest.TestCase):
    """A symlink lands its target's real content; the target is never touched."""

    def setUp(self):
        """Create isolated temp export/backup dirs plus an outside-the-tree store."""
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        # Symlink targets live here, deliberately OUTSIDE export/ -- the escape
        # case the tool must copy from without ever mutating.
        self.outside_dir = os.path.join(self.temp_dir, "elsewhere")
        os.makedirs(self.export_dir, exist_ok=True)
        os.makedirs(self.outside_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        """Remove the temp tree."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _export_path(self, name: str) -> str:
        """Return an absolute path inside the temp export directory."""
        return os.path.join(self.export_dir, name)

    def _outside_path(self, name: str) -> str:
        """Return an absolute path inside the outside-the-tree store."""
        return os.path.join(self.outside_dir, name)

    def _backup_path(self, *parts: str) -> str:
        """Return an absolute path inside the temp backup directory."""
        return os.path.join(self.backup_dir, *parts)

    def _assert_pixel_near(self, path: str, expected: tuple, tol: int = 12):
        """Assert the top-left pixel of ``path`` is within ``tol`` of ``expected``."""
        with Image.open(path) as img:
            actual = img.getpixel((0, 0))
        for channel, (got, want) in enumerate(zip(actual, expected)):
            self.assertLessEqual(
                abs(got - want),
                tol,
                f"pixel channel {channel} at {path} was {actual}, expected ~{expected}",
            )

    # ------------------------------------------------------------------ #
    # 1. Symlink -> real JPEG target outside export/                     #
    # ------------------------------------------------------------------ #

    def test_symlink_to_jpeg_lands_real_bytes_and_spares_target(self):
        """A link to an outside JPEG lands a regular file; the target survives."""
        target = make_exif_jpeg(
            self._outside_path("origin.jpg"),
            date_time_original="2026:08:01 14:12:12",
            color="green",
            size=(48, 48),
        )
        link = self._export_path("link.jpg")
        os.symlink(target, link)

        self.processor.process_all_files(dry_run=False)

        landed = self._backup_path("photos", "2026.08.01.14.12.12.jpg")
        # The archived entry is a REGULAR FILE -- not a symlink -- carrying the
        # target's real image bytes.
        self.assertTrue(os.path.isfile(landed), f"nothing landed at {landed}")
        self.assertFalse(
            os.path.islink(landed),
            "the archived entry must be a real file, not a symlink",
        )
        with Image.open(landed) as img:
            self.assertEqual(img.size, (48, 48))
        self._assert_pixel_near(landed, (0, 128, 0))

        # The target is untouched: still a regular file at its original path
        # with its original content.
        self.assertTrue(
            os.path.isfile(target) and not os.path.islink(target),
            "the symlink target must remain in place, unmodified",
        )
        with Image.open(target) as original:
            self.assertEqual(original.size, (48, 48))

        # Only the link was removed from export/.
        self.assertFalse(
            os.path.lexists(link), "the symlink should be gone from export/"
        )

        # The archive survives the target being tidied away afterwards -- the
        # exact failure the old move-the-link behaviour caused.
        os.remove(target)
        self.assertTrue(
            os.path.isfile(landed),
            "archived photo must stay readable after the target is deleted",
        )
        with Image.open(landed) as img:
            img.load()

    # ------------------------------------------------------------------ #
    # 2. Symlink -> HEIC target outside export/                          #
    # ------------------------------------------------------------------ #

    def test_symlink_to_heic_converts_and_spares_target(self):
        """A link to an outside HEIC converts to a real JPEG; the HEIC survives."""
        target = make_exif_heic(
            self._outside_path("origin.heic"),
            date_time_original="2022:03:04 05:06:07",
            color="blue",
            size=(64, 64),
        )
        link = self._export_path("link.heic")
        os.symlink(target, link)

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["heic_conversions"], 1)
        landed = self._backup_path("photos", "2022.03.04.05.06.07.jpg")
        # Converted JPEG is a regular file, not a link, and decodes as the blue
        # source image.
        self.assertTrue(os.path.isfile(landed), f"converted JPEG not at {landed}")
        self.assertFalse(
            os.path.islink(landed),
            "the converted archive entry must be a real file, not a symlink",
        )
        with Image.open(landed) as img:
            self.assertEqual(img.format, "JPEG")
            self.assertEqual(img.size, (64, 64))
            red, green, blue = img.getpixel((0, 0))
            self.assertTrue(
                blue > 200 and red < 60 and green < 60,
                f"landed pixel {(red, green, blue)} is not the blue source",
            )

        # The target HEIC is untouched -- the cleanup step must unlink the LINK,
        # not the file it points at.
        self.assertTrue(
            os.path.isfile(target) and not os.path.islink(target),
            "the HEIC target must remain in place, unmodified",
        )
        with Image.open(target) as original:
            self.assertEqual(original.format, "HEIF")
        # Only the link left export/.
        self.assertFalse(
            os.path.lexists(link), "the symlink should be gone from export/"
        )
        # No stray .heic leaked into the backup tree.
        for dirpath, _dirs, filenames in os.walk(self.backup_dir):
            for name in filenames:
                self.assertFalse(
                    name.lower().endswith(".heic"),
                    "a .heic file leaked into the backup tree",
                )

    # ------------------------------------------------------------------ #
    # 3. Symlink -> undecodable target outside export/                   #
    # ------------------------------------------------------------------ #

    def test_symlink_to_undecodable_quarantines_copy_and_spares_target(self):
        """A link to a corrupt .jpg quarantines a real copy; the target survives."""
        target = make_corrupt_jpeg(self._outside_path("broken.jpg"))
        link = self._export_path("link.jpg")
        os.symlink(target, link)

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(
            results["files_quarantined"], 1, "the corrupt link should quarantine"
        )
        # It is quarantined under the LINK's name (the source basename), as a
        # real regular file copy -- not a symlink.
        quarantined = self._backup_path("corrupt", "link.jpg")
        self.assertTrue(
            os.path.isfile(quarantined), f"nothing quarantined at {quarantined}"
        )
        self.assertFalse(
            os.path.islink(quarantined),
            "quarantined entry must be a real copy, not a symlink",
        )
        # The copy carries the target's actual (undecodable) bytes.
        with open(quarantined, "rb") as handle:
            self.assertEqual(handle.read(), b"this is not a JPEG")

        # The target is untouched and still present at its original path.
        self.assertTrue(
            os.path.isfile(target) and not os.path.islink(target),
            "the undecodable target must remain in place, unmodified",
        )
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"this is not a JPEG")
        # Only the link left export/.
        self.assertFalse(
            os.path.lexists(link), "the symlink should be gone from export/"
        )
        # The quarantined copy survives the target being deleted afterwards.
        os.remove(target)
        self.assertTrue(os.path.isfile(quarantined))

    # ------------------------------------------------------------------ #
    # 4. Guard: a plain (non-symlink) photo still processes correctly     #
    # ------------------------------------------------------------------ #

    def test_regular_photo_still_moves_normally(self):
        """A normal regular photo is still moved (not copied) and drains export/."""
        make_exif_jpeg(
            self._export_path("IMG_0001.jpg"),
            date_time_original="2024:01:15 14:30:45",
            color="green",
            size=(48, 48),
        )

        self.processor.process_all_files(dry_run=False)

        landed = self._backup_path("photos", "2024.01.15.14.30.45.jpg")
        self.assertTrue(os.path.isfile(landed))
        self.assertFalse(os.path.islink(landed))
        self._assert_pixel_near(landed, (0, 128, 0))
        # A regular file is moved, so export/ is fully drained.
        self.assertEqual(os.listdir(self.export_dir), [])


if __name__ == "__main__":
    unittest.main()
