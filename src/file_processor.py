"""
File processing module for timestamp-based renaming and organization
"""

import os
import shutil
import logging
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional
from datetime import datetime

from .exif_handler import ExifHandler
from .heic_converter import HeicConverter
from .file_categorizer import FileCategorizer, FileCategory

logger = logging.getLogger(__name__)


class FileProcessor:
    """Main file processing coordinator"""

    def __init__(self, export_dir: str = "export", backup_dir: str = "backup"):
        """
        Initialize file processor.

        Args:
            export_dir: Directory containing exported files
            backup_dir: Directory for organized output files
        """
        self.export_dir = export_dir
        self.backup_dir = backup_dir

        # Initialize component handlers
        # (overlap is validated lazily at process time; see
        # ``directory_overlap_error`` and ``process_all_files``)
        self.exif_handler = ExifHandler()
        self.heic_converter = HeicConverter()
        self.categorizer = FileCategorizer()

        # Track processed files and timestamps
        self.processed_files = []
        self.used_timestamps = set()
        self.failed_files = []
        self.conversion_log = []

    @staticmethod
    def directory_overlap_error(export_dir: str, backup_dir: str) -> Optional[str]:
        """
        Return an actionable message if export/backup directories overlap.

        The tool consumes the export tree (it deletes ``.aae`` sidecars and the
        original ``.heic`` after conversion) and writes sorted output into the
        backup tree. If the two roots are the same, or one is nested inside the
        other, a run can re-ingest and destroy its own inputs. Any such overlap
        is a misconfiguration and must be rejected before any filesystem
        mutation.

        Paths are resolved with :meth:`pathlib.Path.resolve` first so symlinked
        or relative aliases of the same location are caught, then compared for
        equality and containment in both directions.

        Args:
            export_dir: Source directory containing exported files.
            backup_dir: Destination directory for organized output.

        Returns:
            A human-readable error message naming both paths when they overlap,
            or ``None`` when the configuration is safe.
        """
        export_root = Path(export_dir).resolve()
        backup_root = Path(backup_dir).resolve()

        if export_root == backup_root:
            return (
                "--export-dir and --backup-dir must not be the same directory: "
                f"{export_root}"
            )
        if backup_root.is_relative_to(export_root):
            return (
                f"--backup-dir ({backup_root}) must not be inside --export-dir "
                f"({export_root}); each run would re-ingest and destroy the archive"
            )
        if export_root.is_relative_to(backup_root):
            return (
                f"--export-dir ({export_root}) must not be inside --backup-dir "
                f"({backup_root}); the source tree would be consumed from within "
                "the destination"
            )
        return None

    def process_all_files(self, dry_run: bool = False) -> Dict[str, any]:
        """
        Process all files in export directory.

        Args:
            dry_run: If True, show what would be done without making changes

        Returns:
            Dictionary with processing results and statistics

        Raises:
            ValueError: If the export and backup directories overlap (same
                directory, or one nested inside the other), which would let a
                run destroy its own inputs.
        """
        logger.info(f"Starting file processing (dry_run={dry_run})")

        # Refuse to run when the export and backup roots overlap: the export
        # tree is consumed in place, so an overlapping destination lets a run
        # clobber files it is meant to preserve. Guard before any scan/mutation.
        overlap_error = self.directory_overlap_error(self.export_dir, self.backup_dir)
        if overlap_error:
            logger.error(overlap_error)
            raise ValueError(overlap_error)

        # Scan export directory
        all_files = self._scan_export_directory()
        if not all_files:
            logger.warning(f"No files found in {self.export_dir}")
            return self._generate_summary()

        logger.info(f"Found {len(all_files)} files to process")

        # Categorize files
        categorized = self.categorizer.batch_categorize(all_files)
        logger.info(f"Categorization complete:\n{self.categorizer.get_file_summary()}")

        # Delete sidecar files immediately
        self._delete_sidecar_files(categorized[FileCategory.SIDECAR], dry_run)

        # Process files by category
        processable_files = self.categorizer.get_processable_files()

        # Create target directories
        if not dry_run:
            self.categorizer.ensure_target_directories(self.backup_dir)

        # Process each category
        for category, files in processable_files.items():
            if files:
                self._process_category(category, files, dry_run)

        # Generate summary
        return self._generate_summary()

    def _scan_export_directory(self) -> List[str]:
        """
        Scan export directory for all files.

        Returns:
            List of file paths
        """
        if not os.path.exists(self.export_dir):
            logger.error(f"Export directory does not exist: {self.export_dir}")
            return []

        files = []
        for root, dirs, filenames in os.walk(self.export_dir):
            for filename in filenames:
                # Skip hidden files
                if not filename.startswith('.'):
                    files.append(os.path.join(root, filename))

        return files

    def _delete_sidecar_files(self, sidecar_files: List[str], dry_run: bool):
        """
        Delete Apple sidecar files.

        Args:
            sidecar_files: List of sidecar file paths
            dry_run: If True, only log what would be deleted
        """
        if not sidecar_files:
            return

        logger.info(f"Processing {len(sidecar_files)} sidecar files for deletion")

        for file_path in sidecar_files:
            if dry_run:
                logger.info(f"[DRY RUN] Would delete sidecar file: {file_path}")
            else:
                try:
                    os.remove(file_path)
                    logger.info(f"Deleted sidecar file: {file_path}")
                except Exception as e:
                    logger.error(f"Failed to delete sidecar file {file_path}: {e}")
                    self.failed_files.append(('delete_sidecar', file_path, str(e)))

    def _process_category(self, category: FileCategory, files: List[str], dry_run: bool):
        """
        Process files for a specific category.

        Args:
            category: FileCategory to process
            files: List of file paths
            dry_run: If True, only show what would be done
        """
        logger.info(f"Processing {len(files)} {category.value} files")

        target_dir = self.categorizer.get_target_directory(category, self.backup_dir)

        for file_path in files:
            try:
                self._process_single_file(file_path, category, target_dir, dry_run)
            except Exception as e:
                logger.error(f"Failed to process file {file_path}: {e}")
                self.failed_files.append(('process_file', file_path, str(e)))

    def _process_single_file(self, file_path: str, category: FileCategory, target_dir: str, dry_run: bool):
        """
        Process a single file: convert if needed, rename with timestamp, move to target.

        Args:
            file_path: Source file path
            category: FileCategory
            target_dir: Target directory path
            dry_run: If True, only show what would be done
        """
        original_path = file_path
        current_path = file_path

        # Step 1: Convert HEIC to JPEG if needed
        if self.heic_converter.is_heic_file(file_path):
            if dry_run:
                logger.info(f"[DRY RUN] Would convert HEIC to JPEG: {file_path}")
                # For dry run, simulate the converted filename
                converted_path = str(Path(file_path).with_suffix('.jpg'))
            else:
                converted_path = self.heic_converter.convert_heic_to_jpeg(file_path)
                if converted_path:
                    self.conversion_log.append((file_path, converted_path))
                    current_path = converted_path
                    # Delete original HEIC after successful conversion
                    try:
                        os.remove(file_path)
                        logger.info(f"Deleted original HEIC file: {file_path}")
                    except Exception as e:
                        logger.warning(f"Could not delete original HEIC file {file_path}: {e}")
                else:
                    logger.error(f"HEIC conversion failed for {file_path}")
                    return

        # Step 2: Extract timestamp and generate filename
        timestamp = self.exif_handler.extract_timestamp(current_path)
        if timestamp is None:
            # Use fallback timestamp
            timestamp = self.exif_handler.get_fallback_timestamp(current_path)
            logger.warning(f"Using fallback timestamp for {current_path}")

        # Handle duplicate timestamps
        adjusted_timestamp = self.exif_handler.handle_duplicate_timestamp(
            timestamp, self.used_timestamps
        )

        # Generate new filename
        timestamp_filename = self.exif_handler.format_timestamp_filename(adjusted_timestamp)
        file_extension = Path(current_path).suffix
        new_filename = f"{timestamp_filename}{file_extension}"

        # Step 3: Move file to target directory with new name
        target_path = os.path.join(target_dir, new_filename)

        if dry_run:
            logger.info(f"[DRY RUN] Would move: {current_path} -> {target_path}")
        else:
            try:
                # Ensure target directory exists
                os.makedirs(target_dir, exist_ok=True)

                # Move file
                shutil.move(current_path, target_path)
                logger.info(f"Moved: {current_path} -> {target_path}")

                # Track used timestamp
                self.used_timestamps.add(timestamp_filename)

                # Record successful processing
                self.processed_files.append({
                    'original_path': original_path,
                    'final_path': target_path,
                    'category': category.value,
                    'timestamp': adjusted_timestamp.isoformat(),
                    'converted_from_heic': self.heic_converter.is_heic_file(original_path)
                })

            except Exception as e:
                logger.error(f"Failed to move file {current_path} to {target_path}: {e}")
                self.failed_files.append(('move_file', current_path, str(e)))

    def _generate_summary(self) -> Dict[str, any]:
        """
        Generate processing summary.

        Returns:
            Dictionary with processing statistics and results
        """
        stats = self.categorizer.get_categorization_stats()
        heic_stats = self.heic_converter.get_conversion_stats()
        missing_exif_files = self.exif_handler.get_missing_exif_files()

        return {
            'files_processed': len(self.processed_files),
            'files_failed': len(self.failed_files),
            'categorization_stats': stats,
            'heic_conversions': heic_stats['successful_conversions'],
            'heic_conversion_failures': heic_stats['failed_conversions'],
            'missing_exif_files': len(missing_exif_files),
            'processed_files': self.processed_files.copy(),
            'failed_files': self.failed_files.copy(),
            'conversion_log': self.conversion_log.copy(),
            'missing_exif_list': missing_exif_files
        }

    def get_missing_exif_directory(self) -> str:
        """Get directory path for files with missing EXIF data"""
        return os.path.join(self.backup_dir, "missing_exif")

    def handle_missing_exif_files(self, dry_run: bool = False) -> List[str]:
        """
        Move files with missing EXIF data to special directory.

        Args:
            dry_run: If True, only show what would be done

        Returns:
            List of files moved to missing EXIF directory
        """
        missing_files = self.exif_handler.get_missing_exif_files()
        if not missing_files:
            return []

        missing_dir = self.get_missing_exif_directory()
        moved_files = []

        if not dry_run:
            os.makedirs(missing_dir, exist_ok=True)

        for file_path in missing_files:
            if os.path.exists(file_path):
                filename = os.path.basename(file_path)
                target_path = os.path.join(missing_dir, filename)

                if dry_run:
                    logger.info(f"[DRY RUN] Would move to missing EXIF dir: {file_path} -> {target_path}")
                else:
                    try:
                        shutil.move(file_path, target_path)
                        moved_files.append(target_path)
                        logger.info(f"Moved to missing EXIF directory: {file_path} -> {target_path}")
                    except Exception as e:
                        logger.error(f"Failed to move missing EXIF file {file_path}: {e}")

        return moved_files

    def clear_processing_state(self):
        """Clear all processing state for a fresh run"""
        self.processed_files.clear()
        self.used_timestamps.clear()
        self.failed_files.clear()
        self.conversion_log.clear()
        self.exif_handler.clear_missing_files_log()
        self.heic_converter.clear_stats()
        self.categorizer.clear_categorization()