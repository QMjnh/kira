"""Perceptual photo similarity shared by Google matching and compare grouping.

Signatures combine a 32x32 RGB pixel block, a 256-bit difference hash, a
64-bit DCT perceptual hash of blurred grayscale, and a normalized HSV palette
histogram. Two tiers decide whether photos show the same content:
"reencoded" (pixel-faithful copies) and "edited" (exposure/crop/angle variants).

Compare clustering uses stricter "edited" limits plus a capture-time gate so
that bursts of one scene group tightly instead of chaining across themes.
"""
from __future__ import annotations

import concurrent.futures
import functools
import math
import time
from io import BytesIO
from pathlib import Path

try:
    from PIL import Image, ImageFilter, ImageOps
except ImportError:  # pragma: no cover - Pillow is an explicit runtime dependency
    Image = None
    ImageFilter = None
    ImageOps = None

VISUAL_SIZE = 32
VISUAL_COLOR_THRESHOLD = 8.0
VISUAL_HASH_THRESHOLD = 12
VISUAL_PALETTE_THRESHOLD = 0.83
VISUAL_PHASH_THRESHOLD = 36
VISUAL_DIMENSION_TOLERANCE = 0.7

# Stricter compare-cluster limits: random outdoor pairs average ~26/63 pHash
# bits and ~0.87 palette overlap, so loose limits chain whole folders together.
CLUSTER_PHASH_THRESHOLD = 18
CLUSTER_PALETTE_THRESHOLD = 0.9
# Scene variants are shot within minutes; re-edited copies keep the original's
# capture time. Edited-tier pairs further apart than this never join.
CLUSTER_TIME_GATE_SECONDS = 180.0
# Re-encoded copies tolerate clock drift between devices, so their gate is
# wider; it also bounds the pairwise scan once entries are time-sorted.
REENCODE_TIME_GATE_SECONDS = 6 * 3600.0
_EXIF_DATETIME_ORIGINAL = 36867
_EXIF_IFD_POINTER = 34665


@functools.lru_cache(maxsize=8)
def _dct_table(size: int) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(
            math.cos((2 * column + 1) * coefficient * math.pi / (2 * size))
            for column in range(size)
        )
        for coefficient in range(size)
    )


def image_signature(source: Path | bytes) -> dict:
    if Image is None or ImageOps is None:
        raise RuntimeError("Pillow is required for visual photo matching")
    stream = BytesIO(source) if isinstance(source, bytes) else source
    with Image.open(stream) as opened:
        width, height = opened.size
        try:
            orientation = int(opened.getexif().get(274, 1))
        except (AttributeError, TypeError, ValueError):
            orientation = 1
        if orientation in {5, 6, 7, 8}:
            width, height = height, width
        capture_time = None
        try:
            exif = opened.getexif()
            sub_ifd = exif.get_ifd(_EXIF_IFD_POINTER)
            capture_time = (
                str(sub_ifd.get(_EXIF_DATETIME_ORIGINAL) or exif.get(_EXIF_DATETIME_ORIGINAL) or "")
                .strip()
                or None
            )
        except (AttributeError, TypeError, ValueError):
            pass
        opened.draft("RGB", (256, 256))
        image = ImageOps.exif_transpose(opened)
        rgb = image.convert("RGB")
        small_rgb = rgb.resize((VISUAL_SIZE, VISUAL_SIZE), Image.Resampling.LANCZOS)
        grayscale = rgb.convert("L").resize((17, 16), Image.Resampling.LANCZOS)
        grayscale_bytes = grayscale.tobytes()
        difference_hash = 0
        for row in range(16):
            offset = row * 17
            for column in range(16):
                difference_hash = (difference_hash << 1) | (
                    grayscale_bytes[offset + column]
                    > grayscale_bytes[offset + column + 1]
                )
        perceptual_gray = ImageOps.autocontrast(
            rgb.convert("L").resize((VISUAL_SIZE, VISUAL_SIZE), Image.Resampling.LANCZOS)
        ).filter(ImageFilter.GaussianBlur(1.5))
        perceptual_hash = _perceptual_hash(perceptual_gray)
        palette = _palette(rgb.resize((64, 64), Image.Resampling.LANCZOS))
        return {
            "width": width,
            "height": height,
            "pixels": small_rgb.tobytes(),
            "difference_hash": difference_hash,
            "perceptual_hash": perceptual_hash,
            "palette": palette,
            "capture_time": capture_time,
        }


def _perceptual_hash(grayscale) -> int:
    size = grayscale.size[0]
    pixels = list(grayscale.getdata())
    table = _dct_table(size)
    coefficients = [
        math.fsum(
            pixels[row * size + column] * table[coefficient][column]
            for column in range(size)
        )
        for row in range(8)
        for coefficient in range(8)
        if not (row == 0 and coefficient == 0)
    ]
    median = sorted(coefficients)[len(coefficients) // 2]
    perceptual_hash = 0
    for value in coefficients:
        perceptual_hash = (perceptual_hash << 1) | (value > median)
    return perceptual_hash


def _palette(rgb) -> list[float]:
    histogram = [0.0] * 65
    for hue, saturation, value in rgb.convert("HSV").getdata():
        if value < 30 or saturation < 20:
            index = 64
        else:
            index = min(hue * 16 // 256, 15) * 4 + min(saturation * 4 // 256, 3)
        histogram[index] += 1
    total = sum(histogram) or 1.0
    return [count / total for count in histogram]


def signature_distance(left: dict, right: dict) -> tuple[float, int]:
    color = sum(
        abs(first - second) for first, second in zip(left["pixels"], right["pixels"])
    ) / len(left["pixels"])
    perceptual = (left["difference_hash"] ^ right["difference_hash"]).bit_count()
    return color, perceptual


def perceptual_hash_distance(left: dict, right: dict) -> int:
    return (left["perceptual_hash"] ^ right["perceptual_hash"]).bit_count()


def palette_similarity(left: dict, right: dict) -> float:
    return math.fsum(
        math.sqrt(first * second)
        for first, second in zip(left["palette"], right["palette"])
    )


def similarity_metrics(left: dict, right: dict) -> dict:
    color, difference_hash = signature_distance(left, right)
    return {
        "color": color,
        "difference_hash": difference_hash,
        "perceptual_hash": perceptual_hash_distance(left, right),
        "palette": palette_similarity(left, right),
    }


def match_tier(metrics: dict) -> str | None:
    if metrics["color"] <= VISUAL_COLOR_THRESHOLD and metrics["difference_hash"] <= VISUAL_HASH_THRESHOLD:
        return "reencoded"
    if (
        metrics["palette"] >= VISUAL_PALETTE_THRESHOLD
        and metrics["perceptual_hash"] <= VISUAL_PHASH_THRESHOLD
    ):
        return "edited"
    return None


def dimensions_compatible(left: tuple[int, int], right: tuple[int, int]) -> bool:
    if min(left) <= 0 or min(right) <= 0:
        return False
    left_landscape = (max(left), min(left))
    right_landscape = (max(right), min(right))
    tolerance = VISUAL_DIMENSION_TOLERANCE
    return (
        right_landscape[0] * tolerance
        <= left_landscape[0] <= right_landscape[0] / tolerance
        and right_landscape[1] * tolerance
        <= left_landscape[1] <= right_landscape[1] / tolerance
    )


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, index: int) -> int:
        root = index
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[index] != root:
            self.parent[index], index = root, self.parent[index]
        return root

    def union(self, first: int, second: int) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            self.parent[max(first_root, second_root)] = min(first_root, second_root)


def compute_signatures(paths: list[Path], max_workers: int = 12) -> tuple[dict[Path, dict], list[dict]]:
    signatures: dict[Path, dict] = {}
    errors: list[dict] = []
    workers = max(1, min(max_workers, len(paths)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_cached_image_signature, path): path for path in paths}
        for future in concurrent.futures.as_completed(futures):
            path = futures[future]
            try:
                signatures[path] = future.result()
            except Exception as exc:
                errors.append({"path": str(path), "filename": Path(path).name, "error": str(exc)})
    return signatures, errors


# Re-running compare grouping over an unchanged folder skips all JPEG decoding.
_SIGNATURE_CACHE_LIMIT = 8192
_signature_cache: dict[tuple[str, int, int], dict] = {}


def _cached_image_signature(path: Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return image_signature(path)
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _signature_cache.get(key)
    if cached is not None:
        return cached
    signature = image_signature(path)
    while len(_signature_cache) >= _SIGNATURE_CACHE_LIMIT:
        _signature_cache.pop(next(iter(_signature_cache)))
    _signature_cache[key] = signature
    return signature


def _capture_epoch(signature: dict) -> float | None:
    raw = signature.get("capture_time")
    if not raw:
        return None
    try:
        parsed = time.strptime(str(raw)[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    return time.mktime(parsed)


def _try_cluster_pair(
    first_signature: dict,
    second_signature: dict,
    epoch_gap: float | None,
) -> bool:
    """Union two signatures when a match tier accepts them."""
    if not dimensions_compatible(
        (first_signature["width"], first_signature["height"]),
        (second_signature["width"], second_signature["height"]),
    ):
        return False
    difference_distance = (
        first_signature["difference_hash"] ^ second_signature["difference_hash"]
    ).bit_count()
    perceptual_distance = (
        first_signature["perceptual_hash"] ^ second_signature["perceptual_hash"]
    ).bit_count()
    if (
        perceptual_distance <= CLUSTER_PHASH_THRESHOLD
        and (epoch_gap is None or epoch_gap <= CLUSTER_TIME_GATE_SECONDS)
        and palette_similarity(first_signature, second_signature) >= CLUSTER_PALETTE_THRESHOLD
    ):
        return True
    return (
        difference_distance <= VISUAL_HASH_THRESHOLD
        and (epoch_gap is None or epoch_gap <= REENCODE_TIME_GATE_SECONDS)
        and signature_distance(first_signature, second_signature)[0] <= VISUAL_COLOR_THRESHOLD
    )


def cluster_signatures(entries: list[tuple[str, dict]]) -> list[list[str]]:
    """Group signature entries into connected components of similar photos.

    Entries are (key, signature) pairs; keys are returned verbatim so callers
    can map clusters back to files or assets. Exact pixel-identical signatures
    are merged first; remaining representatives are joined when either match
    tier accepts them. Edited variants must be captured within minutes of each
    other, so visually loose scenes cannot chain into one giant component;
    re-encoded copies tolerate a wider window for camera-clock drift. Entries
    without capture times are never time-gated.
    """
    union_find = _UnionFind(len(entries))
    by_pixels: dict[bytes, int] = {}
    for index, (_, signature) in enumerate(entries):
        anchor = by_pixels.setdefault(signature["pixels"], index)
        union_find.union(anchor, index)

    epochs = [_capture_epoch(signature) for _, signature in entries]
    timed = sorted(
        (index for index in range(len(entries)) if epochs[index] is not None),
        key=lambda index: epochs[index],
    )
    untimed = [index for index in range(len(entries)) if epochs[index] is None]

    def gap(first_index: int, second_index: int) -> float | None:
        if epochs[first_index] is None or epochs[second_index] is None:
            return None
        return abs(epochs[second_index] - epochs[first_index])

    def join_if_similar(first_index: int, second_index: int) -> bool:
        if union_find.find(first_index) == union_find.find(second_index):
            return False
        if _try_cluster_pair(
            entries[first_index][1],
            entries[second_index][1],
            gap(first_index, second_index),
        ):
            union_find.union(first_index, second_index)
            return True
        return False

    # Timed representatives are scanned in capture order so the inner loop can
    # stop as soon as the widest re-encode window is exceeded.
    for position, first_index in enumerate(timed):
        for second_index in timed[position + 1 :]:
            if gap(first_index, second_index) > REENCODE_TIME_GATE_SECONDS:
                break
            join_if_similar(first_index, second_index)
    # Untimed representatives must meet every other photo exactly once.
    for position, first_index in enumerate(untimed):
        for second_index in untimed[position + 1 :]:
            join_if_similar(first_index, second_index)
        for second_index in timed:
            join_if_similar(first_index, second_index)

    clusters: dict[int, list[str]] = {}
    for index, (key, _) in enumerate(entries):
        clusters.setdefault(union_find.find(index), []).append(key)
    return list(clusters.values())
