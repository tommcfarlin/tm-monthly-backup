"""
Tests for real per-file progress during processing (issue #14).

Before this change, ``_CLIProgressReporter`` advanced a single generic
"Processing files..." bar once per processed file with no indication of which
phase (HEIC conversion vs. moving) was underway, and the expensive HEIC
conversion phase -- which, for a batch at or above
``FileProcessor.HEIC_PARALLEL_THRESHOLD``, runs entirely up front in a process
pool (issue #42) -- produced no progress feedback at all until the much
cheaper sequential place phase started afterward and caught the bar up all at
once. These tests pin the fix:

* ``FileProcessor`` now fires a new ``ProgressReporter.on_heic_converted(path)``
  hook once per HEIC file as its OWN conversion completes -- in the pool's
  ``as_completed`` loop for a large batch, or inline for a small one -- and
  that hook fires strictly before ``on_file`` fires for the same file.
* For a large (pooled) batch, every ``on_heic_converted`` call fires before
  any ``on_file`` call: the conversion phase genuinely completes before
  organizing starts, matching the two real, separately-timed phases.
* ``on_categorized``'s ``stats`` dict now carries a ``'heic'`` key -- the
  processable HEIC count -- so a reporter can size a conversion-specific total
  independent of the move/organize total.
* ``on_file``'s ``action`` argument is now ``"convert"`` for a HEIC file and
  ``"move"`` for everything else, instead of the fixed placeholder
  ``"process"`` issue #13 left behind.
* ``_CLIProgressReporter`` renders two real per-phase bars sized from the
  above (a conversion bar only when there is HEIC work, an organize bar
  always) and shows the current filename -- escaped through ``safe_markup``
  (issue #9) -- in each bar's description.
"""

import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from rich.console import Console
from rich.text import Text

from src.cli_interface import CLIInterface, _CLIProgressReporter
from src.file_processor import FileProcessor, ProgressReporter
from tests.fixtures import make_exif_heic, make_exif_jpeg


class _RecordingReporter(ProgressReporter):
    """A ProgressReporter recording every hook call, in firing order."""

    def __init__(self, proceed=True):
        self.proceed = proceed
        self.categorized_calls = []  # list of (total, stats)
        self.files = []              # list of (path, category, action)
        self.heic_converted = []     # list of paths, in call order
        self.events = []             # combined ordered log: ('heic', path) or
                                      # ('file', path, category, action)

    def on_no_files(self):
        pass

    def on_categorized(self, total, stats):
        self.categorized_calls.append((total, dict(stats)))
        return self.proceed

    def on_heic_converted(self, path):
        self.heic_converted.append(path)
        self.events.append(('heic', path))

    def on_file(self, path, category, action):
        self.files.append((path, category, action))
        self.events.append(('file', path, category, action))


def _recording_cli(export_dir, backup_dir):
    """Return a CLIInterface whose console records to an in-memory buffer."""
    cli = CLIInterface(export_dir, backup_dir)
    cli.console = Console(file=io.StringIO(), record=True, width=120)
    return cli


class TestOnHeicConvertedHook(unittest.TestCase):
    """on_heic_converted fires once per HEIC file, before that file's on_file."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_inline_path_fires_once_per_file_before_its_own_on_file(self):
        """Below HEIC_PARALLEL_THRESHOLD, each HEIC converts inline."""
        paths = []
        for index in range(5):
            path = make_exif_heic(
                os.path.join(self.export, f"IMG_{index:04d}.heic"),
                date_time_original=f"2024:05:01 12:00:{index:02d}",
            )
            paths.append(path)

        processor = FileProcessor(self.export, self.backup)
        self.assertLess(len(paths), processor.HEIC_PARALLEL_THRESHOLD)
        reporter = _RecordingReporter()

        summary = processor.process_all_files(dry_run=False, progress=reporter)

        self.assertEqual(summary['files_processed'], 5)
        # Fired exactly once per HEIC file, for exactly the HEIC files.
        self.assertEqual(len(reporter.heic_converted), 5)
        self.assertEqual(set(reporter.heic_converted), set(paths))

        # For each file, its own 'heic' event precedes its own 'file' event.
        heic_index = {}
        file_index = {}
        for position, event in enumerate(reporter.events):
            if event[0] == 'heic':
                heic_index[event[1]] = position
            else:
                file_index[event[1]] = position
        for path in paths:
            self.assertLess(
                heic_index[path], file_index[path],
                "on_heic_converted must fire before on_file for the same file",
            )

    def test_pool_path_converts_every_file_before_any_is_organized(self):
        """At/above the threshold, the pool converts everything up front."""
        paths = []
        for index in range(9):
            path = make_exif_heic(
                os.path.join(self.export, f"IMG_{index:04d}.heic"),
                date_time_original=f"2024:03:10 09:15:{index:02d}",
            )
            paths.append(path)

        processor = FileProcessor(self.export, self.backup)
        self.assertGreaterEqual(len(paths), processor.HEIC_PARALLEL_THRESHOLD)
        reporter = _RecordingReporter()

        summary = processor.process_all_files(dry_run=False, progress=reporter)

        self.assertEqual(summary['files_processed'], 9)
        # Fired exactly once per HEIC file (no duplicates from the cached
        # lookup in the sequential place phase).
        self.assertEqual(len(reporter.heic_converted), 9)
        self.assertEqual(set(reporter.heic_converted), set(paths))

        heic_positions = [i for i, e in enumerate(reporter.events) if e[0] == 'heic']
        file_positions = [i for i, e in enumerate(reporter.events) if e[0] == 'file']
        self.assertEqual(len(file_positions), 9)
        self.assertLess(
            max(heic_positions), min(file_positions),
            "the whole pooled conversion phase must complete before "
            "organizing starts -- this is the defect issue #14 fixes",
        )

    def test_never_fires_in_dry_run(self):
        """A dry run performs no conversion, so the hook never fires (#10)."""
        for index in range(9):
            make_exif_heic(
                os.path.join(self.export, f"IMG_{index:04d}.heic"),
                date_time_original=f"2024:03:10 09:15:{index:02d}",
            )
        processor = FileProcessor(self.export, self.backup)
        reporter = _RecordingReporter()

        processor.process_all_files(dry_run=True, progress=reporter)

        self.assertEqual(reporter.heic_converted, [])


class TestCategorizedStatsCarryHeicCount(unittest.TestCase):
    """on_categorized's stats dict now carries the processable HEIC count."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_heic_key_counts_only_heic_files(self):
        make_exif_heic(
            os.path.join(self.export, "a.heic"),
            date_time_original="2024:01:15 14:30:45",
        )
        make_exif_heic(
            os.path.join(self.export, "b.heic"),
            date_time_original="2024:01:16 14:30:45",
        )
        make_exif_jpeg(
            os.path.join(self.export, "c.jpg"),
            date_time_original="2024:01:17 14:30:45",
        )

        processor = FileProcessor(self.export, self.backup)
        reporter = _RecordingReporter()
        processor.process_all_files(dry_run=False, progress=reporter)

        self.assertEqual(len(reporter.categorized_calls), 1)
        total, stats = reporter.categorized_calls[0]
        self.assertEqual(total, 3)
        self.assertEqual(stats['heic'], 2)


class TestOnFileActionReflectsPhase(unittest.TestCase):
    """on_file's action is 'convert' for HEIC, 'move' for everything else."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_convert_for_heic_move_for_non_heic(self):
        make_exif_heic(
            os.path.join(self.export, "a.heic"),
            date_time_original="2024:01:15 14:30:45",
        )
        make_exif_jpeg(
            os.path.join(self.export, "b.jpg"),
            date_time_original="2024:01:16 14:30:45",
        )

        processor = FileProcessor(self.export, self.backup)
        reporter = _RecordingReporter()
        processor.process_all_files(dry_run=False, progress=reporter)

        actions = {os.path.basename(p): a for p, _c, a in reporter.files}
        self.assertEqual(actions['a.heic'], 'convert')
        self.assertEqual(actions['b.jpg'], 'move')


class TestCLIProgressBars(unittest.TestCase):
    """_CLIProgressReporter renders two real, independently-sized bars."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _task(self, reporter, task_id):
        return next(t for t in reporter._progress.tasks if t.id == task_id)

    def test_convert_and_organize_totals_and_completion(self):
        for index in range(3):
            make_exif_heic(
                os.path.join(self.export, f"IMG_{index:04d}.heic"),
                date_time_original=f"2024:06:01 08:00:{index:02d}",
            )
        for index in range(2):
            make_exif_jpeg(
                os.path.join(self.export, f"pic_{index}.jpg"),
                date_time_original=f"2024:06:02 09:00:0{index}",
            )

        cli = _recording_cli(self.export, self.backup)
        reporter = _CLIProgressReporter(cli, dry_run=False)
        with patch("src.cli_interface.Confirm.ask", return_value=True):
            summary = cli.processor.process_all_files(
                dry_run=False, progress=reporter
            )

        self.assertEqual(summary['files_processed'], 5)
        self.assertIsNotNone(reporter._convert_task)
        self.assertIsNotNone(reporter._organize_task)

        # Snapshot the tasks before close() -- it tears down ``_progress``
        # (sets it back to ``None``) once the live display is stopped.
        convert_task = self._task(reporter, reporter._convert_task)
        organize_task = self._task(reporter, reporter._organize_task)

        # Real totals: the HEIC count for conversion, every processable file
        # for organizing -- and both fully advanced by the end of the run.
        self.assertEqual(convert_task.total, 3)
        self.assertEqual(convert_task.completed, 3)
        self.assertEqual(organize_task.total, 5)
        self.assertEqual(organize_task.completed, 5)

        reporter.close()

    def test_no_convert_bar_when_batch_has_no_heic(self):
        make_exif_jpeg(
            os.path.join(self.export, "a.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        make_exif_jpeg(
            os.path.join(self.export, "b.jpg"),
            date_time_original="2024:01:16 14:30:45",
        )

        cli = _recording_cli(self.export, self.backup)
        reporter = _CLIProgressReporter(cli, dry_run=False)
        with patch("src.cli_interface.Confirm.ask", return_value=True):
            cli.processor.process_all_files(dry_run=False, progress=reporter)

        self.assertIsNone(reporter._convert_task)
        self.assertIsNotNone(reporter._organize_task)
        self.assertEqual(len(reporter._progress.tasks), 1)

        reporter.close()

    def test_hostile_filenames_render_safely_in_bar_descriptions(self):
        """A crafted filename in a bar description must not vanish or crash.

        Mirrors the issue #9 invariant: filenames reach a ``rich``
        markup-parsed context (the progress bar's ``TextColumn``, whose
        template wraps ``{task.description}`` in ``[progress.description]``
        and parses the whole formatted string as markup on render). A
        filename component can never itself contain ``/`` (the path
        separator), so the reachable threat here is a valid-looking style tag
        (e.g. ``[bold red]``) being parsed and silently swallowed rather than
        displayed -- exactly what an unescaped basename would suffer, and
        what ``safe_markup`` must prevent.
        """
        cli = _recording_cli(self.export, self.backup)
        reporter = _CLIProgressReporter(cli, dry_run=False)
        stats = {
            'photos': 2, 'videos': 0, 'screenshots': 0, 'generated': 0,
            'unknown': 0, 'sidecar': 0, 'total': 2, 'heic': 1,
        }
        with patch("src.cli_interface.Confirm.ask", return_value=True):
            proceed = reporter.on_categorized(2, stats)
        self.assertTrue(proceed)

        # Neither hostile basename contains "/" -- a real filename never can,
        # since the filesystem treats it as a path separator.
        hostile_heic = "/export/IMG_[bold red]1234.HEIC"
        hostile_other = "/export/other_[bold]evil1234.jpg"
        reporter.on_heic_converted(hostile_heic)
        reporter.on_file(hostile_other, "photos", "move")

        # Snapshot the tasks before close() -- it tears down ``_progress``.
        convert_task = self._task(reporter, reporter._convert_task)
        organize_task = self._task(reporter, reporter._organize_task)

        # Render exactly the template rich's TextColumn uses for this bar; it
        # must not raise, and the bracketed text must survive as literal
        # characters rather than being parsed away as styling markup.
        convert_rendered = Text.from_markup(
            f"[progress.description]{convert_task.description}"
        )
        organize_rendered = Text.from_markup(
            f"[progress.description]{organize_task.description}"
        )
        self.assertIn("IMG_[bold red]1234.HEIC", convert_rendered.plain)
        self.assertIn("other_[bold]evil1234.jpg", organize_rendered.plain)

        reporter.close()


if __name__ == "__main__":
    unittest.main()
