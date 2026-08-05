"""
Tests for the ``--yes`` non-interactive flag (issue #33).

``Confirm.ask`` routes to Python's builtin ``input()``, which raises
``EOFError`` with no TTY attached -- exactly the shape of a cron job, a CI
runner, or any other piped/headless invocation. These tests pin the fix at
three levels:

* The command layer (``main.py``) detects a non-interactive stdin and fails
  fast with an actionable message naming ``--yes``/``--dry-run`` instead of
  letting an ``EOFError`` (or a prompt that just hangs) reach the user.
* ``--yes`` suppresses both confirmation points -- ``check_directories``'s
  empty-export prompt and ``_CLIProgressReporter.on_categorized``'s "proceed"
  prompt -- and lets the real pipeline run to completion without
  ``Confirm.ask`` ever being patched or called.
* Declining the prompt (the ``--yes``-absent path) still aborts before any
  side effect and still exits 130; an auto-confirmed run that actually fails
  still exits 1, not 0 -- ``--yes`` changes whether the run is asked, never
  what exit code its outcome earns.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from src.cli_interface import CLIInterface
from src.main import (
    EXIT_PARTIAL_FAILURE,
    EXIT_PRECONDITION,
    EXIT_SUCCESS,
    main,
)
from tests.fixtures import make_exif_heic, make_exif_jpeg


class TestNonInteractiveGate(unittest.TestCase):
    """The command-layer TTY check in ``main.py``."""

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
            "--export-dir", self.export_dir,
            "--backup-dir", self.backup_dir,
            *extra,
        ]

    def test_no_terminal_no_yes_no_dry_run_fails_with_actionable_message(self):
        """Non-interactive stdin without --yes/--dry-run names the flags.

        ``click.testing.CliRunner`` never presents stdin as a tty (verified:
        even with ``input="y\\n"`` supplied, ``sys.stdin.isatty()`` is
        ``False`` inside the invoked command), so a plain invocation with
        neither escape hatch exercises exactly the no-TTY branch. This must
        fail with the flag-naming message, not the old bare
        ``EOFError``/``Unexpected error`` shape.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "pic.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )

        result = self.runner.invoke(main, self._args())

        self.assertEqual(result.exit_code, EXIT_PRECONDITION)
        self.assertIn("--yes", result.output)
        self.assertIn("--dry-run", result.output)
        self.assertNotIn("EOFError", result.output)
        self.assertNotIn("Unexpected error", result.output)
        # Nothing was attempted: the source file is untouched and no backup
        # tree was created.
        self.assertTrue(os.path.exists(os.path.join(self.export_dir, "pic.jpg")))
        self.assertFalse(os.path.exists(self.backup_dir))

    def test_yes_flag_completes_a_real_run_with_non_interactive_stdin(self):
        """--yes over non-interactive stdin processes a file and exits 0.

        This is the exact scenario the issue's cron recipe needs: no TTY, no
        stdin input at all (``CliRunner``'s default, standing in for
        ``< /dev/null``), and no ``Confirm.ask`` patch anywhere.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "pic.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )

        result = self.runner.invoke(main, self._args("--yes"))

        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertNotIn("EOFError", result.output)
        self.assertTrue(
            os.path.exists(os.path.join(self.backup_dir, "photos"))
        )

    def test_dry_run_completes_non_interactively_over_empty_export(self):
        """--dry-run never prompts, even against an empty export/ and no TTY.

        The empty-export "Continue anyway?" prompt in ``check_directories``
        guards against a real, destructive run over nothing -- a dry run
        touches nothing, so that risk does not exist and the prompt must not
        block it either (issue #33 point 4).
        """
        result = self.runner.invoke(main, self._args("--dry-run"))

        self.assertEqual(result.exit_code, EXIT_SUCCESS)
        self.assertNotIn("EOFError", result.output)


class TestYesFlagSuppressesConfirmPrompt(unittest.TestCase):
    """``CLIInterface.process_with_progress(yes=True)``: the seam directly."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)
        self.cli = CLIInterface(self.export_dir, self.backup_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_yes_true_runs_the_full_pipeline_without_patching_confirm_ask(self):
        """yes=True proceeds and files the photo -- Confirm.ask is never
        patched or called, proving the prompt is genuinely skipped rather
        than auto-answered.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "pic.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )

        with patch("src.cli_interface.Confirm.ask") as mock_confirm:
            result = self.cli.process_with_progress(dry_run=False, yes=True)

        mock_confirm.assert_not_called()
        self.assertEqual(result.get("status"), "completed")
        self.assertEqual(result.get("files_processed"), 1)
        self.assertTrue(
            os.path.isdir(os.path.join(self.backup_dir, "photos"))
        )

    def test_without_yes_the_prompt_still_appears(self):
        """yes=False (the default) still calls Confirm.ask -- unchanged."""
        make_exif_jpeg(
            os.path.join(self.export_dir, "pic.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )

        with patch(
            "src.cli_interface.Confirm.ask", return_value=True
        ) as mock_confirm:
            self.cli.process_with_progress(dry_run=False, yes=False)

        mock_confirm.assert_called_once()

    def test_declining_without_yes_aborts_before_any_side_effect(self):
        """Declining the prompt (no --yes) deletes no sidecar and moves no
        file, and the run is tagged 'cancelled' -- exactly the outcome
        ``determine_exit_code`` maps to 130.
        """
        make_exif_jpeg(
            os.path.join(self.export_dir, "pic.jpg"),
            date_time_original="2024:01:15 14:30:45",
        )
        sidecar = os.path.join(self.export_dir, "pic.aae")
        open(sidecar, "wb").close()

        with patch("src.cli_interface.Confirm.ask", return_value=False):
            result = self.cli.process_with_progress(dry_run=False, yes=False)

        self.assertEqual(result, {"status": "cancelled"})
        # Nothing touched: the sidecar is still there and no photo landed.
        self.assertTrue(os.path.exists(sidecar))
        self.assertFalse(os.path.exists(os.path.join(self.backup_dir, "photos")))

    def test_yes_true_skips_the_empty_export_prompt_too(self):
        """check_directories(auto_confirm=True) proceeds over an empty
        export/ without ever calling Confirm.ask."""
        with patch("src.cli_interface.Confirm.ask") as mock_confirm:
            ready = self.cli.check_directories(auto_confirm=True)

        mock_confirm.assert_not_called()
        self.assertTrue(ready)
        self.assertTrue(os.path.isdir(self.backup_dir))


class TestAutoConfirmedRunKeepsTheRealExitCode(unittest.TestCase):
    """An auto-confirmed run must still report its actual outcome (#31)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_dir = os.path.join(self.temp_dir, "export")
        self.backup_dir = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_yes_with_a_failing_file_still_exits_partial_failure_not_success(self):
        """--yes bypasses the prompt only -- a real per-file failure still
        drives exit code 1, never the success code, through the full
        ``main`` invocation.
        """
        make_exif_heic(
            os.path.join(self.export_dir, "broken.heic"),
            date_time_original="2024:09:10 10:10:10",
        )
        runner = CliRunner()

        with patch(
            "src.heic_converter.HeicConverter.convert_heic_to_jpeg",
            return_value=None,
        ):
            result = runner.invoke(
                main,
                [
                    "--export-dir", self.export_dir,
                    "--backup-dir", self.backup_dir,
                    "--yes",
                ],
            )

        self.assertEqual(result.exit_code, EXIT_PARTIAL_FAILURE)
        self.assertNotEqual(result.exit_code, EXIT_SUCCESS)
        self.assertNotIn("Success!", result.output)

    def test_yes_with_a_clean_run_still_exits_success(self):
        """--yes over a clean run exits 0, not some other auto-confirm code."""
        make_exif_jpeg(
            os.path.join(self.export_dir, "good.jpg"),
            date_time_original="2024:08:08 08:08:08",
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

        self.assertEqual(result.exit_code, EXIT_SUCCESS)


if __name__ == "__main__":
    unittest.main()
