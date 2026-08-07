#!/usr/bin/env python3
"""
tm-monthly-backup: CLI utility for organizing Apple Photos exports
"""

import logging
import sys
import click

from src import __version__
from src.cli_interface import CLIInterface, is_unqualified_success, setup_logging
from src.file_processor import Settings

logger = logging.getLogger(__name__)


# Exit code taxonomy. Each code carries exactly one meaning so a caller can act
# on the result of a run (documented in ``docs/cli-usage.md``, issue #31).
EXIT_SUCCESS = 0            # every discovered file was processed; no failures
EXIT_PARTIAL_FAILURE = 1    # processing ran but one or more files failed
EXIT_PRECONDITION = 2       # cannot run: bad/overlapping dirs, unexpected error
EXIT_CANCELLED = 130        # user declined the prompt or sent SIGINT (128 + 2)


def _stdin_is_interactive() -> bool:
    """
    Report whether stdin is an interactive terminal.

    Factored out of :func:`main` so the non-interactive gate below can be
    exercised in tests without a real TTY. Patching ``sys.stdin.isatty``
    directly does not reliably work under ``click.testing.CliRunner``: the
    runner replaces the ``sys.stdin`` object itself during ``invoke()``, so a
    patch applied to whatever object was ``sys.stdin`` beforehand attaches to
    an object the runner immediately discards. Patching this function's
    return value instead is unaffected by that swap.

    Guarded defensively: ``sys.stdin`` can be ``None`` (a detached or
    GUI-launched process has no ``isatty`` attribute at all -- an
    ``AttributeError`` on lookup) or a closed stream (``isatty()`` itself
    raises ``ValueError`` rather than returning a bool). Either would
    otherwise escape as a raw traceback in exactly the headless context this
    flag exists to serve, which is the specific outcome the gate's actionable
    message (acceptance criterion 3) is supposed to prevent -- so both are
    treated as "not interactive" rather than left to propagate.

    Returns:
        ``sys.stdin.isatty()``, or ``False`` if stdin is missing, closed, or
        otherwise cannot answer the question.
    """
    try:
        isatty = getattr(sys.stdin, "isatty", None)
        return bool(isatty and isatty())
    except (AttributeError, ValueError):
        return False


def determine_exit_code(results: dict) -> int:
    """
    Map a processing-results dict to a process exit code.

    The result is classified before its failure count is consulted so a
    cancelled run is never reported as a success:

    * ``status == "cancelled"`` -> :data:`EXIT_CANCELLED`.
    * any recorded per-file failure -> :data:`EXIT_PARTIAL_FAILURE`.
    * otherwise (including an empty or dry run) -> :data:`EXIT_SUCCESS`.

    Precondition failures (missing/overlapping directories, unexpected
    exceptions) are decided by the caller before or around processing and map to
    :data:`EXIT_PRECONDITION`; they never reach this function.

    Args:
        results: The dict returned by ``CLIInterface.process_with_progress``.

    Returns:
        One of the module-level ``EXIT_*`` codes.
    """
    if results.get('status') == 'cancelled':
        return EXIT_CANCELLED
    if results.get('files_failed', 0) > 0:
        return EXIT_PARTIAL_FAILURE
    return EXIT_SUCCESS


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option('--dry-run', is_flag=True, help='Show what would be done without making changes')
@click.option('--yes', '-y', is_flag=True,
              help='Assume yes for all prompts (required for non-interactive use)')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
@click.option('--export-dir', default='export', help='Directory containing exported files (default: export)')
@click.option('--backup-dir', default='backup', help='Directory for organized output (default: backup)')
@click.option('--jpeg-quality', type=click.IntRange(1, 100), default=Settings().jpeg_quality,
              show_default=True, help='JPEG quality for HEIC conversion')
@click.version_option(version=__version__, prog_name='tm-monthly-backup')
def main(dry_run, yes, verbose, export_dir, backup_dir, jpeg_quality):
    """
    Process exported Apple Photos files and organize them by type.

    This tool will:
    - Convert HEIC files to JPEG with EXIF preservation
    - Rename files using EXIF timestamp format (YYYY.MM.DD.HH.MM.SS)
    - Organize files into photos/, videos/, and screenshots/ directories
    - Remove Apple sidecar (.aae) files
    - Handle duplicate timestamps intelligently

    Interactive confirmation prompts require a terminal. Pass --yes to assume
    yes for all of them (needed for cron, CI, or any other non-interactive
    invocation); --dry-run never prompts, since it makes no changes to confirm.

    --jpeg-quality controls the HEIC->JPEG encode quality (1-100, default 98).
    A converted HEIC's original is deleted once its conversion is verified
    good; a conversion that fails verification is recorded as a failure and
    the original is left in place.
    """

    # A run with no terminal to prompt from (cron, CI, `nohup`, a piped
    # invocation) would otherwise hit a bare, unexplained EOFError once a
    # confirmation prompt is actually reached. Fail fast here instead, before
    # any other setup, with an actionable instruction naming both opt-outs:
    # --yes assumes yes for every prompt, and --dry-run never prompts at all
    # because it makes no change that needs confirming (issue #33).
    if not _stdin_is_interactive() and not (yes or dry_run):
        raise click.UsageError(
            "No terminal available for confirmation. Re-run with --yes or --dry-run."
        )

    # Setup logging with rich formatting
    setup_logging(verbose)

    # Initialize CLI interface
    cli = CLIInterface(export_dir, backup_dir, jpeg_quality=jpeg_quality)

    # Display welcome banner
    cli.display_welcome()

    try:
        # Check directories and prerequisites. A directory problem (missing
        # export dir, unwritable backup dir, or the #52 overlap rejection) is
        # a precondition failure -- nothing was attempted -- so it exits
        # distinctly from a partial processing failure. ``auto_confirm`` skips
        # the empty-export "Continue anyway?" prompt: --yes is the explicit
        # non-interactive opt-out, and --dry-run touches nothing, so that
        # prompt guards no risk on a dry run either (issue #33).
        #
        # This call is now INSIDE the try (issue #39 fix round 1): narrowing
        # check_directories' own mkdir catch from Exception to OSError means a
        # non-OSError bug (a malformed --backup-dir producing a TypeError, for
        # instance) propagates out of check_directories rather than being
        # mislabeled and swallowed there. Before this move, check_directories
        # was called BEFORE this try block even started, so that propagating
        # exception would have gone straight past main() uncaught -- a raw,
        # unsanitized crash with exit code 1 (colliding with
        # EXIT_PARTIAL_FAILURE's documented meaning) and no logger.exception,
        # no traceback recorded, no "Unexpected error" message. Moving the
        # call inside the try is what makes the iterdir()/mkdir narrowing
        # actually deliver on its promise that the real exception type
        # propagates and is reported, instead of only being true up to the
        # boundary of this function.
        if not cli.check_directories(auto_confirm=yes or dry_run):
            cli.console.print("[red]Cannot proceed due to directory issues.[/red]")
            sys.exit(EXIT_PRECONDITION)

        # Process files with beautiful progress indicators
        results = cli.process_with_progress(dry_run=dry_run, yes=yes)

        exit_code = determine_exit_code(results)

        if results.get('status') == 'cancelled':
            # process_with_progress already printed the cancellation notice;
            # exit with the POSIX cancel code without rendering a results table.
            sys.exit(exit_code)

        # Display results
        cli.display_results(results, dry_run=dry_run)

        # Report and exit with the code the taxonomy chose. The unqualified
        # "success" message consults the exact same predicate
        # ``CLIInterface.display_results`` uses to decide its own title
        # (``is_unqualified_success``) rather than recomputing its own
        # answer -- issue #30's fix round 1 caught this file independently
        # re-deriving "does this run look clean," which is precisely the
        # kind of duplicated decision that drifts the moment one side
        # changes and the other does not. A quarantined file or a kept
        # sidecar candidate is not an unqualified success (issues #58/#57),
        # and neither is a file the scan skipped for an unexpected
        # (non-junk) reason (issue #30); a junk-only skip
        # (.DS_Store/.localized/Thumbs.db) does not disqualify a run --
        # see ``is_unqualified_success``'s docstring.
        if exit_code == EXIT_PARTIAL_FAILURE:
            cli.console.print(f"\n[yellow]Completed with {results['files_failed']} failures.[/yellow]")
        elif (
            not dry_run
            and results.get('files_processed', 0) > 0
            and is_unqualified_success(results)
        ):
            cli.console.print("\n[bold green]All files processed successfully![/bold green]")
        elif dry_run:
            cli.console.print("\n[blue]Dry run completed. Use without --dry-run to process files.[/blue]")

        sys.exit(exit_code)

    except ValueError as e:
        # Raised by process_all_files when export/backup overlap (#52): a
        # pre-flight misconfiguration, so nothing was processed.
        cli.console.print(f"\n[red]Configuration error: {e}[/red]")
        sys.exit(EXIT_PRECONDITION)
    except KeyboardInterrupt:
        cli.console.print("\n[yellow]Processing interrupted by user.[/yellow]")
        sys.exit(EXIT_CANCELLED)
    except Exception as e:
        cli.console.print(f"\n[red]Unexpected error: {e}[/red]")
        # logger.exception records the traceback at ERROR regardless of
        # --verbose (issue #39): before this, the traceback was discarded
        # unless --verbose happened to be passed, and by the time a user
        # realizes they needed it the only way to recover it is to reproduce
        # the failure. The one-line message above stays -- it is what a
        # user reads first -- while this guarantees the detail needed to
        # actually diagnose the failure is never silently lost. Routed
        # through the logger (not a second console.print) so it passes
        # through setup_logging's sanitizing filter (issue #9): an
        # unexpected exception's own str() can embed an untrusted filename,
        # and that text must not reach the terminal unsanitized.
        logger.exception("Unexpected error: %s", e)
        sys.exit(EXIT_PRECONDITION)


if __name__ == '__main__':
    main()
