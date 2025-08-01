# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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