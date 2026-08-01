"""
EXIF timestamp extraction and handling module for photos and videos
"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple
from pathlib import Path
from PIL import Image
from PIL.ExifTags import TAGS
import pillow_heif

# Video metadata extraction
try:
    from hachoir.parser import createParser
    from hachoir.metadata import extractMetadata
    HACHOIR_AVAILABLE = True
except ImportError:
    HACHOIR_AVAILABLE = False

# Enable HEIF support in Pillow
pillow_heif.register_heif_opener()

logger = logging.getLogger(__name__)


class ExifHandler:
    """Handles EXIF data extraction and timestamp processing for photos and videos"""

    # EXIF timestamp tags in order of preference
    TIMESTAMP_TAGS = [
        'DateTimeOriginal',    # Camera capture time (preferred)
        'DateTime',            # File modification time
        'DateTimeDigitized'    # Digitization time
    ]

    # Video file extensions that need special handling
    VIDEO_EXTENSIONS = {
        '.mov', '.mp4', '.m4v', '.avi', '.mkv', '.wmv',
        '.flv', '.webm', '.3gp', '.mpg', '.mpeg'
    }

    def __init__(self):
        self.missing_exif_files = []

    def _is_video_file(self, file_path: str) -> bool:
        """Check if file is a video file based on extension"""
        return Path(file_path).suffix.lower() in self.VIDEO_EXTENSIONS

    def extract_timestamp(self, file_path: str) -> Optional[datetime]:
        """
        Extract timestamp from EXIF data (photos) or metadata (videos).

        Args:
            file_path: Path to image or video file

        Returns:
            datetime object if found, None if missing/invalid
        """
        # Handle video files differently
        if self._is_video_file(file_path):
            return self._extract_video_timestamp(file_path)

        # Handle image files with EXIF data
        try:
            with Image.open(file_path) as image:
                exif_data = image.getexif()

                if not exif_data:
                    logger.warning(f"No EXIF data found in {file_path}")
                    self.missing_exif_files.append(file_path)
                    return None

                # Build a name -> value lookup so we can honor tag priority
                exif_by_name = {
                    TAGS.get(tag_id, tag_id): value
                    for tag_id, value in exif_data.items()
                }

                # Walk TIMESTAMP_TAGS in declared priority order and take the
                # first tag that is actually present in the EXIF data.
                for tag_name in self.TIMESTAMP_TAGS:
                    if tag_name in exif_by_name:
                        return self._parse_exif_datetime(exif_by_name[tag_name], file_path)

                logger.warning(f"No timestamp tags found in EXIF data for {file_path}")
                self.missing_exif_files.append(file_path)
                return None

        except Exception as e:
            logger.error(f"Error reading EXIF data from {file_path}: {e}")
            self.missing_exif_files.append(file_path)
            return None

    def _extract_video_timestamp(self, file_path: str) -> Optional[datetime]:
        """
        Extract creation timestamp from video metadata.

        Args:
            file_path: Path to video file

        Returns:
            datetime object if found, None if missing/invalid
        """
        if not HACHOIR_AVAILABLE:
            logger.warning(f"Hachoir not available for video metadata extraction: {file_path}")
            self.missing_exif_files.append(file_path)
            return None

        try:
            parser = createParser(file_path)
            if not parser:
                logger.warning(f"Could not create parser for video file: {file_path}")
                self.missing_exif_files.append(file_path)
                return None

            with parser:
                metadata = extractMetadata(parser)
                if not metadata:
                    logger.warning(f"No metadata found in video file: {file_path}")
                    self.missing_exif_files.append(file_path)
                    return None

                # Try to get creation date from various metadata fields
                creation_date = None

                # Common metadata fields for creation date
                for field_name in ['creation_date', 'date', 'creation_time', 'media_creation']:
                    try:
                        if hasattr(metadata, field_name):
                            creation_date = getattr(metadata, field_name)
                            break
                    except:
                        continue

                # If no direct field, try iterating through all metadata
                if not creation_date:
                    for line in metadata.exportPlaintext():
                        line_lower = line.lower()
                        if any(keyword in line_lower for keyword in ['creation', 'date', 'time']):
                            # Extract timestamp from metadata line
                            try:
                                import re
                                # Look for date patterns
                                date_match = re.search(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line)
                                if date_match:
                                    date_str = date_match.group(1).replace('T', ' ')
                                    creation_date = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
                                    break
                            except:
                                continue

                if creation_date:
                    logger.info(f"Extracted video creation date: {file_path} -> {creation_date}")
                    return creation_date
                else:
                    logger.warning(f"No creation date found in video metadata: {file_path}")
                    self.missing_exif_files.append(file_path)
                    return None

        except Exception as e:
            logger.error(f"Error extracting video metadata from {file_path}: {e}")
            self.missing_exif_files.append(file_path)
            return None

    def _parse_exif_datetime(self, datetime_str: str, file_path: str) -> Optional[datetime]:
        """
        Parse EXIF datetime string to datetime object.

        Args:
            datetime_str: EXIF datetime string (format: "YYYY:MM:DD HH:MM:SS")
            file_path: File path for logging

        Returns:
            datetime object if valid, None if invalid
        """
        try:
            # EXIF datetime format: "YYYY:MM:DD HH:MM:SS"
            return datetime.strptime(datetime_str, "%Y:%m:%d %H:%M:%S")
        except ValueError as e:
            logger.error(f"Invalid EXIF datetime format in {file_path}: {datetime_str} - {e}")
            self.missing_exif_files.append(file_path)
            return None

    def get_fallback_timestamp(self, file_path: str) -> datetime:
        """
        Get fallback timestamp from filename patterns or file system metadata.

        Args:
            file_path: Path to file

        Returns:
            datetime object from filename pattern or file creation/modification time
        """
        # Try to extract date from filename first
        filename_timestamp = self._extract_timestamp_from_filename(file_path)
        if filename_timestamp:
            logger.info(f"Extracted timestamp from filename: {file_path} -> {filename_timestamp}")
            return filename_timestamp

        try:
            # Use file creation time if available (macOS/Windows), otherwise modification time
            stat_info = os.stat(file_path)

            # Try creation time first (macOS: st_birthtime, Windows: st_ctime)
            if hasattr(stat_info, 'st_birthtime') and stat_info.st_birthtime != stat_info.st_ctime:
                timestamp = stat_info.st_birthtime
            else:
                # Fall back to modification time
                timestamp = stat_info.st_mtime

            return datetime.fromtimestamp(timestamp)
        except OSError as e:
            logger.error(f"Error getting file timestamp for {file_path}: {e}")
            # Ultimate fallback: current time
            return datetime.now()

    def _extract_timestamp_from_filename(self, file_path: str) -> Optional[datetime]:
        """
        Try to extract timestamp from filename patterns.

        Args:
            file_path: Path to file

        Returns:
            datetime object if pattern found, None otherwise
        """
        filename = Path(file_path).name

        import re

        # Pattern 1: IMG_YYYY-MM-DD-HH-MM-SS or similar
        pattern1 = r'(\d{4})[_-](\d{2})[_-](\d{2})[_-](\d{2})[_-](\d{2})[_-](\d{2})'
        match = re.search(pattern1, filename)
        if match:
            try:
                year, month, day, hour, minute, second = map(int, match.groups())
                return datetime(year, month, day, hour, minute, second)
            except ValueError:
                pass

        # Pattern 2: YYYYMMDD_HHMMSS
        pattern2 = r'(\d{8})[_-](\d{6})'
        match = re.search(pattern2, filename)
        if match:
            try:
                date_str, time_str = match.groups()
                year = int(date_str[:4])
                month = int(date_str[4:6])
                day = int(date_str[6:8])
                hour = int(time_str[:2])
                minute = int(time_str[2:4])
                second = int(time_str[4:6])
                return datetime(year, month, day, hour, minute, second)
            except ValueError:
                pass

        # Pattern 3: IMG_XXXX with iOS patterns (these don't contain dates)
        # Pattern 4: Attachment-1, FullSizeRender, etc. (no dates)

        return None

    def format_timestamp_filename(self, dt: datetime) -> str:
        """
        Format datetime object to filename format: YYYY.MM.DD.HH.MM.SS

        Args:
            dt: datetime object

        Returns:
            Formatted timestamp string
        """
        return dt.strftime("%Y.%m.%d.%H.%M.%S")

    def handle_duplicate_timestamp(self, timestamp: datetime, existing_files: set) -> datetime:
        """
        Handle duplicate timestamps by incrementing seconds.

        Args:
            timestamp: Original timestamp
            existing_files: Set of existing formatted timestamp strings

        Returns:
            Adjusted timestamp that doesn't conflict
        """
        base_format = self.format_timestamp_filename(timestamp)

        if base_format not in existing_files:
            return timestamp

        # Increment seconds until we find a unique timestamp
        adjusted = timestamp
        attempts = 0
        max_attempts = 3600  # Max 1 hour of adjustments

        while attempts < max_attempts:
            # timedelta handles second/minute/hour/day/month/year rollover
            adjusted = adjusted + timedelta(seconds=1)

            formatted = self.format_timestamp_filename(adjusted)
            if formatted not in existing_files:
                logger.info(f"Resolved timestamp conflict: {base_format} -> {formatted}")
                return adjusted

            attempts += 1

        logger.error(f"Could not resolve timestamp conflict after {max_attempts} attempts")
        return adjusted

    def get_missing_exif_files(self) -> list:
        """Return list of files that had missing/invalid EXIF data"""
        return self.missing_exif_files.copy()

    def clear_missing_files_log(self):
        """Clear the missing EXIF files log"""
        self.missing_exif_files.clear()