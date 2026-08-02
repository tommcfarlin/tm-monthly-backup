# CLI Usage Guide

Complete reference for using the tm-monthly-backup command-line interface.

## Quick Start

```bash
# Basic usage - process files in export/ directory
python src/main.py

# Dry run to preview what would be done
python src/main.py --dry-run

# Verbose output for debugging
python src/main.py --verbose
```

## Command Options

### Basic Options

| Option | Short | Description | Default |
|--------|-------|-------------|---------|
| `--dry-run` | | Preview operations without making changes | `False` |
| `--verbose` | `-v` | Enable detailed logging output | `False` |
| `--help` | `-h` | Show help message and exit | |
| `--version` | | Show version information | |

### Directory Configuration

| Option | Description | Default |
|--------|-------------|---------|
| `--export-dir` | Directory containing exported files | `export` |
| `--backup-dir` | Directory for organized output files | `backup` |

## Usage Examples

### Basic Processing

```bash
# Process files with default settings
python src/main.py

# Output example:
# tm-monthly-backup
# Automated Apple Photos Organization Tool
#
# ✓ Backup directory ready: backup
#
# File Discovery Summary
# ┌─────────────┬───────┬─────────────────────────────┐
# │ Category    │ Count │ Description                 │
# ├─────────────┼───────┼─────────────────────────────┤
# │ Photos      │    45 │ JPEG, PNG, HEIC, etc.     │
# │ Videos      │    12 │ MOV, MP4, M4V, etc.       │
# │ Screenshots │     8 │ PNG files with patterns    │
# │ Sidecar     │    23 │ Apple .aae files (deleted) │
# │ Total       │    88 │ Files to process           │
# └─────────────┴───────┴─────────────────────────────┘
```

### Dry Run Mode

```bash
# Preview operations without making changes
python src/main.py --dry-run

# Shows exactly what would be processed:
# [DRY RUN] Would delete sidecar file: export/IMG_1234.aae
# [DRY RUN] Would convert HEIC to JPEG: export/photo.heic
# [DRY RUN] Would move: export/photo.jpg -> backup/photos/2024.01.15.14.30.45.jpg
```

### Custom Directories

```bash
# Use custom export and backup directories
python src/main.py --export-dir /path/to/icloud/export --backup-dir /path/to/organized

# Relative paths work too
python src/main.py --export-dir ../downloads --backup-dir ./monthly-backup
```

### Verbose Logging

```bash
# Enable detailed logging for troubleshooting
python src/main.py --verbose

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
└── unknown/              # Files with unrecognized extensions
    └── document.txt
```

## File Processing Details

### File Types Supported

| Category | Extensions | Notes |
|----------|------------|-------|
| **Photos** | `.jpg`, `.jpeg`, `.png`, `.gif`, `.heic`, `.heif`, `.tiff`, `.dng`, `.raw` | HEIC files converted to JPEG |
| **Videos** | `.mov`, `.mp4`, `.m4v`, `.avi`, `.mkv`, `.wmv` | Moved without conversion |
| **Screenshots** | `.png` with screenshot patterns | Detected by filename patterns |
| **Sidecar** | `.aae` | Apple sidecar files (deleted) |
| **Unknown** | All others | Moved to `unknown/` directory |

### Screenshot Detection

PNG files are categorized as screenshots if they match these patterns:

- `Screenshot 2024-01-15 at 2.30.45 PM.png`
- `Screen Shot 2024-01-15 at 2.30.45 PM.png`
- `IMG_1234.png` (iOS screenshot pattern)
- `Simulator Screen Shot - iPhone 15 Pro - 2024-01-15.png`

### Filename Convention

All processed files are renamed using EXIF timestamp data:

- **Format**: `YYYY.MM.DD.HH.MM.SS.extension`
- **Example**: `2024.01.15.14.30.45.jpg`
- **Duplicates**: Timestamp conflicts resolved by incrementing seconds

### HEIC Conversion

HEIC files are automatically converted to high-quality JPEG:

- **Quality**: 95% (lossless visual quality)
- **EXIF Preservation**: All metadata preserved
- **Original Cleanup**: HEIC files deleted after successful conversion

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
┌─────────────────────┬───────┐
│ Metric              │ Count │
├─────────────────────┼───────┤
│ Files Processed     │    65 │
│ HEIC Conversions    │    12 │
│ Missing EXIF Files  │     3 │
└─────────────────────┴───────┘

File Organization
┌─────────────┬───────┬──────────────────────┐
│ Category    │ Files │ Location             │
├─────────────┼───────┼──────────────────────┤
│ Photos      │    45 │ backup/photos/       │
│ Videos      │    12 │ backup/videos/       │
│ Screenshots │     8 │ backup/screenshots/  │
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

### Environment Variables

Currently, all configuration is done via command-line options. Future versions may support:

- `TM_BACKUP_EXPORT_DIR` - Default export directory
- `TM_BACKUP_BACKUP_DIR` - Default backup directory
- `TM_BACKUP_QUALITY` - JPEG conversion quality (1-100)

### Configuration File

Future versions may support a configuration file:

```yaml
# ~/.tm-monthly-backup.yml
export_dir: ~/Downloads/icloud-export
backup_dir: ~/Photos/organized
jpeg_quality: 95
keep_heic_originals: false
```

## Exit Codes

The CLI uses distinct exit codes so scripts can act on the result of a run.
Each code carries exactly one meaning:

| Code | Meaning | Description |
|------|---------|-------------|
| `0` | Success | Every discovered file was processed; zero failures (also returned for a dry run and for an empty export directory) |
| `1` | Partial failure | Processing ran but one or more files failed; the failures are listed in the summary |
| `2` | Precondition failure | The run could not start or was aborted before completing: a missing or unwritable directory, an export/backup overlap, or an unexpected error. Nothing was processed |
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