# tm-monthly-backup

A robust Python CLI utility for automating monthly photo organization from Apple Photos exports.

## Features

- **Smart File Processing**: Converts HEIC files to high-quality JPEG with EXIF preservation
- **Intelligent Organization**: Separates files into photos, videos, screenshots, and generated content
- **Timestamp-Based Renaming**: Uses EXIF/metadata timestamps (YYYY.MM.DD.HH.MM.SS format)
- **Video Metadata Extraction**: Extracts creation dates from video file metadata (MOV, MP4, etc.)
- **AI Content Detection**: Automatically identifies and separates AI-generated images and heavily edited photos
- **Screenshot Recognition**: Detects iOS screenshots with pattern matching
- **Automatic Cleanup**: Removes Apple sidecar (.aae) files once their content validates as a genuine plist
- **Duplicate Handling**: Intelligently resolves timestamp conflicts
- **Rich CLI Experience**: Beautiful progress bars, colored output, and detailed summaries
- **Dry-Run Mode**: Safe testing without file modifications
- **Non-Interactive Mode**: `--yes` skips confirmation prompts for cron/CI/automated use
- **Configurable HEIC Handling**: `--jpeg-quality` tunes the HEIC->JPEG encode (1-100, default 98)
- **Comprehensive Logging**: Detailed error handling and processing reports

## Requirements

- Python 3.8+
- macOS (tested), Linux (should work), Windows (untested)

## Installation

1. Clone the repository:
```bash
git clone https://github.com/tommcfarlin/tm-monthly-backup.git
cd tm-monthly-backup
```

2. Create and activate a virtual environment (recommended):
```bash
python3 -m venv tm-backup-env
source tm-backup-env/bin/activate  # On Windows: tm-backup-env\Scripts\activate
```

3. Install the package (editable install, recommended):
```bash
pip install -e .
```

This installs the dependencies from `pyproject.toml` and registers a
`tm-monthly-backup` console command, so the tool can be run from anywhere:
```bash
tm-monthly-backup --help
```

Alternatively, install just the dependencies without the console command:
```bash
pip install -r requirements.txt
```
`requirements.txt` mirrors the dependency list in `pyproject.toml`, which is the
single source of truth.

### Dependencies
- `click>=8.0.0,<9` - CLI interface framework
- `pillow>=10.0.0,<12` - Image processing and EXIF data extraction
- `pillow-heif>=0.10.0,<1` - HEIC file format support
- `python-dateutil>=2.8.0,<3` - Advanced date/time parsing
- `rich>=13.0.0,<15` - Beautiful CLI progress bars and formatting
- `hachoir>=3.1.0,<4` - Video metadata extraction

## Usage

1. Export photos from iCloud Photos to the `export/` directory
2. Run the backup utility:

```bash
# Dry run (recommended first)
python -m src.main --dry-run

# Process files with progress display
python -m src.main

# Non-interactive (cron/CI/automated) -- skips confirmation prompts
python -m src.main --yes

# Verbose output for debugging
python -m src.main --verbose

# View help and options
python -m src.main --help
```

After `pip install -e .`, the same commands are available through the
`tm-monthly-backup` console entry point from any directory:

```bash
tm-monthly-backup --dry-run
tm-monthly-backup --help
```

## Directory Structure

```
tm-monthly-backup/
├── export/          # Place exported iCloud files here
├── backup/          # Organized output files
│   ├── photos/      # Real photos with EXIF timestamps
│   ├── videos/      # Videos with metadata timestamps
│   ├── screenshots/ # iOS screenshots and screen captures
│   ├── generated/   # AI-generated and heavily edited content
│   └── unknown/     # Unrecognized file types (if any)
├── src/             # Source code
├── tests/           # Test suite
└── docs/            # Documentation
```

## Smart Content Detection

### AI-Generated Content
Automatically detects and separates AI-generated images:
- **C2PA Metadata**: Files with ChatGPT, GPT-4o, or OpenAI signatures
- **UUID Filenames**: 36-character UUID-format names (often generated content)
- **Editing Software**: Files processed by editing software without original EXIF data

### Video Timestamps
Extracts real creation dates from video metadata:
- **MOV, MP4, M4V**: Uses embedded creation timestamps
- **Screen Recordings**: Handles screen capture metadata
- **Fallback Handling**: Uses filesystem dates when metadata unavailable

### Screenshot Recognition
Identifies iOS and macOS screenshots:
- **Filename Patterns**: `IMG_3XXX.PNG`, `Screenshot`, `Screen Shot`
- **iOS Patterns**: Recognizes standard iOS screenshot naming

## File Processing

- **HEIC files**: Converted to JPEG (default quality 98, configurable with `--jpeg-quality`; lossy at any setting) with EXIF preservation; the original HEIC is deleted after a verified conversion
- **Apple sidecar files (.aae)**: Deleted automatically once their content validates as a genuine plist; a look-alike that merely shares the extension is kept and reported
- **Hidden files**: Known OS junk (`.DS_Store`, `.localized`, `Thumbs.db`) and any other dotted filename (e.g. `.hidden_photo.jpg`) are skipped rather than archived — matching the policy already applied to hidden directories, which are never walked at all — but never silently dropped: every skip is counted (`files_skipped`) and named in the results, so a real photo that happens to carry a leading dot is never absent from every total the way it used to be
- **Naming convention**: Files renamed using EXIF/metadata timestamps (YYYY.MM.DD.HH.MM.SS); the extension is lowercased and normalized to one spelling per format (`.jpeg` -> `.jpg`, `.tiff` -> `.tif`), so the same format never appears under two spellings in the same backup folder. Quarantined (`backup/corrupt/`) and unrecognized (`backup/unknown/`) files are the exception: they keep their exact original filename, case included
- **Video metadata**: Extracts creation timestamps from video file headers
- **Duplicate handling**: Timestamp conflicts resolved by incrementing seconds
- **AI content separation**: Generated and heavily edited content goes to dedicated folder
- **Missing metadata**: Fallback to filesystem timestamps with user warnings

## Testing

The project includes a comprehensive test suite covering unit tests and integration tests.

### Run All Tests
```bash
python tests/run_tests.py
```

### Run Specific Test Types
```bash
# Unit tests only
python tests/run_tests.py --unit-only

# Integration tests only
python tests/run_tests.py --integration-only
```

See [tests/README.md](tests/README.md) for detailed testing documentation.

## Documentation

### User Guides
- [CLI Usage Guide](docs/cli-usage.md) - Complete command-line reference
- [Troubleshooting Guide](docs/troubleshooting.md) - Solutions for common issues

### Developer Documentation
- [Development Workflow](docs/development-workflow.md) - Contribution guidelines
- [Test Documentation](tests/README.md) - Test suite information

## Development

See [docs/development-workflow.md](docs/development-workflow.md) for contribution guidelines.

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for version history.
