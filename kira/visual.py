"""Perceptual hashes that tell whether two files are renditions of the same capture.

The library uses them to spot resized or recompressed copies and edited versions of a photo.
"""

from __future__ import annotations

import functools
import math
from io import BytesIO

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - Pillow is an explicit runtime dependency
    Image = None
    ImageOps = None

try:  # Optional: lets Pillow open iPhone HEIC photos for previews and matching.
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - HEIC files then match by metadata only
    pass

VISUAL_SIZE = 32


@functools.lru_cache(maxsize=8)
def _dct_table(size: int) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(math.cos((2 * column + 1) * coefficient * math.pi / (2 * size)) for column in range(size))
        for coefficient in range(size)
    )


# Deciding whether one file is another rendition of the same capture needs a
# structural comparison that survives resizing, recompression and tonal edits.
# These hashes are computed on contrast-normalized grayscale, so color grading
# and black-and-white conversions do not change them.

SAME_PIXELS_PHASH = 3
SAME_PIXELS_DHASH = 24
EDIT_PHASH = 14
EDIT_MARGIN = 4


def version_signature(source) -> tuple[int, int]:
    """Return (63-bit 2-D DCT hash, 256-bit difference hash) of normalized luminance.

    ``source`` is a path, encoded image bytes, or an already upright Pillow image.
    """
    if Image is None or ImageOps is None:
        raise RuntimeError("Pillow is required for visual photo matching")
    if isinstance(source, Image.Image):
        gray = source.convert("L")
    else:
        with Image.open(BytesIO(source) if isinstance(source, bytes) else source) as opened:
            opened.draft("L", (256, 256))
            gray = ImageOps.exif_transpose(opened).convert("L")
    size = VISUAL_SIZE
    table = _dct_table(size)
    pixels = ImageOps.autocontrast(gray.resize((size, size), Image.Resampling.LANCZOS), cutoff=1).tobytes()
    rows = [
        [math.fsum(pixels[row * size + column] * table[u][column] for column in range(size)) for u in range(8)]
        for row in range(size)
    ]
    coefficients = [math.fsum(rows[row][u] * table[v][row] for row in range(size)) for v in range(8) for u in range(8)][
        1:
    ]
    median = sorted(coefficients)[len(coefficients) // 2]
    frequency_hash = 0
    for value in coefficients:
        frequency_hash = (frequency_hash << 1) | (value > median)
    strip = ImageOps.autocontrast(gray.resize((17, 16), Image.Resampling.LANCZOS), cutoff=1).tobytes()
    difference_hash = 0
    for row in range(16):
        for column in range(16):
            difference_hash = (difference_hash << 1) | (strip[row * 17 + column] > strip[row * 17 + column + 1])
    return frequency_hash, difference_hash


def version_distance(left: tuple[int, int], right: tuple[int, int]) -> tuple[int, int]:
    return (left[0] ^ right[0]).bit_count(), (left[1] ^ right[1]).bit_count()


def same_pixels(distance: tuple[int, int]) -> bool:
    """True when two files show the same picture, only resized or recompressed."""
    return distance[0] <= SAME_PIXELS_PHASH and distance[1] <= SAME_PIXELS_DHASH
