# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Backup Files No Longer Overwritten Across Runs**: `FileProcessor` judged filename collisions with `used_timestamps`, an in-memory `set` populated only during the current process and never seeded from what already sat in `backup/`. Because `shutil.move` overwrites its destination silently, two photos processed in *different runs* (e.g. two monthly dumps) that resolved to the same `YYYY.MM.DD.HH.MM.SS` name caused the second run to overwrite — and irrecoverably destroy — the first run's file, while reporting a clean summary and exiting `0`. An attacker who could influence a filename in `export/` could even *aim* the overwrite at a known archive name. Collision detection is now filesystem-authoritative and scoped per target directory: before a file is filed, its destination path is reserved atomically with `os.open(..., O_CREAT | O_EXCL)`, which closes the check-then-move TOCTOU window and can never select an already-present file as the move target. The timestamp is bumped a second at a time until a free path is claimed, and the real file is then moved onto the empty placeholder the reservation created (a failed move drops the placeholder). Each per-directory name set is seeded once from the directory's on-disk contents so resolution does not rediscover conflicts one filesystem probe at a time. A dry run stays side-effect free but now reports the *bumped* path a file would take when its natural name is already occupied, instead of a name a real run would overwrite.
- **Cross-Category Timestamp Collisions Eliminated**: `used_timestamps` was a single flat set shared across every category, so a photo and a video (or screenshot) resolving to the same second were treated as a collision and one was bumped by a second — even though they are written to *separate* directories and can never collide on disk. The resulting filename no longer matched the media's true capture time. Collision tracking is now keyed by target directory (`Dict[str, Set[str]]`), so files in different category directories share no collision namespace and each keeps its exact timestamp; two files in the *same* directory still bump correctly.
- **HEIC Original Verified Before Deletion**: `FileProcessor._process_single_file` deleted the original `.heic` with `os.remove` the instant `convert_heic_to_jpeg` returned a non-`None` path, with no check that the JPEG was actually valid on disk. Because the converter returns its output path immediately after `image.save` without inspecting the result, a truncated write, a zero-byte file, a full disk, or a partial decode all produced a "successful" return — after which the only copy of the original photo was destroyed. The pipeline now calls `HeicConverter.verify_conversion` against the *actual* converted path (the unique `mkstemp` name, not a stale `{stem}.jpg`) before the original becomes eligible for deletion: the JPEG must exist, decode, match the source dimensions, and preserve EXIF. On verification failure the original `.heic` is left in place in `export/`, the unverifiable artifact is removed, and the failure is recorded in `failed_files` so it drives a non-zero `files_failed`. The delete is further deferred until *after* the verified JPEG has landed in `backup/photos/<timestamp>.jpg`, so a failure during the move can never leave the user with neither the original nor a filed copy. Deletion now routes through the single `HeicConverter.cleanup_original_heic` implementation, retiring the inline `os.remove` duplicate; `cleanup_original_heic` was also repaired to verify against an explicit converted path rather than the stale `{stem}.jpg` name.
- **HEIC Conversion No Longer Overwrites Existing Files**: `HeicConverter.convert_heic_to_jpeg` wrote the converted JPEG to a fixed `{stem}.jpg` beside the HEIC. When a real sibling with that name already sat in the export tree — an exact `IMG_1234.jpg`, or an uppercase `IMG_1234.JPG` on a case-insensitive APFS volume — the conversion silently overwrote and destroyed that photo, reporting `files_failed: 0` with no error. Because `os.walk` returns APFS entries in hash order, the loss occurred on roughly half of all same-stem pairs. The converter now reserves a guaranteed-unique output path with `tempfile.mkstemp` (atomic `O_CREAT | O_EXCL`) on the same filesystem, so it can never collide with an existing sibling in either the exact-name or case-insensitive case, and the downstream `shutil.move` to `backup/photos/<timestamp>.jpg` stays a cheap same-volume rename. Callers already use the returned path, so the transient intermediate name is invisible to the final timestamp-derived backup filename.
- **Overlapping Export/Backup Directories Rejected**: The tool now refuses to run when `--export-dir` and `--backup-dir` overlap — the same directory, or one nested inside the other. Because the export tree is consumed in place (sidecars and converted-from HEIC originals are deleted), an overlapping destination let a run re-ingest and destroy its own archive. Paths are resolved with `Path.resolve()` (normalizing symlinked and relative aliases) and compared for equality and containment in both directions. The guard runs on both entry surfaces: `CLIInterface.check_directories` reports the conflict and exits non-zero, and `FileProcessor.process_all_files` raises `ValueError` before scanning so a programmatic caller cannot bypass it.
- **Timestamp Rollover Crash**: `ExifHandler.handle_duplicate_timestamp` no longer raises `ValueError` when incrementing past a second, minute, hour, or month boundary. Replaced the manual `datetime.replace` arithmetic with `timedelta`, which handles all rollovers correctly.
- **EXIF Sub-IFD Timestamp Reachability**: `ExifHandler.extract_timestamp` now reads the Exif sub-IFD (pointer tag `0x8769`) via `Image.Exif.get_ifd`, merging it with IFD0 before the priority walk. `DateTimeOriginal` (0x9003) and `DateTimeDigitized` (0x9004) live in the sub-IFD, which `Image.getexif()` does not expose at the top level, so every photo was previously named from IFD0 `DateTime` (file modification time) instead of capture time. The declared priority `DateTimeOriginal` > `DateTime` > `DateTimeDigitized` is now actually honored, and a malformed higher-priority tag falls through to the next candidate rather than aborting. Applies to HEIC as well, since pillow-heif exposes EXIF through the same API.

## [1.0.0] - 2025-08-01

### Added
- **Smart AI Content Detection**: Automatically identifies and separates AI-generated images (C2PA metadata, ChatGPT signatures)
- **Video Metadata Extraction**: Real timestamp extraction from MOV, MP4, M4V files using hachoir library
- **Generated Content Category**: New `backup/generated/` directory for AI-generated and heavily edited content
- **UUID Filename Detection**: Identifies likely generated content with UUID-format filenames
- **Enhanced Screenshot Recognition**: Improved iOS screenshot pattern detection
- **Rich CLI Interface**: Beautiful progress bars, colored output, and detailed file processing summaries
- **Comprehensive Editing Software Detection**: Identifies Snapseed, Photoshop, Lightroom processed files
- **Virtual Environment Setup**: Added recommended venv installation instructions
- Comprehensive CLI usage documentation with examples and configuration options
- Detailed troubleshooting guide covering common issues and solutions
- Complete user and developer documentation structure
- Advanced debugging and diagnostic procedures
- Performance optimization guidelines
- Environment-specific troubleshooting for macOS, Windows, and Linux

### Changed
- **File Organization**: Now creates 5 categories (photos, videos, screenshots, generated, unknown)
- **CLI Command Structure**: Updated to use `python -m src.main` for proper module execution
- **Directory Structure**: Added `backup/generated/` for AI-generated and heavily edited content
- **Timestamp Processing**: Enhanced fallback handling with filesystem creation times on macOS
- **Dependencies**: Added `hachoir>=3.1.0` for video metadata extraction
- Enhanced README with structured documentation links and new feature descriptions
- Improved documentation organization with user vs developer guides

### Deprecated

### Removed

### Fixed
- **Critical Duplicate Processing Bug**: Eliminated dual processing loop that caused files to be duplicated across categories
- **Video Timestamp Accuracy**: Videos now get correct creation dates from metadata instead of export dates
- **PNG File Categorization**: Fixed logic to properly route PNG files through AI detection before photo categorization
- **Missing EXIF Handling**: Improved fallback timestamp extraction using filesystem metadata

### Security