"""
File categorization module for organizing files by type
"""

import os
import logging
from pathlib import Path
from typing import Dict, List, Set, Tuple
from enum import Enum

logger = logging.getLogger(__name__)


class FileCategory(Enum):
    """File category enumeration"""
    PHOTO = "photos"
    VIDEO = "videos"
    SCREENSHOT = "screenshots"
    GENERATED = "generated"  # AI-generated or heavily edited content
    UNKNOWN = "unknown"
    SIDECAR = "sidecar"


class FileCategorizer:
    """Categorizes files by type based on extension and content analysis"""

    # File extension mappings
    PHOTO_EXTENSIONS = {
        '.jpg', '.jpeg', '.png', '.gif', '.heic', '.heif',
        '.tiff', '.tif', '.bmp', '.webp', '.dng', '.raw',
        '.cr2', '.nef', '.arw', '.orf', '.rw2'
    }

    VIDEO_EXTENSIONS = {
        '.mov', '.mp4', '.m4v', '.avi', '.mkv', '.wmv',
        '.flv', '.webm', '.3gp', '.mpg', '.mpeg'
    }

    SCREENSHOT_EXTENSIONS = {
        '.png'  # Screenshots are typically PNG on iOS/macOS
    }

    SIDECAR_EXTENSIONS = {
        '.aae'  # Apple's sidecar files
    }

    def __init__(self):
        self.categorized_files = {
            FileCategory.PHOTO: [],
            FileCategory.VIDEO: [],
            FileCategory.SCREENSHOT: [],
            FileCategory.GENERATED: [],
            FileCategory.UNKNOWN: [],
            FileCategory.SIDECAR: []
        }

        # Convert to lowercase for case-insensitive matching
        self.photo_exts = {ext.lower() for ext in self.PHOTO_EXTENSIONS}
        self.video_exts = {ext.lower() for ext in self.VIDEO_EXTENSIONS}
        self.screenshot_exts = {ext.lower() for ext in self.SCREENSHOT_EXTENSIONS}
        self.sidecar_exts = {ext.lower() for ext in self.SIDECAR_EXTENSIONS}

    def categorize_file(self, file_path: str) -> FileCategory:
        """
        Categorize a single file based on extension and filename patterns.

        Args:
            file_path: Path to file

        Returns:
            FileCategory enum value
        """
        file_path_obj = Path(file_path)
        ext = file_path_obj.suffix.lower()
        filename = file_path_obj.name.lower()

        # Check for sidecar files first
        if ext in self.sidecar_exts:
            return FileCategory.SIDECAR

        # Check for screenshots (PNG files with screenshot patterns)
        if ext in self.screenshot_exts:
            # Additional heuristics for screenshot detection
            if self._is_likely_screenshot(filename):
                return FileCategory.SCREENSHOT
            # Check if it's AI-generated or heavily edited content
            if self._is_generated_content(file_path):
                return FileCategory.GENERATED
            # If PNG but not clearly a screenshot, treat as photo
            return FileCategory.PHOTO

        # Check for photos
        if ext in self.photo_exts:
            # Check if it's AI-generated or heavily edited content
            if self._is_generated_content(file_path):
                return FileCategory.GENERATED
            return FileCategory.PHOTO

        # Check for videos
        if ext in self.video_exts:
            return FileCategory.VIDEO

        # Unknown file type
        logger.warning(f"Unknown file type: {file_path}")
        return FileCategory.UNKNOWN

    def _is_likely_screenshot(self, filename: str) -> bool:
        """
        Determine if a file is likely a screenshot based on filename patterns.

        Args:
            filename: Lowercase filename

        Returns:
            True if filename suggests it's a screenshot
        """
        screenshot_patterns = [
            'screenshot',
            'screen shot',
            'img_',  # iOS screenshot pattern
            'simulator screen shot',  # iOS Simulator
            'screen recording',
            'img_3',  # iOS screenshot pattern (IMG_3XXX)
            'screen_',
            'capture',
        ]

        # iOS screenshots often have specific patterns
        # IMG_XXXX.PNG where XXXX is 4+ digits starting with 3
        if filename.startswith('img_3') and filename.endswith('.png'):
            return True

        return any(pattern in filename for pattern in screenshot_patterns)

    def _is_generated_content(self, file_path: str) -> bool:
        """
        Detect AI-generated or heavily edited content using pure Python.

        Args:
            file_path: Path to file

        Returns:
            True if file appears to be AI-generated or heavily edited
        """
        try:
            from PIL import Image

            with Image.open(file_path) as img:
                # Check for C2PA/AI metadata in PNG files
                if file_path.lower().endswith('.png'):
                    # Look for C2PA markers in PNG text chunks
                    if hasattr(img, 'text') and img.text:
                        for key, value in img.text.items():
                            if any(ai_marker in str(value).lower() for ai_marker in
                                   ['gpt', 'chatgpt', 'openai', 'c2pa', 'ai', 'generated']):
                                logger.info(f"Detected AI-generated content: {file_path}")
                                return True

                # Check EXIF data for editing software + missing original timestamps
                exif = img.getexif()
                if exif:
                    from PIL.ExifTags import TAGS

                    has_editing_software = False
                    has_original_timestamp = False

                    for tag_id, value in exif.items():
                        tag = TAGS.get(tag_id, str(tag_id))

                        # Check for editing software
                        if tag == 'Software' and any(editor in str(value).lower() for editor in
                                                    ['snapseed', 'photoshop', 'lightroom', 'gimp', 'canva']):
                            has_editing_software = True

                        # Check for original timestamp tags
                        if tag in ['DateTimeOriginal', 'DateTimeDigitized']:
                            has_original_timestamp = True

                    # If edited but no original timestamp, likely heavily processed
                    if has_editing_software and not has_original_timestamp:
                        logger.info(f"Detected heavily edited content: {file_path}")
                        return True

                # Check for UUID-style filenames (often generated content)
                filename = Path(file_path).stem
                if len(filename) == 36 and filename.count('-') == 4:  # UUID format
                    logger.info(f"Detected UUID filename (likely generated): {file_path}")
                    return True

        except Exception as e:
            logger.debug(f"Error checking generated content for {file_path}: {e}")

        return False

    def batch_categorize(self, file_paths: List[str]) -> Dict[FileCategory, List[str]]:
        """
        Categorize multiple files.

        Args:
            file_paths: List of file paths to categorize

        Returns:
            Dictionary mapping categories to lists of file paths
        """
        # Clear previous categorization
        for category in self.categorized_files:
            self.categorized_files[category].clear()

        for file_path in file_paths:
            if not os.path.exists(file_path):
                logger.warning(f"File not found: {file_path}")
                continue

            category = self.categorize_file(file_path)
            self.categorized_files[category].append(file_path)

        return self.categorized_files.copy()

    def get_files_by_category(self, category: FileCategory) -> List[str]:
        """
        Get files for a specific category.

        Args:
            category: FileCategory to retrieve

        Returns:
            List of file paths for the category
        """
        return self.categorized_files[category].copy()

    def get_sidecar_files(self) -> List[str]:
        """
        Get list of sidecar files that should be deleted.

        Returns:
            List of sidecar file paths
        """
        return self.get_files_by_category(FileCategory.SIDECAR)

    def get_processable_files(self) -> Dict[FileCategory, List[str]]:
        """
        Get files that should be processed (excludes sidecar and unknown).

        Returns:
            Dictionary of processable files by category
        """
        return {
            FileCategory.PHOTO: self.get_files_by_category(FileCategory.PHOTO),
            FileCategory.VIDEO: self.get_files_by_category(FileCategory.VIDEO),
            FileCategory.SCREENSHOT: self.get_files_by_category(FileCategory.SCREENSHOT),
            FileCategory.GENERATED: self.get_files_by_category(FileCategory.GENERATED)
        }

    def get_target_directory(self, category: FileCategory, base_backup_dir: str) -> str:
        """
        Get target directory path for a file category.

        Args:
            category: FileCategory
            base_backup_dir: Base backup directory path

        Returns:
            Full path to target directory
        """
        if category == FileCategory.PHOTO:
            return os.path.join(base_backup_dir, "photos")
        elif category == FileCategory.VIDEO:
            return os.path.join(base_backup_dir, "videos")
        elif category == FileCategory.SCREENSHOT:
            return os.path.join(base_backup_dir, "screenshots")
        elif category == FileCategory.GENERATED:
            return os.path.join(base_backup_dir, "generated")
        elif category == FileCategory.UNKNOWN:
            return os.path.join(base_backup_dir, "unknown")
        else:
            raise ValueError(f"No target directory defined for category: {category}")

    def ensure_target_directories(self, base_backup_dir: str) -> List[str]:
        """
        Create target directories if they don't exist.

        Args:
            base_backup_dir: Base backup directory path

        Returns:
            List of created directory paths
        """
        directories = []
        for category in [FileCategory.PHOTO, FileCategory.VIDEO, FileCategory.SCREENSHOT, FileCategory.GENERATED, FileCategory.UNKNOWN]:
            target_dir = self.get_target_directory(category, base_backup_dir)
            os.makedirs(target_dir, exist_ok=True)
            directories.append(target_dir)

        return directories

    def get_categorization_stats(self) -> Dict[str, int]:
        """
        Get statistics about file categorization.

        Returns:
            Dictionary with categorization counts
        """
        return {
            'photos': len(self.categorized_files[FileCategory.PHOTO]),
            'videos': len(self.categorized_files[FileCategory.VIDEO]),
            'screenshots': len(self.categorized_files[FileCategory.SCREENSHOT]),
            'generated': len(self.categorized_files[FileCategory.GENERATED]),
            'unknown': len(self.categorized_files[FileCategory.UNKNOWN]),
            'sidecar': len(self.categorized_files[FileCategory.SIDECAR]),
            'total': sum(len(files) for files in self.categorized_files.values())
        }

    def get_file_summary(self) -> str:
        """
        Get human-readable summary of categorized files.

        Returns:
            Formatted string summary
        """
        stats = self.get_categorization_stats()

        summary_lines = [
            f"File Categorization Summary:",
            f"  Photos: {stats['photos']} files",
            f"  Videos: {stats['videos']} files",
            f"  Screenshots: {stats['screenshots']} files",
            f"  Unknown: {stats['unknown']} files",
            f"  Sidecar (to delete): {stats['sidecar']} files",
            f"  Total: {stats['total']} files"
        ]

        return "\n".join(summary_lines)

    def clear_categorization(self):
        """Clear all categorized files"""
        for category in self.categorized_files:
            self.categorized_files[category].clear()