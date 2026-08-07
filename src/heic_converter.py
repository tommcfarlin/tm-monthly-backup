"""
HEIC to JPEG conversion module with EXIF preservation
"""

import os
import logging
import tempfile
from pathlib import Path
from typing import Optional
from PIL import Image
import pillow_heif

# Enable HEIF support in Pillow
pillow_heif.register_heif_opener()

logger = logging.getLogger(__name__)


class HeicConverter:
    """Handles HEIC to JPEG conversion with EXIF preservation"""

    def __init__(self, jpeg_quality: int = 98, optimize: bool = False):
        """
        Initialize HEIC converter.

        Args:
            jpeg_quality: JPEG quality (1-100, default 98 for high quality)
            optimize: Whether to run libjpeg's extra Huffman-optimization pass
                on encode. Defaults to False. At quality 95 that second pass
                costs +152% on the encode step (+71.5 ms per real HEIC in the
                performance audit, the dominant share of a HEIC-heavy run) and
                buys only ~2.3% smaller JPEGs -- a trade an archive tool on a
                Mac with terabytes of disk should not take by default. The whole
                pipeline is ~1.31x faster with it off (issue #40).
        """
        self.jpeg_quality = jpeg_quality
        self.optimize = optimize
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

        The JPEG is written to a guaranteed-unique path rather than a fixed
        ``{stem}.jpg``. If a real sibling named ``{stem}.jpg`` (or an
        uppercase ``{stem}.JPG`` on a case-insensitive APFS volume) already
        sits beside the HEIC, a fixed name would silently overwrite and destroy
        that photo. Callers must use the returned path -- the on-disk name is an
        implementation detail of the transient conversion output.
        """
        output_path = None
        try:
            if not self.is_heic_file(heic_path):
                logger.warning("File is not HEIC format: %s", heic_path)
                return None

            # Determine the target directory for the converted JPEG.
            heic_file = Path(heic_path)
            target_dir = Path(output_dir) if output_dir else heic_file.parent

            # Ensure output directory exists
            target_dir.mkdir(parents=True, exist_ok=True)

            # Reserve a guaranteed-unique output path on the same filesystem as
            # the target directory. ``tempfile.mkstemp`` creates the file
            # atomically with ``O_CREAT | O_EXCL``, so it can never collide with
            # -- and therefore never overwrite -- an existing sibling, including
            # a case-insensitive ``{stem}.JPG`` match on APFS. Keeping it on the
            # same volume also lets the downstream ``shutil.move`` stay a cheap
            # rename. The final backup filename is timestamp-derived, so this
            # intermediate name never reaches the user.
            fd, tmp_name = tempfile.mkstemp(
                prefix=f"{heic_file.stem}-",
                suffix=".jpg",
                dir=str(target_dir),
            )
            os.close(fd)
            output_path = Path(tmp_name)

            # Open and convert HEIC image
            with Image.open(heic_path) as image:
                # Convert to RGB if necessary (HEIC can be in different color spaces)
                if image.mode not in ['RGB', 'L']:
                    image = image.convert('RGB')

                # Get EXIF data before conversion
                exif_data = image.getexif()

                # Save as JPEG with EXIF preservation. ``optimize`` is off by
                # default (issue #40): the extra Huffman-optimization pass does
                # not change a single pixel -- output is numerically identical,
                # only the entropy coding differs -- yet at quality 95 it more
                # than doubles the encode time for a ~2.3% size saving, and
                # encode is the dominant cost of a HEIC-heavy run.
                save_kwargs = {
                    'format': 'JPEG',
                    'quality': self.jpeg_quality,
                    'optimize': self.optimize
                }

                # Preserve EXIF data if present.
                # Deliberate: this is a personal archive, so the full EXIF block --
                # including GPS and device identifiers -- is preserved. See README,
                # "What Ends Up in backup/". Do not strip without changing that contract.
                if exif_data:
                    save_kwargs['exif'] = exif_data

                image.save(output_path, **save_kwargs)

            logger.info("Successfully converted HEIC to JPEG: %s -> %s", heic_path, output_path)
            self.converted_files.append((heic_path, str(output_path)))
            return str(output_path)

        except Exception as e:
            logger.error("Failed to convert HEIC file %s: %s", heic_path, e)
            self.failed_conversions.append((heic_path, str(e)))
            # Remove the reserved-but-unwritten temp file so a 0-byte artifact is
            # not left behind in export to be re-ingested on a later run.
            if output_path is not None:
                try:
                    output_path.unlink()
                except OSError:
                    pass
            return None

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
                logger.warning("Size mismatch in conversion: %s vs %s", heic_size, jpeg_size)
                return False

            # Check EXIF preservation (at least some data should be preserved)
            if heic_exif and not jpeg_exif:
                logger.warning("EXIF data lost in conversion: %s", original_heic)
                return False

            return True

        except Exception as e:
            logger.error("Error verifying conversion: %s", e)
            return False

    def cleanup_original_heic(
        self,
        heic_path: str,
        converted_jpeg: Optional[str] = None,
        verify_first: bool = True,
    ) -> bool:
        """
        Delete an original HEIC file after a verified-good conversion.

        This is the single, safe implementation of "delete an original HEIC".
        When ``verify_first`` is set, the deletion is gated on
        :meth:`verify_conversion` against the *actual* converted JPEG path the
        caller was handed by :meth:`convert_heic_to_jpeg`. Since #26 that path is
        a unique ``mkstemp`` name, not a fixed ``{stem}.jpg`` sibling, so the
        caller must pass ``converted_jpeg`` explicitly -- there is no longer a
        derivable name to fall back on, and guessing one risks verifying (and
        then deleting against) the wrong file.

        Args:
            heic_path: Path to the original HEIC file to delete.
            converted_jpeg: Path to the converted JPEG. Required when
                ``verify_first`` is True; verification targets exactly this file.
            verify_first: Whether to re-verify the conversion before deleting. Pass
                False only when the caller has already verified the conversion and
                has since moved the JPEG out of reach (e.g. filed into ``backup/``).

        Returns:
            True if the original was deleted, False if verification failed, the
            converted path was missing when required, or the unlink errored.
        """
        try:
            heic_file = Path(heic_path)

            if verify_first:
                if converted_jpeg is None:
                    logger.error(
                        "Refusing to delete original without a converted path to "
                        f"verify against, keeping original: {heic_path}"
                    )
                    return False
                if not self.verify_conversion(heic_path, converted_jpeg):
                    logger.error("Conversion verification failed, keeping original: %s", heic_path)
                    return False

            heic_file.unlink()
            logger.info("Deleted original HEIC file: %s", heic_path)
            return True

        except Exception as e:
            logger.error("Failed to delete HEIC file %s: %s", heic_path, e)
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