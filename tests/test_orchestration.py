"""
Test suite pinning the single-public-API orchestration refactor (issue #13).

Before #13, ``CLIInterface.process_with_progress`` drove the pipeline by
reaching into ``FileProcessor`` private methods AND separately calling
``process_all_files``, so scanning, categorization, sidecar deletion, and
summary generation each ran twice per run. These tests pin the fix:

* Each of ``_scan_export_directory``, ``batch_categorize``,
  ``_delete_sidecar_files``, and ``_generate_summary`` runs EXACTLY ONCE per
  confirmed run -- they would each be >1 against the old double-invocation.
* The ``ProgressReporter`` seam consumed by issue #14 fires: ``on_categorized``
  once with the scanned total, and ``on_file`` once per processable file.
* A reporter that declines at ``on_categorized`` aborts the run before any
  sidecar is deleted or any file is processed (the confirmation seam).
"""

import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from rich.console import Console

from src.cli_interface import CLIInterface
from src.file_processor import FileProcessor, ProgressReporter
from tests.fixtures import make_exif_jpeg


def _recording_cli(export_dir, backup_dir):
    """Return a CLIInterface whose console records to an in-memory buffer."""
    cli = CLIInterface(export_dir, backup_dir)
    cli.console = Console(file=io.StringIO(), record=True, width=120)
    return cli


class _RecordingReporter(ProgressReporter):
    """A ProgressReporter that records every hook invocation."""

    def __init__(self, proceed=True):
        self.proceed = proceed
        self.no_files_calls = 0
        self.categorized_calls = []  # list of (total, stats)
        self.files = []              # list of (path, category, action)

    def on_no_files(self):
        self.no_files_calls += 1

    def on_categorized(self, total, stats):
        self.categorized_calls.append((total, dict(stats)))
        return self.proceed

    def on_file(self, path, category, action):
        self.files.append((path, category, action))


class TestExactlyOncePerRun(unittest.TestCase):
    """A confirmed run scans, categorizes, deletes sidecars, summarizes ONCE."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)
        # Two photos and a sidecar: exercises scan, categorize, sidecar delete,
        # and per-file processing together.
        make_exif_jpeg(
            os.path.join(self.export, "a.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        make_exif_jpeg(
            os.path.join(self.export, "b.jpg"),
            date_time_original="2024:02:16 15:31:46",
        )
        open(os.path.join(self.export, "a.aae"), "wb").close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run_counting(self, dry_run):
        """Run process_with_progress, returning the per-method call counts."""
        cli = _recording_cli(self.export, self.backup)
        processor = cli.processor
        counts = {}

        def counting(name, target, attr):
            original = getattr(target, attr)

            def wrapper(*args, **kwargs):
                counts[name] = counts.get(name, 0) + 1
                return original(*args, **kwargs)

            return patch.object(target, attr, side_effect=wrapper)

        with counting("scan", processor, "_scan_export_directory"), \
                counting("categorize", processor.categorizer, "batch_categorize"), \
                counting("sidecar", processor, "_delete_sidecar_files"), \
                counting("summary", processor, "_generate_summary"), \
                patch("src.cli_interface.Confirm.ask", return_value=True):
            cli.process_with_progress(dry_run=dry_run)

        return counts

    def test_real_run_invokes_each_stage_exactly_once(self):
        """A confirmed real run runs each pipeline stage exactly once."""
        counts = self._run_counting(dry_run=False)
        self.assertEqual(counts.get("scan"), 1, "scan ran more than once")
        self.assertEqual(counts.get("categorize"), 1, "categorization ran more than once")
        self.assertEqual(counts.get("sidecar"), 1, "sidecar deletion ran more than once")
        self.assertEqual(counts.get("summary"), 1, "summary generation ran more than once")

    def test_dry_run_invokes_each_stage_exactly_once(self):
        """A dry run runs each pipeline stage exactly once too."""
        counts = self._run_counting(dry_run=True)
        self.assertEqual(counts.get("scan"), 1)
        self.assertEqual(counts.get("categorize"), 1)
        self.assertEqual(counts.get("sidecar"), 1)
        self.assertEqual(counts.get("summary"), 1)


class TestProgressReporterSeam(unittest.TestCase):
    """The ProgressReporter fires: total once, and once per processable file."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_callback_fires_total_once_and_once_per_file(self):
        """on_categorized fires once with the total; on_file once per file."""
        make_exif_jpeg(
            os.path.join(self.export, "a.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        make_exif_jpeg(
            os.path.join(self.export, "b.jpg"),
            date_time_original="2024:02:16 15:31:46",
        )
        # A sidecar is deleted, not processed: it counts toward the scanned
        # total but must NOT produce an on_file event.
        open(os.path.join(self.export, "a.aae"), "wb").close()

        processor = FileProcessor(self.export, self.backup)
        reporter = _RecordingReporter(proceed=True)
        processor.process_all_files(dry_run=False, progress=reporter)

        # on_categorized fired exactly once, carrying the scanned total (3).
        self.assertEqual(len(reporter.categorized_calls), 1)
        total, stats = reporter.categorized_calls[0]
        self.assertEqual(total, 3)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["sidecar"], 1)

        # on_file fired once per processable file (the two photos, not the
        # sidecar).
        self.assertEqual(len(reporter.files), 2)
        self.assertEqual(
            {os.path.basename(p) for p, _c, _a in reporter.files},
            {"a.jpg", "b.jpg"},
        )
        for _path, category, action in reporter.files:
            self.assertEqual(category, "photos")
            self.assertEqual(action, "process")

    def test_no_files_fires_on_no_files_only(self):
        """An empty export fires on_no_files and neither of the other hooks."""
        processor = FileProcessor(self.export, self.backup)
        reporter = _RecordingReporter()
        processor.process_all_files(dry_run=False, progress=reporter)

        self.assertEqual(reporter.no_files_calls, 1)
        self.assertEqual(reporter.categorized_calls, [])
        self.assertEqual(reporter.files, [])

    def test_declining_at_categorized_aborts_before_side_effects(self):
        """Returning False from on_categorized touches no file and no sidecar."""
        make_exif_jpeg(
            os.path.join(self.export, "a.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        sidecar = os.path.join(self.export, "a.aae")
        open(sidecar, "wb").close()

        processor = FileProcessor(self.export, self.backup)
        reporter = _RecordingReporter(proceed=False)

        with patch.object(
            processor, "_delete_sidecar_files", wraps=processor._delete_sidecar_files
        ) as delete_spy:
            summary = processor.process_all_files(dry_run=False, progress=reporter)

        # Aborted before deletion and before any file was processed.
        delete_spy.assert_not_called()
        self.assertTrue(os.path.exists(sidecar), "sidecar deleted despite abort")
        self.assertEqual(reporter.files, [])
        self.assertEqual(summary["files_processed"], 0)
        # The photo is still in export -- nothing landed in backup.
        self.assertFalse(os.path.isdir(os.path.join(self.backup, "photos")))


class TestHeadlessRunUnaffected(unittest.TestCase):
    """process_all_files with progress=None still processes everything."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_none_reporter_processes_without_gating(self):
        """A None reporter proceeds automatically and files every photo."""
        make_exif_jpeg(
            os.path.join(self.export, "a.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        processor = FileProcessor(self.export, self.backup)

        summary = processor.process_all_files(dry_run=False)

        self.assertEqual(summary["files_processed"], 1)
        self.assertTrue(os.path.isdir(os.path.join(self.backup, "photos")))


if __name__ == "__main__":
    unittest.main()
