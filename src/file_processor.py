"""
File processing module for timestamp-based renaming and organization
"""

import os
import shutil
import logging
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional
from datetime import datetime, timedelta

from PIL import Image, UnidentifiedImageError

from .exif_handler import ExifHandler
from .heic_converter import HeicConverter
from .file_categorizer import FileCategorizer, FileCategory

logger = logging.getLogger(__name__)


class FileProcessor:
    """Main file processing coordinator"""

    # Categories whose files are trusted to be images purely on the strength of
    # their extension. Before one of these is renamed to a fabricated timestamp
    # and filed as a photograph, its bytes must be proven to decode (issue #58).
    IMAGE_CATEGORIES = frozenset({
        FileCategory.PHOTO,
        FileCategory.SCREENSHOT,
        FileCategory.GENERATED,
    })

    # Extensions whose bytes Pillow can reliably decode end to end, so a file
    # among them that fails to decode is genuinely corrupt or mislabeled rather
    # than merely a format Pillow lacks a codec for. RAW formats
    # (``.dng``/``.cr2``/``.nef``/...) are deliberately excluded: Pillow cannot
    # decode them, so a perfectly good RAW would fail the decode check and be
    # falsely quarantined. HEIC/HEIF are excluded too -- their decodability is
    # already established by the conversion-verification path (issue #7), which
    # keeps the original and records a failure on a bad decode -- so the decode
    # gate here concerns only NON-HEIC raster images.
    DECODABLE_IMAGE_EXTENSIONS = frozenset({
        '.jpg', '.jpeg', '.png', '.gif', '.tiff', '.tif', '.bmp', '.webp',
    })

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

        # Track processed files and timestamps.
        #
        # ``used_timestamps`` maps a target directory to the set of formatted
        # timestamp stems already claimed *in that directory*. It is scoped per
        # directory rather than being a single process-global set (issue #21):
        # files written into different category directories share no collision
        # namespace, so a photo and a video resolving to the same second no
        # longer bump each other. Each per-directory set is seeded lazily from
        # the directory's on-disk contents (issue #6) so collision resolution is
        # authoritative against what previous runs already filed in ``backup/``.
        self.processed_files = []
        self.used_timestamps: Dict[str, Set[str]] = {}
        self.failed_files = []
        self.conversion_log = []
        # Files carrying an image extension whose bytes did not decode as an
        # image (truncated, zero-byte, or a non-image mislabeled ``.jpg``/
        # ``.png``). They are moved to ``backup/corrupt/`` under their original
        # name rather than archived as photographs, and reported as a distinct
        # outcome -- neither a clean "processed" nor a tool "failure" (issue #58).
        self.quarantined_files: List[Dict[str, any]] = []

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
        # Unrecognized files carry no photo/video metadata to derive a
        # timestamp from, so renaming them to a fabricated timestamp would erase
        # the one identifying detail the user has -- their original filename.
        # They are filed under that original name into ``backup/unknown/`` for
        # manual review instead. Crucially they ARE moved: ``export/`` is fully
        # drained, so the summary can honestly count them as processed and the
        # documented ``backup/unknown/`` destination becomes real rather than an
        # empty phantom (issue #29).
        if category is FileCategory.UNKNOWN:
            self._process_unknown_file(file_path, target_dir, dry_run)
            return

        # Before trusting a photo/screenshot/generated extension, confirm the
        # bytes actually decode as an image. A truncated download, a zero-byte
        # stub, or a text file mislabeled ``.jpg``/``.png`` would otherwise be
        # renamed to a fabricated timestamp and filed into ``backup/photos/`` as
        # a genuine photograph -- and the rename is the damage: it destroys the
        # original filename, the one clue to what the file really was (issue
        # #58). Undecodable files are quarantined instead. The gate is limited to
        # extensions Pillow can decode (``DECODABLE_IMAGE_EXTENSIONS``) so a
        # valid RAW -- which Pillow cannot decode at all -- is never falsely
        # quarantined, and so HEIC (verified separately in issue #7) is left to
        # its own path.
        if (
            category in self.IMAGE_CATEGORIES
            and Path(file_path).suffix.lower() in self.DECODABLE_IMAGE_EXTENSIONS
            and not self._is_decodable_image(file_path)
        ):
            self._quarantine_file(file_path, dry_run)
            return

        original_path = file_path
        current_path = file_path

        # When a HEIC is converted, this holds the original ``.heic`` path so it
        # can be deleted only *after* its verified-good JPEG has safely landed in
        # ``backup/`` (see Step 3). Deleting earlier risks leaving the user with
        # neither the original nor a filed copy if a later step fails.
        heic_original_to_delete = None

        # Step 1: Convert HEIC to JPEG if needed
        if self.heic_converter.is_heic_file(file_path):
            if dry_run:
                logger.info(f"[DRY RUN] Would convert HEIC to JPEG: {file_path}")
                # For dry run, simulate the converted filename
                converted_path = str(Path(file_path).with_suffix('.jpg'))
            else:
                converted_path = self.heic_converter.convert_heic_to_jpeg(file_path)
                if converted_path:
                    # Verify the conversion is genuinely valid BEFORE the original
                    # -- the only copy of the photo -- becomes eligible for
                    # deletion. ``convert_heic_to_jpeg`` returns its output path
                    # right after ``image.save`` without inspecting the result, so
                    # a truncated write, a full disk, or a partial decode all yield
                    # a "successful" return. ``verify_conversion`` confirms the
                    # JPEG exists, decodes, matches the source dimensions, and
                    # preserved EXIF -- against the actual mkstemp path from #26.
                    if not self.heic_converter.verify_conversion(file_path, converted_path):
                        logger.error(
                            f"HEIC conversion verification failed, keeping original: {file_path}"
                        )
                        # Remove the unverifiable artifact so a corrupt JPEG is not
                        # left in export to be re-ingested on a later run. The
                        # original ``.heic`` is left untouched for the user.
                        try:
                            os.remove(converted_path)
                        except OSError:
                            pass
                        self.failed_files.append(
                            ('convert_heic', file_path, 'conversion verification failed')
                        )
                        return

                    self.conversion_log.append((file_path, converted_path))
                    current_path = converted_path
                    # Defer deletion of the original HEIC until after the JPEG has
                    # been moved into backup/ -- see Step 3.
                    heic_original_to_delete = file_path
                else:
                    # Conversion returned no path: the file was not processed.
                    # Record it so it lands in ``failed_files`` and increments
                    # ``files_failed`` instead of being silently dropped, which
                    # would let the summary report success and the exit code
                    # read 0 for a run that left this file behind (issue #31).
                    logger.error(f"HEIC conversion failed for {file_path}")
                    self.failed_files.append(
                        ('convert_heic', file_path, 'HEIC conversion failed')
                    )
                    return

        # Step 2: Extract timestamp
        timestamp = self.exif_handler.extract_timestamp(current_path)
        if timestamp is None:
            # Use fallback timestamp
            timestamp = self.exif_handler.get_fallback_timestamp(current_path)
            logger.warning(f"Using fallback timestamp for {current_path}")

        file_extension = Path(current_path).suffix

        # Step 3: Move the file into its category directory under a
        # collision-free timestamp name.
        #
        # Occupancy is judged against the target directory's actual on-disk
        # contents, not a process-global in-memory set. This closes two holes:
        #   * #6 -- a file already filed in ``backup/`` by an earlier run (last
        #     month's dump) can never be silently overwritten by this run.
        #   * #21 -- a file resolving to the same second in a *different*
        #     category directory is no longer treated as a collision, because
        #     the two can never share a path on disk.
        if dry_run:
            adjusted_timestamp, target_path = self._resolve_destination_dry_run(
                target_dir, timestamp, file_extension
            )
            logger.info(f"[DRY RUN] Would move: {current_path} -> {target_path}")
        else:
            try:
                # Ensure target directory exists before reserving within it.
                os.makedirs(target_dir, exist_ok=True)

                # Atomically claim a free destination path, then move onto it.
                adjusted_timestamp, target_path = self._reserve_destination(
                    target_dir, timestamp, file_extension
                )

                try:
                    # The destination was reserved with O_CREAT | O_EXCL, so it is
                    # an empty placeholder we own -- never a pre-existing photo.
                    # Overwriting it here therefore cannot destroy user data.
                    shutil.move(current_path, target_path)
                except Exception:
                    # The move failed after the name was reserved; drop the empty
                    # placeholder so a 0-byte stub is not left behind in backup/.
                    self._discard_reservation(target_path)
                    raise
                logger.info(f"Moved: {current_path} -> {target_path}")

                # The verified-good JPEG is now safely filed in backup/, so it is
                # finally safe to delete the original HEIC. Route through the
                # single safe-delete implementation; verification already happened
                # pre-move (the JPEG has since moved out of reach), so skip it here.
                if heic_original_to_delete is not None:
                    self.heic_converter.cleanup_original_heic(
                        heic_original_to_delete, verify_first=False
                    )

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

    def _process_unknown_file(
        self, file_path: str, target_dir: str, dry_run: bool
    ) -> None:
        """
        File an unrecognized file into ``backup/unknown/`` under its own name.

        Unlike a photo or video, an unknown file has no metadata to timestamp
        it by, so it keeps its original filename. ``backup/unknown/`` is created
        here, lazily, so the directory exists only because a file is landing in
        it -- never as an empty phantom (issue #29). A collision with a name a
        prior run (or an earlier file in this batch) already filed here is
        resolved without overwriting, by disambiguating the name. On success the
        move is recorded so ``files_processed`` reflects it and the export tree
        is genuinely drained; on failure it is recorded in ``failed_files`` so
        the run is not reported as a clean success (issue #31).

        Args:
            file_path: Source path of the unrecognized file.
            target_dir: The ``backup/unknown/`` directory to file it into.
            dry_run: If True, only log what would happen; touch nothing.
        """
        original_name = os.path.basename(file_path)

        if dry_run:
            target_path = self._resolve_named_destination_dry_run(
                target_dir, original_name
            )
            logger.info(
                f"[DRY RUN] Would move unrecognized file: {file_path} -> {target_path}"
            )
            return

        target_path = None
        try:
            # Create backup/unknown/ only now that a file is actually landing in
            # it -- this is what keeps the directory from being an empty phantom.
            os.makedirs(target_dir, exist_ok=True)

            target_path = self._reserve_named_destination(target_dir, original_name)
            try:
                shutil.move(file_path, target_path)
            except Exception:
                # The move failed after the name was reserved; drop the empty
                # placeholder so a 0-byte stub is not left behind in backup/.
                self._discard_reservation(target_path)
                raise
            logger.info(f"Moved unrecognized file: {file_path} -> {target_path}")

            self.processed_files.append({
                'original_path': file_path,
                'final_path': target_path,
                'category': FileCategory.UNKNOWN.value,
                'timestamp': None,  # no metadata timestamp; name is preserved
                'converted_from_heic': False,
            })
        except Exception as e:
            logger.error(
                f"Failed to move unrecognized file {file_path} to {target_path}: {e}"
            )
            self.failed_files.append(('move_file', file_path, str(e)))

    def get_quarantine_directory(self) -> str:
        """
        Return the directory undecodable image-typed files are quarantined into.

        Kept as a single accessor so the ``backup/corrupt/`` location is defined
        in exactly one place. The directory is created lazily, only when a file
        actually lands in it (see :meth:`_quarantine_file`), so it is never an
        empty phantom implying corruption that did not occur.

        Returns:
            The ``backup/corrupt/`` path.
        """
        return os.path.join(self.backup_dir, "corrupt")

    def _is_decodable_image(self, file_path: str) -> bool:
        """
        Report whether ``file_path`` genuinely decodes as an image.

        Trusting the extension is exactly the bug (issue #58): a truncated
        JPEG opens fine at the header and only fails deep in the pixel stream,
        and a text file renamed ``.png`` is not an image at all. So the check
        forces a full decode with :meth:`PIL.Image.Image.load` rather than the
        lazier :meth:`PIL.Image.Image.verify` -- ``verify`` validates structure
        but does NOT read the pixel data, so it passes a JPEG truncated to half
        its bytes, the realistic interrupted-download case. A zero-byte file is
        rejected up front without an open. Exactly one decode is performed here;
        the downstream timestamp read only touches EXIF (a lazy header read, no
        pixel decode), so a file is never fully decoded twice.

        Args:
            file_path: Path to the candidate image file.

        Returns:
            True if the file is non-empty and its bytes fully decode as an
            image; False if it is empty, truncated, or not a decodable image.
        """
        try:
            if os.path.getsize(file_path) == 0:
                logger.warning(
                    "Zero-byte file, not archiving as media: %s", file_path
                )
                return False
        except OSError as error:
            logger.warning(
                "Cannot stat %s, treating as undecodable: %s", file_path, error
            )
            return False

        try:
            with Image.open(file_path) as image:
                # load() forces the full pixel decode; a truncated or corrupt
                # stream raises here where verify()/open() alone would not.
                image.load()
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
            logger.warning(
                "Not a decodable image (%s), quarantining: %s",
                type(error).__name__,
                file_path,
            )
            return False
        return True

    def _quarantine_file(self, file_path: str, dry_run: bool) -> None:
        """
        Move an undecodable image-typed file to ``backup/corrupt/`` by its name.

        A corrupt file has no reliable capture timestamp, so -- like an unknown
        file (issue #29) -- it keeps its ORIGINAL filename, the only detail that
        lets the user identify and recover it. ``backup/corrupt/`` is created
        lazily here so it is never an empty phantom, and a name a prior run (or
        an earlier file in this batch) already filed there is disambiguated as
        ``name (1).ext`` rather than overwriting it, reusing the same atomic
        reservation the unknown/timestamped paths use. The original is never
        deleted from ``export/`` until it has safely landed here. On success the
        move is recorded in ``quarantined_files``; on failure it is recorded in
        ``failed_files`` so a botched quarantine cannot masquerade as a clean run.

        In a dry run nothing is moved, but the decision is still recorded and
        logged so ``--dry-run`` surfaces exactly which files a real run would
        quarantine (issue #58 acceptance criterion).

        Args:
            file_path: Source path of the undecodable file.
            dry_run: If True, only log/record the decision; touch nothing.
        """
        original_name = os.path.basename(file_path)
        target_dir = self.get_quarantine_directory()

        if dry_run:
            target_path = self._resolve_named_destination_dry_run(
                target_dir, original_name
            )
            logger.warning(
                "[DRY RUN] Would quarantine undecodable file: %s -> %s",
                file_path,
                target_path,
            )
            self.quarantined_files.append({
                'original_path': file_path,
                'final_path': target_path,
                'reason': 'undecodable',
            })
            return

        target_path = None
        try:
            # Create backup/corrupt/ only now that a file is actually landing in
            # it -- this is what keeps the directory from being an empty phantom.
            os.makedirs(target_dir, exist_ok=True)

            target_path = self._reserve_named_destination(target_dir, original_name)
            try:
                shutil.move(file_path, target_path)
            except Exception:
                # The move failed after the name was reserved; drop the empty
                # placeholder so a 0-byte stub is not left behind in backup/.
                self._discard_reservation(target_path)
                raise
            logger.warning(
                "Quarantined undecodable file: %s -> %s", file_path, target_path
            )

            self.quarantined_files.append({
                'original_path': file_path,
                'final_path': target_path,
                'reason': 'undecodable',
            })
        except Exception as error:
            logger.error(
                "Failed to quarantine %s to %s: %s", file_path, target_path, error
            )
            self.failed_files.append(('quarantine', file_path, str(error)))

    def _reserve_named_destination(self, target_dir: str, filename: str) -> str:
        """
        Atomically reserve a collision-free path that preserves ``filename``.

        Unknown files keep their original name, so two that share a name -- or a
        name a previous run already filed here -- must not clobber each other.
        The path is claimed with ``os.open(..., O_CREAT | O_EXCL)``, the same
        atomic reservation the timestamped path uses (issue #6): the exclusive
        create closes the check-then-move TOCTOU window, because an existing file
        makes the create fail rather than opening it. On collision the name is
        disambiguated as ``stem (1).ext``, ``stem (2).ext``, ... until an unused
        path is successfully claimed.

        Args:
            target_dir: Directory to reserve the destination within.
            filename: The original filename to preserve.

        Returns:
            The reserved path, which exists on disk as an empty placeholder the
            caller is expected to move the real file onto.
        """
        stem = Path(filename).stem
        ext = Path(filename).suffix
        candidate = filename
        counter = 1
        while True:
            target_path = os.path.join(target_dir, candidate)
            try:
                fd = os.open(
                    target_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
                )
                os.close(fd)
                return target_path
            except FileExistsError:
                candidate = f"{stem} ({counter}){ext}"
                counter += 1

    def _resolve_named_destination_dry_run(
        self, target_dir: str, filename: str
    ) -> str:
        """
        Resolve the name-preserving path an unknown file *would* take.

        The dry-run analogue of :meth:`_reserve_named_destination`: it writes
        nothing, so a dry run stays side-effect free, but still reports the
        disambiguated name a real run would use when the original is already
        occupied on disk.

        Args:
            target_dir: Directory the file would be filed into.
            filename: The original filename to preserve.

        Returns:
            The would-be destination path.
        """
        stem = Path(filename).stem
        ext = Path(filename).suffix
        candidate = filename
        counter = 1
        while os.path.exists(os.path.join(target_dir, candidate)):
            candidate = f"{stem} ({counter}){ext}"
            counter += 1
        return os.path.join(target_dir, candidate)

    def _taken_names(self, target_dir: str) -> Set[str]:
        """
        Return the set of timestamp stems already claimed in ``target_dir``.

        The set is scoped per target directory (issue #21) and seeded once, on
        first access, from the directory's on-disk contents (issue #6) so that
        collision resolution is authoritative against every file previous runs
        filed there. Seeding up front lets ``handle_duplicate_timestamp`` resolve
        a run of same-second files in memory instead of rediscovering each
        conflict with a separate filesystem probe; the atomic reservation in
        :meth:`_reserve_destination` remains the source of truth against races.

        Args:
            target_dir: The category directory a file is about to be filed into.

        Returns:
            The mutable per-directory set of claimed formatted-timestamp stems.
        """
        names = self.used_timestamps.get(target_dir)
        if names is None:
            names = set()
            try:
                for entry in os.listdir(target_dir):
                    if os.path.isfile(os.path.join(target_dir, entry)):
                        names.add(Path(entry).stem)
            except FileNotFoundError:
                # The directory does not exist yet: nothing is claimed in it.
                pass
            self.used_timestamps[target_dir] = names
        return names

    def _reserve_destination(
        self, target_dir: str, timestamp: datetime, file_extension: str
    ) -> Tuple[datetime, str]:
        """
        Atomically reserve a collision-free destination path in ``target_dir``.

        Resolves ``timestamp`` against the per-directory set of claimed names,
        then reserves the resulting path by creating it with
        ``os.open(..., O_CREAT | O_EXCL)``. The atomic create closes the
        check-then-move TOCTOU window: an already-present file (a prior run's, or
        one this batch just placed) can never be selected as the target, because
        ``O_EXCL`` fails rather than opening it. If the create loses that race,
        the stem is recorded as taken and the timestamp is bumped a second until
        an unused path is successfully claimed.

        Args:
            target_dir: Directory to reserve the destination within.
            timestamp: The file's resolved timestamp.
            file_extension: Suffix (including the dot) for the destination name.

        Returns:
            A tuple of the possibly-adjusted timestamp and the reserved path.
            The reserved path exists on disk as an empty placeholder the caller
            is expected to move the real file onto.
        """
        names = self._taken_names(target_dir)
        adjusted = self.exif_handler.handle_duplicate_timestamp(timestamp, names)

        while True:
            stem = self.exif_handler.format_timestamp_filename(adjusted)
            target_path = os.path.join(target_dir, f"{stem}{file_extension}")
            try:
                fd = os.open(
                    target_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
                )
                os.close(fd)
                names.add(stem)
                return adjusted, target_path
            except FileExistsError:
                # The path is occupied on disk even though the in-memory set did
                # not know it (a stale seed, a same-stem/different-extension
                # sibling, or a genuine race). Record it and try the next second.
                names.add(stem)
                adjusted = adjusted + timedelta(seconds=1)

    def _resolve_destination_dry_run(
        self, target_dir: str, timestamp: datetime, file_extension: str
    ) -> Tuple[datetime, str]:
        """
        Resolve the destination a file *would* occupy without touching disk.

        The dry-run analogue of :meth:`_reserve_destination`: it performs no
        filesystem write, so a dry run stays side-effect free, but it still seeds
        the per-directory name set from disk. That means a dry run now reports the
        *bumped* path a file would take when its natural name is already occupied
        by an existing backup file, instead of reporting a name a real run would
        overwrite. The resolved stem is added to the in-memory set so two dry-run
        files resolving to the same second within one run still report distinct
        paths, matching a real run.

        Note (#10): because no placeholder is written, a dry run cannot reflect
        the reservations of *other* dry-run files beyond this in-memory bookkeeping
        -- full dry-run/real parity for pre-existing conflicts is tracked there.

        Args:
            target_dir: Directory the file would be filed into.
            timestamp: The file's resolved timestamp.
            file_extension: Suffix (including the dot) for the destination name.

        Returns:
            A tuple of the possibly-adjusted timestamp and the would-be path.
        """
        names = self._taken_names(target_dir)
        adjusted = self.exif_handler.handle_duplicate_timestamp(timestamp, names)
        stem = self.exif_handler.format_timestamp_filename(adjusted)
        names.add(stem)
        target_path = os.path.join(target_dir, f"{stem}{file_extension}")
        return adjusted, target_path

    def _discard_reservation(self, target_path: str) -> None:
        """
        Remove a reserved placeholder after a failed move (best effort).

        Args:
            target_path: The reserved path to unlink; missing/undeletable paths
                are ignored so cleanup never masks the original move failure.
        """
        try:
            os.remove(target_path)
        except OSError:
            pass

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
            # Undecodable image-typed files quarantined to backup/corrupt/. A
            # distinct outcome from processed (they were NOT filed as photos)
            # and from failed (nothing errored; they were handled deliberately
            # and safely), so the count is honest either way (issue #58).
            'files_quarantined': len(self.quarantined_files),
            'categorization_stats': stats,
            'heic_conversions': heic_stats['successful_conversions'],
            'heic_conversion_failures': heic_stats['failed_conversions'],
            'missing_exif_files': len(missing_exif_files),
            'processed_files': self.processed_files.copy(),
            'failed_files': self.failed_files.copy(),
            'quarantined_files': self.quarantined_files.copy(),
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
        self.quarantined_files.clear()
        self.conversion_log.clear()
        self.exif_handler.clear_missing_files_log()
        self.heic_converter.clear_stats()
        self.categorizer.clear_categorization()