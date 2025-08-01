#!/usr/bin/env python3
"""
tm-monthly-backup: CLI utility for organizing Apple Photos exports
"""

import click


@click.command()
@click.option('--dry-run', is_flag=True, help='Show what would be done without making changes')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
def main(dry_run, verbose):
    """Process exported Apple Photos files and organize them by type."""
    click.echo("tm-monthly-backup v0.1.0")

    if dry_run:
        click.echo("DRY RUN MODE: No files will be modified")

    # TODO: Implement core functionality
    click.echo("Core functionality coming soon...")


if __name__ == '__main__':
    main()