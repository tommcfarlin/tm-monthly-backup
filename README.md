# tm-monthly-backup

A robust Python CLI utility for automating monthly photo organization from Apple Photos exports.

## Features

- Converts HEIC files to high-quality JPEG with EXIF preservation
- Organizes files by type: photos, videos, screenshots
- Renames files using EXIF timestamp format (YYYY.MM.DD.HH.MM.SS)
- Removes Apple sidecar (.aae) files automatically
- Handles duplicate timestamps intelligently
- Dry-run mode for safe testing
- Comprehensive error handling and logging

## Requirements

- Python 3.8+
- macOS (tested), Linux (should work), Windows (untested)

## Installation

1. Clone the repository:
```bash
git clone https://github.com/tommcfarlin/tm-monthly-backup.git
cd tm-monthly-backup
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Usage

1. Export photos from iCloud Photos to the `export/` directory
2. Run the backup utility:

```bash
# Dry run (recommended first)
python src/main.py --dry-run

# Process files
python src/main.py

# View help
python src/main.py --help
```

## Directory Structure

```
tm-monthly-backup/
├── export/          # Place exported iCloud files here
├── backup/          # Organized output files
│   ├── photos/      # JPG, PNG, GIF files
│   ├── videos/      # MOV, MP4, M4V files
│   └── screenshots/ # PNG screenshot files
├── src/             # Source code
├── tests/           # Test suite
└── docs/            # Documentation
```

## File Processing

- **HEIC files**: Converted to JPEG (lossless) with EXIF preservation
- **Apple sidecar files (.aae)**: Deleted automatically
- **Naming convention**: Files renamed to EXIF timestamp format
- **Duplicate handling**: Timestamp conflicts resolved by incrementing seconds
- **Missing EXIF**: Files moved to special handling directory

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

## Development

See [docs/development-workflow.md](docs/development-workflow.md) for contribution guidelines.

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for version history.
