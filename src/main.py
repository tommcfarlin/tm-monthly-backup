#!/usr/bin/env python3
"""
tm-monthly-backup: CLI utility for organizing Apple Photos exports
"""

import sys
import click
from pathlib import Path

# Add src directory to Python path for direct execution
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).parent))

try:
    from cli_interface import CLIInterface, setup_logging
except ImportError:
    from .cli_interface import CLIInterface, setup_logging


@click.command()
@click.option('--dry-run', is_flag=True, help='Show what would be done without making changes')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
@click.option('--export-dir', default='export', help='Directory containing exported files (default: export)')
@click.option('--backup-dir', default='backup', help='Directory for organized output (default: backup)')
@click.version_option(version='0.1.0', prog_name='tm-monthly-backup')
def main(dry_run, verbose, export_dir, backup_dir):
    """
    Process exported Apple Photos files and organize them by type.

    This tool will:
    - Convert HEIC files to JPEG with EXIF preservation
    - Rename files using EXIF timestamp format (YYYY.MM.DD.HH.MM.SS)
    - Organize files into photos/, videos/, and screenshots/ directories
    - Remove Apple sidecar (.aae) files
    - Handle duplicate timestamps intelligently
    """

    # Setup logging with rich formatting
    setup_logging(verbose)

    # Initialize CLI interface
    cli = CLIInterface(export_dir, backup_dir)

    # Display welcome banner
    cli.display_welcome()

    # Check directories and prerequisites
    if not cli.check_directories():
        cli.console.print("[red]Cannot proceed due to directory issues.[/red]")
        sys.exit(1)

    try:
        # Process files with beautiful progress indicators
        results = cli.process_with_progress(dry_run=dry_run)

        # Display results
        cli.display_results(results, dry_run=dry_run)

        # Exit with appropriate code
        if results.get('files_failed', 0) > 0:
            cli.console.print(f"\n[yellow]Completed with {results['files_failed']} failures.[/yellow]")
            sys.exit(1)
        else:
            if not dry_run and results.get('files_processed', 0) > 0:
                cli.console.print("\n[bold green]All files processed successfully![/bold green]")
            elif dry_run:
                cli.console.print("\n[blue]Dry run completed. Use without --dry-run to process files.[/blue]")
            sys.exit(0)

    except KeyboardInterrupt:
        cli.console.print("\n[yellow]Processing interrupted by user.[/yellow]")
        sys.exit(1)
    except Exception as e:
        cli.console.print(f"\n[red]Unexpected error: {e}[/red]")
        if verbose:
            import traceback
            cli.console.print(traceback.format_exc())
        sys.exit(1)


if __name__ == '__main__':
    main()