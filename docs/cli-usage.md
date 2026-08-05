# CLI Usage Guide

Complete reference for using the tm-monthly-backup command-line interface.

## How to Invoke the Tool

There are two supported ways to run tm-monthly-backup:

- **Installed console command (recommended).** After `pip install -e .` (see the
  README's Installation section), a `tm-monthly-backup` command is on your PATH
  and runs from any directory:

  ```bash
  tm-monthly-backup --help
  ```

- **No-install module form.** From the repository root, with the dependencies
  installed (`pip install -r requirements.txt`), run the tool as a module:

  ```bash
  python -m src.main --help
  ```

Both invocations accept the identical options and are interchangeable. Running
`python src/main.py` directly does **not** work — the package uses absolute
`src.…` imports, so the script cannot resolve its own package that way. The
examples below use `tm-monthly-backup`; substitute `python -m src.main` for the
same effect without installing.

## Quick Start

```bash
# Basic usage - process files in export/ directory
tm-monthly-backup

# Dry run to preview what would be done
tm-monthly-backup --dry-run

# Verbose output for debugging
tm-monthly-backup --verbose
```

## Command Options

### Basic Options

| Option | Short | Description | Default |
|--------|-------|-------------|---------|
| `--dry-run` | | Preview operations without making changes | `False` |
| `--yes` | `-y` | Assume yes for all prompts (required for non-interactive use) | `False` |
| `--verbose` | `-v` | Enable detailed logging output | `False` |
| `--help` | | Show help message and exit | |
| `--version` | | Show version information | |

### Directory Configuration

| Option | Description | Default |
|--------|-------------|---------|
| `--export-dir` | Directory containing exported files | `export` |
| `--backup-dir` | Directory for organized output files | `backup` |

### HEIC Conversion Options

| Option | Description | Default |
|--------|-------------|---------|
| `--jpeg-quality` | JPEG quality (1-100) for HEIC->JPEG conversion | `98` |
| `--keep-heic` | Keep original HEIC/HEIF files after a verified conversion instead of deleting them | `False` (originals deleted) |

`--jpeg-quality` is validated at the command line (`click.IntRange(1, 100)`); a
value outside that range is rejected before any file is touched.
`--keep-heic` affects only the delete step — a conversion that fails
verification (see "Verify Before Delete" below) is always recorded as a
failure and the original is always left in place, whether or not
`--keep-heic` was passed.

## Usage Examples

### Basic Processing

```bash
# Process files with default settings
tm-monthly-backup

# Output example:
# tm-monthly-backup
# Automated Apple Photos Organization Tool
#
# ✓ Backup directory ready: backup
#
# File Discovery Summary
# ┌───────────────┬───────┬─────────────────────────────────────┐
# │ Category      │ Count │ Description                         │
# ├───────────────┼───────┼─────────────────────────────────────┤
# │ Photos        │    45 │ JPEG, PNG, HEIC, etc.               │
# │ Videos        │    12 │ MOV, MP4, M4V, etc.                 │
# │ Screenshots   │     8 │ PNG files with screenshot patterns  │
# │ Sidecar Files │    23 │ Apple .aae files (will be deleted)  │
# │ Unknown       │     2 │ Unrecognized file types             │
# │ Total         │    90 │ Files to process                    │
# └───────────────┴───────┴─────────────────────────────────────┘
```

The **Unknown** row is shown only when at least one unrecognized file was
found.

### Dry Run Mode

```bash
# Preview operations without making changes
tm-monthly-backup --dry-run

# Shows exactly what would be processed:
# [DRY RUN] Would delete sidecar file: export/IMG_1234.aae
# [DRY RUN] Would convert HEIC to JPEG: export/photo.heic
# [DRY RUN] Would move: export/photo.jpg -> backup/photos/2024.01.15.14.30.45.jpg
```

A dry run predicts the exact plan a real run would execute — the same
destination path (including the `.jpg` extension a HEIC lands as), the same
timestamp-collision bumps, the same quarantine and unknown-file decisions —
without moving, converting, or deleting anything.

### Non-Interactive / Automated Use

Two confirmation prompts require a terminal: "Continue anyway?" (empty
`export/`) and "Proceed with processing N files?" (every real run). Without a
terminal — cron, CI, `nohup`, a piped invocation — reading either prompt raises
an immediate, unhelpful `EOFError`. Pass `--yes` to skip both prompts and
proceed as though they were accepted:

```bash
# Run unattended -- no prompts, ever
tm-monthly-backup --yes

# A monthly automated backup on the 1st of each month at 2 AM (crontab)
0 2 1 * * /path/to/venv/bin/tm-monthly-backup --export-dir /path/to/export --backup-dir /path/to/backup --yes >> /var/log/tm-monthly-backup.log 2>&1
```

`--dry-run` never prompts either way, with or without `--yes` — it makes no
change that needs confirming, so it is always safe to run unattended
(`tm-monthly-backup --dry-run` alone is enough for a scheduled preview run).

Running without a terminal and without `--yes` or `--dry-run` fails fast with
an actionable message instead of the raw `EOFError`:

```
Error: No terminal available for confirmation. Re-run with --yes or --dry-run.
```

This check runs before anything is scanned, so it cannot know in advance
whether a prompt would actually have been reached — it requires `--yes` (or
`--dry-run`) for *any* non-interactive invocation, unconditionally. **This is
a behavior change worth knowing about if you already have a cron job
running**: previously, a run over an `export/` directory that was not
technically empty (e.g. it contained only a stray subdirectory, with no files
anywhere inside it) never hit either prompt at all — the empty-directory check
only looks at the top level, and the scan finding zero files further down
short-circuits to a clean, silent success. That invocation exited `0` with no
prompt before this flag existed; without `--yes` it now exits `2` with the
message above, since the tool cannot tell the two cases apart without a
terminal to ask from. Adding `--yes` to an existing scheduled job (as shown
above) restores the old behavior for that case and every other one.

This exits with the precondition code (`2`, see Exit Codes below) before
anything is scanned, categorized, or touched.

### Custom Directories

```bash
# Use custom export and backup directories
tm-monthly-backup --export-dir /path/to/icloud/export --backup-dir /path/to/organized

# Relative paths work too
tm-monthly-backup --export-dir ../downloads --backup-dir ./monthly-backup
```

The export and backup directories may not overlap. If `--backup-dir` is the same
as `--export-dir`, or one is nested inside the other, the tool refuses to run and
exits with a precondition error (code `2`) before touching anything — an
overlapping destination would let a run re-ingest and destroy its own inputs.

### HEIC Conversion Tuning

```bash
# Smaller JPEGs at the cost of some visible compression artifacting
tm-monthly-backup --jpeg-quality 80

# Keep every original HEIC in export/ alongside the converted JPEG in backup/
tm-monthly-backup --keep-heic

# Both together
tm-monthly-backup --jpeg-quality 90 --keep-heic
```

`--keep-heic` is useful when you want a lossless fallback beside the archived
JPEG, or simply are not ready to trust the conversion yet — but it means
`export/` keeps growing with every HEIC-heavy run rather than being fully
drained. **More importantly, a retained original is invisible to this tool
as "already archived": the next run will re-convert it and re-file it under
a new, bumped timestamp that is not its capture time, producing a duplicate
copy in `backup/`.** This repeats every run for as long as the original
remains in `export/`. Move or delete retained originals out of `export/`
once you have verified the backup, or run without `--keep-heic` again for a
subsequent pass over the same directory.

### Verbose Logging

```bash
# Enable detailed logging for troubleshooting
tm-monthly-backup --verbose

# Shows detailed processing information:
# [14:30:45] INFO     Starting file processing (dry_run=False)
# [14:30:45] INFO     Found 45 files to process
# [14:30:45] INFO     Successfully converted HEIC to JPEG: export/IMG_1234.heic -> backup/photos/2024.01.15.14.30.45.jpg
# [14:30:45] WARNING  Using fallback timestamp for export/no_exif.jpg
```

## Directory Structure

### Input Structure (Export Directory)

The export directory should contain files exported from iCloud Photos:

```
export/
├── IMG_1001.jpg
├── IMG_1001.aae          # Sidecar file (will be deleted)
├── IMG_1002.heic
├── VID_20240115.mov
├── Screenshot 2024-01-15.png
└── subfolder/            # Nested folders supported
    ├── more_photos.jpg
    └── video.mp4
```

### Output Structure (Backup Directory)

After processing, files are organized into categorized directories:

```
backup/
├── photos/
│   ├── 2024.01.15.14.30.45.jpg
│   ├── 2024.01.15.14.31.22.jpg
│   └── 2024.01.15.15.22.33.jpg
├── videos/
│   ├── 2024.01.15.14.32.10.mov
│   └── 2024.01.15.16.45.33.mp4
├── screenshots/
│   ├── 2024.01.15.09.15.42.png
│   └── 2024.01.15.11.33.21.png
├── generated/           # AI-generated and heavily edited content
│   └── 2024.01.15.18.05.10.png
├── unknown/             # Unrecognized extensions (original names kept)
│   └── document.txt
└── corrupt/             # Undecodable image-typed files (quarantined)
    └── truncated.jpg
```

The `photos/`, `videos/`, `screenshots/`, and `generated/` directories are
created up front. `unknown/` and `corrupt/` are created lazily — only when a
file is actually routed into them — so they are never empty directories implying
handling that did not occur.

## File Processing Details

### File Types Supported

| Category | Extensions | Notes |
|----------|------------|-------|
| **Photos** | `.jpg`, `.jpeg`, `.png`, `.gif`, `.heic`, `.heif`, `.tiff`, `.tif`, `.bmp`, `.webp`, `.dng`, `.raw`, `.cr2`, `.nef`, `.arw`, `.orf`, `.rw2` | HEIC files converted to JPEG; raw formats pass through unchanged (see note below) |
| **Videos** | `.mov`, `.mp4`, `.m4v`, `.avi`, `.mkv`, `.wmv`, `.flv`, `.webm`, `.3gp`, `.mpg`, `.mpeg` | Moved without conversion |
| **Screenshots** | `.png` with screenshot patterns | Detected by filename patterns |
| **Generated** | AI-detected or heavily-edited `.png`/photo files | Routed to `generated/` (see below) |
| **Sidecar** | `.aae` | Apple sidecar files (deleted) |
| **Unknown** | All others | Moved to `unknown/` under original name |

The authoritative extension lists are `FileCategorizer.PHOTO_EXTENSIONS` and
`FileCategorizer.VIDEO_EXTENSIONS` in `src/file_categorizer.py`.

**Raw formats are not decode-verified or converted.** Pillow cannot decode raw
formats (`.dng`, `.raw`, `.cr2`, `.nef`, `.arw`, `.orf`, `.rw2`), so they are
passed through and filed by timestamp as-is — they are never quarantined by the
corrupt-file gate (which only applies to formats Pillow can decode). HEIC/HEIF
are also excluded from that gate; their integrity is established separately by
the conversion-verification step.

### Screenshot Detection

PNG files are categorized as screenshots if they match these patterns:

- `Screenshot 2024-01-15 at 2.30.45 PM.png`
- `Screen Shot 2024-01-15 at 2.30.45 PM.png`
- `IMG_1234.png` (iOS screenshot pattern)
- `Simulator Screen Shot - iPhone 15 Pro - 2024-01-15.png`

### Filename Convention

Recognized files (photos, videos, screenshots, generated) are renamed using EXIF timestamp data:

- **Format**: `YYYY.MM.DD.HH.MM.SS.extension`
- **Example**: `2024.01.15.14.30.45.jpg`
- **Duplicates**: Timestamp conflicts resolved by incrementing seconds

Unknown files have no metadata to derive a timestamp from, so they keep their **original filename** when moved to `unknown/`. A name already present there is preserved by disambiguating the incoming file as `name (1).ext`, `name (2).ext`, and so on — an unknown file never overwrites one already filed.

### HEIC Conversion

HEIC files are automatically converted to JPEG:

- **Quality**: JPEG quality 98 by default, configurable with `--jpeg-quality`
  (1-100). This is visually excellent but **lossy** — it is not a lossless
  format, at any quality setting. The encode does not run libjpeg's extra
  Huffman-optimization pass by default (`optimize=False`, issue #40): that
  pass buys only ~2% smaller files for roughly 2.5x the encode time (+152% on
  the encode step, measured in the issue #40 audit), a poor trade for an
  archive tool.
- **EXIF Preservation**: All metadata preserved
- **Retention**: The original HEIC is deleted after a verified conversion by
  default, so no lossless copy remains once the run completes. Pass
  `--keep-heic` to leave the original `.heic`/`.heif` file in place in
  `export/` alongside the converted JPEG in `backup/`. With `--keep-heic`,
  `export/` is **not** fully drained by a HEIC-heavy run — the retained
  originals remain — even though every file is still correctly counted as
  processed and filed. **This has a real consequence, not just a disk-usage
  one: nothing in this tool recognizes a retained original as
  already-archived.** A file left in `export/` is scanned, categorized, and
  processed again exactly like a new file on every subsequent run. The next
  run re-converts it, reads the same EXIF capture timestamp, finds
  `backup/photos/<that timestamp>.jpg` already occupied by the copy the
  previous run filed, and the collision-resolution logic bumps the new
  landing name forward by one second — so the retained photo gets a
  **second, duplicate copy in the archive**, filed under a timestamp that is
  **not** its actual capture time. This repeats on every run for as long as
  the original stays in `export/`. If you use `--keep-heic`, move or delete
  the retained originals out of `export/` before the next run, or expect
  growing duplication in `backup/`.
- **Verify Before Delete**: The original `.heic` is deleted only after the
  converted JPEG is verified on disk (it exists, decodes, matches the source
  dimensions, and preserves EXIF) and has landed in `backup/photos/`. If
  verification fails, the original is left in `export/` and the run records a
  failure — this verification step always runs, regardless of `--keep-heic`;
  the flag changes only whether a *successful* conversion's original is
  deleted afterward.

### Generated / AI-Detected Content

Files identified as AI-generated or heavily edited are routed to
`backup/generated/` instead of `photos/`, so a manual photo cleanup never
sweeps them up unnoticed. Like photos, they are renamed by timestamp. Detection
(in `FileCategorizer._is_generated_content`) is deliberately **precise** rather
than a broad substring match — an earlier bare-substring approach misfiled
ordinary photos (`ai` matched inside "chair", "trail", "portrait"). A file is
treated as generated when any of these hold:

- **PNG provenance keys**: a PNG text chunk whose *key* is a known generator key
  — `c2pa` (Content Provenance manifest) or `parameters` (Stable Diffusion /
  AUTOMATIC1111 generation settings).
- **Word-boundary tool markers**: a PNG text *value* containing a high-signal
  product marker matched at word boundaries — `chatgpt`, `openai`, `gpt-4` /
  `gpt-4o`, `dall-e` / `dall·e`, `midjourney`, `stable diffusion`, `firefly`, or
  `c2pa`. Word boundaries keep these from matching inside longer words.
- **Editing software with no capture time**: EXIF `Software` names an editor
  (Snapseed, Photoshop, Lightroom, GIMP, Canva) **and** the image carries no
  genuine `DateTimeOriginal` / `DateTimeDigitized` capture timestamp (read from
  both IFD0 and the Exif sub-IFD). A real photo retouched in Lightroom keeps its
  capture time and stays a photo.
- **UUID filename**: the filename stem parses as a valid UUID (a common
  convention for generated output), validated by actually parsing it — not by
  counting characters.

### Video Metadata / Local Capture Time

Videos are renamed from their true **local wall-clock** capture time. Apple
records this — with its UTC offset — in the `com.apple.quicktime.creationdate`
metadata key (e.g. `2024-06-15T21:33:03-0400`), and the tool reads it directly
from the QuickTime/MP4 box structure. The local reading is kept as-is
(`21:33:03` stays `21:33:03`, not converted to UTC), so a video and a photo
captured at the same instant share the same `YYYY.MM.DD.HH.MM.SS` stem.

When that key is absent, the tool falls back to hachoir's `mvhd` creation time,
which QuickTime defines as **UTC**. That fallback can therefore be off by the
local UTC offset for containers that lack the Apple key — a known limitation of
metadata that simply does not carry the local time.

### Corrupt-File Quarantine

Before an image-typed file (`.jpg`/`.jpeg`/`.png`/`.gif`/`.tiff`/`.tif`/`.bmp`/
`.webp`) is renamed and filed as a photo, its bytes are decode-verified with a
full pixel decode. A truncated download, a zero-byte stub, or a non-image file
mislabeled `.jpg`/`.png` fails this check and is **quarantined** to
`backup/corrupt/` under its **original filename** (a corrupt file has no reliable
capture time, and the rename would destroy the one clue to what it was). It never
overwrites a name already there (`name (1).ext`, `name (2).ext`, … disambiguation).

Quarantined files are reported as a distinct outcome — a `files_quarantined`
count, neither a clean "processed" nor a tool "failure" — and the results banner
reads "Files Quarantined" rather than "Success!" so you know there are files in
`backup/corrupt/` to review. Raw and HEIC/HEIF files are excluded from this gate
(see the file-types note above).

### Skipped Entries

Some entries in `export/` are intentionally skipped during the scan:

- **Hidden directories** (`.Trashes`, `.Spotlight-V100`, `.fseventsd`, `.git`,
  etc.) are pruned at every depth and never descended into — their contents are
  never categorized, archived, or discovered for sidecar deletion. Hidden files
  at the leaf level are likewise skipped.
- **Non-regular files** (FIFOs/named pipes, sockets, device nodes, broken
  symlinks) are skipped and logged; opening one could block the run forever.
- **Symlinks** pointing at a real image are processed, but the tool copies the
  **resolved target's bytes** into the backup and removes only the link from
  `export/` — the target itself is never moved, modified, or deleted.

## Progress Display

The CLI provides rich visual feedback during processing:

### Progress Bars

```
Categorizing files...     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:00:01
Processing sidecar files... ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:00:00
Converting HEIC files...  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:00:03
Organizing files...       ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:00:02
```

### Results Summary

```
Processing Complete - Success!
┌───────────────────────────┬───────┐
│ Metric                    │ Count │
├───────────────────────────┼───────┤
│ Files Processed           │    68 │
│ HEIC Conversions          │    12 │
│ Missing EXIF Files        │     3 │
└───────────────────────────┴───────┘

File Organization
┌─────────────┬───────┬──────────────────────┐
│ Category    │ Files │ Location             │
├─────────────┼───────┼──────────────────────┤
│ Photos      │    45 │ backup/photos/       │
│ Videos      │    12 │ backup/videos/       │
│ Screenshots │     8 │ backup/screenshots/  │
│ Generated   │     3 │ backup/generated/    │
└─────────────┴───────┴──────────────────────┘
```

The **Generated** row appears only when at least one file was routed there;
likewise an **Unknown** row (`backup/unknown/`) appears when unrecognized files
were filed. When undecodable files were quarantined, the banner changes to
"Processing Complete - Files Quarantined", a "Quarantined (undecodable)" metric
row is added, and a **Quarantined** row pointing at `backup/corrupt/` is included:

```
Processing Complete - Files Quarantined
┌───────────────────────────┬───────┐
│ Metric                    │ Count │
├───────────────────────────┼───────┤
│ Files Processed           │    66 │
│ HEIC Conversions          │    12 │
│ Missing EXIF Files        │     3 │
│ Quarantined (undecodable) │     2 │
└───────────────────────────┴───────┘

File Organization
┌─────────────┬───────┬──────────────────────┐
│ Category    │ Files │ Location             │
├─────────────┼───────┼──────────────────────┤
│ Photos      │    45 │ backup/photos/       │
│ Videos      │    12 │ backup/videos/       │
│ Screenshots │     8 │ backup/screenshots/  │
│ Generated   │     3 │ backup/generated/    │
│ Quarantined │     2 │ backup/corrupt/      │
└─────────────┴───────┴──────────────────────┘
```

## Error Handling

### Common Error Scenarios

The CLI gracefully handles various error conditions:

| Error Type | Behavior | Example |
|------------|----------|---------|
| **Missing Export Directory** | Exit with error message | `Error: Export directory does not exist: /path/to/export` |
| **Permission Issues** | Skip file, continue processing | `Failed to delete sidecar file: Permission denied` |
| **Corrupted Files** | Use fallback timestamp | `Warning: Using fallback timestamp for corrupted.jpg` |
| **Disk Space** | Abort processing, show error | `Error: Insufficient disk space for backup operations` |

### Error Reporting

When errors occur, the CLI provides detailed information:

```
Processing Complete - With Errors
┌─────────────────────┬───────┐
│ Metric              │ Count │
├─────────────────────┼───────┤
│ Files Processed     │    62 │
│ Failed Files        │     3 │
└─────────────────────┴───────┘

Failed Files (3):
┌───────────────┬──────────────────────────┬─────────────────────────┐
│ Operation     │ File                     │ Error                   │
├───────────────┼──────────────────────────┼─────────────────────────┤
│ process_file  │ export/corrupted.jpg     │ Cannot read image file  │
│ move_file     │ export/photo.heic        │ Permission denied       │
│ heic_convert  │ export/invalid.heic      │ Invalid HEIC format     │
└───────────────┴──────────────────────────┴─────────────────────────┘
```

## Configuration

All configuration is via command-line options — there is no environment
variable support and no configuration file, and none is planned. This is a
small, personal tool; the options below are the complete, closed set:

| Option | Controls |
|--------|----------|
| `--export-dir` / `--backup-dir` | Source and destination directories |
| `--jpeg-quality` | HEIC->JPEG encode quality (1-100, default `98`) |
| `--keep-heic` | Whether a converted HEIC's original is deleted or kept |
| `--dry-run` / `--yes` / `--verbose` | Run behavior — see "Command Options" above |

Several other things this tool hardcodes are **deliberately not**
configurable, so a heuristic bug report becomes a precision fix rather than a
new knob to support forever: the photo/video/screenshot/sidecar extension
sets, the screenshot filename patterns, the AI-provenance markers, and the
editing-software list (`src/file_categorizer.py`) are all class constants.
Each is a heuristic with known edge cases (see the false-positive notes in the
"Screenshot Detection" and "Generated / AI-Detected Content" sections above);
the answer to a false positive is a targeted precision fix to that heuristic,
not a configuration flag letting every user carry their own copy of the rule.
The timestamp filename format (`YYYY.MM.DD.HH.MM.SS`) is likewise fixed.

## Exit Codes

The CLI uses distinct exit codes so scripts can act on the result of a run.
Each code carries exactly one meaning:

| Code | Meaning | Description |
|------|---------|-------------|
| `0` | Success | Every discovered file was processed; zero failures (also returned for a dry run and for an empty export directory) |
| `1` | Partial failure | Processing ran but one or more files failed; the failures are listed in the summary |
| `2` | Precondition failure | The run could not start or was aborted before completing: a missing or unwritable directory, an export/backup overlap, no terminal available for confirmation without `--yes`/`--dry-run`, or an unexpected error. Nothing was processed |
| `130` | Cancelled | The user declined the confirmation prompt or interrupted the run with `SIGINT` (Ctrl-C); follows the POSIX `128 + signal` convention |

A `0` means the run is done and no file was left behind, so a script can safely
act on it:

```bash
tm-monthly-backup
if [ $? -eq 0 ]; then
    echo "All files organized into backup/."
fi
```

Any non-zero code means at least one file was not processed (`1`), the run never
started (`2`), or it was cancelled (`130`) -- none of which should be treated as
a completed, safe run.

## Performance Notes

### Processing Speed

Typical processing speeds on modern hardware:

- **File Categorization**: ~1000 files/second
- **HEIC Conversion**: ~5-10 files/second (depends on image size)
- **File Moving**: ~100-500 files/second (depends on storage)

### Memory Usage

- **Base Memory**: ~50MB for the application
- **Per File**: ~1-5MB during HEIC conversion
- **Peak Usage**: Scales linearly with concurrent HEIC conversions

### Disk Space

Ensure adequate disk space before processing:

- **HEIC Files**: Converted JPEG files are typically 70-90% of original size
- **Working Space**: Temporary space needed during conversion (up to 2x file size)
- **Backup Directory**: Space for all processed files