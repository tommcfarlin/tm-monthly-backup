"""
Tests pinning issue #11: categorize each file once per run, not three times.

## Finding, established by measurement before any code was written here

The issue's own table names three call sites:

1. ``cli_interface.py:127`` -- ``display_file_scan_results``, to render the
   discovery table.
2. ``cli_interface.py:195`` -- ``process_with_progress``, to get the sidecar
   list.
3. ``file_processor.py:65`` -- ``process_all_files``, the one that matters.

Checking out the commit immediately before issue #13 (``9c543a5``, the parent
of ``171ae64``) and profiling a 60-file real-photo-resolution batch with
``cProfile`` confirmed all three fired for that code:
``FileCategorizer.batch_categorize`` ran 3 times, ``categorize_file`` 180
times (60 files x 3), and ``_is_generated_content`` 150 times (50 eligible
files x 3).

Issue #13 (single public ``FileProcessor.process_all_files`` entry point) and
issue #14 (the ``ProgressReporter`` seam built on it) already closed this,
just via a different mechanism than the one issue #11 prescribes: instead of
the CLI categorizing once and passing the result down into ``FileProcessor``,
``FileProcessor`` now performs the single categorization pass itself and
reports the resulting stats *up* to the CLI through
``ProgressReporter.on_categorized``, which is what
``CLIInterface.display_categorization_summary`` renders the discovery table
from. ``process_with_progress`` no longer calls ``batch_categorize`` at all
(call site 2 is gone outright); ``display_file_scan_results`` (call site 1)
was, at the time this file was written, still present and still calling
``batch_categorize`` itself -- unreachable from the run path, but a live second
entry point that anyone re-wiring it in from ``main()`` would have restored a
second per-file pass with, and all the tests below still green. Issue #73 removed
the method outright and added an AST-based guard
(``tests/test_reporting_gaps.py::TestNoSecondCategorizationEntryPoint``) that
fails if ``batch_categorize`` is ever called from outside the pipeline again, so
that hole is closed by construction rather than by the counting below. Profiling the SAME 60-file batch at the current HEAD confirmed
``batch_categorize`` now runs exactly once, ``categorize_file`` exactly 60
times (once per file), and ``_is_generated_content`` exactly 50 times (once
per eligible file) -- a real, measured 3x reduction, not a projected one.

Because that reduction was already in place before this file was written, the
tests below are regression coverage, not proof of a fix: no production code
changed alongside them. They were confirmed to FAIL against the genuinely old
code (checked out at ``9c543a5``, i.e. before issue #13) and PASS at the
current HEAD -- see the task report for the exact commands used to verify
that on each side.
"""

import io
import os
import shutil
import tempfile
import unittest
from collections import Counter
from unittest.mock import patch

from rich.console import Console

from src.cli_interface import CLIInterface
from src.file_categorizer import FileCategorizer
from tests.fixtures import make_exif_jpeg, make_png_with_text, write_quicktime_mov


def _recording_cli(export_dir, backup_dir):
    """Return a CLIInterface whose console records to an in-memory buffer."""
    cli = CLIInterface(export_dir, backup_dir)
    cli.console = Console(file=io.StringIO(), record=True, width=120)
    return cli


class TestGeneratedContentProbedOnce(unittest.TestCase):
    """_is_generated_content is called at most once per file per run."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run_real_flow_counting_probe(self, dry_run):
        """Drive the real CLI entry point, counting _is_generated_content calls.

        Uses ``CLIInterface.process_with_progress`` -- the exact method
        ``src.main`` calls -- rather than reaching into ``FileProcessor``
        directly, so this measures the real run path a user actually
        exercises, not just one internal call site.
        """
        cli = _recording_cli(self.export, self.backup)
        # No instance handle needed any more: the patch is on the class (#68).
        call_counts = Counter()
        # Patched on the CLASS with autospec, not on this instance (issue #68).
        # An instance-level patch counts only calls through the object the test
        # happens to hold, so a regression that categorized through a NEWLY
        # CONSTRUCTED FileCategorizer would pass unnoticed -- which is precisely
        # the shape a second categorization pass would take. The original is
        # captured off the class (unbound) so it stays correct for whichever
        # instance the call actually arrives on.
        original = FileCategorizer._is_generated_content

        # Issue #24 gave _is_generated_content the pre-read metadata
        # (exif, ifd0, png_info) as arguments. Forward whatever it is called
        # with rather than pinning an arity, so counting the probe stays
        # independent of the probe's signature.
        def counting(self_categorizer, file_path, *args, **kwargs):
            call_counts[file_path] += 1
            return original(self_categorizer, file_path, *args, **kwargs)

        with patch.object(
            FileCategorizer, "_is_generated_content",
            autospec=True, side_effect=counting,
        ), patch("src.cli_interface.Confirm.ask", return_value=True):
            cli.process_with_progress(dry_run=dry_run)

        return call_counts

    def test_real_run_probes_each_eligible_file_at_most_once(self):
        """A confirmed real run never probes the same file twice."""
        # Plain (non-screenshot-pattern) JPEGs so categorize_file reaches the
        # photo branch, which is the one that calls _is_generated_content.
        eligible = [
            make_exif_jpeg(
                os.path.join(self.export, f"photo_{i}.jpg"),
                date_time_original="2024:01:15 14:30:45",
            )
            for i in range(5)
        ]

        call_counts = self._run_real_flow_counting_probe(dry_run=False)

        self.assertEqual(
            len(call_counts), len(eligible),
            "not every eligible file reached _is_generated_content",
        )
        for path, count in call_counts.items():
            self.assertEqual(
                count, 1,
                f"{path} was probed for generated content {count} times, "
                "not exactly once",
            )

    def test_dry_run_also_probes_each_eligible_file_at_most_once(self):
        """A dry run gets the same once-per-file categorization guarantee."""
        eligible = [
            make_exif_jpeg(
                os.path.join(self.export, f"photo_{i}.jpg"),
                date_time_original="2024:01:15 14:30:45",
            )
            for i in range(4)
        ]

        call_counts = self._run_real_flow_counting_probe(dry_run=True)

        self.assertEqual(len(call_counts), len(eligible))
        for _path, count in call_counts.items():
            self.assertEqual(count, 1)

    def test_png_provenance_probe_also_runs_at_most_once_per_file(self):
        """A PNG that reaches the generated-content probe is probed once.

        Uses a PNG without a screenshot-pattern filename so it reaches the
        photo branch (not the early-return screenshot branch), exercising the
        same PNG-text-chunk probe issue #44 optimized.
        """
        png = make_png_with_text(
            os.path.join(self.export, "render_output.png"),
            text_chunks={},
        )

        call_counts = self._run_real_flow_counting_probe(dry_run=False)

        self.assertEqual(call_counts[png], 1)


class TestCategorizeFileCalledOncePerScannedFile(unittest.TestCase):
    """categorize_file itself -- not just the generated-content probe -- runs
    exactly once per scanned file across a real, mixed-category run."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mixed_batch_categorized_exactly_once_per_file(self):
        """Photo, video, screenshot, unknown, and sidecar files: one pass each."""
        photo = make_exif_jpeg(
            os.path.join(self.export, "photo.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        # A zero-byte .mov makes hachoir's createParser raise inside
        # ExifHandler._extract_video_timestamp_hachoir, and the file handle it
        # opened leaks on that path (a genuine pre-existing defect, tracked
        # separately -- not this issue's to fix). A real minimal QuickTime
        # fixture avoids exercising that leak here so this test's output
        # stays pristine.
        video = write_quicktime_mov(
            os.path.join(self.export, "clip.mov"),
            "2024-03-17T09:00:00-0400",
        )
        screenshot = os.path.join(self.export, "Screenshot 2024.png")
        open(screenshot, "wb").close()
        unknown = os.path.join(self.export, "notes.xyz")
        open(unknown, "wb").close()
        sidecar = os.path.join(self.export, "photo.aae")
        open(sidecar, "wb").close()
        all_files = {photo, video, screenshot, unknown, sidecar}

        cli = _recording_cli(self.export, self.backup)
        # No instance handle needed any more: the patch is on the class (#68).
        call_counts = Counter()
        # Class-level + autospec, for the reason given on the probe test above
        # (issue #68): an instance patch would miss a pass made through a freshly
        # constructed categorizer.
        original = FileCategorizer.categorize_file

        def counting(self_categorizer, file_path):
            call_counts[file_path] += 1
            return original(self_categorizer, file_path)

        with patch.object(
            FileCategorizer, "categorize_file",
            autospec=True, side_effect=counting,
        ), patch("src.cli_interface.Confirm.ask", return_value=True):
            cli.process_with_progress(dry_run=False)

        self.assertEqual(
            set(call_counts), all_files,
            "categorize_file did not run on exactly the scanned files",
        )
        for path, count in call_counts.items():
            self.assertEqual(
                count, 1, f"{path} was categorized {count} times, not once"
            )


if __name__ == "__main__":
    unittest.main()
