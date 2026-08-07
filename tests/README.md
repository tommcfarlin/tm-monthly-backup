# Test Suite Documentation

This directory contains comprehensive tests for the tm-monthly-backup application.

## Test Structure

### Unit Tests
- **`test_exif_handler.py`** - Tests for EXIF timestamp extraction functionality
- **`test_file_categorizer.py`** - Tests for file categorization logic
- **`test_interruption.py`** - Interruption and partial-failure resilience: a
  `KeyboardInterrupt` mid-batch propagates out of `_process_category` (its
  `except Exception` must not swallow a `BaseException`) with files-before
  landed and files-after untouched; a full disk (`ENOSPC`) keeps trying every
  remaining file instead of aborting; and a file deleted between scan and
  processing is absorbed without crashing

### Integration Tests
- **`test_integration.py`** - End-to-end workflow tests and component integration
- **`test_landing_paths.py`** - Real-pipeline landing-path tests that run
  `process_all_files(dry_run=False)` against real fixtures and assert files
  actually arrive at `backup/<category>/<timestamp>.<ext>` with the right
  contents (no mocking of `shutil.move`, `os.remove`, or the HEIC converter)

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

`--unit-only` selects every `test_*.py` module except `test_integration.py`, so a
new unit test module runs automatically with no edit to `run_tests.py`.
`--unit-only` and `--integration-only` are mutually exclusive; passing both is
rejected by argparse rather than silently ignored. Every mode -- default,
`--unit-only`, and `--integration-only` -- prints the same TEST SUMMARY block
and exits `0` on success or `1` on failure.

### Control Verbosity
`unittest.TextTestRunner` only distinguishes three verbosity levels (0, 1, 2),
so `-v` and `-vv` are equivalent -- both select the same maximum level:

```bash
# Quiet: only the final TEST SUMMARY block, no per-test lines or dots
python tests/run_tests.py -q

# Default: one dot per passing test (unittest's normal verbosity)
python tests/run_tests.py

# Verbose: full test name, docstring, and outcome per test (-v and -vv are identical)
python tests/run_tests.py -v
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
- Complete workflow simulation with real (non-dry) runs that assert files land
  at their renamed `backup/<category>/<timestamp>.<ext>` destinations
- Duplicate-timestamp collision resolution end-to-end, including across
  categories (the global `used_timestamps` set)
- CLI interface validation
- Real-world usage scenarios
- Correctness at scale (many files land as many distinct renamed files)
- Error recovery and resilience: a real `shutil.move` failure is captured while
  the other files still land (no mocking of `_process_single_file`)
- Processing-state cleanup: every state container is populated by a real run and
  proven empty after `clear_processing_state()`

### Landing-Path Tests
- Photos land at their exact EXIF-derived path (`backup/photos/<timestamp>.jpg`)
- HEIC fixtures are converted end-to-end to JPEG and the original `.heic` is
  removed -- without mocking the converter -- with pixel dimensions/contents
  verified so a name-only rename cannot pass
- Each processable category (photo, screenshot, video, generated) lands in its
  own subdirectory, one file each, with per-file identity checks against swaps
- `.aae` sidecars are deleted, unknown files are left untouched in export, and
  the backup tree contains exactly the expected renamed files

## Test Data

Tests build real image files on disk in temporary directories via the shared,
self-verifying fixtures in `tests/fixtures.py`. `make_exif_jpeg` writes genuine
EXIF laid out the way a camera does it -- `DateTimeOriginal` /
`DateTimeDigitized` in the Exif sub-IFD (0x8769), `DateTime` in IFD0 -- and
reopens each file to confirm the round-trip before returning, so a caller cannot
silently construct a broken fixture. `make_exif_heic` is the HEIC analogue: it
encodes a genuine HEIF image via pillow-heif and reopens it to confirm both the
HEIF decode and that `DateTimeOriginal` landed in the Exif sub-IFD. The EXIF read
path is therefore exercised end-to-end against real bytes. Mocking is reserved for conditions that are
awkward to reproduce deterministically on disk (e.g. a low-level I/O error on
open). Building these fixtures requires both Pillow and pillow-heif; see
Dependencies below.

## Coverage Areas

✅ **EXIF Processing** - Timestamp extraction, parsing, formatting
✅ **File Categorization** - Extension matching, pattern detection
✅ **Workflow Integration** - End-to-end processing scenarios
✅ **Landing Paths** - Files verified to arrive at their renamed
   `backup/<category>/<timestamp>.<ext>` destinations with correct contents
✅ **HEIC Conversion** - Real HEIC-to-JPEG conversion end-to-end, original removed
✅ **Error Handling** - Graceful failure and recovery (real move failure, other files still land)
✅ **CLI Interface** - User interaction and progress display
✅ **Scale** - Correctness across many files (collision resolution, no overwrites)

## Dependencies

The test suite requires:
- Python standard library `unittest` module
- Application source modules in `src/` directory
- Temporary file system access for test isolation
- `Pillow` and `pillow-heif` -- `tests/fixtures.py` imports both unconditionally
  to build real EXIF/HEIF fixtures on disk; there is no fallback path if
  either is missing, so both must be installed (see `pyproject.toml`)

## Test Isolation

Each test:
- Uses temporary directories that are cleaned up automatically
- Performs real file operations against those temp trees; mocking is reserved
  for conditions awkward to reproduce deterministically (e.g. a forced
  `shutil.move` failure or a low-level open error)
- Runs independently without affecting other tests
- Can be executed in any order

## Continuous Integration

There is no CI workflow configured for this repository yet (tracked as #17),
so the claims below describe the suite's local behavior only, not an
established CI track record:
- No external file dependencies -- the suite needs Python's standard library
  plus Pillow and pillow-heif (see Dependencies above); nothing reaches the
  network or requires a fixture set outside the repo
- Predictable execution time -- the full suite runs in a few seconds on this
  machine (see the timing in `unittest`'s own summary line)
- Clear pass/fail status reporting -- `run_tests.py`'s TEST SUMMARY block and
  exit code (`0`/`1`) make the outcome unambiguous in any of its modes
- Detailed error messages for debugging -- failures and errors are listed by
  test ID in the summary, with full tracebacks available at `-v`