# Test Suite Documentation

This directory contains comprehensive tests for the tm-monthly-backup application.

## Test Structure

### Unit Tests
- **`test_exif_handler.py`** - Tests for EXIF timestamp extraction functionality
- **`test_file_categorizer.py`** - Tests for file categorization logic

### Integration Tests
- **`test_integration.py`** - End-to-end workflow tests and component integration

### Test Runner
- **`run_tests.py`** - Custom test runner with filtering options

## Running Tests

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

### Control Verbosity
```bash
# Quiet output
python tests/run_tests.py -q

# Verbose output
python tests/run_tests.py -v

# Maximum verbosity
python tests/run_tests.py -vv
```

### Run Individual Test Files
```bash
# Single test file
python -m unittest tests.test_exif_handler

# Specific test class
python -m unittest tests.test_exif_handler.TestExifHandler

# Specific test method
python -m unittest tests.test_exif_handler.TestExifHandler.test_extract_timestamp_success
```

## Test Categories

### EXIF Handler Tests
- Timestamp extraction from various image formats
- Fallback mechanisms for missing EXIF data
- Duplicate timestamp resolution
- Error handling for corrupted files

### File Categorizer Tests
- Extension-based categorization
- Screenshot pattern detection
- Case-insensitive file matching
- Edge cases and error scenarios

### Integration Tests
- Complete workflow simulation
- CLI interface validation
- Real-world usage scenarios
- Performance testing with multiple files
- Error recovery and resilience testing

## Test Data

Tests build real image files on disk in temporary directories via the shared,
self-verifying fixtures in `tests/fixtures.py`. `make_exif_jpeg` writes genuine
EXIF laid out the way a camera does it -- `DateTimeOriginal` /
`DateTimeDigitized` in the Exif sub-IFD (0x8769), `DateTime` in IFD0 -- and
reopens each file to confirm the round-trip before returning, so a caller cannot
silently construct a broken fixture. The EXIF read path is therefore exercised
end-to-end against real bytes. Mocking is reserved for conditions that are
awkward to reproduce deterministically on disk (e.g. a low-level I/O error on
open). The suite still runs in any environment without external dependencies
beyond Pillow.

## Coverage Areas

✅ **EXIF Processing** - Timestamp extraction, parsing, formatting
✅ **File Categorization** - Extension matching, pattern detection
✅ **Workflow Integration** - End-to-end processing scenarios
✅ **Error Handling** - Graceful failure and recovery
✅ **CLI Interface** - User interaction and progress display
✅ **Performance** - Processing speed with multiple files

## Dependencies

The test suite requires:
- Python standard library `unittest` module
- Application source modules in `src/` directory
- Temporary file system access for test isolation

Optional:
- `PIL` (Pillow) for enhanced image testing (falls back gracefully if not available)

## Test Isolation

Each test:
- Uses temporary directories that are cleaned up automatically
- Mocks external dependencies and file operations
- Runs independently without affecting other tests
- Can be executed in any order

## Continuous Integration

The test suite is designed to run in CI environments:
- No external file dependencies
- Predictable execution time
- Clear pass/fail status reporting
- Detailed error messages for debugging