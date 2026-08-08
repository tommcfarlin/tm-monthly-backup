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

2. Enable the repository's own commit guard:
```bash
git config core.hooksPath .githooks
```
This installs a `pre-commit` hook that refuses to commit image or video
**content**. It is worth the one command: this repository's subject is a
personal photo library, so it sits one `git add -A` away from publishing family
photographs, and the repo is public. `.gitignore` covers the directories the
tool reads and writes (`export/`, `backup/`, `export-*/`, `backup-*/`) plus
media file extensions anywhere in the tree — but both of those match by *name*,
so a real JPEG saved as `notes.txt` slips through. The hook inspects the leading
bytes of every staged file, so renaming does not evade it. Git does not install
hooks from a clone automatically, which is why this is a manual step.

3. Create and activate a virtual environment (recommended):
```bash
python3 -m venv tm-backup-env
source tm-backup-env/bin/activate  # On Windows: tm-backup-env\Scripts\activate
```

4. Install the package (editable install, recommended):
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

For a reproducible, tamper-evident install (recommended for anything other than
local development), use the hash-pinned lockfile instead of either of the above:
```bash
pip install --require-hashes -r requirements.lock
```
See [Security](#security) below for why this matters for this particular tool.

### Dependencies
- `click>=8.1.7,<9` - CLI interface framework
- `pillow>=10.3.0,<13` - Image processing and EXIF data extraction
- `pillow-heif>=0.16.0,<2` - HEIC file format support
- `python-dateutil>=2.8.2,<3` - Advanced date/time parsing
- `rich>=13.7.0,<16` - Beautiful CLI progress bars and formatting
- `hachoir>=3.3.0,<4` - Video metadata extraction

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

## Security

`export/` is treated as **untrusted input**, not as a folder of your own known-good
photos. Every file in it is handed to Pillow, `pillow-heif` (libheif), and
`hachoir` for decoding -- all three parse untrusted binary data, and Pillow and
`pillow-heif` are C-backed with active CVE histories (heap buffer overflows and
memory-exhaustion bugs in image/HEIF decoding have shipped in past releases,
some of them fixed only in the last one or two minor versions). A crafted or
merely corrupt file dropped into `export/` -- from an iCloud sync glitch, a
damaged download, or deliberate tampering -- reaches these libraries' decoders
directly, which is exactly the class of bug their CVE histories are made of.

Two things follow from that:

1. **Keeping the pinned dependency versions current is a security task, not
   housekeeping.** `pyproject.toml`/`requirements.txt` set floors that exclude
   every release with a known advisory on a code path this tool exercises
   (WebP/HEIF/JPEG/PNG decoding, EXIF parsing) and ceilings at the next major so
   a future breaking release cannot be installed silently. Raising a floor
   again in the future should be treated the same way: find the CVE or advisory
   that motivates it, not just "the newest version."
2. **Prefer the lockfile for anything other than local development.**
   `requirements.lock` (generated with `pip-compile --generate-hashes`, or
   `uv pip compile --generate-hashes` if you use uv) pins every dependency,
   direct and transitive, to one exact version with its package hash, so
   `pip install --require-hashes -r requirements.lock` fails closed rather than
   silently installing a tampered or substituted package. Regenerate it with:
   ```bash
   pip install pip-tools
   pip-compile --generate-hashes --output-file=requirements.lock pyproject.toml
   ```
   after changing anything in `pyproject.toml`'s `dependencies` list.

## What Ends Up in `backup/`

Converted JPEGs preserve the complete EXIF block from the original HEIC, including
**GPS coordinates**, camera make/model, device name, and capture timestamps. A
file that is only moved rather than converted (already a JPEG, PNG, video, and
so on) keeps its bytes untouched, so it carries forward whatever EXIF or other
metadata it already had, unchanged. Treat `backup/` as being exactly as
sensitive as your original photo library. If you sync or share this directory,
you are sharing that metadata.

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
