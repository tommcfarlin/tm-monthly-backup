"""
Regression test for issue #61: GPS metadata must survive HEIC -> JPEG conversion.

``HeicConverter.convert_heic_to_jpeg`` carries the full EXIF block (including
the GPS sub-IFD) across the conversion by design -- see the "What Ends Up in
backup/" section of README.md and the comment at the ``exif_data`` assignment
in ``src/heic_converter.py``. This test pins that contract with a real GPS IFD
(not just a top-level EXIF tag), so a future change that silently drops GPS
tags -- for example by copying only ``getexif()``'s top-level items and
dropping ``get_ifd(GPS_IFD)`` -- is caught rather than shipped as an
undocumented privacy regression.
"""

import os
import shutil
import tempfile
import unittest

import pillow_heif
from PIL import Image

from src.heic_converter import HeicConverter

pillow_heif.register_heif_opener()

# Pointer tag to the GPS sub-IFD, mirroring EXIF_IFD (0x8769) in tests/fixtures.py.
GPS_IFD = 0x8825


def _make_gps_heic(path: str) -> None:
    """Write a real HEIC whose EXIF carries a GPS sub-IFD, laid out the way a
    camera writes it (GPSLatitude/GPSLongitude plus their ref hemispheres)."""
    image = Image.new("RGB", (32, 32), color="green")
    exif = image.getexif()
    gps = exif.get_ifd(GPS_IFD)
    gps[1] = "N"  # GPSLatitudeRef
    gps[2] = (40.0, 26.0, 46.0)  # GPSLatitude (deg, min, sec)
    gps[3] = "W"  # GPSLongitudeRef
    gps[4] = (79.0, 58.0, 56.0)  # GPSLongitude (deg, min, sec)
    image.save(path, format="HEIF", exif=exif)


class TestGpsSurvivesHeicToJpegConversion(unittest.TestCase):
    """Pins the documented "GPS survives conversion" contract (issue #61)."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.converter = HeicConverter()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_gps_ifd_survives_conversion(self):
        heic_path = os.path.join(self.temp_dir, "with_gps.heic")
        _make_gps_heic(heic_path)

        # Confirm the fixture itself actually carries GPS before trusting the
        # conversion assertion below.
        with Image.open(heic_path) as original:
            original_gps = original.getexif().get_ifd(GPS_IFD)
        self.assertEqual(len(original_gps), 4)

        result = self.converter.convert_heic_to_jpeg(heic_path, output_dir=self.temp_dir)
        self.assertIsNotNone(result)

        with Image.open(result) as converted:
            converted_gps = converted.getexif().get_ifd(GPS_IFD)

        self.assertEqual(dict(converted_gps), dict(original_gps))
        self.assertEqual(converted_gps[1], "N")
        self.assertEqual(converted_gps[2], (40.0, 26.0, 46.0))
        self.assertEqual(converted_gps[3], "W")
        self.assertEqual(converted_gps[4], (79.0, 58.0, 56.0))


if __name__ == "__main__":
    unittest.main()
