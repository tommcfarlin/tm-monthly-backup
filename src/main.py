#!/usr/bin/env python3
"""
tm-monthly-backup: CLI utility for organizing Apple Photos exports
"""

import sys
import click

from src.cli_interface import CLIInterface, setup_logging


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


@click.command()
@click.option('--dry-run', is_flag=True, help='Show what would be done without making changes')
@click.option('--yes', '-y', is_flag=True,
              help='Assume yes for all prompts (required for non-interactive use)')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
@click.option('--export-dir', default='export', help='Directory containing exported files (default: export)')
@click.option('--backup-dir', default='backup', help='Directory for organized output (default: backup)')
@click.option('--jpeg-quality', type=click.IntRange(1, 100), default=95,
              show_default=True, help='JPEG quality for HEIC conversion')
@click.option('--keep-heic', is_flag=True,
              help='Keep original HEIC files after conversion')
@click.version_option(version='1.0.0', prog_name='tm-monthly-backup')
def main(dry_run, yes, verbose, export_dir, backup_dir, jpeg_quality, keep_heic):
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

    --jpeg-quality controls the HEIC->JPEG encode quality (1-100, default 95).
    --keep-heic leaves the original .heic/.heif file in export/ after a
    verified-good conversion instead of deleting it; a conversion that fails
    verification is still recorded as a failure either way.
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
    cli = CLIInterface(export_dir, backup_dir, jpeg_quality=jpeg_quality, keep_heic=keep_heic)

    # Display welcome banner
    cli.display_welcome()

    # Check directories and prerequisites. A directory problem (missing export
    # dir, unwritable backup dir, or the #52 overlap rejection) is a
    # precondition failure -- nothing was attempted -- so it exits distinctly
    # from a partial processing failure. ``auto_confirm`` skips the
    # empty-export "Continue anyway?" prompt: --yes is the explicit
    # non-interactive opt-out, and --dry-run touches nothing, so that prompt
    # guards no risk on a dry run either (issue #33).
    if not cli.check_directories(auto_confirm=yes or dry_run):
        cli.console.print("[red]Cannot proceed due to directory issues.[/red]")
        sys.exit(EXIT_PRECONDITION)

    try:
        # Process files with beautiful progress indicators
        results = cli.process_with_progress(dry_run=dry_run, yes=yes)

        exit_code = determine_exit_code(results)

        if results.get('status') == 'cancelled':
            # process_with_progress already printed the cancellation notice;
            # exit with the POSIX cancel code without rendering a results table.
            sys.exit(exit_code)

        # Display results
        cli.display_results(results, dry_run=dry_run)

        # Report and exit with the code the taxonomy chose.
        if exit_code == EXIT_PARTIAL_FAILURE:
            cli.console.print(f"\n[yellow]Completed with {results['files_failed']} failures.[/yellow]")
        elif not dry_run and results.get('files_processed', 0) > 0:
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
        if verbose:
            import traceback
            cli.console.print(traceback.format_exc())
        sys.exit(EXIT_PRECONDITION)


if __name__ == '__main__':
    main()