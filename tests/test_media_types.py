"""
Tests for the shared media-type extension constants (issue #43).

``VIDEO_EXTENSIONS`` used to be defined twice, independently, in
``file_categorizer.py`` and ``exif_handler.py`` -- byte-identical at the time,
but with nothing linking them, so adding a format to one and forgetting the
other would silently desynchronize how a file is filed from how it is read
for a timestamp. This suite pins the fix: both ``FileCategorizer`` and
``ExifHandler`` now read the one definition in ``media_types.py``, so they
can no longer independently drift.
"""

import unittest

from src.file_categorizer import FileCategorizer
from src.exif_handler import ExifHandler
from src.heic_converter import HeicConverter
from src.media_types import VIDEO_EXTENSIONS, HEIC_EXTENSIONS


class TestVideoExtensionAgreement(unittest.TestCase):
    """FileCategorizer and ExifHandler must agree on every video extension."""

    def setUp(self):
        self.exif_handler = ExifHandler()

    def test_both_classes_share_the_same_video_extensions_object(self):
        """Each class's class attribute IS the one shared set object, not a
        second copy that merely happens to be equal (issue #43)."""
        self.assertIs(FileCategorizer.VIDEO_EXTENSIONS, VIDEO_EXTENSIONS)
        self.assertIs(ExifHandler.VIDEO_EXTENSIONS, VIDEO_EXTENSIONS)

    def test_categorizer_and_exif_handler_agree_on_every_video_extension(self):
        """For every video extension, and a sample of non-video extensions,
        FileCategorizer.VIDEO_EXTENSIONS membership and
        ExifHandler._is_video_file must give the identical answer.

        Before issue #43, this held only because the two literal sets
        happened to have been kept in sync by hand; nothing enforced it. This
        test is the enforcement: it would still pass against the pre-#43
        code (the two copies agreed on this date), but it now fails the
        moment either copy drifts, because there is only one copy to drift.
        """
        candidate_extensions = sorted(VIDEO_EXTENSIONS) + [
            '.jpg', '.jpeg', '.png', '.heic', '.heif', '.aae', '.txt', '.gif',
        ]

        for ext in candidate_extensions:
            file_path = f"/export/sample{ext}"
            with self.subTest(ext=ext):
                self.assertEqual(
                    ext in FileCategorizer.VIDEO_EXTENSIONS,
                    self.exif_handler._is_video_file(file_path),
                    f"FileCategorizer and ExifHandler disagree on {ext!r}",
                )


class TestHeicExtensions(unittest.TestCase):
    """HeicConverter.is_heic_file reads from the shared HEIC_EXTENSIONS set."""

    def setUp(self):
        self.converter = HeicConverter()

    def test_heic_extensions_is_a_set(self):
        """AC4 (issue #43): the shared constant is a ``set``, not a ``list``."""
        self.assertIsInstance(HEIC_EXTENSIONS, set)
        self.assertEqual(HEIC_EXTENSIONS, {'.heic', '.heif'})

    def test_is_heic_file_agrees_with_the_shared_constant(self):
        candidates = sorted(HEIC_EXTENSIONS) + ['.jpg', '.mov', '.aae', '.png']
        for ext in candidates:
            file_path = f"/export/sample{ext}"
            with self.subTest(ext=ext):
                self.assertEqual(
                    ext in HEIC_EXTENSIONS,
                    self.converter.is_heic_file(file_path),
                )
