"""
Hidden-file accounting and banner-honesty tests (issue #30).

Before this fix, ``FileProcessor._scan_export_directory`` dropped any file
whose basename started with a dot before it ever entered ``all_files``: the
file was absent from ``batch_categorize``, from
``get_categorization_stats()['total']``, from ``failed_files``, and from
``missing_exif_list`` alike -- there was no number anywhere in the results
dict from which a user could infer that anything had been skipped, and a
run over a real hidden photo with valid EXIF exited ``0`` and printed "All
files processed successfully!" while the photo sat untouched in ``export/``.

These tests pin:

* A hidden file (junk or not) is never silently absent from every count --
  it is recorded in ``skipped_files``/``files_skipped`` (acceptance criteria
  1 and 2).
* The dotted-*file* policy now matches the already-fixed dotted-*directory*
  policy (issue #56) -- acceptance criterion 3. This one is regression
  coverage: issue #56 already made both levels say "ignore," so this test
  cannot fail against the code on this branch; it is here to pin the
  combination explicitly, as the issue's own acceptance criteria ask for.
* The accounting identity
  ``files_scanned == (files_processed + files_quarantined) + sidecars_deleted
  + (files_skipped + sidecars_skipped) + files_failed`` holds after a mixed
  non-dry run (acceptance criterion 4).
* Neither the results banner nor ``main.py``'s standalone success line claims
  an unqualified "All files processed successfully!" while an UNEXPECTED
  (non-junk) file remains in ``export/`` (acceptance criterion 5) -- but a
  junk-only skip (``.DS_Store``/``.localized``/``Thumbs.db``) does NOT
  withhold that claim (fix round 1): essentially every macOS export tree
  carries a ``.DS_Store``, so treating it as disqualifying would make the
  unqualified-success banner never fire in ordinary use, the same reasoning
  behind issue #39's DEBUG-not-WARNING call and issue #57's own banner
  demotion being acceptable specifically because it cannot fire spuriously
  for a user's real (valid) sidecars. The decision lives in exactly one place
  -- ``cli_interface.is_unqualified_success`` -- consulted by both
  ``CLIInterface.display_results`` and ``main.py`` rather than each
  recomputing its own answer.
* Dry-run parity (issue #10): a dry run makes the same skip decisions, with
  the same counts, a real run over the same input would.

All filesystem I/O happens under ``tempfile.mkdtemp`` -- these tests never
touch a real ``export/`` or ``backup/``.
"""

import os
import shutil
import tempfile
import unittest

from click.testing import CliRunner

from src.file_processor import FileProcessor
from src.cli_interface import CLIInterface
from src.main import main
from tests.fixtures import make_exif_heic, make_exif_jpeg


class TestHiddenFileIsCountedNotDropped(unittest.TestCase):
    """Acceptance criteria 1 & 2: a hidden photo and .DS_Store are counted."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _backup_tree(self):
        found = set()
        for dirpath, _dirs, filenames in os.walk(self.backup_dir):
            for filename in filenames:
                found.add(
                    os.path.relpath(os.path.join(dirpath, filename), self.backup_dir)
                )
        return found

    def test_reproduces_the_issue_scenario_end_to_end(self):
        """The issue's own repro: one ordinary photo, one hidden photo, .DS_Store.

        Fails against the pre-fix code with a ``KeyError`` on
        ``results['files_skipped']`` (the key does not exist at all), and --
        even if the key were merely stubbed to ``0`` -- the hidden photo would
        still be absent from ``skipped_files``, ``files_processed``,
        ``files_failed``, and every other bucket, which is exactly the
        "absent from every count" defect this issue fixes.
        """
        ordinary = make_exif_jpeg(
            os.path.join(self.export_dir, "IMG_0001.jpg"),
            date_time_original="2024:01:15 14:30:45",
            color="green",
        )
        hidden = make_exif_jpeg(
            os.path.join(self.export_dir, ".hidden_photo.jpg"),
            date_time_original="2024:02:02 02:02:02",
            color="blue",
        )
        ds_store = os.path.join(self.export_dir, ".DS_Store")
        with open(ds_store, "wb") as handle:
            handle.write(b"junk")

        results = self.processor.process_all_files(dry_run=False)

        # The ordinary photo is processed and lands in backup/.
        self.assertEqual(results["files_processed"], 1)
        self.assertEqual(
            self._backup_tree(),
            {os.path.join("photos", "2024.01.15.14.30.45.jpg")},
        )

        # The hidden photo and .DS_Store are both accounted for as skipped --
        # never silently absent from every count.
        self.assertEqual(results["files_skipped"], 2)
        skipped_by_path = {path: reason for reason, path in results["skipped_files"]}
        self.assertEqual(skipped_by_path[hidden], "hidden")
        self.assertEqual(skipped_by_path[ds_store], "junk")

        # Neither skipped file was moved -- both are still exactly where they
        # were in export/, and the backup tree does not contain either.
        self.assertTrue(os.path.isfile(hidden), "hidden photo was moved out of export/")
        self.assertTrue(os.path.isfile(ds_store), ".DS_Store was moved out of export/")
        self.assertNotIn(
            os.path.join("photos", "2024.02.02.02.02.02.jpg"), self._backup_tree()
        )

    def test_ds_store_alone_is_skipped_and_counted(self):
        """Acceptance criterion 2, isolated: .DS_Store is skipped AND counted."""
        ds_store = os.path.join(self.export_dir, ".DS_Store")
        with open(ds_store, "wb") as handle:
            handle.write(b"junk")

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_skipped"], 1)
        self.assertEqual(results["skipped_files"], [("junk", ds_store)])
        self.assertTrue(os.path.isfile(ds_store))

    def test_localized_and_thumbs_db_are_denylisted_junk(self):
        """The denylist covers all three named junk files, not just .DS_Store."""
        localized = os.path.join(self.export_dir, ".localized")
        thumbs = os.path.join(self.export_dir, "Thumbs.db")
        for junk_path in (localized, thumbs):
            with open(junk_path, "wb") as handle:
                handle.write(b"junk")

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_skipped"], 2)
        reasons = {reason for reason, _path in results["skipped_files"]}
        self.assertEqual(reasons, {"junk"})

    def test_denylist_matching_is_case_insensitive(self):
        """Junk names arrive in whatever case the source filesystem used.

        A Windows-originated export can carry ``thumbs.db`` lowercase, and
        macOS's case-insensitive APFS will hand back ``.ds_store``. A
        case-sensitive match would let those fall through to ``UNKNOWN`` and
        file them into ``backup/unknown/`` as though they were the user's own
        data -- reported, but reported as something they are not.
        """
        # Directories are numbered, not named after the file: this machine's
        # APFS volume is itself case-insensitive, so `export_.DS_STORE` and
        # `export_.ds_store` would collide and the fixture would fail before
        # the assertion ran.
        for i, name in enumerate((".DS_STORE", ".ds_store", "THUMBS.DB",
                                  "thumbs.db", ".Localized")):
            with self.subTest(name=name):
                export = os.path.join(self.temp_dir, f"export_case{i}")
                backup = os.path.join(self.temp_dir, f"backup_case{i}")
                os.makedirs(export)
                with open(os.path.join(export, name), "w") as fh:
                    fh.write("junk")

                processor = FileProcessor(export, backup)
                results = processor.process_all_files(dry_run=False)

                self.assertEqual(
                    results["files_skipped"], 1,
                    f"{name} was not skipped as junk",
                )
                self.assertEqual(
                    [reason for reason, _ in results["skipped_files"]], ["junk"],
                    f"{name} was skipped but not labelled junk",
                )
                self.assertFalse(
                    os.path.isdir(os.path.join(backup, "unknown")),
                    f"{name} was filed into backup/unknown/ instead of skipped",
                )


class TestDottedDirectoryMatchesDottedFilePolicy(unittest.TestCase):
    """Acceptance criterion 3 (regression coverage -- see module docstring).

    Issue #56 already pruned hidden directories with
    ``dirs[:] = [d for d in dirs if not d.startswith('.')]``
    (``src/file_processor.py``). This test cannot fail on this branch; it
    exists because the issue's acceptance criteria explicitly ask for a test
    pinning that a dotted directory and a dotted file are handled the same
    way (both "ignore, do not archive").
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_dotted_directory_contents_and_a_dotted_file_are_both_ignored(self):
        """export/.hidden_dir/photo.jpg and export/.hidden.jpg are both left."""
        hidden_dir_photo_dir = os.path.join(self.export_dir, ".hidden_dir")
        os.makedirs(hidden_dir_photo_dir, exist_ok=True)
        hidden_dir_photo = make_exif_jpeg(
            os.path.join(hidden_dir_photo_dir, "photo.jpg"),
            date_time_original="2020:01:01 01:01:01",
            color="red",
        )
        hidden_file = make_exif_jpeg(
            os.path.join(self.export_dir, ".hidden.jpg"),
            date_time_original="2020:02:02 02:02:02",
            color="blue",
        )

        results = self.processor.process_all_files(dry_run=False)

        # Neither the dotted directory's contents nor the dotted top-level
        # file were archived.
        self.assertEqual(results["files_processed"], 0)
        self.assertTrue(os.path.isfile(hidden_dir_photo))
        self.assertTrue(os.path.isfile(hidden_file))
        # The dotted directory's contents are pruned before they are ever
        # individually visited (issue #56), so only the top-level dotted
        # FILE is recorded in skipped_files -- the directory's contents were
        # never scanned at all, matching how a pruned subtree's contents are
        # never categorized either.
        self.assertEqual(results["files_skipped"], 1)
        self.assertEqual(results["skipped_files"][0][1], hidden_file)


class TestAccountingIdentity(unittest.TestCase):
    """Acceptance criterion 4: scanned == moved + deleted + skipped + failed."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_aae(
        self,
        name,
        content=b'<?xml version="1.0"?><plist version="1.0"><dict/></plist>',
    ):
        path = os.path.join(self.export_dir, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def test_identity_holds_across_every_outcome_after_a_real_run(self):
        """One file in each bucket; the identity must hold exactly.

        Fails against the pre-fix code with a ``KeyError`` on
        ``results['files_scanned']``/``results['files_skipped']`` (neither
        key exists yet).
        """
        # Processed: an ordinary photo.
        make_exif_jpeg(
            os.path.join(self.export_dir, "good.jpg"),
            date_time_original="2024:01:01 01:01:01",
            color="green",
        )
        # Failed: a HEIC whose conversion we force to fail.
        make_exif_heic(
            os.path.join(self.export_dir, "broken.heic"),
            date_time_original="2024:01:02 02:02:02",
        )
        # Sidecar deleted: a genuine plist sidecar.
        self._write_aae("good.aae")
        # Sidecar skipped: an .aae that is not actually a plist.
        self._write_aae("fake.aae", content=b"just some text, not a plist")
        # Scan-skipped, junk: .DS_Store.
        ds_store = os.path.join(self.export_dir, ".DS_Store")
        with open(ds_store, "wb") as handle:
            handle.write(b"junk")
        # Scan-skipped, hidden: a dotted (non-junk) file.
        with open(os.path.join(self.export_dir, ".hidden_note.txt"), "wb") as handle:
            handle.write(b"not a photo")

        from unittest.mock import patch

        with patch.object(
            self.processor.heic_converter, "convert_heic_to_jpeg", return_value=None
        ):
            results = self.processor.process_all_files(dry_run=False)

        # Sanity: every bucket this test set out to populate is actually
        # non-zero, so the identity check below is not vacuously true.
        self.assertEqual(results["files_processed"], 1)
        self.assertEqual(results["files_failed"], 1)
        self.assertEqual(results["sidecars_deleted"], 1)
        self.assertEqual(results["sidecars_skipped"], 1)
        self.assertEqual(results["files_skipped"], 2)
        self.assertEqual(results["files_quarantined"], 0)

        scanned = results["files_scanned"]
        moved = results["files_processed"] + results["files_quarantined"]
        deleted = results["sidecars_deleted"]
        skipped = results["files_skipped"] + results["sidecars_skipped"]
        failed = results["files_failed"]

        self.assertEqual(
            scanned,
            moved + deleted + skipped + failed,
            "files_scanned must equal moved + deleted + skipped + failed",
        )
        # And concretely: 1 processed + 1 failed + 1 deleted-sidecar +
        # 1 skipped-sidecar + 2 scan-skipped = 6 paths on disk.
        self.assertEqual(scanned, 6)


class TestSkippedFileDemotesTheSuccessBanner(unittest.TestCase):
    """Acceptance criterion 5: no unqualified success while a file remains."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_display_results_title_is_not_success_when_a_file_is_skipped(self):
        """The results table title reads "Files Skipped," not "Success!".

        Fails against the pre-fix code: the old ``display_results`` has no
        ``files_skipped`` branch at all, so it falls through to the
        unqualified "Success!" title for this same results dict.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "good.jpg"),
            date_time_original="2024:03:03 03:03:03",
            color="green",
        )
        with open(os.path.join(self.export_dir, ".hidden.jpg"), "wb") as handle:
            handle.write(b"not really a photo")

        results = self.processor.process_all_files(dry_run=False)
        self.assertGreater(results["files_skipped"], 0)  # precondition

        cli = CLIInterface(self.export_dir, self.backup_dir)
        with cli.console.capture() as capture:
            cli.display_results({**results, "status": "completed"}, dry_run=False)
        rendered = capture.get()

        self.assertNotIn("Success!", rendered)
        self.assertIn("Files Skipped", rendered)

    def test_main_does_not_print_unqualified_success_with_a_skipped_file(self):
        """The CLI's standalone success line is withheld too.

        Fails against the pre-fix code: ``main.py`` printed "All files
        processed successfully!" whenever ``files_processed > 0`` and there
        was no per-file failure, with no awareness of a skipped file at all.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "good.jpg"),
            date_time_original="2024:04:04 04:04:04",
            color="green",
        )
        with open(os.path.join(self.export_dir, ".hidden.jpg"), "wb") as handle:
            handle.write(b"not really a photo")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--export-dir", self.export_dir,
                "--backup-dir", self.backup_dir,
                "--yes",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("All files processed successfully", result.output)
        self.assertIn("Files Skipped", result.output)

    def test_display_results_title_still_reads_success_for_a_junk_only_skip(self):
        """A .DS_Store-only skip must NOT withhold the "Success!" title.

        Every macOS export tree carries a `.DS_Store`; if that alone withheld
        the headline it would never read "Success!" in ordinary use and the
        signal would carry no information (fix round 1). The skip is still
        fully counted and named in its own row/detail table -- only the
        headline is unaffected.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "good.jpg"),
            date_time_original="2024:03:30 03:30:03",
            color="green",
        )
        ds_store = os.path.join(self.export_dir, ".DS_Store")
        with open(ds_store, "wb") as handle:
            handle.write(b"junk")

        results = self.processor.process_all_files(dry_run=False)
        self.assertEqual(results["files_skipped"], 1)  # precondition

        cli = CLIInterface(self.export_dir, self.backup_dir)
        with cli.console.capture() as capture:
            cli.display_results({**results, "status": "completed"}, dry_run=False)
        rendered = capture.get()

        self.assertIn("Success!", rendered)
        # The skip is still reported -- counting is unconditional, only the
        # headline's "clean run" claim is unaffected by junk.
        self.assertIn("Files Skipped", rendered)

    def test_main_prints_unqualified_success_for_a_junk_only_skip(self):
        """``main.py``'s standalone success line also survives a junk-only skip."""
        make_exif_jpeg(
            os.path.join(self.export_dir, "good.jpg"),
            date_time_original="2024:03:31 03:31:03",
            color="green",
        )
        with open(os.path.join(self.export_dir, ".DS_Store"), "wb") as handle:
            handle.write(b"junk")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--export-dir", self.export_dir,
                "--backup-dir", self.backup_dir,
                "--yes",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("All files processed successfully", result.output)
        self.assertIn("Files Skipped", result.output)

    def test_success_still_prints_with_no_skips_or_other_issues(self):
        """Guard against over-broadening: an ordinary clean run still says so."""
        make_exif_jpeg(
            os.path.join(self.export_dir, "good.jpg"),
            date_time_original="2024:05:05 05:05:05",
            color="green",
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--export-dir", self.export_dir,
                "--backup-dir", self.backup_dir,
                "--yes",
            ],
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("All files processed successfully", result.output)


class TestDryRunParityForSkippedFiles(unittest.TestCase):
    """Issue #10: a dry run reports the same skip decisions a real run would."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.dry_export = os.path.join(self.temp_dir, "dry_export")
        self.dry_backup = os.path.join(self.temp_dir, "dry_backup")
        self.real_export = os.path.join(self.temp_dir, "real_export")
        self.real_backup = os.path.join(self.temp_dir, "real_backup")
        for path in (self.dry_export, self.real_export):
            os.makedirs(path, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed(self, export_dir):
        make_exif_jpeg(
            os.path.join(export_dir, "good.jpg"),
            date_time_original="2024:06:06 06:06:06",
            color="green",
        )
        make_exif_jpeg(
            os.path.join(export_dir, ".hidden_photo.jpg"),
            date_time_original="2024:07:07 07:07:07",
            color="blue",
        )
        with open(os.path.join(export_dir, ".DS_Store"), "wb") as handle:
            handle.write(b"junk")

    def test_dry_run_reports_the_same_skip_counts_as_a_real_run(self):
        self._seed(self.dry_export)
        self._seed(self.real_export)

        dry_processor = FileProcessor(self.dry_export, self.dry_backup)
        dry_results = dry_processor.process_all_files(dry_run=True)

        real_processor = FileProcessor(self.real_export, self.real_backup)
        real_results = real_processor.process_all_files(dry_run=False)

        self.assertEqual(dry_results["files_skipped"], 2)
        self.assertEqual(
            dry_results["files_skipped"], real_results["files_skipped"]
        )
        dry_reasons = sorted(reason for reason, _path in dry_results["skipped_files"])
        real_reasons = sorted(reason for reason, _path in real_results["skipped_files"])
        self.assertEqual(dry_reasons, real_reasons)
        self.assertEqual(dry_reasons, ["hidden", "junk"])

        # A dry run performs zero mutation: both skipped files are still
        # exactly where they were.
        self.assertTrue(os.path.isfile(os.path.join(self.dry_export, ".hidden_photo.jpg")))
        self.assertTrue(os.path.isfile(os.path.join(self.dry_export, ".DS_Store")))


if __name__ == "__main__":
    unittest.main()
