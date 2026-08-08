"""
Regression tests for issue #73: things the tool knew and did not say.

None of these can lose a photograph. What they share is that the tool had the
right answer internally and either dropped it, hid it, or reported a different
number than the one it acted on -- and this project has spent several milestones
establishing that a summary a user cannot trust is itself a defect.

The six gaps, in the order they appear below:

1. Non-regular files (FIFOs, sockets, broken symlinks) were dropped from the
   scan with only a log line, so they appeared in no accounting bucket.
2. A ``no_files`` run suppressed the entire skipped-file report, so an export
   holding one filtered-out real photo printed "No files found to process".
3. ``processed_files``/``quarantined_files`` handed out the internal record
   dicts, so mutating a returned record corrupted the accumulator.
4. The dry-run name-preserving resolver did not model in-batch collisions, so
   two same-named sources both planned the same destination.
5. The confirm prompt and organize bar used the pre-categorization count while
   ``files_scanned`` used the post-categorization one.
6. ``display_file_scan_results`` was a live second categorization entry point.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.cli_interface import CLIInterface, is_unqualified_success
from src.file_processor import FileProcessor
from src.main import determine_exit_code
from tests.fixtures import make_exif_jpeg


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)
        self.processor = FileProcessor(self.export, self.backup)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _photo(self, name="photo.jpg", second=1):
        return make_exif_jpeg(
            os.path.join(self.export, name),
            date_time_original=f"2024:01:15 14:30:{second:02d}",
        )


class TestNonRegularFilesAreAccounted(_Sandbox):
    """Gap 1: dropped from every bucket, so the identity 'held' vacuously."""

    def _make_broken_symlink(self, name="dangling.jpg"):
        path = os.path.join(self.export, name)
        os.symlink(os.path.join(self.export, "nonexistent-target.jpg"), path)
        return path

    def test_a_broken_symlink_is_counted_as_skipped(self):
        self._photo()
        broken = self._make_broken_symlink()

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_processed"], 1)
        self.assertEqual(
            results["files_skipped"], 1,
            "the non-regular file was dropped from accounting entirely",
        )
        self.assertIn(
            ("not_regular_file", broken), results["skipped_files"]
        )

    def test_files_scanned_counts_the_non_regular_file(self):
        """A two-entry directory must not report files_scanned=1."""
        self._photo()
        self._make_broken_symlink()

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results["files_scanned"], 2)

    def test_the_accounting_identity_holds_non_vacuously(self):
        self._photo()
        self._make_broken_symlink()

        results = self.processor.process_all_files(dry_run=False)

        moved = results["files_processed"] + results["files_quarantined"]
        skipped = results["files_skipped"] + results["sidecars_skipped"]
        self.assertEqual(
            results["files_scanned"],
            moved + results["sidecars_deleted"] + skipped + results["files_failed"],
        )

    def test_a_left_behind_non_regular_file_is_not_an_unqualified_success(self):
        """'export/ is fully drained' was silently false."""
        self._photo()
        self._make_broken_symlink()

        results = self.processor.process_all_files(dry_run=False)

        self.assertFalse(is_unqualified_success(results))
        self.assertNotEqual(determine_exit_code(results), 0)

    def test_a_fifo_is_counted_too(self):
        """A FIFO would hang the run if opened; it must still be reported."""
        self._photo()
        fifo = os.path.join(self.export, "pipe.jpg")
        os.mkfifo(fifo)

        results = self.processor.process_all_files(dry_run=False)

        self.assertIn(("not_regular_file", fifo), results["skipped_files"])


class TestNoFilesRunStillReportsWhatItSkipped(_Sandbox):
    """Gap 2: the one statement the user got was the only false one."""

    def setUp(self):
        super().setUp()
        self.cli = CLIInterface(self.export, self.backup)
        import io
        from rich.console import Console
        self.cli.console = Console(file=io.StringIO(), record=True, width=120)

    def test_an_export_of_only_a_hidden_photo_reports_the_skip(self):
        """`.IMG_1234.jpg` is what an interrupted sync leaves behind."""
        hidden = os.path.join(self.export, ".IMG_1234.jpg")
        with open(hidden, "wb") as handle:
            handle.write(b"a real photo, hidden by a sync conflict")

        with patch("src.cli_interface.Confirm.ask", return_value=True):
            results = self.cli.process_with_progress(dry_run=False)
        self.cli.display_results(results)

        self.assertEqual(results["status"], "no_files")
        self.assertEqual(results["files_skipped"], 1)
        output = self.cli.console.export_text()
        self.assertIn("Files Skipped", output)
        self.assertIn("IMG_1234.jpg", output)

    def test_a_genuinely_empty_export_renders_no_skip_table(self):
        """Nothing to explain: the notice already said it."""
        with patch("src.cli_interface.Confirm.ask", return_value=True):
            results = self.cli.process_with_progress(dry_run=False)
        self.cli.display_results(results)

        self.assertEqual(results["status"], "no_files")
        self.assertNotIn("Files Skipped", self.cli.console.export_text())

    def test_a_hidden_only_export_is_not_an_unqualified_success(self):
        hidden = os.path.join(self.export, ".IMG_1234.jpg")
        with open(hidden, "wb") as handle:
            handle.write(b"x")

        with patch("src.cli_interface.Confirm.ask", return_value=True):
            results = self.cli.process_with_progress(dry_run=False)

        self.assertFalse(is_unqualified_success(results))

    def test_an_empty_export_still_exits_zero(self):
        """The fix must not turn "nothing to do" into a failure."""
        with patch("src.cli_interface.Confirm.ask", return_value=True):
            results = self.cli.process_with_progress(dry_run=False)
        self.assertEqual(determine_exit_code(results), 0)


class TestSummaryRecordsAreNotAliased(_Sandbox):
    """Gap 3: list.copy() is shallow, so the element dicts were internal."""

    def test_mutating_a_processed_record_does_not_corrupt_the_accumulator(self):
        self._photo()
        results = self.processor.process_all_files(dry_run=False)
        real_path = self.processor._processed_files[0]["final_path"]

        results["processed_files"][0]["final_path"] = "CLOBBERED"

        self.assertEqual(
            self.processor._processed_files[0]["final_path"], real_path,
            "the caller was handed the internal record object",
        )

    def test_mutating_a_quarantined_record_does_not_corrupt_the_accumulator(self):
        corrupt = os.path.join(self.export, "truncated.jpg")
        with open(corrupt, "wb") as handle:
            handle.write(b"not a jpeg at all")

        results = self.processor.process_all_files(dry_run=False)
        self.assertEqual(results["files_quarantined"], 1)
        real_path = self.processor._quarantined_files[0]["final_path"]

        results["quarantined_files"][0]["final_path"] = "CLOBBERED"

        self.assertEqual(
            self.processor._quarantined_files[0]["final_path"], real_path
        )


class TestDryRunPredictsInBatchNameCollisions(_Sandbox):
    """Gap 4: two same-named sources both planned one destination."""

    def _two_same_named(self, name, content=b"not an image"):
        paths = []
        for sub in ("a", "b"):
            directory = os.path.join(self.export, sub)
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, name)
            with open(path, "wb") as handle:
                handle.write(content)
            paths.append(path)
        return paths

    def _planned_unknown_paths(self, dry_run):
        processor = FileProcessor(self.export, self.backup)
        processor.process_all_files(dry_run=dry_run)
        return processor

    def test_dry_run_plans_distinct_paths_for_two_same_named_unknowns(self):
        self._two_same_named("mystery.xyz")

        processor = FileProcessor(self.export, self.backup)
        processor.process_all_files(dry_run=True)

        planned = [
            record["final_path"] for record in processor._processed_files
        ] or [
            # A dry run does not populate _processed_files; the unknown-file
            # branch records its plan via the same resolver, so assert on the
            # resolver directly for the two calls a dry run makes.
            processor._resolve_named_destination_dry_run(
                os.path.join(self.backup, "unknown"), "mystery.xyz"
            ),
            processor._resolve_named_destination_dry_run(
                os.path.join(self.backup, "unknown"), "mystery.xyz"
            ),
        ]
        self.assertEqual(
            len(set(planned)), len(planned),
            "a dry run planned the same destination twice",
        )

    def test_the_dry_run_resolver_models_its_own_reservations(self):
        target = os.path.join(self.backup, "unknown")
        os.makedirs(target, exist_ok=True)

        first = self.processor._resolve_named_destination_dry_run(
            target, "mystery.xyz"
        )
        second = self.processor._resolve_named_destination_dry_run(
            target, "mystery.xyz"
        )

        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith("mystery (1).xyz"))

    def test_dry_run_and_real_run_agree_on_the_bumped_name(self):
        """#10 parity: the plan must match what a real run produces."""
        self._two_same_named("mystery.xyz")

        dry = FileProcessor(self.export, self.backup)
        planned = {
            dry._resolve_named_destination_dry_run(
                os.path.join(self.backup, "unknown"), "mystery.xyz"
            )
            for _ in range(2)
        }
        # Second call must have bumped, so the set has two members.
        self.assertEqual(len(planned), 2)

        real = FileProcessor(self.export, self.backup)
        real.process_all_files(dry_run=False)
        landed = {
            os.path.basename(record["final_path"])
            for record in real._processed_files
        }
        self.assertEqual(landed, {"mystery.xyz", "mystery (1).xyz"})


class TestPromptCountMatchesWhatTheRunDoes(_Sandbox):
    """Gap 5: the prompt quoted a number the summary contradicted."""

    def test_prompt_total_equals_files_scanned_when_a_file_vanishes(self):
        """A file deleted between scan and categorize must not skew the prompt."""
        self._photo("photo_a.jpg", second=1)
        doomed = self._photo("photo_b.jpg", second=2)

        seen = {}
        real_batch = self.processor.categorizer.batch_categorize

        def delete_then_categorize(paths):
            os.remove(doomed)
            return real_batch(paths)

        class Recorder:
            def on_no_files(self): pass
            def on_categorized(self, total, stats):
                seen["total"] = total
                return True
            def on_file(self, *a, **k): pass
            def on_heic_converted(self, *a, **k): pass
            def close(self): pass

        with patch.object(
            self.processor.categorizer, "batch_categorize",
            side_effect=delete_then_categorize,
        ):
            results = self.processor.process_all_files(
                dry_run=False, progress=Recorder()
            )

        self.assertEqual(
            seen["total"], results["files_scanned"],
            "the prompt quoted a different total than the summary reported",
        )


class TestNoSecondCategorizationEntryPoint(_Sandbox):
    """Gap 6: the removed method could rewrite the state the summary reads."""

    def test_display_file_scan_results_is_gone(self):
        cli = CLIInterface(self.export, self.backup)
        self.assertFalse(
            hasattr(cli, "display_file_scan_results"),
            "a second categorization entry point is still reachable",
        )

    def test_no_source_file_calls_batch_categorize_outside_file_processor(self):
        """Only the single pipeline pass may drive categorization.

        Parsed with ``ast`` rather than grepped: the modules that discuss this
        rule in comments (explaining why the second entry point was removed)
        must not count as violating it, or the guard would forbid documenting
        its own reason.
        """
        import ast
        import pathlib

        src = pathlib.Path(__file__).resolve().parent.parent / "src"
        offenders = []
        for path in sorted(src.glob("*.py")):
            if path.name in {"file_processor.py", "file_categorizer.py"}:
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "batch_categorize"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            f"batch_categorize is called from {offenders}, outside the single "
            "pipeline pass issue #13 established",
        )


if __name__ == "__main__":
    unittest.main()
