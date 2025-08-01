"""
Rich CLI interface with progress bars and beautiful output
"""

import os
import sys
import logging
from typing import Dict, List
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.logging import RichHandler
from rich.prompt import Confirm

from .file_processor import FileProcessor
from .file_categorizer import FileCategory

# Initialize rich console
console = Console()


def setup_logging(verbose: bool = False):
    """
    Setup logging with rich handler for beautiful output.

    Args:
        verbose: Enable verbose logging
    """
    log_level = logging.DEBUG if verbose else logging.INFO

    # Configure rich logging handler
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=verbose,
        markup=True
    )

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

        # Check export directory
        if not export_path.exists():
            self.console.print(f"[red]Error: Export directory does not exist: {export_path}[/red]")
            self.console.print(f"[yellow]Please create the directory and place your exported photos there:[/yellow]")
            self.console.print(f"[dim]  mkdir {export_path}[/dim]")
            self.console.print(f"[dim]  # Then copy your iCloud Photos export files to {export_path}/[/dim]")
            return False

        if not any(export_path.iterdir()):
            self.console.print(f"[yellow]Warning: Export directory is empty: {export_path}[/yellow]")
            if not Confirm.ask("Continue anyway?"):
                return False

        # Create backup directory if needed
        try:
            backup_path.mkdir(parents=True, exist_ok=True)
            self.console.print(f"[green]✓[/green] Backup directory ready: {backup_path}")
        except Exception as e:
            self.console.print(f"[red]Error: Cannot create backup directory {backup_path}: {e}[/red]")
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

        Args:
            dry_run: If True, only show what would be done

        Returns:
            Processing results dictionary
        """
        # Scan files first
        with self.console.status("[bold green]Scanning export directory...") as status:
            files = self.processor._scan_export_directory()

        if not files:
            self.console.print("[yellow]No files found to process[/yellow]")
            return {}

        self.display_file_scan_results(files)

        if dry_run:
            self.console.print(f"\n[bold blue]DRY RUN MODE[/bold blue] - No files will be modified")

        # Confirm processing
        if not dry_run:
            if not Confirm.ask(f"\nProceed with processing {len(files)} files?"):
                self.console.print("[yellow]Processing cancelled[/yellow]")
                return {}

        # Process with progress tracking
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=self.console
        ) as progress:

            # Add tasks for different phases
            categorize_task = progress.add_task("Categorizing files...", total=1)
            sidecar_task = progress.add_task("Processing sidecar files...", total=1)
            convert_task = progress.add_task("Converting HEIC files...", total=None)
            organize_task = progress.add_task("Organizing files...", total=None)

            # Categorize files
            categorized = self.processor.categorizer.batch_categorize(files)
            progress.update(categorize_task, completed=1)

            # Delete sidecar files
            sidecar_files = categorized.get(FileCategory.SIDECAR, [])
            if sidecar_files:
                progress.update(sidecar_task, total=len(sidecar_files))
                self.processor._delete_sidecar_files(sidecar_files, dry_run)
            progress.update(sidecar_task, completed=progress.tasks[sidecar_task].total or 1)

            # Process all files using the FileProcessor's main method
            # This replaces the duplicate processing loop that was causing duplicates
            self.processor.process_all_files(dry_run=dry_run)

            # Update progress bars to completed
            progress.update(convert_task, completed=progress.tasks[convert_task].total or 1)
            progress.update(organize_task, completed=progress.tasks[organize_task].total or 1)

            # Ensure all progress bars are complete
            progress.update(convert_task, completed=progress.tasks[convert_task].total or 1)
            progress.update(organize_task, completed=progress.tasks[organize_task].total or 1)

        # Generate and return summary
        return self.processor._generate_summary()

    def display_results(self, results: Dict, dry_run: bool = False):
        """
        Display processing results in a beautiful summary.

        Args:
            results: Processing results dictionary
            dry_run: Whether this was a dry run
        """
        if not results:
            return

        # Success/failure summary
        success_count = results.get('files_processed', 0)
        failure_count = results.get('files_failed', 0)

        if dry_run:
            title = "Dry Run Results"
            title_style = "bold blue"
        elif failure_count == 0:
            title = "Processing Complete - Success!"
            title_style = "bold green"
        else:
            title = "Processing Complete - With Errors"
            title_style = "bold yellow"

        # Create results table
        table = Table(title=title, show_header=True, header_style="bold magenta")
        table.add_column("Metric", style="cyan", width=25)
        table.add_column("Count", justify="right", style="green")

        table.add_row("Files Processed", str(success_count))
        table.add_row("HEIC Conversions", str(results.get('heic_conversions', 0)))
        table.add_row("Missing EXIF Files", str(results.get('missing_exif_files', 0)))

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
            failure_table.add_row(operation, file_path, error)

        self.console.print(failure_table)

    def display_missing_exif_warning(self, missing_files: List[str]):
        """
        Display warning about files with missing EXIF data.

        Args:
            missing_files: List of files with missing EXIF data
        """
        warning_text = Text(f"Found {len(missing_files)} files with missing/invalid EXIF data.", style="bold yellow")
        warning_text.append("\nThese files were processed using filesystem timestamps.", style="dim")
        warning_text.append("\nConsider manually reviewing these files:", style="dim")

        for file_path in missing_files[:5]:  # Show first 5
            warning_text.append(f"\n  • {file_path}", style="yellow")

        if len(missing_files) > 5:
            warning_text.append(f"\n  ... and {len(missing_files) - 5} more", style="dim")

        panel = Panel(
            warning_text,
            title="Missing EXIF Data Warning",
            border_style="yellow",
            padding=(1, 2)
        )
        self.console.print(panel)