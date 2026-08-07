"""
File processing module for timestamp-based renaming and organization
"""

import os
import shutil
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple, Optional
from datetime import datetime, timedelta
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

from PIL import Image, UnidentifiedImageError

from .exif_handler import ExifHandler
from .heic_converter import HeicConverter
from .file_categorizer import FileCategorizer, FileCategory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    """
    Immutable, run-scoped tunables for :class:`FileProcessor` (issue #41).

    Before this existed, every knob `HeicConverter` bothered to expose --
    `jpeg_quality`, `optimize` -- was unreachable from the CLI: `FileProcessor`
    constructed `HeicConverter()` with no arguments, so a value a user actually
    wanted to change could only be edited into the source. Bundling the
    tunables this issue makes user-facing into a single frozen record, rather
    than adding each as its own `FileProcessor.__init__` parameter, keeps the
    constructor stable as more tunables arrive later -- they extend this
    dataclass instead of the parameter list. `frozen=True` guarantees no code
    path can mutate configuration mid-run: every stage of a run reads the same
    values from the initial scan through the final summary.

    Args:
        jpeg_quality: JPEG quality (1-100) passed to
            :class:`HeicConverter` for HEIC->JPEG conversion. Default 98
            matches `HeicConverter`'s own default (issue #40).
    """

    jpeg_quality: int = 98


def _convert_heic_worker(
    heic_path: str, jpeg_quality: int, optimize: bool
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Convert a single HEIC file to JPEG inside a worker process.

    This is the picklable unit of work mapped across the process pool by the
    parallel conversion phase (issue #42). It is deliberately a module-level
    function taking only picklable scalars and returning only a picklable
    tuple: no ``self``, no Pillow objects, and no shared mutable state cross the
    process boundary. Each spawned worker re-imports this module, which imports
    :mod:`src.heic_converter` and therefore calls
    ``pillow_heif.register_heif_opener()`` at import time, so the HEIF opener is
    registered in every worker (macOS uses the ``spawn`` start method).

    The work performed is exactly the decode + encode + write of the sequential
    path: it constructs a throwaway :class:`HeicConverter` with the same
    ``jpeg_quality``/``optimize`` the main process uses and calls
    :meth:`HeicConverter.convert_heic_to_jpeg` with no ``output_dir``, so the
    transient ``mkstemp`` ``.jpg`` (issue #26) lands beside the source HEIC in
    ``export/`` just as it does sequentially. Verification (#7), timestamping,
    moving, and deleting the original are NOT done here -- those stay in the
    sequential place phase so collision resolution and landing order are
    byte-for-byte identical to a sequential run.

    A decode/encode failure never raises across the boundary: it is captured
    and returned so a single corrupt HEIC fails exactly one file instead of
    poisoning the pool.

    Args:
        heic_path: Absolute path to the HEIC file to convert.
        jpeg_quality: JPEG quality (1-100) for the encode.
        optimize: Whether to run libjpeg's extra optimization pass (issue #40).

    Returns:
        A ``(heic_path, output_path, error)`` tuple. On success ``output_path``
        is the transient JPEG path and ``error`` is ``None``; on failure
        ``output_path`` is ``None`` and ``error`` carries a short message.
    """
    converter = HeicConverter(jpeg_quality=jpeg_quality, optimize=optimize)
    try:
        output_path = converter.convert_heic_to_jpeg(heic_path)
    except Exception as exc:  # pragma: no cover - defensive across the boundary
        return (heic_path, None, str(exc))
    if output_path is None:
        error = (
            converter.failed_conversions[-1][1]
            if converter.failed_conversions
            else 'HEIC conversion failed'
        )
        return (heic_path, None, error)
    return (heic_path, output_path, None)


class ProgressReporter:
    """
    Presentation seam for :meth:`FileProcessor.process_all_files` (issue #13).

    ``process_all_files`` owns the whole run -- scan, categorize, delete
    sidecars, process, summarize -- and reports to an optional
    ``ProgressReporter`` at the meaningful points so a UI can render (and gate)
    the run *without* reaching into private methods or re-driving the pipeline.
    Passing ``progress=None`` runs the pipeline headless, exactly as before this
    seam existed, so every non-interactive caller (and the test suite) is
    unaffected.

    Every hook has a no-op / permissive default, so a subclass overrides only
    the hooks it needs. The contract is intentionally small and stable; issue
    #14 subclasses it to drive real per-file progress bars.

    Hook order for one run:

    1. :meth:`on_no_files` -- the scan found nothing; the run ends here.
    2. :meth:`on_categorized` -- fired once, right after the single
       categorization pass, with the scanned-file total and the categorization
       stats. Returning ``False`` aborts the run before any file is touched or
       any sidecar is deleted (the confirmation seam); the default proceeds.
    3. :meth:`on_heic_converted` -- fired once per HEIC file as its conversion
       attempt (success or failure) completes, interleaved with the other two
       hooks below depending on batch size (see its docstring).
    4. :meth:`on_file` -- fired once per processable file as it is handled
       (moved / converted / quarantined / routed). Never fired for sidecar
       files, which are deleted rather than processed.

    Issue #14 extension: ``stats`` passed to :meth:`on_categorized` carries an
    additional ``'heic'`` key -- the count of processable files that are HEIC
    -- beyond the keys documented on
    :meth:`FileCategorizer.get_categorization_stats`, so a reporter can size a
    conversion-specific total (a HEIC file is not its own categorization
    category; it is a photo/screenshot/generated file that happens to carry a
    ``.heic``/``.heif`` extension). :meth:`on_heic_converted` is a new hook
    added for the same reason: the HEIC conversion phase can run entirely
    *before* the per-file loop that fires :meth:`on_file` (issue #42's
    parallel pool, used above ``HEIC_PARALLEL_THRESHOLD`` files), so a reporter
    that only implemented :meth:`on_file` would see the expensive conversion
    phase produce no progress at all and then watch :meth:`on_file` catch up
    all at once once placement starts -- the exact "stalled bar, then a snap
    to 100%" defect this hook exists to fix.
    """

    def on_no_files(self) -> None:
        """Called when the export scan yields no files. Default: no-op."""

    def on_categorized(self, total: int, stats: Dict[str, int]) -> bool:
        """
        Called once after categorization, before any file is processed.

        Args:
            total: Number of files the scan discovered (sidecars included) --
                the count a "process N files?" prompt should quote.
            stats: The categorization breakdown from
                :meth:`FileCategorizer.get_categorization_stats`, plus one key
                that method does not provide: ``'heic'``, the count of
                processable files that will go through HEIC conversion (issue
                #14) -- enough for a reporter to size a conversion-specific
                progress total independent of the move/organize total.

        Returns:
            ``True`` to proceed with processing, ``False`` to abort the run
            before any file is touched or any sidecar deleted. Default ``True``.
        """
        return True

    def on_heic_converted(self, path: str) -> None:
        """
        Called once per HEIC file as its conversion attempt completes.

        Fires right after the decode+encode(+write) attempt for ``path``
        finishes -- success or failure -- and strictly before :meth:`on_file`
        fires for that same file (the move/place step happens afterward).

        Timing depends on batch size: for a batch at or above
        ``HEIC_PARALLEL_THRESHOLD`` every HEIC file is converted in the
        parallel pool (issue #42) as a dedicated up-front phase, so every
        ``on_heic_converted`` call for that run fires before any
        :meth:`on_file` call. Below the threshold each HEIC file is converted
        inline as it is reached, so ``on_heic_converted`` for that file fires
        immediately before the matching :meth:`on_file` call. Either way it
        fires exactly once per HEIC file -- never for a non-HEIC file, and
        never twice for the same file (a pool-converted file's cached result is
        merely looked up later; that lookup does not re-fire this hook).

        Args:
            path: The source ``.heic``/``.heif`` path whose conversion just
                completed (success or failure -- the hook does not carry the
                outcome; :meth:`on_file`'s eventual action, or the run's
                failure list, reflects that).

        Default: no-op.
        """

    def on_file(self, path: str, category: str, action: str) -> None:
        """
        Called once per processable file as it is handled.

        Args:
            path: The source path of the file just handled.
            category: The file's category value (e.g. ``"photo"``).
            action: A coarse label for the phase this file went through:
                ``"convert"`` for a HEIC file (which was also converted, and
                whose :meth:`on_heic_converted` already fired for it), or
                ``"move"`` for every other file (moved, quarantined, or routed
                to ``backup/unknown/`` with no conversion). Determined from the
                file's own extension, independent of success or failure.

        Default: no-op.
        """


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

    # Upper bound on the HEIC conversion process pool (issue #42). The
    # performance audit measured returns flattening at 4-6 workers and going
    # *backwards* at 8: libheif is already internally threaded (a sequential run
    # already uses ~207% CPU), so oversubscribing regresses. Memory also scales
    # at ~0.5 GB per worker, so the cap bounds RSS too. The pool is sized at
    # ``min(MAX_HEIC_WORKERS, os.cpu_count())`` -- deliberately NOT
    # ``os.cpu_count()`` on a host with more cores than this.
    MAX_HEIC_WORKERS = 6

    # Minimum number of HEIC files before the process pool is worth spawning.
    # Each spawned worker re-imports Pillow + pillow-heif (~65 ms one-time), so
    # for a handful of files the spawn overhead is not amortized and the
    # sequential inline path is faster. Below this threshold no pool is created.
    HEIC_PARALLEL_THRESHOLD = 8

    # Byte prefixes a genuine Apple sidecar's content starts with: an XML
    # property list (``<?xml ...``) or a binary property list (``bplist00``).
    # Membership in ``FileCategory.SIDECAR`` is decided purely by the ``.aae``
    # extension (issue #57's own defect), so before a candidate is unlinked --
    # the only irreversible operation this tool performs on a file it is not
    # archiving -- its content must actually look like one of these. Checked
    # with ``bytes.startswith(SIDECAR_MAGIC)``, which accepts a tuple.
    SIDECAR_MAGIC = (b"<?xml", b"bplist00")

    # Filenames that are known operating-system/filesystem junk rather than
    # potential photo content -- matched by exact (case-sensitive) basename,
    # never by a blanket dot-prefix rule (issue #30). Naming these explicitly
    # is what makes the scan's intent legible: THESE specific names are always
    # correctly ignored on sight, which is a different, stronger claim than
    # "anything hidden is junk" -- a real photo can carry a leading dot too
    # (an interrupted ``rsync``/``scp`` partial, a cloud-sync conflict copy),
    # and that file must still be accounted for rather than silently dropped.
    # Compared case-insensitively (see ``_is_denylisted_junk``). These names
    # arrive from filesystems that do not preserve case the way the canonical
    # spelling suggests: a Windows-originated export can carry ``thumbs.db``
    # lowercase, and macOS's own case-insensitive APFS will happily hand back
    # ``.ds_store``. A case-sensitive match would let those fall through to
    # ``UNKNOWN`` and be filed into ``backup/unknown/`` as if they were the
    # user's data.
    HIDDEN_FILE_DENYLIST = frozenset({'.ds_store', '.localized', 'thumbs.db'})

    def _is_denylisted_junk(self, filename: str) -> bool:
        """
        Is this basename one of the known-junk names the scan always ignores?

        Case-insensitive: see :attr:`HIDDEN_FILE_DENYLIST` for why. The
        denylist itself is stored pre-lowercased so this is a plain membership
        test rather than a set comprehension per file.
        """
        return filename.lower() in self.HIDDEN_FILE_DENYLIST

    def __init__(
        self,
        export_dir: str = "export",
        backup_dir: str = "backup",
        settings: Optional[Settings] = None,
    ):
        """
        Initialize file processor.

        Args:
            export_dir: Directory containing exported files
            backup_dir: Directory for organized output files
            settings: Immutable HEIC conversion tunables (issue
                #41) -- see :class:`Settings`. Defaults to ``Settings()``
                (quality 98, originals deleted) when omitted, so every
                existing caller is unaffected.
        """
        self.export_dir = export_dir
        self.backup_dir = backup_dir
        self.settings = settings if settings is not None else Settings()

        # Initialize component handlers
        # (overlap is validated lazily at process time; see
        # ``directory_overlap_error`` and ``process_all_files``)
        self.exif_handler = ExifHandler()
        self.heic_converter = HeicConverter(jpeg_quality=self.settings.jpeg_quality)
        self.categorizer = FileCategorizer()

        # Track processed files and timestamps.
        #
        # ``_used_timestamps`` maps a target directory to the set of formatted
        # timestamp stems already claimed *in that directory*. It is scoped per
        # directory rather than being a single process-global set (issue #21):
        # files written into different category directories share no collision
        # namespace, so a photo and a video resolving to the same second no
        # longer bump each other. Each per-directory set is seeded lazily from
        # the directory's on-disk contents (issue #6) so collision resolution is
        # authoritative against what previous runs already filed in ``backup/``.
        #
        # These four accumulators (plus ``_quarantined_files`` below) are
        # per-run bookkeeping, not public API: they are private (issue #35) so
        # ``_generate_summary`` is the only reader, and ``process_all_files``
        # resets all of them -- via ``clear_processing_state`` -- at the start
        # of every call, so a FileProcessor instance is safe to reuse across
        # multiple runs (see the reuse-contract note on ``process_all_files``).
        self._processed_files = []
        self._used_timestamps: Dict[str, Set[str]] = {}
        self._failed_files = []
        self._conversion_log = []
        # Files a run filed using a fallback (non-EXIF) timestamp, recorded with
        # BOTH the original source path and the final backup/ path (issue #35).
        # The final path is what the user can actually go look at by the time
        # ``display_missing_exif_warning`` renders it -- the source path was
        # already moved/renamed/deleted by then. Populated only when the file
        # was actually filed successfully; a file that also failed to move is
        # not double-reported here (it is already in ``_failed_files``).
        self._missing_exif_records: List[Dict[str, Optional[str]]] = []
        # Pre-computed HEIC conversions from the parallel convert phase (issue
        # #42): maps a source ``.heic`` path to ``(output_path, error)``. It is
        # populated once, up front, only when a run has enough HEIC files to
        # amortize the pool (see ``_prepare_heic_conversions``); the sequential
        # place phase consults it per file. Empty means "convert inline",
        # preserving the exact sequential behaviour for small/dry-run batches.
        self._converted_heic: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        # Files carrying an image extension whose bytes did not decode as an
        # image (truncated, zero-byte, or a non-image mislabeled ``.jpg``/
        # ``.png``). They are moved to ``backup/corrupt/`` under their original
        # name rather than archived as photographs, and reported as a distinct
        # outcome -- neither a clean "processed" nor a tool "failure" (issue #58).
        self._quarantined_files: List[Dict[str, Any]] = []
        # Apple sidecar (.aae) bookkeeping (issue #57). A candidate is either
        # deleted (its content was validated against SIDECAR_MAGIC and, on a
        # real run, os.remove succeeded) or kept -- recorded here as
        # (reason, path) with reason 'not_plist' (content was read but did
        # not match the magic bytes), 'unreadable' (the file could not be
        # opened/read at all -- a distinct reason from 'not_plist' because
        # its content was never actually inspected), 'delete_failed' (a
        # validated sidecar's own os.remove raised), or
        # 'run_archived_nothing' (every processable file this run attempted
        # failed, so nothing is deleted at all -- see process_all_files).
        # Neither list feeds _processed_files or _failed_files: a sidecar was
        # never a processable file to begin with (it is excluded from
        # FileCategorizer.get_processable_files), so counting either outcome
        # there would inflate files_processed or files_failed past
        # files_seen (issue #31).
        self._deleted_sidecars: List[str] = []
        self._skipped_sidecars: List[Tuple[str, str]] = []
        # Files the SCAN itself declined to collect at all, recorded as
        # (reason, path) with reason 'junk' (basename matched
        # HIDDEN_FILE_DENYLIST -- e.g. .DS_Store) or 'hidden' (a dotted name
        # that did not match the denylist) (issue #30). This is deliberately a
        # DIFFERENT accumulator from _skipped_sidecars above rather than a
        # shared one: _skipped_sidecars holds .aae candidates that WERE
        # categorized and DID reach the delete-validation step but failed it,
        # whereas a file recorded here never became a categorized/processable
        # file at all -- it is filtered at the scan boundary, one stage
        # earlier, for an unrelated reason (its name, not its content). Two
        # mechanisms, not one, because they answer two different user
        # questions ("why didn't my sidecar get deleted" vs. "why didn't this
        # file get backed up at all"); they are added together only where the
        # two must agree -- the files_scanned accounting identity in
        # _generate_summary -- and are otherwise rendered as separate rows so
        # neither explanation gets diluted by the other.
        self._skipped_files: List[Tuple[str, str]] = []

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

    def process_all_files(
        self,
        dry_run: bool = False,
        progress: Optional[ProgressReporter] = None,
    ) -> Dict[str, Any]:
        """
        Run the whole pipeline and return the summary -- the single public API.

        Scans the export directory, categorizes, converts and files every
        processable file, deletes validated sidecars, and returns the summary.
        This is the ONE entry point a presentation layer calls: scanning,
        categorization, sidecar deletion, and summary generation each happen
        exactly once per run, so no caller re-drives (or reaches into) the
        pipeline (issue #13). Sidecar deletion runs LAST, after every
        processable file has been filed or recorded as failed (issue #57): the
        original ordering deleted the user's ``.aae`` edit history before a
        single photo was safely in ``backup/``, so a run that crashed midway
        left the edits gone and the photos unprocessed. If anything earlier in
        this method raises without being caught (the per-file catch-all in
        ``_process_category`` and ``_process_single_file`` normally absorbs
        per-file failures, but ``ensure_target_directories`` or a target-
        directory lookup failing outright would not be), that exception
        propagates before the deletion call is ever reached -- so a run that
        fails to process any photo leaves every ``.aae`` candidate untouched.

        Reuse contract (issue #35): a FileProcessor instance is explicitly
        reusable -- calling this method a second (or Nth) time on the SAME
        instance is legal and reports on that run alone. Every per-run
        accumulator (the processed/failed/quarantined lists, the conversion
        log, the per-directory timestamp-collision sets, and each
        collaborator's own bookkeeping -- ``FileCategorizer``,
        ``HeicConverter``, ``ExifHandler``) is reset at the top of this call via
        :meth:`clear_processing_state`, so the dict this method returns can
        never describe the union of this run and an earlier one on the same
        instance. The one deliberate exception is *what* the per-directory
        collision sets are reseeded WITH: they are not restored from a
        snapshot of the previous run, but reseeded lazily, from each target
        directory's actual on-disk contents, the next time a file is placed
        there (issue #6). That is correct, not an oversight -- a second run
        must see whatever this run's own destination, or a completely
        different process, has since written to ``backup/``, not a stale
        in-memory picture of it.

        Args:
            dry_run: If True, show what would be done without making changes.
            progress: Optional :class:`ProgressReporter` the run reports to at
                its meaningful points. ``None`` runs headless (no gating, no
                per-file notifications), exactly as before the seam existed.

        Returns:
            Dictionary with processing results and statistics for THIS run
            only.

        Raises:
            ValueError: If the export and backup directories overlap (same
                directory, or one nested inside the other), which would let a
                run destroy its own inputs.
        """
        # Reset every per-run accumulator -- including each collaborator's own
        # bookkeeping -- before this run's first side effect. See the reuse
        # contract note above: this is what makes calling this method more
        # than once on the same instance safe.
        self.clear_processing_state()

        logger.info("Starting file processing (dry_run=%s)", dry_run)

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
            logger.warning("No files found in %s", self.export_dir)
            if progress is not None:
                progress.on_no_files()
            return self._generate_summary()

        logger.info("Found %s files to process", len(all_files))

        # Categorize files
        categorized = self.categorizer.batch_categorize(all_files)
        logger.info("Categorization complete:\n%s", self.categorizer.get_file_summary())

        # Compute the processable set now -- before the confirmation gate --
        # so its HEIC count can ride along on the ``on_categorized`` report
        # below (issue #14: a reporter needs this to size a conversion-total
        # progress bar). This is a pure read of the categorization already
        # collected above (``get_processable_files`` only filters
        # ``self.categorized_files`` in memory), so moving it earlier changes
        # nothing about sidecar deletion or processing order.
        processable_files = self.categorizer.get_processable_files()

        # Report the categorized total to the presentation layer and let it gate
        # the run. This is the confirmation seam: a reporter that declines
        # (returns False) aborts BEFORE any sidecar is deleted or any file is
        # moved, so a cancelled run touches nothing (issue #13). ``None`` and the
        # permissive default both proceed, preserving headless behaviour.
        if progress is not None:
            stats = self.categorizer.get_categorization_stats()
            # Issue #14 addition: the count of processable files that are HEIC
            # -- not a categorization category of its own, so it is not among
            # the keys ``get_categorization_stats`` already returns. Uses the
            # SAME selection ``_prepare_heic_conversions`` uses below, so the
            # conversion bar's total can never drift from the set of files
            # that actually go through ``on_heic_converted``.
            stats['heic'] = len(self._collect_heic_files(processable_files))
            if not progress.on_categorized(len(all_files), stats):
                logger.info("Processing aborted by progress reporter")
                return self._generate_summary()

        # Create target directories
        if not dry_run:
            self.categorizer.ensure_target_directories(self.backup_dir)

        # Phase A (parallel): convert HEIC files up front in a bounded process
        # pool (issue #42). This is a pure per-file map -- decode/encode/write,
        # no shared state -- whose results feed the sequential place phase
        # below. A dry run converts nothing, so it never enters here and never
        # spawns a pool (#10 parity preserved).
        if not dry_run:
            self._prepare_heic_conversions(processable_files, progress)

        # Phase B (sequential): timestamp, resolve collisions, move, and delete
        # originals in the SAME deterministic order as a fully sequential run,
        # so ``_used_timestamps`` resolution and landing paths are byte-for-byte
        # identical regardless of whether Phase A ran in a pool.
        for category, files in processable_files.items():
            if files:
                self._process_category(category, files, dry_run, progress)

        # Validate and delete Apple sidecar (.aae) candidates only now that
        # every processable file has been filed in backup/ or recorded as
        # failed -- see the reordering note on this method's docstring, and
        # _delete_sidecar_files below for the content validation itself
        # (issue #57).
        #
        # Completing the loop above is NOT proof anything actually landed in
        # backup/: _process_category's own per-file try/except (issue #39's
        # sanctioned broad catch) guarantees the loop completes even when
        # EVERY single processable file failed -- a full disk partway
        # through a real run is the ordinary way this happens. Deferring the
        # delete call is worthless if it still fires unconditionally once
        # the loop returns, so a run that archived nothing must not delete
        # anything either; the survivors are recorded so the existing Kept
        # table explains why they are still there.
        #
        # "Archived" is computed as attempted-minus-failed, NOT by reading
        # len(self._processed_files) directly: a dry run's branch of
        # _process_single_file returns before ever appending to
        # _processed_files (it only plans, it moves nothing), so a direct
        # read would make every dry run look like "nothing archived" and
        # report sidecars_deleted=0 while a real run over the same input
        # reports N -- breaking the #10 dry-run/real-run parity this fix
        # itself is required to preserve. attempted-minus-failed instead
        # counts a quarantined file (issue #58; genuinely undecodable, but
        # its bytes DID land safely in backup/corrupt/, in both dry and real
        # runs) as "archived", which is correct: nothing was lost for it.
        attempted = sum(len(files) for files in processable_files.values())
        archived = attempted - len(self._failed_files)
        if attempted > 0 and archived <= 0:
            logger.warning(
                "Every processable file failed (%s of %s); keeping all %s "
                "sidecar candidate(s) rather than delete a user's edit "
                "history for photos that never safely landed in backup/",
                len(self._failed_files), attempted,
                len(categorized[FileCategory.SIDECAR]),
            )
            for file_path in categorized[FileCategory.SIDECAR]:
                self._skipped_sidecars.append(('run_archived_nothing', file_path))
        else:
            self._delete_sidecar_files(categorized[FileCategory.SIDECAR], dry_run)

        # Generate summary
        return self._generate_summary()

    def _scan_export_directory(self) -> List[str]:
        """
        Scan export directory for all files.

        A dotted (hidden) file's basename is no longer a silent drop (issue
        #30): a name matching :data:`HIDDEN_FILE_DENYLIST` (known OS junk --
        ``.DS_Store``, ``.localized``, ``Thumbs.db``) or any other dotted name
        is excluded from the returned list -- unchanged from before this
        fix -- but is now also appended to ``self._skipped_files`` as
        ``(reason, path)`` before being skipped, so it is counted rather than
        vanishing from every downstream total. This matches the policy
        ``_scan_export_directory`` already applies to hidden *directories*
        one level up (issue #56's ``dirs[:] = ...`` prune below): a dotted
        name is never a pipeline participant at either level, but unlike a
        pruned directory's contents (which were never individually visited),
        a skipped top-level file IS individually visited here, so it is the
        right place to record it.

        Returns:
            List of retained (non-skipped, regular) file paths. Skipped
            hidden/junk files are recorded as a side effect in
            ``self._skipped_files``, not returned here -- see
            :meth:`_generate_summary` for how the two are reconciled into the
            files_scanned/files_skipped accounting.
        """
        if not Path(self.export_dir).exists():
            logger.error("Export directory does not exist: %s", self.export_dir)
            return []

        files = []
        for root, dirs, filenames in os.walk(self.export_dir):
            # Prune hidden directories in place so os.walk never descends into
            # them (issue #56). macOS export volumes are littered with system
            # dot-directories -- .Trashes, .Spotlight-V100, .fseventsd,
            # .DocumentRevisions-V100 -- and a repo drop-off adds .git. These are
            # never intended photo input: descending would archive files out of
            # .Trashes/ and .git/ under fabricated timestamps and delete .aae
            # sidecars found inside them. Mutating ``dirs`` in place is the
            # documented os.walk mechanism for skipping subtrees, and it applies
            # at every depth, so a hidden directory nested arbitrarily deep is
            # pruned too. The leaf-level hidden-file skip below is retained, and
            # -- as of issue #30 -- counted rather than silently dropped, so a
            # dotted name is handled identically (ignored, not archived) at
            # both levels, but no longer invisibly at the file level.
            dirs[:] = [d for d in dirs if not d.startswith('.')]

            for filename in filenames:
                # A name matching the denylist is genuine, expected OS junk
                # (issue #30 item 1): correctly never archived, before AND
                # after this fix. Anything else dotted is policy-ignored too
                # (matching the hidden-directory prune above), but -- unlike
                # before -- is recorded rather than dropped, since a leading
                # dot alone does not prove a file is junk (an interrupted
                # rsync/scp partial or a cloud-sync conflict copy can carry
                # real photo content under a dotted name).
                if self._is_denylisted_junk(filename):
                    skip_reason = 'junk'
                elif filename.startswith('.'):
                    skip_reason = 'hidden'
                else:
                    skip_reason = None

                if skip_reason is not None:
                    skipped_path = str(Path(root) / filename)
                    logger.info(
                        "Skipping %s file: %s", skip_reason, skipped_path
                    )
                    self._skipped_files.append((skip_reason, skipped_path))
                    continue

                path = str(Path(root) / filename)

                # Reject anything that is not a regular file (issue #54).
                # os.walk yields directory entries, but its ``filenames`` list
                # can still include FIFOs (named pipes), sockets, and device
                # nodes -- non-regular files an untrusted archive (tar/cpio)
                # can materialize in export/. Opening a FIFO for reading blocks
                # forever until a writer appears, hanging the entire run (even
                # --dry-run) once a later Image.open reaches it. Path.is_file()
                # uses stat() -- it never opens the file, so this check cannot
                # itself block -- and returns True only for regular files and
                # symlinks pointing at regular files. A symlink to a real image
                # is therefore kept (its target is resolved/named in issue #63);
                # FIFOs, sockets, devices, and broken symlinks are skipped.
                if not Path(path).is_file():
                    logger.warning("Skipping non-regular file: %s", path)
                    continue

                files.append(path)

        return files

    def _looks_like_apple_sidecar(self, file_path: str) -> Optional[bool]:
        """
        Report whether ``file_path``'s CONTENT looks like an Apple sidecar.

        Membership in ``FileCategory.SIDECAR`` is decided purely by the
        ``.aae`` extension (``FileCategorizer``), which is attacker/accident
        influenceable: any file merely named ``*.aae`` -- of any size, from
        anywhere in the export tree -- would otherwise qualify for deletion.
        This reads the first 16 bytes and checks them against
        :data:`SIDECAR_MAGIC` (an XML property list starts ``<?xml``; a binary
        property list starts ``bplist00``), which is enough to distinguish a
        genuine sidecar from an unrelated file that merely shares the
        extension, without reading (or trusting) the rest of the file.

        Fails CLOSED on ``OSError`` -- covering a plain permission error, a
        scan/delete race where the file vanished, AND the non-obvious
        ``IsADirectoryError`` case (a directory happens to be named
        ``dir.aae``; ``open(..., 'rb')`` raises rather than silently reading
        nothing) -- by returning ``None`` rather than ``False``. The two are
        deliberately distinct return values: ``False`` means the content WAS
        read and did not match; ``None`` means the content was never actually
        inspected at all, because the file could not be opened. Collapsing
        both into ``False`` would let :meth:`_delete_sidecar_files` tell a
        user their perfectly good, unreadable sidecar "was not a plist
        despite the .aae extension" -- a claim about content nobody checked.

        Args:
            file_path: Candidate sidecar path (matched the ``.aae`` extension).

        Returns:
            ``True`` if the file's leading bytes match a known plist magic
            prefix; ``False`` if the file was read but they do not match;
            ``None`` if the file could not be opened/read at all.
        """
        try:
            with open(file_path, 'rb') as handle:
                head = handle.read(16)
        except OSError as error:
            logger.warning(
                "Could not read candidate sidecar %s, keeping: %s", file_path, error
            )
            return None
        return head.startswith(self.SIDECAR_MAGIC)

    def _delete_sidecar_files(self, sidecar_files: List[str], dry_run: bool):
        """
        Validate, then delete, Apple sidecar files -- never on extension alone.

        Called only after every processable file has been filed or recorded as
        failed (see the reordering note on :meth:`process_all_files`), and only
        for a candidate whose content actually validates as a plist via
        :meth:`_looks_like_apple_sidecar`, which returns one of three states.
        Content read but not matching is recorded in ``_skipped_sidecars`` as
        ``('not_plist', file_path)``; content that could not be read AT ALL
        (permission error, directory named ``*.aae``, a scan/delete race) is
        recorded separately as ``('unreadable', file_path)`` -- distinct
        reasons, because "not a plist" is a claim about content that was
        never actually inspected in the second case. Both are left on disk
        untouched. A validated sidecar whose ``os.remove`` itself fails (a
        race, a permissions change) is likewise left in place and recorded as
        ``('delete_failed', file_path)``.

        Neither outcome is recorded in ``_processed_files`` or
        ``_failed_files``: a sidecar was never a processable file (issue #31)
        -- it is excluded from ``FileCategorizer.get_processable_files`` --
        so counting it there would let ``files_processed + files_failed``
        exceed the number of files actually seen as processable.

        A dry run validates content exactly as a real run does -- it is what
        makes the reported count match a real run for the same input (issue
        #10) -- but never opens the file for writing and never calls
        ``os.remove``; a validated candidate is simply recorded in
        ``_deleted_sidecars`` as "would delete" without touching disk.

        Args:
            sidecar_files: Candidate sidecar paths (matched the ``.aae``/
                ``.AAE`` extension; content not yet checked).
            dry_run: If True, validate and report but perform no deletion.
        """
        if not sidecar_files:
            return

        logger.info("Validating %s candidate sidecar files for deletion", len(sidecar_files))

        for file_path in sidecar_files:
            validated = self._looks_like_apple_sidecar(file_path)
            if validated is None:
                # _looks_like_apple_sidecar already logged WHY it could not
                # be read; this reason is distinct from 'not_plist' below --
                # its content was never actually inspected.
                self._skipped_sidecars.append(('unreadable', file_path))
                continue
            if not validated:
                logger.warning(
                    "Not an Apple sidecar despite the .aae extension, keeping: %s",
                    file_path,
                )
                self._skipped_sidecars.append(('not_plist', file_path))
                continue

            if dry_run:
                logger.info("[DRY RUN] Would delete sidecar file: %s", file_path)
                self._deleted_sidecars.append(file_path)
                continue

            try:
                os.remove(file_path)
                logger.info("Deleted sidecar file: %s", file_path)
                self._deleted_sidecars.append(file_path)
            except OSError as e:
                logger.error("Failed to delete sidecar file %s: %s", file_path, e)
                self._skipped_sidecars.append(('delete_failed', file_path))

    def _process_category(
        self,
        category: FileCategory,
        files: List[str],
        dry_run: bool,
        progress: Optional[ProgressReporter] = None,
    ):
        """
        Process files for a specific category.

        Args:
            category: FileCategory to process
            files: List of file paths
            dry_run: If True, only show what would be done
            progress: Optional reporter notified once per file after it is
                handled (issue #13 seam for #14's per-file progress).
        """
        logger.info("Processing %s %s files", len(files), category.value)

        target_dir = self.categorizer.get_target_directory(category, self.backup_dir)

        for file_path in files:
            # Determined from the file's own extension, independent of
            # success/failure below -- a HEIC always goes through the convert
            # phase (whose own completion was already reported via
            # ``on_heic_converted``, in the pool or inline); everything else
            # (including quarantined and unknown files) only moves (issue #14).
            action = 'convert' if self.heic_converter.is_heic_file(file_path) else 'move'
            try:
                # A broad ``except Exception`` is deliberate and correct HERE
                # (issue #39's general rule): this is the per-file batch loop
                # -- one bad file among hundreds must not abort the run -- and
                # it satisfies the rule's other half too, logging at ERROR
                # with the path and the exception before recording it below.
                self._process_single_file(file_path, category, target_dir, dry_run, progress)
            except Exception as e:
                logger.error("Failed to process file %s: %s", file_path, e)
                # The exception object itself, not str(e) -- see the
                # _delete_sidecar_files comment above for why.
                self._failed_files.append(('process_file', file_path, e))
            # Notify after the file is handled -- whether it landed, was
            # quarantined, or was recorded as failed. Placed after the
            # ``except Exception`` (not in a ``finally``) so a propagating
            # ``KeyboardInterrupt`` is not reported as a completed file.
            if progress is not None:
                progress.on_file(file_path, category.value, action)

    def _collect_heic_files(
        self, processable_files: Dict[FileCategory, List[str]]
    ) -> List[str]:
        """
        Return every HEIC/HEIF file across all processable categories.

        Single source of truth for "which files are HEIC" among the
        processable set, shared by :meth:`process_all_files` (to size the
        issue #14 conversion-bar total) and :meth:`_prepare_heic_conversions`
        (to select the pool's input). The conversion bar's correctness IS the
        equality of those two counts: if the selection changed independently
        in each place, the bar would drift from what ``on_heic_converted``
        actually reports -- hanging short of its total or overshooting it,
        since ``rich`` does not clamp ``completed`` to ``total``.

        Args:
            processable_files: The per-category file lists about to be placed.

        Returns:
            Every HEIC/HEIF source path among ``processable_files``, in
            per-category-then-scan order (the same order
            ``processable_files`` itself iterates in).
        """
        return [
            path
            for files in processable_files.values()
            for path in files
            if self.heic_converter.is_heic_file(path)
        ]

    def _prepare_heic_conversions(
        self,
        processable_files: Dict[FileCategory, List[str]],
        progress: Optional[ProgressReporter] = None,
    ) -> None:
        """
        Run the parallel HEIC convert phase, populating ``_converted_heic``.

        Collects every HEIC file across all processable categories (via
        :meth:`_collect_heic_files`) and, only when there are enough of them
        to amortize pool startup (``HEIC_PARALLEL_THRESHOLD``), converts them
        in a bounded process pool. Below the threshold nothing is done:
        ``_converted_heic`` stays empty and each HEIC is converted inline in
        the sequential place phase, exactly as before issue #42. Non-HEIC
        files never enter here.

        This method performs conversions only; it assigns no timestamps, moves
        nothing, and deletes nothing. All of that stays in the sequential place
        phase so ordering and collision resolution are unchanged.

        Args:
            processable_files: The per-category file lists about to be placed.
            progress: Optional reporter whose ``on_heic_converted`` fires once
                per HEIC file converted here (issue #14). Below the threshold
                this method converts nothing, so no callback fires from here --
                the sequential place phase reports those files itself, inline,
                as each is actually converted.
        """
        heic_files = self._collect_heic_files(processable_files)
        if len(heic_files) < self.HEIC_PARALLEL_THRESHOLD:
            # Not worth a pool: leave the map empty so the place phase converts
            # these inline (sequentially), matching pre-#42 behaviour exactly.
            return
        self._converted_heic = self._convert_heic_files_parallel(heic_files, progress)

    def _heic_worker_count(self, num_files: int) -> int:
        """
        Return the bounded worker count for the HEIC conversion pool.

        Capped at :data:`MAX_HEIC_WORKERS` and never ``os.cpu_count()`` when the
        host has more cores than the cap (issue #42): oversubscription measurably
        regressed against an already-threaded libheif. Also never more workers
        than files, and always at least one.

        Args:
            num_files: Number of HEIC files to be converted.

        Returns:
            The number of worker processes to spawn.
        """
        cpu = os.cpu_count() or 1
        capped = min(self.MAX_HEIC_WORKERS, cpu)
        return max(1, min(capped, num_files))

    def _convert_heic_files_parallel(
        self,
        heic_files: List[str],
        progress: Optional[ProgressReporter] = None,
    ) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
        """
        Convert HEIC files in a bounded process pool; return per-file results.

        Maps :func:`_convert_heic_worker` across ``heic_files`` in a
        :class:`concurrent.futures.ProcessPoolExecutor` sized by
        :meth:`_heic_worker_count`. Results are consumed via
        :func:`concurrent.futures.as_completed`, which is also where the
        per-file progress callback (issue #14) fires: as each future completes
        -- in whatever order workers happen to finish, not submission order --
        ``progress.on_heic_converted`` is called for that file. This reports
        real, live progress during the expensive parallel phase itself, rather
        than only after the (unrelated, cheap) sequential place phase that
        follows it. It does not touch the #42 equivalence guarantee: the
        ``results`` dict this method returns is keyed by path, not by
        completion order, and the sequential place phase iterates
        ``processable_files`` in its own fixed order regardless of which
        worker happened to finish first -- so the callback firing order is a
        pure side channel with no effect on landing paths, collision
        resolution, or failure accounting. A single failing file is recorded
        rather than aborting the batch.

        Robustness guarantees:

        * A worker that returns a failure tuple is recorded as ``(None, error)``
          -- the batch continues.
        * If the pool itself dies (:class:`BrokenProcessPool`), every file
          without a result is converted inline (sequentially) so no file is
          silently lost.
        * ``KeyboardInterrupt`` cancels outstanding work and shuts the pool down
          without orphaning workers, then propagates to the top-level handler.

        Args:
            heic_files: Source ``.heic`` paths to convert.
            progress: Optional reporter whose ``on_heic_converted`` fires once
                per file as its conversion (pooled or backfilled) completes.

        Returns:
            A dict mapping each source path to ``(output_path, error)``.
        """
        worker_count = self._heic_worker_count(len(heic_files))
        quality = self.heic_converter.jpeg_quality
        optimize = self.heic_converter.optimize
        results: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

        logger.info(
            "Converting %s HEIC files in a pool of %s workers",
            len(heic_files),
            worker_count,
        )
        try:
            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(
                        _convert_heic_worker, path, quality, optimize
                    ): path
                    for path in heic_files
                }
                try:
                    for future in as_completed(futures):
                        _src, output_path, error = future.result()
                        path = futures[future]
                        results[path] = (output_path, error)
                        if progress is not None:
                            progress.on_heic_converted(path)
                except KeyboardInterrupt:
                    # Do not wait on in-flight work; cancel what has not started
                    # and tear the pool down before re-raising so no worker is
                    # orphaned. main.py catches the re-raised interrupt.
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
        except BrokenProcessPool as exc:
            # A worker died unexpectedly (e.g. OOM-killed). Rather than lose the
            # whole batch, fall back to sequential conversion for the remainder.
            logger.error(
                "HEIC conversion pool broke (%s); converting remaining files "
                "sequentially",
                exc,
            )

        # Backfill any file the pool did not resolve -- a broken pool, or a
        # future cancelled on interrupt that we nonetheless reached here for --
        # by converting it inline. Guarantees every input has a result, and
        # each backfilled file still reports exactly once.
        for path in heic_files:
            if path not in results:
                _src, output_path, error = _convert_heic_worker(
                    path, quality, optimize
                )
                results[path] = (output_path, error)
                if progress is not None:
                    progress.on_heic_converted(path)

        return results

    def _convert_heic(
        self, file_path: str, progress: Optional[ProgressReporter] = None
    ) -> Optional[str]:
        """
        Return the converted-JPEG path for a HEIC, or ``None`` on failure.

        Bridges the parallel convert phase (issue #42) and the sequential place
        phase. When ``file_path`` was pre-converted by the pool its cached
        result is used and the converter's stats bookkeeping is mirrored so the
        summary is identical to a sequential run; the worker performed the write
        in a throwaway process, so the main-process converter never saw it.
        When there is no cached result (batch below the pool threshold), it
        converts inline via :meth:`HeicConverter.convert_heic_to_jpeg` -- the
        exact pre-#42 sequential path.

        Args:
            file_path: Source ``.heic`` path.
            progress: Optional reporter whose ``on_heic_converted`` fires once
                the conversion performed *here* completes (issue #14). Fired
                only on the inline branch: a cached (pool) result was already
                reported when the pool produced it, in
                :meth:`_convert_heic_files_parallel`, so reporting it again
                here would double-count that file.

        Returns:
            The transient JPEG path on success, or ``None`` on conversion
            failure (the caller records the ``('convert_heic', ...)`` failure).
        """
        if file_path in self._converted_heic:
            output_path, error = self._converted_heic[file_path]
            if output_path is not None:
                # Mirror the bookkeeping convert_heic_to_jpeg records in-process
                # so conversion stats (heic_conversions) match sequential.
                self.heic_converter.converted_files.append((file_path, output_path))
                return output_path
            self.heic_converter.failed_conversions.append(
                (file_path, error or 'HEIC conversion failed')
            )
            return None
        # No pooled result: convert inline, exactly as the pre-#42 code did.
        result = self.heic_converter.convert_heic_to_jpeg(file_path)
        if progress is not None:
            progress.on_heic_converted(file_path)
        return result

    def _process_single_file(
        self,
        file_path: str,
        category: FileCategory,
        target_dir: str,
        dry_run: bool,
        progress: Optional[ProgressReporter] = None,
    ):
        """
        Process a single file: convert if needed, rename with timestamp, move to target.

        Args:
            file_path: Source file path
            category: FileCategory
            target_dir: Target directory path
            dry_run: If True, only show what would be done
            progress: Optional reporter passed through to :meth:`_convert_heic`
                so an inline (below-threshold) HEIC conversion can report its
                own completion (issue #14).
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

        # The suffix the file will carry once it is filed in ``backup/``. For a
        # HEIC this is the JPEG suffix its conversion produces, not the original
        # ``.heic`` -- derived here (issue #10) so a dry run plans the SAME final
        # extension a real run lands. For every other file it is the source
        # suffix, normalized to its canonical lowercase spelling (issue #55) so
        # ``IMG_1.JPG`` and ``IMG_2.jpg`` land under the same spelling instead of
        # preserving whatever case/alias the source happened to use. Both modes
        # read this single value, so the planned destination name can never
        # diverge on extension between them. This normalization deliberately
        # does NOT reach ``backup/unknown/`` or ``backup/corrupt/`` -- both
        # routes above return before this line, filing under the file's
        # untouched original name, because their whole point is preserving that
        # name as the sole recovery/identification clue (issues #29, #58).
        planned_extension = FileCategorizer.normalize_extension(Path(file_path).suffix)

        # When a HEIC is converted, this holds the original ``.heic`` path so it
        # can be deleted only *after* its verified-good JPEG has safely landed in
        # ``backup/`` (see Step 3). Deleting earlier risks leaving the user with
        # neither the original nor a filed copy if a later step fails.
        heic_original_to_delete = None

        # Step 1: Convert HEIC to JPEG if needed
        if self.heic_converter.is_heic_file(file_path):
            # A converted HEIC always lands under the ``.jpg`` suffix: the
            # converter writes an ``mkstemp`` ``.jpg`` and the final backup name
            # is timestamp-derived, so the on-disk intermediate name never
            # reaches the plan. Both modes therefore plan a ``.jpg`` destination;
            # the ONLY difference is whether the conversion side effect runs.
            planned_extension = '.jpg'
            if dry_run:
                logger.debug("[DRY RUN] Would convert HEIC to JPEG: %s", file_path)
                # ``current_path`` deliberately stays the ``.heic``: its EXIF
                # timestamp is identical to the converted JPEG's (conversion
                # preserves EXIF), so the timestamp read below matches a real run
                # without performing -- or writing -- any conversion.
            else:
                converted_path = self._convert_heic(file_path, progress)
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
                            "HEIC conversion verification failed, keeping original: %s", file_path
                        )
                        # Remove the unverifiable artifact so a corrupt JPEG is not
                        # left in export to be re-ingested on a later run. The
                        # original ``.heic`` is left untouched for the user.
                        try:
                            os.remove(converted_path)
                        except OSError:
                            pass
                        self._failed_files.append(
                            ('convert_heic', file_path, 'conversion verification failed')
                        )
                        return

                    self._conversion_log.append((file_path, converted_path))
                    current_path = converted_path
                    # Defer deletion of the original HEIC until after the JPEG has
                    # been moved into backup/ -- see Step 3.
                    heic_original_to_delete = file_path
                else:
                    # Conversion returned no path: the file was not processed.
                    # Record it so it lands in ``_failed_files`` and increments
                    # ``files_failed`` instead of being silently dropped, which
                    # would let the summary report success and the exit code
                    # read 0 for a run that left this file behind (issue #31).
                    logger.error("HEIC conversion failed for %s", file_path)
                    self._failed_files.append(
                        ('convert_heic', file_path, 'HEIC conversion failed')
                    )
                    return

        # Step 2: Extract timestamp. Pass forward the metadata FileCategorizer
        # already read once for this file while categorizing it (issue #24),
        # keyed by the ORIGINAL file_path (not current_path -- a converted
        # HEIC's current_path is a different, temporary JPEG whose EXIF is
        # verified-preserved from the original by this point). A cache miss
        # (video/unknown files never populate it; RAW/undecodable files that
        # failed to open at categorize time don't either) yields None, and
        # extract_timestamp falls back to opening current_path itself.
        timestamp = self.exif_handler.extract_timestamp(
            current_path, self.categorizer.get_image_metadata(file_path)
        )
        # Recorded now, before ``timestamp`` is overwritten below, so the
        # missing-EXIF record (issue #35) reflects whether a genuine capture
        # timestamp was found for THIS file, independent of exif_handler's own
        # path-keyed bookkeeping (which records whatever path it was called
        # with -- the transient converted-JPEG path for a HEIC -- not a path
        # useful to report to the user).
        exif_was_missing = timestamp is None
        if timestamp is None:
            # Use fallback timestamp
            timestamp = self.exif_handler.get_fallback_timestamp(current_path)
            logger.warning("Using fallback timestamp for %s", current_path)

        # Use the extension the file will actually carry in backup/ (``.jpg`` for
        # a converted HEIC), so the dry-run plan and the real run agree (#10).
        file_extension = planned_extension

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
            logger.debug("[DRY RUN] Would move: %s -> %s", current_path, target_path)
            if exif_was_missing:
                # A dry run never moves anything, so ``original_path`` is the
                # only path that actually exists on disk right now -- the
                # planned ``target_path`` is a projection, not a real file.
                # ``final_path`` is left ``None`` so the display layer shows
                # only the path that genuinely exists (issue #35).
                self._missing_exif_records.append({
                    'original_path': original_path,
                    'final_path': None,
                })
        else:
            # Bound up front, mirroring _process_unknown_file/_quarantine_file,
            # so the ``except`` below can always reference it even if
            # _reserve_destination raises before ever assigning it -- e.g. when
            # target_dir does not exist (see the trade-off note in the ``try``
            # below). Without this, that failure mode raised UnboundLocalError
            # out of the ``except`` handler itself, masking the real
            # FileNotFoundError behind a Python-internals message instead of
            # reporting the actual cause (issue #23 fix round 1).
            target_path = None
            try:
                # No mkdir() here (issue #23): target_dir is always one of
                # the four directories process_all_files already created via
                # ensure_target_directories(self.backup_dir) before this loop
                # started -- every category reaching this branch is PHOTO,
                # VIDEO, SCREENSHOT, or GENERATED (UNKNOWN returned above, at
                # the top of this method). A per-file exist_ok=True call here
                # was therefore always a no-op syscall repeated once per file
                # instead of once per run. This is NOT the same situation as
                # backup/unknown/ or backup/corrupt/ (_process_unknown_file,
                # _quarantine_file), which are deliberately excluded from
                # ensure_target_directories and created lazily on first use so
                # they are never an empty phantom implying handling that never
                # happened (issues #29, #58) -- those per-file calls stay.
                #
                # Trade-off: before this fix, a category directory deleted out
                # from under a run (e.g. someone rm -rf's backup/photos/ mid-run,
                # or a volume hiccup) was silently recreated before every
                # subsequent file in that category. Now it is not -- every
                # subsequent file in that category fails, with the real cause
                # recorded, until the directory is restored outside the tool.
                # Fail-loud-with-the-real-reason was judged the better trade for
                # a tool whose failures the user must act on, but it IS a real
                # behavior change from the old self-healing-by-accident path.

                # Atomically claim a free destination path, then move onto it.
                adjusted_timestamp, target_path = self._reserve_destination(
                    target_dir, timestamp, file_extension
                )

                try:
                    # The destination was reserved with O_CREAT | O_EXCL, so it is
                    # an empty placeholder we own -- never a pre-existing photo.
                    # Overwriting it here therefore cannot destroy user data.
                    # ``_place_source_content`` moves a regular file but COPIES a
                    # symlink's target bytes and removes only the link, so the
                    # archive holds the real photo rather than a pointer (#63).
                    self._place_source_content(current_path, target_path)
                except Exception:
                    # The move failed after the name was reserved; drop the empty
                    # placeholder so a 0-byte stub is not left behind in backup/.
                    self._discard_reservation(target_path)
                    raise
                logger.debug("Moved: %s -> %s", current_path, target_path)

                # The verified-good JPEG is now safely filed in backup/, so it is
                # finally safe to delete the original HEIC. Route through the
                # single safe-delete implementation; verification already happened
                # pre-move (the JPEG has since moved out of reach), so skip it here.
                # A conversion that failed verification is recorded as a failure
                # above and never reaches this line (issue #7).
                if heic_original_to_delete is not None:
                    self.heic_converter.cleanup_original_heic(
                        heic_original_to_delete, verify_first=False
                    )

                # Record successful processing
                self._processed_files.append({
                    'original_path': original_path,
                    'final_path': target_path,
                    'category': category.value,
                    'timestamp': adjusted_timestamp.isoformat(),
                    'converted_from_heic': self.heic_converter.is_heic_file(original_path)
                })

                if exif_was_missing:
                    # The file has now safely landed at ``target_path``; that
                    # is the path the user can actually go look at, unlike the
                    # source path (moved) or the transient converted-JPEG path
                    # (renamed away), either of which is already gone by the
                    # time the summary is displayed (issue #35).
                    self._missing_exif_records.append({
                        'original_path': original_path,
                        'final_path': target_path,
                    })

            except Exception as e:
                logger.error("Failed to move file %s to %s: %s", current_path, target_path, e)
                # The exception object itself, not str(e) -- see the
                # _delete_sidecar_files comment above for why.
                self._failed_files.append(('move_file', current_path, e))

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
        is genuinely drained; on failure it is recorded in ``_failed_files`` so
        the run is not reported as a clean success (issue #31).

        Args:
            file_path: Source path of the unrecognized file.
            target_dir: The ``backup/unknown/`` directory to file it into.
            dry_run: If True, only log what would happen; touch nothing.
        """
        original_name = Path(file_path).name

        if dry_run:
            target_path = self._resolve_named_destination_dry_run(
                target_dir, original_name
            )
            logger.info(
                "[DRY RUN] Would move unrecognized file: %s -> %s", file_path, target_path
            )
            return

        target_path = None
        try:
            # Create backup/unknown/ only now that a file is actually landing in
            # it -- this is what keeps the directory from being an empty phantom.
            Path(target_dir).mkdir(parents=True, exist_ok=True)

            target_path = self._reserve_named_destination(target_dir, original_name)
            try:
                self._place_source_content(file_path, target_path)
            except Exception:
                # The move failed after the name was reserved; drop the empty
                # placeholder so a 0-byte stub is not left behind in backup/.
                self._discard_reservation(target_path)
                raise
            logger.info("Moved unrecognized file: %s -> %s", file_path, target_path)

            self._processed_files.append({
                'original_path': file_path,
                'final_path': target_path,
                'category': FileCategory.UNKNOWN.value,
                'timestamp': None,  # no metadata timestamp; name is preserved
                'converted_from_heic': False,
            })
        except Exception as e:
            # Lazy %s (issue #39, in the spirit of #9): file_path is an
            # untrusted filename, and keeping it a logging parameter rather
            # than interpolating it into the format string matches every
            # other per-file log call site.
            logger.error(
                "Failed to move unrecognized file %s to %s: %s",
                file_path, target_path, e,
            )
            # The exception object itself, not str(e) -- see the
            # _delete_sidecar_files comment above for why.
            self._failed_files.append(('move_file', file_path, e))

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
        return str(Path(self.backup_dir) / "corrupt")

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
        move is recorded in ``_quarantined_files``; on failure it is recorded in
        ``_failed_files`` so a botched quarantine cannot masquerade as a clean run.

        In a dry run nothing is moved, but the decision is still recorded and
        logged so ``--dry-run`` surfaces exactly which files a real run would
        quarantine (issue #58 acceptance criterion).

        Args:
            file_path: Source path of the undecodable file.
            dry_run: If True, only log/record the decision; touch nothing.
        """
        original_name = Path(file_path).name
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
            self._quarantined_files.append({
                'original_path': file_path,
                'final_path': target_path,
                'reason': 'undecodable',
            })
            return

        target_path = None
        try:
            # Create backup/corrupt/ only now that a file is actually landing in
            # it -- this is what keeps the directory from being an empty phantom.
            Path(target_dir).mkdir(parents=True, exist_ok=True)

            target_path = self._reserve_named_destination(target_dir, original_name)
            try:
                self._place_source_content(file_path, target_path)
            except Exception:
                # The move failed after the name was reserved; drop the empty
                # placeholder so a 0-byte stub is not left behind in backup/.
                self._discard_reservation(target_path)
                raise
            logger.warning(
                "Quarantined undecodable file: %s -> %s", file_path, target_path
            )

            self._quarantined_files.append({
                'original_path': file_path,
                'final_path': target_path,
                'reason': 'undecodable',
            })
        except Exception as error:
            logger.error(
                "Failed to quarantine %s to %s: %s", file_path, target_path, error
            )
            # The exception object itself, not str(error) -- see the
            # _delete_sidecar_files comment above for why.
            self._failed_files.append(('quarantine', file_path, error))

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
            target_path = str(Path(target_dir) / candidate)
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
        while (Path(target_dir) / candidate).exists():
            candidate = f"{stem} ({counter}){ext}"
            counter += 1
        return str(Path(target_dir) / candidate)

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
        names = self._used_timestamps.get(target_dir)
        if names is None:
            names = set()
            try:
                for entry in Path(target_dir).iterdir():
                    if entry.is_file():
                        names.add(entry.stem)
            except FileNotFoundError:
                # The directory does not exist yet: nothing is claimed in it.
                pass
            self._used_timestamps[target_dir] = names
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
            target_path = str(Path(target_dir) / f"{stem}{file_extension}")
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

        Because no placeholder is written, a dry run reflects the reservations of
        *other* dry-run files purely through this in-memory bookkeeping. That
        bookkeeping mirrors what :meth:`_reserve_destination` does on disk, so the
        two agree on both pre-existing conflicts (seeded from disk) and
        within-batch collisions (the in-memory ``names.add``); dry-run/real
        destination parity is asserted end to end in the #10 parity test.

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
        target_path = str(Path(target_dir) / f"{stem}{file_extension}")
        return adjusted, target_path

    def _discard_reservation(self, target_path: str) -> None:
        """
        Remove a reserved placeholder after a failed move (best effort).

        Args:
            target_path: The reserved path to unlink; missing/undeletable paths
                are ignored so cleanup never masks the original move failure.
        """
        try:
            Path(target_path).unlink()
        except OSError:
            pass

    def _place_source_content(self, source: str, destination: str) -> None:
        """
        Place the real content of ``source`` onto the reserved ``destination``.

        ``destination`` is an empty placeholder previously claimed with
        ``O_CREAT | O_EXCL`` (issue #6), so overwriting it here can never destroy
        existing user data. The two source shapes are handled differently:

        * A **regular file** is moved with :func:`shutil.move`, which stays a
          single cheap ``os.rename`` on the common same-filesystem case and
          fully drains it from ``export/``.

        * A **symlink** is the whole of issue #63. ``shutil.move`` falls through
          to ``os.rename`` on a same-filesystem move, which relocates the *link
          itself* -- the archive would then hold a pointer back into the source
          tree instead of the photo, and the "backup" turns into a dead link the
          moment the user tidies the original away. The link's target may also
          live entirely outside ``export/``, and the tool must never move,
          delete, or modify that target. So the target's real bytes are COPIED
          onto the destination (:func:`shutil.copy2` of the fully resolved real
          path, which also mirrors the target's mtime), and then only the *link*
          is removed from ``export/`` -- :meth:`Path.unlink` on a symlink
          unlinks the link, never the file it points at. The target is left exactly
          where it was. Issue #54 already guaranteed a symlink reaching here
          points at a regular file (broken links and links to FIFOs/sockets were
          filtered at the scan boundary), so the resolved path is a real file.

        The link is unlinked only *after* the copy succeeds: if the copy raises,
        the exception propagates (the caller discards the reserved placeholder)
        and the symlink is left untouched in ``export/`` so nothing is lost.

        Args:
            source: The scanned source path -- a regular file, or a symlink to
                a regular file whose target content should be archived.
            destination: The reserved placeholder path to fill with real bytes.
        """
        if os.path.islink(source):
            shutil.copy2(os.path.realpath(source), destination)
            Path(source).unlink()
        else:
            shutil.move(source, destination)

    def _generate_summary(self) -> Dict[str, Any]:
        """
        Generate processing summary.

        This is the ONLY reader of the four private per-run accumulators
        (``_processed_files``, ``_used_timestamps``, ``_failed_files``,
        ``_conversion_log``) and of ``_missing_exif_records`` (issue #35):
        they are internal bookkeeping, not public API, so every count and
        list below is derived here rather than read directly by a caller.

        Accounting identity (issue #30, shared with #29): after a non-dry
        run, every path the scan visited lands in exactly one of four
        buckets --

            files_scanned == (files_processed + files_quarantined)   # moved
                            + sidecars_deleted                        # deleted
                            + (files_skipped + sidecars_skipped)      # skipped
                            + files_failed                            # failed

        ``files_quarantined`` counts toward "moved" because a quarantined
        file DID leave ``export/`` for ``backup/corrupt/`` -- it is just not
        filed as a photograph. ``files_skipped`` and ``sidecars_skipped`` are
        summed together because they are two mechanisms for the same
        bucket -- a file the scan itself declined to collect (hidden/junk;
        issue #30) versus a ``.aae`` candidate that reached, and failed,
        delete validation (issue #57) -- that both mean "still sitting in
        ``export/``, and not because anything failed." The identity holds
        because ``FileCategorizer.categorize_file`` always assigns exactly one
        of six categories (so every scanned-and-retained file is either
        processable or a sidecar candidate, never neither), and every
        processable file is recorded in exactly one of processed/quarantined/
        failed while every sidecar candidate is recorded in exactly one of
        deleted/skipped -- see ``test_hidden_files_accounting.py`` for the
        assertion. It is deliberately NOT asserted with a hard ``assert`` in
        this method: a run a caller aborted via the ``on_categorized`` gate
        (issue #13) reaches this method with files scanned but none of the
        other four buckets populated yet, which is correct (nothing was
        processed) rather than a defect this identity should reject.

        Returns:
            Dictionary with processing statistics and results for the run
            that just completed -- never a previous run on the same instance
            (see the reuse-contract note on :meth:`process_all_files`).
        """
        stats = self.categorizer.get_categorization_stats()
        heic_stats = self.heic_converter.get_conversion_stats()

        return {
            # Every path the scan actually visited, INCLUDING hidden/junk
            # files this run declined to collect (issue #30) -- unlike
            # ``stats['total']`` alone, which only covers what
            # ``batch_categorize`` saw, i.e. the files ``_scan_export_directory``
            # already filtered ``_skipped_files`` out of. This is the "scanned"
            # term in the accounting identity documented below.
            'files_scanned': stats['total'] + len(self._skipped_files),
            'files_processed': len(self._processed_files),
            'files_failed': len(self._failed_files),
            # Undecodable image-typed files quarantined to backup/corrupt/. A
            # distinct outcome from processed (they were NOT filed as photos)
            # and from failed (nothing errored; they were handled deliberately
            # and safely), so the count is honest either way (issue #58).
            'files_quarantined': len(self._quarantined_files),
            # Apple sidecars actually deleted (real run) or that a dry run
            # confirmed it WOULD delete -- the two counts are equal for
            # identical input (issue #10 parity). Neither this nor
            # 'sidecars_skipped' below feeds files_processed/files_failed:
            # a sidecar was never a processable file (issue #31/#57).
            'sidecars_deleted': len(self._deleted_sidecars),
            # Candidates that matched the .aae extension but were kept rather
            # than deleted -- see the __init__ comment on _skipped_sidecars
            # for the four reasons.
            'sidecars_skipped': len(self._skipped_sidecars),
            # Hidden/junk files the SCAN itself declined to collect (issue
            # #30) -- distinct from 'sidecars_skipped' above, which counts
            # .aae candidates that WERE categorized and reached (and failed)
            # sidecar-delete validation. Never fed into files_processed or
            # files_failed for the same reason 'sidecars_skipped' is not: a
            # skipped file was never a processable file to begin with (it
            # never even reached the categorizer), so counting it there would
            # inflate those totals past files_scanned's own accounting.
            'files_skipped': len(self._skipped_files),
            'categorization_stats': stats,
            'heic_conversions': heic_stats['successful_conversions'],
            'heic_conversion_failures': heic_stats['failed_conversions'],
            'missing_exif_files': len(self._missing_exif_records),
            'processed_files': self._processed_files.copy(),
            'failed_files': self._failed_files.copy(),
            'quarantined_files': self._quarantined_files.copy(),
            'conversion_log': self._conversion_log.copy(),
            # Each entry carries BOTH 'original_path' and 'final_path' so the
            # display layer can show a path that genuinely exists on disk
            # (issue #35) -- 'final_path' for a real run (the source is gone
            # by the time this is rendered) or None for a dry run (nothing
            # moved, so 'original_path' is the one that exists). A plain
            # list comprehension of fresh dicts, not the internal list
            # itself, so a caller mutating the result cannot corrupt
            # ``_missing_exif_records`` (issue #37's no-aliasing precedent).
            'missing_exif_list': [
                record.copy() for record in self._missing_exif_records
            ],
            # (reason, path) pairs for candidates kept rather than deleted --
            # see _delete_sidecar_files. A fresh list, not the internal one
            # itself, matching 'missing_exif_list' above (issue #37's
            # no-aliasing precedent).
            'skipped_sidecar_files': self._skipped_sidecars.copy(),
            # (reason, path) pairs for files the scan declined to collect --
            # see the __init__ comment on _skipped_files for 'junk' vs.
            # 'hidden'. A fresh list, not the internal one itself, matching
            # 'missing_exif_list'/'skipped_sidecar_files' above (issue #37's
            # no-aliasing precedent).
            'skipped_files': self._skipped_files.copy(),
        }

    def clear_processing_state(self):
        """
        Reset every per-run accumulator, including each collaborator's own.

        Called automatically as the first statement of
        :meth:`process_all_files` (issue #35), so a FileProcessor instance is
        safe to reuse across multiple runs without a caller having to
        remember to call this explicitly. It remains a public method -- kept,
        not removed, per the issue's Option B -- so a caller with an unusual
        need to reset mid-lifecycle (or a test asserting the reset is
        complete) still has an explicit hook.

        Resets this object's own seven private accumulators
        (``_processed_files``, ``_used_timestamps``, ``_failed_files``,
        ``_quarantined_files``, ``_deleted_sidecars``, ``_skipped_sidecars``,
        ``_skipped_files``), ``_conversion_log`` and ``_missing_exif_records``,
        plus every collaborator's own bookkeeping
        (``ExifHandler.missing_exif_files``,
        ``HeicConverter.converted_files``/``failed_conversions``,
        ``FileCategorizer.categorized_files``) -- a reset that only cleared
        this object's attributes and left the collaborators' stale would be a
        half-measure, since ``_generate_summary`` reads categorization and
        HEIC stats FROM those collaborators, not from a local copy.
        """
        self._processed_files.clear()
        self._used_timestamps.clear()
        self._failed_files.clear()
        self._quarantined_files.clear()
        self._conversion_log.clear()
        self._missing_exif_records.clear()
        self._deleted_sidecars.clear()
        self._skipped_sidecars.clear()
        self._skipped_files.clear()
        self._converted_heic = {}
        self.exif_handler.clear_missing_files_log()
        self.heic_converter.clear_stats()
        self.categorizer.clear_categorization()
