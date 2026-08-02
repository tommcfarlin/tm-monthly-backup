"""
Dry-run / real-run parity tests (issue #10).

``--dry-run`` is the only safety mechanism a user has before this tool
irreversibly renames, moves, and deletes their photo library, so a dry run must
predict *exactly* what a real run would do. The single legitimate difference
between the two modes is whether the side effect (move/copy/remove/mkdir) is
performed -- never the planned destination, the resolved collision name, the
converted HEIC extension, or the quarantine/unknown routing.

The definitive test here builds a mixed input set -- a photo with EXIF, a HEIC,
a screenshot, a video, an unknown ``.xyz``, an undecodable ``.jpg``, and two
photos that collide on the same timestamp -- and asserts that the set of planned
destinations a DRY run reports (captured from its own log output) equals the set
of relative paths a REAL run actually produces in a separate backup tree.

The remaining tests pin the two symptoms issue #10 called out explicitly (the
HEIC extension and collision-bumped names) and prove a dry run performs zero
disk side effects.
"""

import logging
import os
import shutil
import tempfile
import unittest
from typing import List, Set
from unittest.mock import patch

from src.file_processor import FileProcessor
from tests.fixtures import make_exif_heic, make_exif_jpeg


class _PlanCapture(logging.Handler):
    """A logging handler that records the destinations a dry run reports.

    Every landing a dry run plans is announced on the ``src.file_processor``
    logger as ``[DRY RUN] ... <source> -> <destination>`` (a timestamped move,
    an unknown-file move, or a quarantine). This handler keeps the raw,
    pre-rendering ``record.getMessage()`` text of exactly those lines so a test
    can recover the planned destination paths without parsing console output.
    """

    def __init__(self) -> None:
        super().__init__()
        self.targets: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("[DRY RUN]") and " -> " in message:
            self.targets.append(message.rsplit(" -> ", 1)[1].strip())


def _build_mixed_export(export_dir: str) -> None:
    """Populate ``export_dir`` with one file of every routing outcome.

    The set deliberately spans every branch dry-run parity must cover: an
    EXIF-timestamped photo, a HEIC (converted to ``.jpg``), a filename-dated
    screenshot and video, an unrecognized ``.xyz``, an undecodable ``.jpg``
    (quarantined), and two photos sharing one timestamp (collision resolution).

    Args:
        export_dir: An existing, empty directory to fill with fixtures.
    """
    # 1. Photo with real EXIF -> photos/2024.01.15.14.30.45.jpg
    make_exif_jpeg(
        os.path.join(export_dir, "IMG_0001.jpg"),
        date_time_original="2024:01:15 14:30:45",
        color="green",
    )
    # 2. HEIC -> converted, lands as photos/2022.03.04.05.06.07.jpg
    make_exif_heic(
        os.path.join(export_dir, "IMG_0002.HEIC"),
        date_time_original="2022:03:04 05:06:07",
        color="blue",
    )
    # 3. Screenshot (PNG, name marks it + carries a fallback date) ->
    #    screenshots/2023.05.06.07.08.09.png
    from PIL import Image
    Image.new("RGB", (16, 16), color="red").save(
        os.path.join(export_dir, "Screenshot_2023-05-06-07-08-09.png"),
        format="PNG",
    )
    # 4. Video (name carries a fallback date) -> videos/2021.02.03.04.05.06.mov
    with open(os.path.join(export_dir, "VID_2021-02-03-04-05-06.mov"), "wb") as handle:
        handle.write(b"not a real video" * 4)
    # 5. Unknown extension -> unknown/mystery.xyz (name preserved)
    with open(os.path.join(export_dir, "mystery.xyz"), "wb") as handle:
        handle.write(b"who knows what this is")
    # 6. Undecodable image-typed file -> corrupt/broken.jpg (name preserved)
    with open(os.path.join(export_dir, "broken.jpg"), "wb") as handle:
        handle.write(b"this is definitely not a JPEG")
    # 7 & 8. Two photos that collide on one timestamp -> one bumped a second.
    make_exif_jpeg(
        os.path.join(export_dir, "IMG_0010.jpg"),
        date_time_original="2020:06:06 06:06:06",
        color="green",
    )
    make_exif_jpeg(
        os.path.join(export_dir, "IMG_0011.jpg"),
        date_time_original="2020:06:06 06:06:06",
        color="green",
    )


def _relative_backup_tree(backup_dir: str) -> Set[str]:
    """Return every file under ``backup_dir`` as a path relative to it.

    Each entry is ``<category>/<filename>`` (e.g. ``photos/2024.01.15...jpg``),
    which is exactly the ``(category, target)`` pairing the parity comparison
    turns on. Empty category directories a real run pre-creates contribute
    nothing, since only files are collected.
    """
    found: Set[str] = set()
    for dirpath, _dirs, filenames in os.walk(backup_dir):
        for filename in filenames:
            abs_path = os.path.join(dirpath, filename)
            found.add(os.path.relpath(abs_path, backup_dir))
    return found


class TestDryRunParity(unittest.TestCase):
    """Assert a dry run plans exactly what a real run performs."""

    def setUp(self) -> None:
        """Create isolated temp trees for the dry run and the real run."""
        self.temp_dir = tempfile.mkdtemp()
        # Dry-run world.
        self.dry_export = os.path.join(self.temp_dir, "dry_export")
        self.dry_backup = os.path.join(self.temp_dir, "dry_backup")
        # Real-run world (a separate, identical input set).
        self.real_export = os.path.join(self.temp_dir, "real_export")
        self.real_backup = os.path.join(self.temp_dir, "real_backup")
        for path in (self.dry_export, self.real_export):
            os.makedirs(path, exist_ok=True)

    def tearDown(self) -> None:
        """Remove the temp tree."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _capture_dry_run_plan(self, export_dir: str, backup_dir: str) -> Set[str]:
        """Run a dry run and return the planned destinations, relative to backup.

        The plan is recovered from the processor's own ``[DRY RUN] ... -> dest``
        log lines -- the exact output a user relies on -- then made relative to
        ``backup_dir`` so it can be compared against a real run's on-disk tree.
        """
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

    def test_dry_run_plan_matches_real_run_landings(self):
        """The definitive parity check across every routing category.

        A dry run's reported plan (from its logs) must equal the set of relative
        paths a real run actually lands in a fresh backup tree, for the same
        mixed input set. If any category diverges -- a HEIC reported as ``.heic``,
        a collision left unresolved, a quarantine mis-routed -- the sets differ.
        """
        _build_mixed_export(self.dry_export)
        _build_mixed_export(self.real_export)

        dry_plan = self._capture_dry_run_plan(self.dry_export, self.dry_backup)

        real_processor = FileProcessor(self.real_export, self.real_backup)
        real_processor.process_all_files(dry_run=False)
        real_tree = _relative_backup_tree(self.real_backup)

        # Guard against a vacuous pass: both sides must actually contain the
        # eight expected landings before we trust their equality.
        expected = {
            os.path.join("photos", "2024.01.15.14.30.45.jpg"),
            os.path.join("photos", "2022.03.04.05.06.07.jpg"),
            os.path.join("screenshots", "2023.05.06.07.08.09.png"),
            os.path.join("videos", "2021.02.03.04.05.06.mov"),
            os.path.join("unknown", "mystery.xyz"),
            os.path.join("corrupt", "broken.jpg"),
            os.path.join("photos", "2020.06.06.06.06.06.jpg"),
            os.path.join("photos", "2020.06.06.06.06.07.jpg"),
        }
        self.assertEqual(
            real_tree, expected, "real run did not land the expected mixed set"
        )
        self.assertEqual(
            dry_plan,
            real_tree,
            "dry-run plan diverged from what the real run produced",
        )

    def test_heic_dry_run_reports_the_jpeg_a_real_run_lands(self):
        """A dry run over a HEIC reports a ``.jpg`` target, matching the real run.

        This pins issue #10's headline symptom: the dry run must not report the
        original ``.heic`` extension for a file the tool converts to JPEG.
        """
        make_exif_heic(
            os.path.join(self.dry_export, "IMG_ONLY.HEIC"),
            date_time_original="2022:03:04 05:06:07",
            color="blue",
        )
        make_exif_heic(
            os.path.join(self.real_export, "IMG_ONLY.HEIC"),
            date_time_original="2022:03:04 05:06:07",
            color="blue",
        )

        dry_plan = self._capture_dry_run_plan(self.dry_export, self.dry_backup)

        real_processor = FileProcessor(self.real_export, self.real_backup)
        real_processor.process_all_files(dry_run=False)
        real_tree = _relative_backup_tree(self.real_backup)

        expected = {os.path.join("photos", "2022.03.04.05.06.07.jpg")}
        self.assertEqual(dry_plan, expected)
        self.assertEqual(real_tree, expected)
        # And explicitly: nothing in the plan carries the source extension.
        self.assertFalse(
            any(target.lower().endswith(".heic") for target in dry_plan),
            "dry run reported a .heic target for a file that converts to .jpg",
        )

    def test_collision_dry_run_reports_the_bumped_names_a_real_run_produces(self):
        """Colliding timestamps report the same resolved names in both modes.

        Three photos share one capture second; a real run resolves them to
        ``...06``, ``...07``, ``...08``. The dry run must report the identical
        bumped set rather than three copies of the unadjusted name.
        """
        for export_dir in (self.dry_export, self.real_export):
            for index in range(3):
                make_exif_jpeg(
                    os.path.join(export_dir, f"IMG_100{index}.jpg"),
                    date_time_original="2020:06:06 06:06:06",
                    color="green",
                )

        dry_plan = self._capture_dry_run_plan(self.dry_export, self.dry_backup)

        real_processor = FileProcessor(self.real_export, self.real_backup)
        real_processor.process_all_files(dry_run=False)
        real_tree = _relative_backup_tree(self.real_backup)

        expected = {
            os.path.join("photos", "2020.06.06.06.06.06.jpg"),
            os.path.join("photos", "2020.06.06.06.06.07.jpg"),
            os.path.join("photos", "2020.06.06.06.06.08.jpg"),
        }
        self.assertEqual(real_tree, expected)
        self.assertEqual(dry_plan, expected)

    def test_dry_run_touches_nothing_on_disk(self):
        """A dry run performs zero moves/copies/removes and creates no backup tree.

        The mutating primitives are patched to prove they are never called, and
        the backup directory is asserted to remain absent -- no category
        subdirectories, no placeholder files, nothing.
        """
        _build_mixed_export(self.dry_export)
        processor = FileProcessor(self.dry_export, self.dry_backup)

        with patch("src.file_processor.shutil.move") as mock_move, \
                patch("src.file_processor.shutil.copy2") as mock_copy2, \
                patch("src.file_processor.os.remove") as mock_remove, \
                patch("src.file_processor.os.makedirs") as mock_makedirs:
            processor.process_all_files(dry_run=True)

        self.assertEqual(mock_move.call_count, 0, "dry run moved a file")
        self.assertEqual(mock_copy2.call_count, 0, "dry run copied a file")
        self.assertEqual(mock_remove.call_count, 0, "dry run removed a file")
        self.assertEqual(
            mock_makedirs.call_count, 0, "dry run created a directory"
        )
        # The backup tree was never materialized at all.
        self.assertFalse(
            os.path.exists(self.dry_backup),
            "dry run created the backup directory tree",
        )
        # And every source file is still sitting in export, untouched.
        self.assertEqual(
            len(os.listdir(self.dry_export)),
            8,
            "dry run altered the export directory",
        )


if __name__ == "__main__":
    unittest.main()
