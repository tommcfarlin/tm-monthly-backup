"""
EXIF timestamp extraction and handling module
"""

import os
import logging
from datetime import datetime
from typing import Optional, Tuple
from PIL import Image
from PIL.ExifTags import TAGS
import pillow_heif

# Enable HEIF support in Pillow
pillow_heif.register_heif_opener()

logger = logging.getLogger(__name__)


class ExifHandler:
    """Handles EXIF data extraction and timestamp processing"""

    # EXIF timestamp tags in order of preference
    TIMESTAMP_TAGS = [
        'DateTimeOriginal',    # Camera capture time (preferred)
        'DateTime',            # File modification time
        'DateTimeDigitized'    # Digitization time
    ]

    def __init__(self):
        self.missing_exif_files = []

    def extract_timestamp(self, file_path: str) -> Optional[datetime]:
        """
        Extract timestamp from EXIF data.

        Args:
            file_path: Path to image file

        Returns:
            datetime object if found, None if missing/invalid
        """
        try:
            with Image.open(file_path) as image:
                exif_data = image.getexif()

                if not exif_data:
                    logger.warning(f"No EXIF data found in {file_path}")
                    self.missing_exif_files.append(file_path)
                    return None

                # Try each timestamp tag in order of preference
                for tag_id, value in exif_data.items():
                    tag_name = TAGS.get(tag_id, tag_id)

                    if tag_name in self.TIMESTAMP_TAGS:
                        return self._parse_exif_datetime(value, file_path)

                logger.warning(f"No timestamp tags found in EXIF data for {file_path}")
                self.missing_exif_files.append(file_path)
                return None

        except Exception as e:
            logger.error(f"Error reading EXIF data from {file_path}: {e}")
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
        Get fallback timestamp from file system metadata.

        Args:
            file_path: Path to file

        Returns:
            datetime object from file creation/modification time
        """
        try:
            # Use file modification time as fallback
            timestamp = os.path.getmtime(file_path)
            return datetime.fromtimestamp(timestamp)
        except OSError as e:
            logger.error(f"Error getting file timestamp for {file_path}: {e}")
            # Ultimate fallback: current time
            return datetime.now()

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
            adjusted = adjusted.replace(second=adjusted.second + 1)

            # Handle second overflow
            if adjusted.second >= 60:
                adjusted = adjusted.replace(second=0, minute=adjusted.minute + 1)
                if adjusted.minute >= 60:
                    adjusted = adjusted.replace(minute=0, hour=adjusted.hour + 1)
                    if adjusted.hour >= 24:
                        adjusted = adjusted.replace(hour=0, day=adjusted.day + 1)

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