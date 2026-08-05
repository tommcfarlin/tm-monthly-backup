"""
Test suite for the CLI entry point in ``src/main.py``.

Covers the exit-code taxonomy directly via ``determine_exit_code`` and drives
the ``main`` command through click's ``CliRunner`` to pin each terminal branch:
success, dry run, partial failure, cancellation, the precondition/config-error
paths, ``KeyboardInterrupt`` handling, the unexpected-exception handler, and
``--version``.
"""

import importlib
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from src.main import (
    EXIT_CANCELLED,
    EXIT_PARTIAL_FAILURE,
    EXIT_PRECONDITION,
    EXIT_SUCCESS,
    determine_exit_code,
    main,
)
from tests.fixtures import make_exif_jpeg


class TestDetermineExitCode(unittest.TestCase):
    """The pure result-dict -> exit-code mapping."""

    def test_cancelled_wins_over_failures(self):
        """A cancelled run maps to 130 even if a failure count is present."""
        self.assertEqual(
            determine_exit_code({"status": "cancelled", "files_failed": 3}),
            EXIT_CANCELLED,
        )

    def test_failures_map_to_partial(self):
        """A completed run with failures maps to the partial-failure code."""
        self.assertEqual(
            determine_exit_code({"status": "completed", "files_failed": 2}),
            EXIT_PARTIAL_FAILURE,
        )

    def test_clean_run_maps_to_success(self):
        """A completed run with no failures maps to success."""
        self.assertEqual(
            determine_exit_code({"status": "completed", "files_failed": 0}),
            EXIT_SUCCESS,
        )

    def test_empty_dict_maps_to_success(self):
        """A dict lacking both keys defaults to success (empty/dry run)."""
        self.assertEqual(determine_exit_code({}), EXIT_SUCCESS)


class TestMainCli(unittest.TestCase):
    """End-to-end invocation of ``main`` through CliRunner."""

    def setUp(self):
        self.runner = CliRunner()
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _args(self, *extra):
        return [
            "--export-dir",
            self.export_dir,
            "--backup-dir",
            self.backup_dir,
            *extra,
        ]

    def test_version_option(self):
        """--version prints the program name/version and exits cleanly."""
        result = self.runner.invoke(main, ["--version"])

        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertIn("tm-monthly-backup", result.output)
        self.assertIn("1.0.0", result.output)

    def test_help_documents_yes_flag(self):
        """--help lists --yes: the issue's safety framing rests on the flag
        being discoverable, not just present (issue #33)."""
        result = self.runner.invoke(main, ["--help"])

        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertIn("--yes", result.output)

    def test_console_entry_point_target_resolves(self):
        """The ``src.main:main`` console entry point (pyproject.toml) resolves.

        Guards the string the installed ``tm-monthly-backup`` command dispatches
        to: the module must import and expose a callable ``main``. The
        throwaway-venv ``pip install -e .`` is the end-to-end proof; this pins
        the target without requiring an install so a rename can't break it
        silently.
        """
        module_path, _, attr = "src.main:main".partition(":")
        module = importlib.import_module(module_path)
        entry = getattr(module, attr)

        self.assertTrue(callable(entry))
        self.assertIs(entry, main)

    def test_missing_export_dir_is_precondition_failure(self):
        """A nonexistent export directory exits with the precondition code."""
        missing = os.path.join(self.temp_dir, "does_not_exist")
        result = self.runner.invoke(
            main,
            ["--export-dir", missing, "--backup-dir", self.backup_dir, "--yes"],
        )

        self.assertEqual(result.exit_code, EXIT_PRECONDITION)
        self.assertIn("Cannot proceed", result.output)

    def test_successful_run_exits_zero(self):
        """A confirmed (--yes) run over a decodable photo processes it and
        exits 0, with no prompt to answer and without patching ``Confirm.ask``
        (issue #33)."""
        make_exif_jpeg(
            os.path.join(self.export_dir, "pic.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )

        result = self.runner.invoke(main, self._args("--yes"))

        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertIn("All files processed successfully", result.output)
        self.assertTrue(
            os.path.exists(os.path.join(self.backup_dir, "photos"))
        )

    def test_dry_run_exits_zero_and_changes_nothing(self):
        """--dry-run reports intent, exits 0, and moves no files."""
        make_exif_jpeg(
            os.path.join(self.export_dir, "pic.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )

        result = self.runner.invoke(main, self._args("--dry-run"))

        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertIn("Dry run completed", result.output)
        # The source file is still in export/, untouched.
        self.assertTrue(
            os.path.exists(os.path.join(self.export_dir, "pic.jpg"))
        )

    def test_declining_prompt_is_cancelled(self):
        """Answering no to the confirmation exits with the cancel code.

        No ``--yes`` is passed here -- the point is to exercise the real
        prompt -- so the non-interactive gate (issue #33) is bypassed by
        forcing ``_stdin_is_interactive`` True, standing in for a real TTY
        that ``CliRunner`` cannot provide (it never presents stdin as a tty,
        even when ``input=`` supplies text for a prompt to read).
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "pic.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )

        with patch("src.main._stdin_is_interactive", return_value=True):
            result = self.runner.invoke(main, self._args(), input="n\n")

        self.assertEqual(result.exit_code, EXIT_CANCELLED)

    def test_accepting_real_prompt_completes_run_and_exits_zero(self):
        """Answering yes to the *real* prompt (no --yes) still works end to
        end -- the accept-side counterpart of ``test_declining_prompt_is_cancelled``
        above. ``test_successful_run_exits_zero`` now covers the ``--yes``
        bypass instead of the real prompt, so this restores coverage of
        actually driving ``Confirm.ask`` to a genuine accepted answer (fix
        round 1 on issue #33): nothing else in the suite exercises "prompt
        appears, user says yes, run completes" as one path.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "pic.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )

        with patch("src.main._stdin_is_interactive", return_value=True):
            result = self.runner.invoke(main, self._args(), input="y\n")

        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertIn("All files processed successfully", result.output)
        self.assertTrue(
            os.path.exists(os.path.join(self.backup_dir, "photos"))
        )

    def test_partial_failure_exit_code_and_message(self):
        """A completed run reporting failures exits 1 with a summary line."""
        fake_results = {
            "status": "completed",
            "files_processed": 1,
            "files_failed": 2,
            "files_quarantined": 0,
        }
        with patch(
            "src.main.CLIInterface.process_with_progress",
            return_value=fake_results,
        ), patch("src.main.CLIInterface.check_directories", return_value=True):
            result = self.runner.invoke(main, self._args("--yes"))

        self.assertEqual(result.exit_code, EXIT_PARTIAL_FAILURE)
        # Rich styles the count separately, so match the surrounding phrase.
        self.assertIn("Completed with", result.output)
        self.assertIn("failures", result.output)

    def test_cancelled_status_skips_results_table(self):
        """A cancelled status exits 130 without rendering a results table."""
        with patch(
            "src.main.CLIInterface.process_with_progress",
            return_value={"status": "cancelled"},
        ), patch("src.main.CLIInterface.check_directories", return_value=True):
            result = self.runner.invoke(main, self._args("--yes"))

        self.assertEqual(result.exit_code, EXIT_CANCELLED)

    def test_value_error_is_configuration_precondition(self):
        """A ValueError from processing is reported as a config precondition."""
        with patch(
            "src.main.CLIInterface.process_with_progress",
            side_effect=ValueError("overlapping directories"),
        ), patch("src.main.CLIInterface.check_directories", return_value=True):
            result = self.runner.invoke(main, self._args("--yes"))

        self.assertEqual(result.exit_code, EXIT_PRECONDITION)
        self.assertIn("Configuration error", result.output)

    def test_keyboard_interrupt_is_cancelled(self):
        """A SIGINT during processing exits with the cancel code."""
        with patch(
            "src.main.CLIInterface.process_with_progress",
            side_effect=KeyboardInterrupt,
        ), patch("src.main.CLIInterface.check_directories", return_value=True):
            result = self.runner.invoke(main, self._args("--yes"))

        self.assertEqual(result.exit_code, EXIT_CANCELLED)
        self.assertIn("interrupted by user", result.output)

    def test_unexpected_exception_is_precondition_with_traceback(self):
        """An unexpected error exits 2; --verbose adds a traceback."""
        with patch(
            "src.main.CLIInterface.process_with_progress",
            side_effect=RuntimeError("boom"),
        ), patch("src.main.CLIInterface.check_directories", return_value=True):
            result = self.runner.invoke(main, self._args("--verbose", "--yes"))

        self.assertEqual(result.exit_code, EXIT_PRECONDITION)
        self.assertIn("Unexpected error", result.output)
        # --verbose prints the traceback frames.
        self.assertIn("RuntimeError", result.output)

    def test_unexpected_exception_traceback_shown_without_verbose(self):
        """The traceback is recorded even when --verbose was not passed.

        Issue #39: the old handler only ever printed the traceback inside
        ``if verbose:`` -- by the time a user without --verbose realizes they
        needed it, the only way to recover it is to reproduce the failure.
        ``logger.exception`` now records it at ERROR unconditionally.

        Fails against the old code: without --verbose, the old handler prints
        only "Unexpected error: boom" and never reaches the
        ``traceback.format_exc()`` branch, so "RuntimeError" never appears in
        the output.
        """
        with patch(
            "src.main.CLIInterface.process_with_progress",
            side_effect=RuntimeError("boom"),
        ), patch("src.main.CLIInterface.check_directories", return_value=True):
            result = self.runner.invoke(main, self._args("--yes"))

        self.assertEqual(result.exit_code, EXIT_PRECONDITION)
        self.assertIn("Unexpected error", result.output)
        self.assertIn("RuntimeError", result.output)
        self.assertIn("Traceback", result.output)


if __name__ == "__main__":
    unittest.main()
