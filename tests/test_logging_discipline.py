"""
Tests for logging level discipline and lazy formatting (issue #48).

Four independent properties, each pinned by its own test class:

1. ``setup_logging(verbose=True)`` must actually raise the root logger to
   DEBUG and install the sanitizing ``RichHandler`` even when the root
   logger already carries a handler -- ``logging.basicConfig`` silently
   does nothing in that case unless told ``force=True``.
2. No log call anywhere in ``src/`` uses eager f-string interpolation
   (``ruff --select G`` gates this mechanically, but nothing currently runs
   ruff automatically -- see the project's own #17 -- so this pins it
   structurally too).
3. A real multi-file run at the default (non-verbose) level emits no
   per-file success chatter -- only warnings/errors and the run's own
   summary -- while ``--verbose`` (DEBUG) restores the per-file detail.
4. Each unknown file produces exactly one "Unknown file type" warning per
   run (already true going into this issue, since #11/#13/#14 already made
   categorization single-pass; pinned here rather than left unverified).
"""

import ast
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from src.cli_interface import setup_logging
from src.file_processor import FileProcessor
from tests.fixtures import make_exif_jpeg

SRC_DIR = Path(__file__).resolve().parent.parent / "src"

# Every logging module method that can carry a format string as its first
# positional argument. An f-string there is exactly issue #48's G004 defect:
# it formats the message whether or not the record is ever emitted.
_LOGGER_CALL_METHODS = {"debug", "info", "warning", "error", "critical", "exception", "log"}


class TestSetupLoggingForcesConfiguration(unittest.TestCase):
    """setup_logging must take effect even against an already-configured root."""

    def setUp(self):
        self._original_handlers = list(logging.getLogger().handlers)
        self._original_level = logging.getLogger().level

    def tearDown(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in self._original_handlers:
            root.addHandler(handler)
        root.setLevel(self._original_level)

    def test_verbose_raises_root_level_even_with_a_pre_existing_handler(self):
        """Simulates the exact failure mode the issue reproduced: something
        else configures logging first (a test module, a future --log-file
        option, an eager library import), then setup_logging(verbose=True)
        must still win rather than being silently ignored by
        logging.basicConfig's documented early return."""
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        root.addHandler(logging.StreamHandler())
        root.setLevel(logging.WARNING)

        setup_logging(verbose=True)

        self.assertEqual(
            logging.getLogger().level,
            logging.DEBUG,
            "setup_logging(verbose=True) did not raise the root level to "
            "DEBUG against an already-configured root -- basicConfig's "
            "silent no-op struck",
        )

    def test_the_installed_handler_is_the_sanitizing_rich_handler(self):
        """Not just the level: the sanitizing RichHandler itself must
        actually be installed, since that handler is the boundary issue #9
        relies on to neutralize untrusted filenames in every log record."""
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        root.addHandler(logging.StreamHandler())

        setup_logging(verbose=False)

        from rich.logging import RichHandler

        self.assertTrue(
            any(isinstance(h, RichHandler) for h in logging.getLogger().handlers),
            "setup_logging did not install its RichHandler against an "
            "already-configured root",
        )


class TestNoEagerFStringLoggerCalls(unittest.TestCase):
    """No logging call in src/ formats its message eagerly (ruff G004)."""

    def test_no_logger_call_uses_an_f_string_argument(self):
        offenders = []
        for path in sorted(SRC_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr in _LOGGER_CALL_METHODS):
                    continue
                if not node.args:
                    continue
                first_arg = node.args[0] if func.attr != "log" else (
                    node.args[1] if len(node.args) > 1 else None
                )
                if isinstance(first_arg, ast.JoinedStr):
                    offenders.append("%s:%d" % (path.name, node.lineno))
        self.assertEqual(offenders, [])


class TestDefaultLevelIsQuietVerboseRestoresDetail(unittest.TestCase):
    """A real run's per-file chatter is DEBUG-only; --verbose restores it."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        for i in range(5):
            make_exif_jpeg(
                os.path.join(self.export_dir, "photo_%d.jpg" % i),
                date_time_original="2024:01:15 14:30:%02d" % (45 + i),
            )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_level_run_has_no_per_file_moved_lines(self):
        """At the default level (INFO), a multi-file real run logs no
        per-file 'Moved:' chatter -- the exact flood issue #48 exists to
        remove. The run-level summary line (Categorization complete: ...)
        is unaffected, since that is a once-per-run summary, not per-file
        chatter, and stays at INFO by design."""
        processor = FileProcessor(self.export_dir, self.backup_dir)

        with self.assertLogs("src.file_processor", level="INFO") as captured:
            processor.process_all_files(dry_run=False)

        moved_lines = [m for m in captured.output if "Moved:" in m]
        self.assertEqual(
            moved_lines, [],
            "a per-file 'Moved:' line reached the default (INFO) level",
        )
        self.assertTrue(
            any("Categorization complete" in m for m in captured.output),
            "the once-per-run summary line went missing along with the "
            "per-file chatter -- only the per-file lines should have moved",
        )

    def test_verbose_level_restores_per_file_moved_lines(self):
        """The identical run, observed at DEBUG (what --verbose selects via
        setup_logging), shows the per-file detail that was hidden above."""
        processor = FileProcessor(self.export_dir, self.backup_dir)

        with self.assertLogs("src.file_processor", level="DEBUG") as captured:
            processor.process_all_files(dry_run=False)

        moved_lines = [m for m in captured.output if "Moved:" in m]
        self.assertEqual(
            len(moved_lines), 5,
            "--verbose (DEBUG) did not restore one 'Moved:' line per file",
        )


class TestUnknownFileWarningFiresExactlyOncePerFile(unittest.TestCase):
    """categorize_file's own once-per-file warning is not multiplied by a
    run that (before #11/#13/#14) used to categorize the same batch more
    than once."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_two_unknown_files_each_warn_exactly_once(self):
        mystery_a = os.path.join(self.export_dir, "mystery_a.xyz")
        mystery_b = os.path.join(self.export_dir, "mystery_b.dat")
        open(mystery_a, "wb").close()
        open(mystery_b, "wb").close()
        processor = FileProcessor(self.export_dir, self.backup_dir)

        with self.assertLogs("src.file_categorizer", level="WARNING") as captured:
            processor.process_all_files(dry_run=True)

        for path in (mystery_a, mystery_b):
            matching = [m for m in captured.output if path in m]
            self.assertEqual(
                len(matching), 1,
                "expected exactly one 'Unknown file type' warning for %s, got %d"
                % (path, len(matching)),
            )


if __name__ == "__main__":
    unittest.main()
