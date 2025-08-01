"""
HEIC to JPEG conversion module with EXIF preservation
"""

import os
import logging
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image
import pillow_heif

# Enable HEIF support in Pillow
pillow_heif.register_heif_opener()

logger = logging.getLogger(__name__)


class HeicConverter:
    """Handles HEIC to JPEG conversion with EXIF preservation"""

    def __init__(self, jpeg_quality: int = 95):
        """
        Initialize HEIC converter.

        Args:
            jpeg_quality: JPEG quality (1-100, default 95 for high quality)
        """
        self.jpeg_quality = jpeg_quality
        self.converted_files = []
        self.failed_conversions = []

    def is_heic_file(self, file_path: str) -> bool:
        """
        Check if file is a HEIC/HEIF file.

        Args:
            file_path: Path to file

        Returns:
            True if file is HEIC/HEIF format
        """
        ext = Path(file_path).suffix.lower()
        return ext in ['.heic', '.heif']

    def convert_heic_to_jpeg(self, heic_path: str, output_dir: str = None) -> Optional[str]:
        """
        Convert HEIC file to JPEG with EXIF preservation.

        Args:
            heic_path: Path to HEIC file
            output_dir: Output directory (default: same as input file)

        Returns:
            Path to converted JPEG file, None if conversion failed
        """
        try:
            if not self.is_heic_file(heic_path):
                logger.warning(f"File is not HEIC format: {heic_path}")
                return None

            # Determine output path
            heic_file = Path(heic_path)
            if output_dir:
                output_path = Path(output_dir) / f"{heic_file.stem}.jpg"
            else:
                output_path = heic_file.parent / f"{heic_file.stem}.jpg"

            # Ensure output directory exists
            output_path.parent.mkdir(parents=True, exist_ok=True)

            # Open and convert HEIC image
            with Image.open(heic_path) as image:
                # Convert to RGB if necessary (HEIC can be in different color spaces)
                if image.mode not in ['RGB', 'L']:
                    image = image.convert('RGB')

                # Get EXIF data before conversion
                exif_data = image.getexif()

                # Save as JPEG with EXIF preservation
                save_kwargs = {
                    'format': 'JPEG',
                    'quality': self.jpeg_quality,
                    'optimize': True
                }

                # Preserve EXIF data if present
                if exif_data:
                    save_kwargs['exif'] = exif_data

                image.save(output_path, **save_kwargs)

            logger.info(f"Successfully converted HEIC to JPEG: {heic_path} -> {output_path}")
            self.converted_files.append((heic_path, str(output_path)))
            return str(output_path)

        except Exception as e:
            logger.error(f"Failed to convert HEIC file {heic_path}: {e}")
            self.failed_conversions.append((heic_path, str(e)))
            return None

    def batch_convert(self, heic_files: list, output_dir: str = None) -> Tuple[list, list]:
        """
        Convert multiple HEIC files to JPEG.

        Args:
            heic_files: List of HEIC file paths
            output_dir: Output directory for converted files

        Returns:
            Tuple of (successful_conversions, failed_conversions)
        """
        successful = []
        failed = []

        for heic_file in heic_files:
            jpeg_path = self.convert_heic_to_jpeg(heic_file, output_dir)
            if jpeg_path:
                successful.append((heic_file, jpeg_path))
            else:
                failed.append(heic_file)

        return successful, failed

    def verify_conversion(self, original_heic: str, converted_jpeg: str) -> bool:
        """
        Verify that conversion preserved important metadata.

        Args:
            original_heic: Path to original HEIC file
            converted_jpeg: Path to converted JPEG file

        Returns:
            True if conversion appears successful
        """
        try:
            # Check that both files exist
            if not (Path(original_heic).exists() and Path(converted_jpeg).exists()):
                return False

            # Compare basic metadata
            with Image.open(original_heic) as heic_img:
                heic_exif = heic_img.getexif()
                heic_size = heic_img.size

            with Image.open(converted_jpeg) as jpeg_img:
                jpeg_exif = jpeg_img.getexif()
                jpeg_size = jpeg_img.size

            # Check size preservation
            if heic_size != jpeg_size:
                logger.warning(f"Size mismatch in conversion: {heic_size} vs {jpeg_size}")
                return False

            # Check EXIF preservation (at least some data should be preserved)
            if heic_exif and not jpeg_exif:
                logger.warning(f"EXIF data lost in conversion: {original_heic}")
                return False

            return True

        except Exception as e:
            logger.error(f"Error verifying conversion: {e}")
            return False

    def cleanup_original_heic(self, heic_path: str, verify_first: bool = True) -> bool:
        """
        Delete original HEIC file after successful conversion.

        Args:
            heic_path: Path to original HEIC file
            verify_first: Whether to verify conversion before deletion

        Returns:
            True if file was deleted successfully
        """
        try:
            heic_file = Path(heic_path)

            if verify_first:
                # Find corresponding JPEG file
                jpeg_path = heic_file.parent / f"{heic_file.stem}.jpg"
                if not self.verify_conversion(heic_path, str(jpeg_path)):
                    logger.error(f"Conversion verification failed, keeping original: {heic_path}")
                    return False

            heic_file.unlink()
            logger.info(f"Deleted original HEIC file: {heic_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to delete HEIC file {heic_path}: {e}")
            return False

    def get_conversion_stats(self) -> dict:
        """
        Get statistics about conversions performed.

        Returns:
            Dictionary with conversion statistics
        """
        return {
            'total_conversions': len(self.converted_files),
            'successful_conversions': len(self.converted_files),
            'failed_conversions': len(self.failed_conversions),
            'converted_files': self.converted_files.copy(),
            'failed_files': self.failed_conversions.copy()
        }

    def clear_stats(self):
        """Clear conversion statistics"""
        self.converted_files.clear()
        self.failed_conversions.clear()