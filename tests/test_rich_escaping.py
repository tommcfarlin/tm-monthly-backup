"""
Tests for neutralizing untrusted filenames before they reach ``rich`` (issue #9).

Filenames arrive untrusted -- from iCloud, AirDrop, downloads, and manual
renames -- and flow into ``rich`` markup contexts (``Console.print``,
``Table.add_row``, ``Text.append``) and into log records. Three hostile shapes
must be neutralized:

* an unmatched closing tag such as ``[/]`` raises ``MarkupError`` and, on the
  ``categorize_file`` warning path (which runs inside ``batch_categorize`` with
  no surrounding ``try/except``), aborts the entire run;
* a valid tag such as ``[bold red]`` is silently swallowed, corrupting output;
* raw ANSI/control sequences reach the terminal and can drive the cursor.

These tests assert -- through a recording ``rich.Console`` -- that each render
and log path neither raises nor swallows nor leaks control sequences. A real
filename cannot itself contain ``/``, so the ``[/]`` shape is exercised either
as a literal string handed to a render function or, end to end, via a
subdirectory named ``photo[`` holding a file named ``].xyz`` whose joined path
contains the offending ``[/]`` sequence.
"""

import io
import logging
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager

from rich.console import Console
from rich.logging import RichHandler

from src.cli_interface import (
    CLIInterface,
    _SanitizingLogFilter,
    safe_markup,
    sanitize_for_display,
    setup_logging,
)
from src.file_categorizer import FileCategorizer
from src.file_processor import FileProcessor

# A filename fragment that carries every hostile shape at once: a swallowed
# valid tag, an unmatched closing tag, and a raw ANSI colour sequence.
HOSTILE_MARKUP = "IMG_[bold red]1234.jpg"
HOSTILE_CLOSING = "photo[/].jpg"
HOSTILE_ANSI = "evil\x1b[31mX.jpg"


def _recording_cli(export_dir, backup_dir):
    """Return a CLIInterface whose console records to an in-memory buffer."""
    cli = CLIInterface(export_dir, backup_dir)
    cli.console = Console(file=io.StringIO(), record=True, width=120)
    return cli


@contextmanager
def _capture_logger(name):
    """
    Temporarily route a named logger through a recording ``RichHandler``.

    Installs a handler configured exactly as :func:`setup_logging` builds it
    (``markup=False`` plus the sanitizing filter) on a non-terminal recording
    console, yields that console, and restores the logger's original state.
    ``export_text`` on the yielded console gives the plain rendered text.
    """
    logger = logging.getLogger(name)
    console = Console(file=io.StringIO(), record=True, width=200)
    handler = RichHandler(
        console=console, markup=False, show_time=False, show_path=False
    )
    handler.addFilter(_SanitizingLogFilter())

    saved_handlers = logger.handlers[:]
    saved_propagate = logger.propagate
    saved_level = logger.level
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    try:
        yield console
    finally:
        logger.handlers = saved_handlers
        logger.propagate = saved_propagate
        logger.setLevel(saved_level)


class TestSanitizeHelpers(unittest.TestCase):
    """The sanitizer and markup-escaper neutralize the right things."""

    def test_sanitize_strips_ansi_and_control_keeps_brackets(self):
        """ANSI/control sequences are removed; printable brackets survive."""
        cleaned = sanitize_for_display("a\x1b[31mred\x1b[0m\x07[/].jpg")
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("\x07", cleaned)
        self.assertNotIn("31m", cleaned)  # the CSI payload went with the ESC
        self.assertEqual(cleaned, "ared[/].jpg")

    def test_sanitize_preserves_ordinary_whitespace(self):
        """Tabs and newlines used by the tool's own text are left intact."""
        self.assertEqual(sanitize_for_display("a\tb\nc"), "a\tb\nc")

    def test_safe_markup_escapes_tags_and_strips_control(self):
        """safe_markup escapes markup and drops control sequences together."""
        result = safe_markup("x\x1b[31m[bold]y[/].jpg")
        self.assertNotIn("\x1b", result)
        # rich.markup.escape backslash-escapes the opening bracket.
        self.assertIn("\\[bold]", result)


class TestSetupLoggingConfig(unittest.TestCase):
    """setup_logging installs a markup-disabled, sanitizing handler."""

    def setUp(self):
        root = logging.getLogger()
        self._saved = root.handlers[:]
        root.handlers = []

    def tearDown(self):
        logging.getLogger().handlers = self._saved

    def test_handler_disables_markup_and_filters(self):
        """The configured RichHandler has markup off and the sanitizer on."""
        setup_logging(verbose=False)
        handlers = logging.getLogger().handlers
        rich_handlers = [h for h in handlers if isinstance(h, RichHandler)]
        self.assertTrue(rich_handlers, "expected a RichHandler on the root logger")
        handler = rich_handlers[0]
        self.assertFalse(handler.markup, "log markup must be disabled (issue #9)")
        self.assertTrue(
            any(isinstance(f, _SanitizingLogFilter) for f in handler.filters),
            "the sanitizing filter must be attached",
        )


class TestLoggingNeutralizesHostileNames(unittest.TestCase):
    """The logging path cannot raise, swallow, or leak on hostile names."""

    def test_unknown_file_warning_does_not_raise_on_closing_tag(self):
        """categorize_file's warning renders a [/] path without raising."""
        # categorize_file inspects the path string; an unknown extension drives
        # the "Unknown file type" warning without touching disk. The path holds
        # the unmatched closing tag that aborted the run before the fix.
        with _capture_logger("src.file_categorizer") as console:
            category = FileCategorizer().categorize_file("photo[/].xyz")
            text = console.export_text()
        self.assertEqual(category.value, "unknown")
        self.assertIn("photo[/].xyz", text)  # literal, not swallowed

    def test_valid_tag_is_not_swallowed_from_logs(self):
        """A valid [bold red] tag survives verbatim instead of vanishing."""
        with _capture_logger("src.file_categorizer") as console:
            FileCategorizer().categorize_file("IMG_[bold red]1234.zzz")
            text = console.export_text()
        self.assertIn("IMG_[bold red]1234.zzz", text)

    def test_ansi_sequence_is_stripped_from_logs(self):
        """A raw ANSI sequence in a logged name is neutralized, not emitted."""
        with _capture_logger("src.file_categorizer") as console:
            FileCategorizer().categorize_file("evil\x1b[31mX.zzz")
            text = console.export_text()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("31m", text)
        self.assertIn("evilX.zzz", text)

    def test_exception_traceback_is_sanitized_of_ansi(self):
        """A logged exception's traceback cannot smuggle ANSI through logger.exception.

        Issue #39 adds a top-level ``logger.exception`` call in ``main.py`` so
        an unexpected error's traceback is always recorded. ``Formatter.format``
        appends the formatted traceback (``record.exc_text``, built from
        ``record.exc_info``) to the message AFTER handler filters run -- so,
        unlike ``record.msg``, it was never touched by
        ``_SanitizingLogFilter`` before this fix. An exception whose own
        ``str()`` embeds a raw ANSI sequence (e.g. from an untrusted filename
        interpolated into an f-string, rather than passed as a lazy ``%s``
        argument) would otherwise reach the terminal unsanitized from inside
        the traceback text even though the same value in the log message
        itself is already safe.

        Fails against the old filter: it never inspects ``record.exc_info``/
        ``record.exc_text`` at all, so the raw ``\\x1b[31m`` sequence below
        survives into the rendered output.
        """
        # Built at runtime, not as a source-literal escape, so the traceback's
        # own rendering of the ``raise`` source line does not itself contain
        # the literal text "31m" -- only the exception's *runtime* str(),
        # which is what must be sanitized, carries the raw ESC byte.
        hostile_name = "evil" + chr(0x1B) + "[31mX.jpg"
        with _capture_logger("src.rich_escaping_test") as console:
            logger = logging.getLogger("src.rich_escaping_test")
            try:
                raise ValueError(hostile_name)
            except ValueError:
                logger.exception("boom")
            text = console.export_text()

        self.assertNotIn("\x1b", text)
        self.assertNotIn("31m", text)
        self.assertIn("evilX.jpg", text)  # literal, not swallowed
        self.assertIn("boom", text)


class TestRenderBoundariesDoNotRaise(unittest.TestCase):
    """Every display path escapes hostile filenames rather than choking."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cli = _recording_cli(
            os.path.join(self.temp_dir, "export"),
            os.path.join(self.temp_dir, "backup"),
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_display_failures_renders_closing_tag_literally(self):
        """A [/] filename in the failure table renders without MarkupError."""
        self.cli.display_failures(
            [("move_file", HOSTILE_CLOSING, "disk full")]
        )
        text = self.cli.console.export_text()
        self.assertIn(HOSTILE_CLOSING, text)
        self.assertIn("disk full", text)

    def test_display_failures_preserves_valid_tag(self):
        """A [bold red] filename is not partially swallowed in the table."""
        self.cli.display_failures(
            [("move_file", HOSTILE_MARKUP, "boom")]
        )
        self.assertIn(HOSTILE_MARKUP, self.cli.console.export_text())

    def test_display_failures_strips_ansi(self):
        """An ANSI sequence in a failed path cannot reach the rendered output."""
        self.cli.display_failures([("move_file", HOSTILE_ANSI, "boom")])
        text = self.cli.console.export_text()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("31m", text)
        self.assertIn("evilX.jpg", text)

    def test_display_missing_exif_warning_handles_markup_and_ansi(self):
        """The EXIF warning panel renders hostile names literally and safely.

        Missing-EXIF entries are ``{'original_path', 'final_path'}`` dicts
        (issue #35); the hostile strings are spread across both fields and
        both record shapes (a final_path present, as a real run would produce,
        and None, as a dry run would) so a hostile name reaches the render path
        either way.
        """
        self.cli.display_missing_exif_warning([
            {"original_path": HOSTILE_CLOSING, "final_path": None},
            {"original_path": "export/ok.jpg", "final_path": HOSTILE_MARKUP},
            {"original_path": HOSTILE_ANSI, "final_path": None},
        ])
        text = self.cli.console.export_text()
        self.assertIn(HOSTILE_CLOSING, text)
        self.assertIn(HOSTILE_MARKUP, text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("31m", text)

    def test_check_directories_handles_markup_in_export_path(self):
        """A missing export path bearing [/] is reported without raising."""
        hostile_export = os.path.join(self.temp_dir, "no[", "]such")
        cli = _recording_cli(hostile_export, os.path.join(self.temp_dir, "backup"))
        self.assertFalse(cli.check_directories())  # must not raise
        self.assertIn("does not exist", cli.console.export_text())


class TestEndToEndHostilePath(unittest.TestCase):
    """A hostile path survives a full process_all_files run end to end."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir, exist_ok=True)
        self.processor = FileProcessor(self.export_dir, self.backup_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_closing_tag_path_processes_without_aborting(self):
        """
        A path containing ``[/]`` runs through batch_categorize without aborting.

        ``batch_categorize`` is not wrapped in a ``try/except``, so before the
        fix the unmatched closing tag raised ``MarkupError`` out of
        ``categorize_file`` and killed the run. A subdirectory ``photo[`` holding
        an unknown-type file ``].xyz`` yields the joined path
        ``export/photo[/].xyz`` -- the ``[/]`` sequence -- while staying
        filesystem-legal.
        """
        hostile_dir = os.path.join(self.export_dir, "photo[")
        os.makedirs(hostile_dir, exist_ok=True)
        with open(os.path.join(hostile_dir, "].xyz"), "wb") as handle:
            handle.write(b"unknown bytes")

        with _capture_logger("src.file_categorizer") as console:
            # Must not raise; the unknown file is counted in the summary.
            results = self.processor.process_all_files(dry_run=True)
            logged = console.export_text()

        self.assertGreaterEqual(results["categorization_stats"]["unknown"], 1)
        self.assertIn("photo[/].xyz", logged)  # literal path reached the log

    def test_valid_tag_filename_processes_and_is_logged_literally(self):
        """A [bold red] photo processes and its literal name is not swallowed."""
        from tests.fixtures import make_exif_jpeg

        make_exif_jpeg(
            os.path.join(self.export_dir, HOSTILE_MARKUP),
            date_time_original="2024:01:15 14:30:45",
        )

        with _capture_logger("src.file_processor") as console:
            results = self.processor.process_all_files(dry_run=True)
            logged = console.export_text()

        self.assertEqual(results["files_failed"], 0)
        self.assertIn(HOSTILE_MARKUP, logged)


if __name__ == "__main__":
    unittest.main()
