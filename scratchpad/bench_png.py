#!/usr/bin/env python3
"""
Benchmark for issue #44: stop forcing a full PNG decode to read text chunks.

Builds a realistic PNG fixture -- large enough that a full pixel decode is
measurable, carrying a real ``eXIf`` chunk (so the unrelated
``_exif_shows_synthetic_edit`` / ``img.getexif()`` path in
``_is_generated_content`` does not itself force a decode, exactly as happens
for real camera-captured PNGs) and a ``tEXt`` chunk with an AI marker (so
``_is_generated_content`` returns True, exercising the same code path the
issue's audit measured) -- then times, best-of-5:

  * ``Image.open`` alone
  * ``Image.open`` + ``.info`` access (the new, cheap path)
  * ``Image.open`` + ``.text`` access (the old, decode-forcing path)
  * ``FileCategorizer._is_generated_content`` end to end (the body the
    acceptance criterion is about)

Run from the repo root:

    /Users/tommcfarlin/Projects/02-tm/tm-monthly-backup/tm-backup-env/bin/python scratchpad/bench_png.py
"""

import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image
from PIL.PngImagePlugin import PngInfo
from PIL.ExifTags import TAGS

from src.file_categorizer import FileCategorizer


def build_fixture_png(path: str, size=(2000, 2000)) -> None:
    """Write a real, decode-costly PNG with a text chunk and real EXIF."""
    import random

    random.seed(42)
    width, height = size
    # Per-pixel noise so the deflate/decode step has real work to do, rather
    # than a flat color Pillow's encoder/decoder can shortcut.
    pixels = bytes(
        random.randrange(0, 256) for _ in range(width * height * 3)
    )
    image = Image.frombytes("RGB", size, pixels)

    metadata = PngInfo()
    metadata.add_text("Comment", "Created with ChatGPT / OpenAI")

    exif = image.getexif()
    software_tag = next(tid for tid, name in TAGS.items() if name == "Software")
    exif[software_tag] = "Adobe Photoshop 2024"

    image.save(path, format="PNG", pnginfo=metadata, exif=exif)


def best_of(fn, n=5):
    best = None
    for _ in range(n):
        start = time.perf_counter()
        fn()
        elapsed = (time.perf_counter() - start) * 1000.0
        if best is None or elapsed < best:
            best = elapsed
    return best


def main():
    tmp_dir = tempfile.mkdtemp(prefix="bench_png_")
    png_path = os.path.join(tmp_dir, "fixture.png")
    build_fixture_png(png_path)

    categorizer = FileCategorizer()

    def open_only():
        with Image.open(png_path) as img:
            pass

    def open_plus_info():
        with Image.open(png_path) as img:
            _ = img.info

    def open_plus_text():
        with Image.open(png_path) as img:
            _ = img.text

    def is_generated_content_body():
        categorizer._is_generated_content(png_path)

    print(f"Fixture: {png_path} ({os.path.getsize(png_path) / 1024:.0f} KB)")
    print()
    print(f"{'operation':<45} {'best-of-5 ms':>12}")
    print(f"{'Image.open only':<45} {best_of(open_only):>12.2f}")
    print(f"{'Image.open + im.info (dict)':<45} {best_of(open_plus_info):>12.2f}")
    print(f"{'Image.open + .text (old path)':<45} {best_of(open_plus_text):>12.2f}")
    print(
        f"{'_is_generated_content body (new path)':<45} "
        f"{best_of(is_generated_content_body):>12.2f}"
    )

    # Confirm detection is unchanged: this fixture carries an AI marker and
    # must still classify as generated after the fix.
    detected = categorizer._is_generated_content(png_path)
    print()
    print(f"Detected as generated content: {detected} (expected True)")

    os.remove(png_path)
    os.rmdir(tmp_dir)


if __name__ == "__main__":
    main()
