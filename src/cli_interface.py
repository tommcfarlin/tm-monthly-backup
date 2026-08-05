"""
Rich CLI interface with progress bars and beautiful output
"""

import os
import re
import sys
import logging
from typing import Dict, List, Optional
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.markup import escape
from rich.logging import RichHandler
from rich.prompt import Confirm

from .file_processor import FileProcessor, ProgressReporter

# Initialize rich console
console = Console()

# Filenames reach this tool untrusted -- from iCloud, AirDrop, downloads, and
# manual renames -- and flow into ``rich`` render paths and log records. Two
# hostile shapes must be neutralized before any value is displayed:
#
# * ``rich`` markup: square-bracket tags such as ``[bold]`` or ``[/]`` are
#   parsed in ``Console.print`` and ``Table.add_row``. An unmatched tag raises
#   ``MarkupError`` (aborting the run); a valid tag is silently swallowed.
# * ANSI / control sequences: raw escape codes reach the terminal unfiltered and
#   can reposition the cursor or rewrite already-printed output.
#
# ``escape`` (from ``rich.markup``) handles the first, but leaves control bytes
# untouched, so a dedicated sanitizer strips the second.

# A full ANSI escape sequence: ESC, an optional intermediate, a final byte.
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# C0/C1 control characters, excluding the ordinary whitespace (tab, newline,
# carriage return) that legitimately appears in the tool's own rendered text.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def sanitize_for_display(value: object) -> str:
    """
    Strip ANSI escape and control sequences from a value for safe display.

    Removes anything a hostile filename could use to drive the terminal (color
    codes, cursor movement, screen rewrites) while leaving ordinary printable
    text -- brackets included -- intact. Markup neutralization is a separate
    concern handled by :func:`safe_markup`; this function alone is correct for
    ``rich`` contexts that do not parse markup (e.g. ``Text.append``).

    Args:
        value: Any value; coerced to ``str`` before sanitizing.

    Returns:
        The value with ANSI/control sequences removed.
    """
    text = str(value)
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _CONTROL_CHARS_RE.sub("", text)
    return text


def safe_markup(value: object) -> str:
    """
    Make an untrusted value safe to interpolate into a markup-parsed context.

    Strips ANSI/control sequences (via :func:`sanitize_for_display`) and then
    escapes ``rich`` markup so square-bracket tags render literally instead of
    being parsed. Use this for every untrusted value entering ``Console.print``
    f-strings and ``Table.add_row`` cells.

    Args:
        value: Any value; coerced to ``str`` before sanitizing.

    Returns:
        A string safe to render where ``rich`` parses markup.
    """
    return escape(sanitize_for_display(value))


class _SanitizingLogFilter(logging.Filter):
    """
    Strip ANSI/control sequences from fully rendered log messages.

    Attached to the ``RichHandler``, this collapses each record to its final
    text (applying any lazy ``%s`` arguments) and removes control sequences, so
    an untrusted filename in a log message cannot drive the terminal. Combined
    with ``markup=False`` on the handler, it makes the entire logging path safe
    at a single boundary regardless of how individual call sites format.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_for_display(record.getMessage())
        record.args = ()
        return True


def setup_logging(verbose: bool = False):
    """
    Setup logging with rich handler for beautiful output.

    Args:
        verbose: Enable verbose logging
    """
    log_level = logging.DEBUG if verbose else logging.INFO

    # Configure rich logging handler. ``markup=False`` is deliberate: log
    # messages carry untrusted filenames, and parsing markup in them lets a
    # crafted name either abort the run (unmatched tag -> MarkupError) or vanish
    # from the log (valid tag -> swallowed). The tool never emits intentional
    # markup through the logger -- styled output goes through the console
    # directly -- so disabling it costs nothing and closes the hole (issue #9).
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=verbose,
        markup=False
    )
    # Neutralize any ANSI/control sequences carried by untrusted values before
    # they reach the terminal, at one boundary for every log record.
    rich_handler.addFilter(_SanitizingLogFilter())

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[rich_handler]
    )

    # Reduce pillow logging noise
    logging.getLogger('PIL').setLevel(logging.WARNING)


class CLIInterface:
    """Rich CLI interface for the file processor"""

    def __init__(self, export_dir: str = "export", backup_dir: str = "backup"):
        """
        Initialize CLI interface.

        Args:
            export_dir: Export directory path
            backup_dir: Backup directory path
        """
        self.processor = FileProcessor(export_dir, backup_dir)
        self.console = console

    def display_welcome(self):
        """Display welcome banner"""
        welcome_text = Text("tm-monthly-backup", style="bold blue")
        welcome_text.append("\nAutomated Apple Photos Organization Tool", style="dim")

        panel = Panel(
            welcome_text,
            title="Welcome",
            border_style="blue",
            padding=(1, 2)
        )
        self.console.print(panel)

    def check_directories(self) -> bool:
        """
        Check if required directories exist and are accessible.

        Returns:
            True if directories are ready, False otherwise
        """
        export_path = Path(self.processor.export_dir)
        backup_path = Path(self.processor.backup_dir)

        # Refuse to run when the export and backup directories overlap (same
        # directory, or one nested inside the other). Such a configuration lets
        # a run consume and clobber its own inputs, so reject it up front before
        # creating anything.
        overlap_error = FileProcessor.directory_overlap_error(
            self.processor.export_dir, self.processor.backup_dir
        )
        if overlap_error:
            self.console.print(f"[red]Error: {safe_markup(overlap_error)}[/red]")
            return False

        # Check export directory
        safe_export = safe_markup(export_path)
        if not export_path.exists():
            self.console.print(f"[red]Error: Export directory does not exist: {safe_export}[/red]")
            self.console.print(f"[yellow]Please create the directory and place your exported photos there:[/yellow]")
            self.console.print(f"[dim]  mkdir {safe_export}[/dim]")
            self.console.print(f"[dim]  # Then copy your iCloud Photos export files to {safe_export}/[/dim]")
            return False

        if not any(export_path.iterdir()):
            self.console.print(f"[yellow]Warning: Export directory is empty: {safe_export}[/yellow]")
            if not Confirm.ask("Continue anyway?"):
                return False

        # Create backup directory if needed
        try:
            backup_path.mkdir(parents=True, exist_ok=True)
            self.console.print(f"[green]✓[/green] Backup directory ready: {safe_markup(backup_path)}")
        except Exception as e:
            self.console.print(f"[red]Error: Cannot create backup directory {safe_markup(backup_path)}: {safe_markup(e)}[/red]")
            return False

        return True

    def display_file_scan_results(self, files: List[str]):
        """
        Display file scan results in a beautiful table.

        Args:
            files: List of discovered files
        """
        if not files:
            self.console.print("[yellow]No files found to process[/yellow]")
            return

        # Categorize files for display
        self.processor.categorizer.batch_categorize(files)
        stats = self.processor.categorizer.get_categorization_stats()
        self.display_categorization_summary(stats)

    def display_categorization_summary(self, stats: Dict[str, int]):
        """
        Render the file-discovery table from a categorization stats dict.

        Split out of :meth:`display_file_scan_results` so the run path can
        render the discovery table from the single categorization pass
        ``FileProcessor`` already performed -- no second ``batch_categorize``
        (issue #13).

        Args:
            stats: Categorization counts from
                :meth:`FileCategorizer.get_categorization_stats`.
        """
        # Create summary table
        table = Table(title="File Discovery Summary", show_header=True, header_style="bold magenta")
        table.add_column("Category", style="cyan", width=15)
        table.add_column("Count", justify="right", style="green")
        table.add_column("Description", style="dim")

        table.add_row("Photos", str(stats['photos']), "JPEG, PNG, HEIC, etc.")
        table.add_row("Videos", str(stats['videos']), "MOV, MP4, M4V, etc.")
        table.add_row("Screenshots", str(stats['screenshots']), "PNG files with screenshot patterns")
        table.add_row("Sidecar Files", str(stats['sidecar']), "Apple .aae files (will be deleted)")

        if stats['unknown'] > 0:
            table.add_row("Unknown", str(stats['unknown']), "Unrecognized file types", style="yellow")

        table.add_row("", "", "", style="dim")
        table.add_row("Total", str(stats['total']), "Files to process", style="bold")

        self.console.print(table)

    def process_with_progress(self, dry_run: bool = False) -> Dict:
        """
        Process files with beautiful progress indicators.

        Drives the run through the single public ``FileProcessor`` entry point
        (:meth:`FileProcessor.process_all_files`), passing a
        :class:`_CLIProgressReporter` that renders the discovery table, runs the
        confirmation prompt, and advances two real per-phase progress bars --
        HEIC conversion and file organization -- from actual per-file
        completions (issue #14). All scanning, categorization, sidecar
        deletion, and summary generation now happen exactly once, inside the
        processor -- this method reaches into no private members and
        re-drives no part of the pipeline (issue #13).

        Args:
            dry_run: If True, only show what would be done

        Returns:
            Processing results dictionary
        """
        reporter = _CLIProgressReporter(self, dry_run)
        try:
            summary = self.processor.process_all_files(
                dry_run=dry_run, progress=reporter
            )
        finally:
            reporter.close()

        # Tag the outcome so the caller can distinguish a completed run from a
        # cancelled or empty one and choose an exit code accordingly (#31).
        if reporter.no_files:
            # Nothing to do: distinct from "cancelled" and "done".
            summary['status'] = 'no_files'
            return summary
        if reporter.cancelled:
            # Signal cancellation explicitly rather than with an empty dict so
            # the caller exits with the POSIX cancel code (130) and never
            # confuses a declined run with a clean success (issue #31).
            return {'status': 'cancelled'}

        summary['status'] = 'completed'
        return summary

    def display_results(self, results: Dict, dry_run: bool = False):
        """
        Display processing results in a beautiful summary.

        Args:
            results: Processing results dictionary
            dry_run: Whether this was a dry run
        """
        # Nothing to render for an empty, cancelled, or no-work result: the
        # relevant notice was already printed by ``process_with_progress``.
        if not results or results.get('status') in ('cancelled', 'no_files'):
            return

        # Success/failure summary
        success_count = results.get('files_processed', 0)
        failure_count = results.get('files_failed', 0)
        quarantine_count = results.get('files_quarantined', 0)

        if dry_run:
            title = "Dry Run Results"
            title_style = "bold blue"
        elif failure_count > 0:
            title = "Processing Complete - With Errors"
            title_style = "bold yellow"
        elif quarantine_count > 0:
            # No file errored, but undecodable files were quarantined rather than
            # archived. That is not an unqualified success -- the user has files
            # in backup/corrupt/ to review -- so the banner says so (issue #58).
            title = "Processing Complete - Files Quarantined"
            title_style = "bold yellow"
        else:
            title = "Processing Complete - Success!"
            title_style = "bold green"

        # Create results table
        table = Table(title=title, show_header=True, header_style="bold magenta")
        table.add_column("Metric", style="cyan", width=25)
        table.add_column("Count", justify="right", style="green")

        table.add_row("Files Processed", str(success_count))
        table.add_row("HEIC Conversions", str(results.get('heic_conversions', 0)))
        table.add_row("Missing EXIF Files", str(results.get('missing_exif_files', 0)))

        if quarantine_count > 0:
            table.add_row(
                "Quarantined (undecodable)", str(quarantine_count), style="yellow"
            )

        if failure_count > 0:
            table.add_row("Failed Files", str(failure_count), style="red")

        if results.get('heic_conversion_failures', 0) > 0:
            table.add_row("HEIC Conversion Failures", str(results['heic_conversion_failures']), style="red")

        self.console.print(table)

        # Category breakdown
        if 'categorization_stats' in results:
            stats = results['categorization_stats']

            breakdown_table = Table(title="File Organization", show_header=True, header_style="bold cyan")
            breakdown_table.add_column("Category", style="cyan")
            breakdown_table.add_column("Files", justify="right", style="green")
            breakdown_table.add_column("Location", style="dim")

            breakdown_table.add_row("Photos", str(stats.get('photos', 0)), "backup/photos/")
            breakdown_table.add_row("Videos", str(stats.get('videos', 0)), "backup/videos/")
            breakdown_table.add_row("Screenshots", str(stats.get('screenshots', 0)), "backup/screenshots/")

            if stats.get('generated', 0) > 0:
                breakdown_table.add_row("Generated", str(stats['generated']), "backup/generated/", style="magenta")

            if stats.get('unknown', 0) > 0:
                breakdown_table.add_row("Unknown", str(stats['unknown']), "backup/unknown/", style="yellow")

            if quarantine_count > 0:
                breakdown_table.add_row(
                    "Quarantined", str(quarantine_count), "backup/corrupt/", style="yellow"
                )

            self.console.print(breakdown_table)

        # Display failures if any
        if failure_count > 0:
            self.display_failures(results.get('failed_files', []))

        # Display missing EXIF files if any
        missing_exif = results.get('missing_exif_list', [])
        if missing_exif:
            self.display_missing_exif_warning(missing_exif)

    def display_failures(self, failed_files: List):
        """
        Display failed files in a table.

        Args:
            failed_files: List of failed file tuples
        """
        if not failed_files:
            return

        self.console.print(f"\n[bold red]Failed Files ({len(failed_files)}):[/bold red]")

        failure_table = Table(show_header=True, header_style="bold red")
        failure_table.add_column("Operation", style="red")
        failure_table.add_column("File", style="cyan")
        failure_table.add_column("Error", style="yellow")

        for operation, file_path, error in failed_files:
            # ``Table`` parses markup in string cells, so every untrusted value
            # (the path, and errors that embed a path) must be escaped and
            # stripped of control sequences before it becomes a row.
            failure_table.add_row(
                safe_markup(operation),
                safe_markup(file_path),
                safe_markup(error),
            )

        self.console.print(failure_table)

    def display_missing_exif_warning(self, missing_files: List[Dict[str, Optional[str]]]):
        """
        Display warning about files with missing EXIF data.

        Each entry names a path the user can actually go look at (issue #35):
        by the time this renders, a real run has already moved/renamed the
        source file, so ``'original_path'`` alone would name something that no
        longer exists. Entries carry ``'final_path'`` (the file's location in
        ``backup/``) when the file was actually filed there, plus
        ``'original_path'`` shown parenthetically for identification (e.g. "was
        export/IMG_1234.jpg"). A dry run never moves anything, so its entries
        carry ``'final_path': None`` and only the still-existing
        ``'original_path'`` is shown.

        Args:
            missing_files: List of ``{'original_path', 'final_path'}`` dicts
                for files with missing/invalid EXIF data.
        """
        warning_text = Text(f"Found {len(missing_files)} files with missing/invalid EXIF data.", style="bold yellow")
        warning_text.append("\nThese files were processed using filesystem timestamps.", style="dim")
        warning_text.append("\nConsider manually reviewing these files:", style="dim")

        for record in missing_files[:5]:  # Show first 5
            original_path = record.get('original_path', '')
            final_path = record.get('final_path')
            # Show the path that genuinely exists on disk right now: the
            # backup/ destination once the file has landed there, or -- for a
            # dry run, which moves nothing -- the still-in-place original. The
            # original is included parenthetically either way as the one clue
            # to the file's pre-backup identity (its export/ name).
            if final_path:
                line = f"{final_path}  (was {original_path})"
            else:
                line = original_path
            # ``Text.append`` renders its argument literally (no markup parsing),
            # so escaping would corrupt the name; only strip control sequences.
            warning_text.append(f"\n  • {sanitize_for_display(line)}", style="yellow")

        if len(missing_files) > 5:
            warning_text.append(f"\n  ... and {len(missing_files) - 5} more", style="dim")

        panel = Panel(
            warning_text,
            title="Missing EXIF Data Warning",
            border_style="yellow",
            padding=(1, 2)
        )
        self.console.print(panel)


class _CLIProgressReporter(ProgressReporter):
    """
    Bridge ``FileProcessor``'s run to the rich CLI (issues #13, #14).

    Implements the :class:`~src.file_processor.ProgressReporter` seam so
    ``process_all_files`` can render, gate, and report a run without the CLI
    reaching into any private processor method. Responsibilities:

    * :meth:`on_no_files` -- print the "nothing to do" notice and flag it.
    * :meth:`on_categorized` -- render the discovery table, print the dry-run
      notice, and (for a real run) run the confirmation prompt. Declining aborts
      the run before anything is touched. On proceeding, it starts two real
      progress bars sized from data the seam now carries: a conversion bar
      (total = the ``'heic'`` count) shown only when there is at least one HEIC
      file, and an organize bar (total = every processable file).
    * :meth:`on_heic_converted` -- advance the conversion bar by one, with the
      just-converted file's name in its description. This is what makes the
      conversion bar move *during* the parallel HEIC pool phase (issue #42)
      for large batches, rather than only once the (much faster) sequential
      place phase reaches those files afterward.
    * :meth:`on_file` -- advance the organize bar by one, with the file's name
      in its description. Fires for every processable file exactly once,
      regardless of ``action``.

    Every filename entering a task description is reduced to its basename and
    passed through ``safe_markup`` (issue #9): filenames are untrusted and
    ``rich`` parses bracketed text in task descriptions as markup.

    The ``no_files`` / ``cancelled`` flags let
    :meth:`CLIInterface.process_with_progress` tag the summary (and pick an exit
    code) without inspecting private state.
    """

    def __init__(self, cli: "CLIInterface", dry_run: bool):
        """
        Args:
            cli: The owning interface (for its console and render helpers).
            dry_run: Whether this run is a dry run (skips the confirm prompt).
        """
        self.cli = cli
        self.dry_run = dry_run
        self.no_files = False
        self.cancelled = False
        self._progress = None
        self._convert_task = None
        self._organize_task = None

    def on_no_files(self) -> None:
        """Record and announce that the scan found nothing to process."""
        self.no_files = True
        self.cli.console.print("[yellow]No files found to process[/yellow]")

    def on_categorized(self, total: int, stats: Dict[str, int]) -> bool:
        """
        Render the discovery table, then gate the run on the confirm prompt.

        Args:
            total: Number of scanned files (the count the prompt quotes).
            stats: Categorization counts for the discovery table, plus the
                ``'heic'`` count issue #14 added to size the conversion bar.

        Returns:
            True to proceed, False to abort (user declined the prompt).
        """
        self.cli.display_categorization_summary(stats)

        if self.dry_run:
            self.cli.console.print(
                "\n[bold blue]DRY RUN MODE[/bold blue] - No files will be modified"
            )
        else:
            if not Confirm.ask(f"\nProceed with processing {total} files?"):
                self.cli.console.print("[yellow]Processing cancelled[/yellow]")
                self.cancelled = True
                return False

        # Start the bars only after any prompt is answered, so the live
        # display never overlaps the interactive confirm. Sidecars are
        # deleted, not processed, so the organize total excludes them.
        processable_total = total - stats.get('sidecar', 0)
        heic_total = stats.get('heic', 0)
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=self.cli.console,
        )
        self._progress.start()
        # Only shown when there is HEIC work that will actually run: a batch
        # with no HEIC files would show a permanently-empty bar for a phase
        # that never runs, and a dry run converts nothing at all (issue #10),
        # so a HEIC-carrying dry run would show the same thing -- a spinning
        # 0/N that reads as work pending or hung, when there is in fact no
        # conversion phase in a dry run to report progress on.
        if heic_total > 0 and not self.dry_run:
            self._convert_task = self._progress.add_task(
                "Converting HEIC files...", total=heic_total
            )
        # ``total=processable_total`` (never ``or None``): an all-sidecar
        # batch reaches here with ``processable_total == 0`` (files exist, so
        # ``on_no_files`` never fired), and a bar with a real total of 0 is
        # immediately, truthfully complete -- an indeterminate spinner would
        # instead sit there forever, since there is nothing left to advance it.
        self._organize_task = self._progress.add_task(
            "Organizing files...", total=processable_total
        )
        return True

    def on_heic_converted(self, path: str) -> None:
        """Advance the conversion bar by one, naming the file just converted."""
        if self._progress is None or self._convert_task is None:
            return
        name = safe_markup(os.path.basename(path))
        self._progress.update(
            self._convert_task, description=f"Converting {name}..."
        )
        self._progress.advance(self._convert_task)

    def on_file(self, path: str, category: str, action: str) -> None:
        """Advance the organize bar by one, naming the file just handled."""
        if self._progress is None or self._organize_task is None:
            return
        name = safe_markup(os.path.basename(path))
        self._progress.update(
            self._organize_task, description=f"Organizing {name}..."
        )
        self._progress.advance(self._organize_task)

    def close(self) -> None:
        """Stop the live progress display if it was started."""
        if self._progress is not None:
            self._progress.stop()
            self._progress = None