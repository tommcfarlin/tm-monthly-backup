# Troubleshooting Guide

Solutions for common issues when using tm-monthly-backup.

> **Invocation:** examples below use the installed `tm-monthly-backup` console
> command (available after `pip install -e .`). If you have not installed the
> package, substitute `python -m src.main` — run from the repository root — for
> every `tm-monthly-backup` below. Running `python src/main.py` directly does not
> work; the package uses absolute `src.…` imports.

## Quick Diagnostics

Run these commands to quickly identify common issues:

```bash
# Check if Python and dependencies are available
python --version
python -c "import click, PIL, rich; print('Dependencies OK')"

# Test basic functionality
tm-monthly-backup --version
tm-monthly-backup --help

# Run diagnostic test
python tests/run_tests.py --unit-only -q
```

## Common Issues

### Installation and Setup

#### Issue: "ModuleNotFoundError: No module named 'click'"

**Symptoms:**
```
ModuleNotFoundError: No module named 'click'
```

**Cause:** Missing Python dependencies.

**Solution:**
```bash
# Install dependencies
pip install -r requirements.txt

# Verify installation
python -c "import click, PIL, rich; print('All modules installed')"
```

**Alternative Solutions:**
```bash
# Use virtual environment (recommended)
python -m venv tm-backup-env
source tm-backup-env/bin/activate  # On Windows: tm-backup-env\Scripts\activate
pip install -r requirements.txt

# Install specific missing modules
pip install click>=8.0.0 pillow>=10.0.0 rich>=13.0.0
```

---

#### Issue: "ImportError: cannot import name 'pillow_heif'"

**Symptoms:**
```
ImportError: cannot import name 'pillow_heif' from 'PIL'
```

**Cause:** HEIF support not properly installed.

**Solution:**
```bash
# Install pillow-heif
pip install pillow-heif>=0.10.0

# On macOS, you might need:
brew install libheif
pip install pillow-heif

# On Ubuntu/Debian:
sudo apt-get install libheif-dev
pip install pillow-heif
```

---

### File Processing Issues

#### Issue: "Export directory does not exist"

**Symptoms:**
```
Error: Export directory does not exist: export
```

**Cause:** The export directory hasn't been created or is in wrong location.

**Solution:**
```bash
# Create export directory
mkdir -p export

# Or specify custom directory
tm-monthly-backup --export-dir /path/to/your/icloud/export

# Check current directory
pwd
ls -la
```

---

#### Issue: "Permission denied" errors

**Symptoms:**
```
Failed to delete sidecar file: Permission denied
Failed to move file: Permission denied
```

**Cause:** Insufficient file system permissions.

**Solution:**
```bash
# Check file permissions
ls -la export/
ls -la backup/

# Fix permissions if needed
chmod -R 755 export/
chmod -R 755 backup/

# Run with sudo if necessary (not recommended)
sudo tm-monthly-backup

# Better: Change ownership
sudo chown -R $USER:$USER export/ backup/
```

---

#### Issue: "No files found to process"

**Symptoms:**
```
Warning: No files found in export
```

**Cause:** Export directory is empty or files are in subdirectories.

**Solution:**
```bash
# Check directory contents
ls -la export/
find export/ -type f

# Verify you're in the right location
pwd

# Check for hidden files
ls -la export/.*

# Use correct export directory
tm-monthly-backup --export-dir /correct/path/to/icloud/export
```

---

### HEIC Conversion Issues

#### Issue: "HEIC conversion failed"

**Symptoms:**
```
ERROR: Failed to convert HEIC file export/IMG_1234.heic: Cannot open image
```

**Cause:** HEIF libraries not properly installed or corrupted HEIC file.

**Solution:**
```bash
# Test HEIF support
python -c "import pillow_heif; print('HEIF support available')"

# Reinstall HEIF support
pip uninstall pillow-heif
pip install pillow-heif>=0.10.0

# Test with a single file
python -c "
from PIL import Image
import pillow_heif
pillow_heif.register_heif_opener()
img = Image.open('export/IMG_1234.heic')
print(f'Image size: {img.size}')
"
```

**If file is corrupted:** an unconvertible HEIC is left in place in `export/`
and recorded as a failure (the run does not delete an original it could not
convert). Undecodable non-HEIC image files (a truncated `.jpg`, a mislabeled
`.png`) are quarantined automatically to `backup/corrupt/` under their original
name — no manual move needed. To set a problem HEIC aside and continue:
```bash
# Move the problem HEIC out of the export set
mkdir -p corrupted_files
mv export/problem_file.heic corrupted_files/

# Continue processing other files
tm-monthly-backup
```

---

#### Issue: "EXIF data lost during HEIC conversion"

**Symptoms:** Converted JPEG files missing timestamp information.

**Cause:** EXIF preservation not working properly.

**Solution:**
```bash
# Verify EXIF preservation works
python -c "
from PIL import Image
img = Image.open('backup/photos/2024.01.15.14.30.45.jpg')
exif = img.getexif()
print(f'EXIF data present: {len(exif) > 0}')
"

# Check conversion quality setting
grep -n "jpeg_quality" src/heic_converter.py
```

**If EXIF is consistently lost:**
- Update Pillow to latest version: `pip install --upgrade pillow`
- Check original HEIC files have EXIF data
- File an issue with specific file examples

---

### File Organization Issues

#### Issue: Screenshots incorrectly categorized as photos

**Symptoms:** PNG screenshot files appear in `photos/` instead of `screenshots/`.

**Cause:** Screenshot detection patterns not matching filename.

**Solution:**
```bash
# Check filename patterns
python -c "
from src.file_categorizer import FileCategorizer
cat = FileCategorizer()
print(cat._is_likely_screenshot('your_filename.png'))
"

# Manually fix categorization
mv backup/photos/screenshot_file.png backup/screenshots/

# Update screenshot patterns if needed
# Edit the pattern list in FileCategorizer._is_likely_screenshot (src/file_categorizer.py)
```

---

#### Issue: "Duplicate timestamp conflicts"

**Symptoms:**
```
INFO: Resolved timestamp conflict: 2024.01.15.14.30.45 -> 2024.01.15.14.30.46
```

**Cause:** Multiple files have identical EXIF timestamps (normal behavior).

**Solution:** This is expected behavior. The system automatically resolves conflicts by incrementing seconds.

**To verify resolution:**
```bash
# Check for duplicate filenames
ls backup/photos/ | sort | uniq -d

# Should return empty (no duplicates)
```

---

### Performance Issues

#### Issue: Processing is very slow

**Symptoms:** Takes several minutes to process small number of files.

**Cause:** Usually HEIC conversion on older hardware or large files.

**Solution:**
```bash
# Profile processing time
time tm-monthly-backup --dry-run

# Process in smaller batches
mkdir temp_export
mv export/*.heic temp_export/
tm-monthly-backup  # Process non-HEIC files first
mv temp_export/* export/
tm-monthly-backup  # Process HEIC files separately

# Check available system resources
df -h  # Disk space
free -h  # Memory (Linux)
top  # CPU usage
```

---

#### Issue: "Disk space insufficient"

**Symptoms:**
```
OSError: [Errno 28] No space left on device
```

**Cause:** Not enough disk space for backup operations.

**Solution:**
```bash
# Check disk space
df -h

# Clean up space
rm -rf backup/unknown/*  # Remove unknown files if not needed
rm -rf corrupted_files/   # Remove corrupted files

# Use different backup location
tm-monthly-backup --backup-dir /path/to/larger/drive/backup

# Process in smaller batches
mkdir temp_backup
tm-monthly-backup --backup-dir temp_backup
# Move temp_backup contents to final location
```

---

### Advanced Troubleshooting

#### Debug Mode

Enable detailed logging for complex issues:

```bash
# Maximum verbosity
tm-monthly-backup --verbose

# Capture logs to file
tm-monthly-backup --verbose 2>&1 | tee processing.log

# Review log file
grep -i error processing.log
grep -i warning processing.log
```

#### Test Individual Components

```bash
# Test EXIF extraction
python -c "
from src.exif_handler import ExifHandler
handler = ExifHandler()
timestamp = handler.extract_timestamp('export/test.jpg')
print(f'Extracted timestamp: {timestamp}')
"

# Test file categorization
python -c "
from src.file_categorizer import FileCategorizer
cat = FileCategorizer()
category = cat.categorize_file('export/test.jpg')
print(f'File category: {category}')
"

# Test HEIC conversion
python -c "
from src.heic_converter import HeicConverter
converter = HeicConverter()
result = converter.convert_heic_to_jpeg('export/test.heic')
print(f'Conversion result: {result}')
"
```

#### Run Specific Tests

```bash
# Test suite for specific components
python tests/run_tests.py --unit-only

# Test specific functionality
python -m unittest tests.test_exif_handler.TestExifHandler.test_extract_timestamp_success

# Integration tests
python tests/run_tests.py --integration-only
```

## Error Codes Reference

| Exit Code | Meaning | Solution |
|-----------|---------|----------|
| `0` | Success — every discovered file processed (also a dry run or empty export) | No action needed |
| `1` | Partial failure — one or more files failed | Review the failure table, check file permissions |
| `2` | Precondition failure — missing/unwritable directory, export/backup overlap, no terminal available for confirmation without `--yes`/`--dry-run`, or unexpected error; nothing was processed | Check the directory paths, permissions, and disk space; add `--yes` for non-interactive/cron use |
| `130` | Cancelled — declined the confirmation prompt or interrupted with Ctrl-C (`SIGINT`) | Re-run when ready |

See the [CLI Usage Guide](cli-usage.md#exit-codes) for the authoritative
exit-code reference.

## Environment-Specific Issues

### macOS

**Issue: "Operation not permitted" on system directories**
```bash
# Avoid processing system directories
tm-monthly-backup --export-dir ~/Downloads/icloud-export

# Grant full disk access in System Preferences if needed
# System Preferences > Security & Privacy > Privacy > Full Disk Access
```

### Windows

**Issue: Path length limitations**
```bash
# Use shorter paths
tm-monthly-backup --export-dir C:\Export --backup-dir C:\Backup

# Enable long path support (Windows 10+)
# Group Policy: Computer Configuration > Administrative Templates > System > Filesystem
```

### Linux

**Issue: Package manager conflicts**
```bash
# Use virtual environment to avoid conflicts
python3 -m venv tm-backup-env
source tm-backup-env/bin/activate
pip install -r requirements.txt
```

## Getting Help

### Collecting Debug Information

When reporting issues, include:

```bash
# System information
python --version
pip list | grep -E "(click|pillow|rich)"
uname -a  # Linux/macOS
systeminfo | findstr /B /C:"OS Name" /C:"OS Version"  # Windows

# Application information
tm-monthly-backup --version
tm-monthly-backup --help

# Test results
python tests/run_tests.py -q
```

### Log Files

Create detailed logs for issue reports:

```bash
# Run with full logging
tm-monthly-backup --verbose --dry-run 2>&1 | tee debug.log

# Sanitize log file (remove personal paths)
sed 's|/Users/[^/]*/|/Users/USER/|g' debug.log > sanitized_debug.log
```

**Redact before pasting.** Every `Moved: <src> -> <dst>` line and the missing-EXIF
warning render the full absolute path of each file, which on macOS/Linux
includes your account username (`/Users/<username>/...` or `/home/<username>/...`).
This is local terminal output on your own machine, not something the tool
sends anywhere — but if you paste a run log into a public issue or forum post,
run it through the `sed` command above (or otherwise redact the path prefix)
first.

### Common Log Patterns

Look for these patterns in logs:

| Pattern | Meaning | Action |
|---------|---------|--------|
| `FileNotFoundError` | Missing file or directory | Check paths, create directories |
| `PermissionError` | Access denied | Check file permissions |
| `OSError: [Errno 28]` | Disk full | Free up space |
| `ImportError` | Missing dependency | Install required packages |
| `Not a decodable image` | Undecodable image-typed file | Auto-quarantined to `backup/corrupt/`; review or replace the source file |

## Prevention

### Best Practices

1. **Always run dry-run first**
   ```bash
   tm-monthly-backup --dry-run
   ```

2. **Keep backups of original exports**
   ```bash
   cp -r export/ export_backup_$(date +%Y%m%d)/
   ```

3. **Test with small batches**
   ```bash
   # Process a few files first
   mkdir test_export
   cp export/*.jpg test_export/  # Copy just JPG files
   tm-monthly-backup --export-dir test_export --backup-dir test_backup
   ```

4. **Monitor disk space**
   ```bash
   df -h
   # Ensure backup drive has 2x the size of export directory
   ```

5. **Regular testing**
   ```bash
   # Run tests periodically
   python tests/run_tests.py
   ```

### Maintenance

```bash
# Clean up old backups periodically
find backup/ -name "*.jpg" -mtime +365 -delete

# Update dependencies
pip install --upgrade -r requirements.txt

# Check for application updates
git pull origin main
```