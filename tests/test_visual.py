"""Tests for the shared visual similarity core and compare-group clustering."""
from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageEnhance

from kira.visual import (
    VISUAL_COLOR_THRESHOLD,
    VISUAL_HASH_THRESHOLD,
    VISUAL_PALETTE_THRESHOLD,
    cluster_signatures,
    image_signature,
    match_tier,
    similarity_metrics,
)


def scene(size: tuple[int, int], warm: bool) -> Image.Image:
    image = Image.new("RGB", size)
    if warm:
        image.putdata(
            [
                (240 - (x + y) % 50, x % 140, y % 80)
                for y in range(size[1])
                for x in range(size[0])
            ]
        )
    else:
        image.putdata(
            [
                (x % 80, (y * 2) % 120 + 60, 255 - (x + y) % 60)
                for y in range(size[1])
                for x in range(size[0])
            ]
        )
    return image


class VisualClusterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _signed_scene(self, name: str, image: Image.Image) -> tuple[str, dict]:
        path = self.root / f"{name}.jpg"
        image.save(path, quality=90)
        return name, image_signature(path)

    def test_edited_variants_cluster_and_unrelated_scenes_stay_apart(self) -> None:
        cool = scene((160, 120), warm=False)
        entries = [
            self._signed_scene("cool-original", cool),
            self._signed_scene("cool-dark", ImageEnhance.Brightness(cool).enhance(0.42)),
            self._signed_scene("cool-bright", ImageEnhance.Brightness(cool).enhance(1.4)),
            self._signed_scene("warm-original", scene((160, 120), warm=True)),
            self._signed_scene("warm-dark", ImageEnhance.Brightness(scene((160, 120), warm=True)).enhance(0.5)),
        ]

        clusters = sorted(cluster_signatures(entries), key=lambda keys: keys[0])

        self.assertEqual(len(clusters), 2)
        cluster_names = {tuple(sorted(cluster)) for cluster in clusters}
        self.assertIn(("cool-bright", "cool-dark", "cool-original"), cluster_names)
        self.assertIn(("warm-dark", "warm-original"), cluster_names)

    def test_min_group_size_is_enforced_by_caller_filtering(self) -> None:
        cool = scene((160, 120), warm=False)
        warm = scene((160, 120), warm=True)
        entries = [
            self._signed_scene("a", cool),
            self._signed_scene("b", ImageEnhance.Brightness(cool).enhance(0.5)),
            self._signed_scene("c", ImageEnhance.Brightness(warm).enhance(1.4)),
        ]

        clusters = cluster_signatures(entries)
        kept = [cluster for cluster in clusters if len(cluster) >= 3]

        self.assertEqual(kept, [])

    def test_exact_pixel_copies_share_one_cluster(self) -> None:
        cool = scene((96, 96), warm=False)
        entries = [
            self._signed_scene("copy-one", cool),
            self._signed_scene("copy-two", cool.copy()),
        ]

        clusters = cluster_signatures(entries)

        self.assertEqual(len(clusters), 1)
        self.assertEqual(sorted(clusters[0]), ["copy-one", "copy-two"])

    def test_match_tier_prefers_reencoded_for_pixel_faithful_pairs(self) -> None:
        first = image_signature(self._save("re-96.jpg", scene((160, 120), warm=False)))
        second_path = self.root / "re-78.jpg"
        scene((160, 120), warm=False).save(second_path, quality=78)
        second = image_signature(second_path)

        metrics = similarity_metrics(first, second)

        self.assertLessEqual(metrics["color"], VISUAL_COLOR_THRESHOLD)
        self.assertLessEqual(metrics["difference_hash"], VISUAL_HASH_THRESHOLD)
        self.assertEqual(match_tier(metrics), "reencoded")

    def test_palette_threshold_separates_unrelated_scenes(self) -> None:
        left = image_signature(self._save("sep-cool.jpg", scene((160, 120), warm=False)))
        right = image_signature(self._save("sep-warm.jpg", scene((160, 120), warm=True)))

        metrics = similarity_metrics(left, right)

        self.assertLess(metrics["palette"], VISUAL_PALETTE_THRESHOLD)
        self.assertIsNone(match_tier(metrics))

    def test_capture_time_gate_keeps_near_variants_and_splits_distant_ones(self) -> None:
        # Distinct difference hashes force every merge through the edited
        # tier, so these pairs exercise only the capture-time gate.
        entries = [
            ("morning-a", self._timed("2026:04:03 09:00:00", b"a")),
            ("morning-b", self._timed("2026:04:03 09:02:00", b"b", difference_hash=0x0F0F0F0F)),
        ]
        self.assertEqual(len(cluster_signatures(entries)), 1)

        distant = [
            ("noon-a", self._timed("2026:04:03 12:00:00", b"c")),
            ("afternoon-b", self._timed("2026:04:03 15:00:00", b"d", difference_hash=0x0F0F0F0F)),
        ]
        self.assertEqual(len(cluster_signatures(distant)), 2)

    def test_reencoded_gate_is_wider_than_edited_gate(self) -> None:
        # Pixel-faithful copy captured three hours later (camera clock drift)
        # still joins through the re-encoded tier; a looser visual match at
        # that gap must not join at all.
        original = ("orig", self._timed("2026:04:03 08:00:00", b"e", difference_hash=0xF0F0F0F0))
        copy = ("copy", self._timed("2026:04:03 11:00:00", b"f", difference_hash=0xF0F0F0F0))
        self.assertEqual(len(cluster_signatures([original, copy])), 1)

        variant = ("variant", self._timed(
            "2026:04:03 11:00:00", b"g", perceptual_hash=10, difference_hash=0x0F0F0F0F
        ))
        self.assertEqual(len(cluster_signatures([original, variant])), 2)

    def test_untimed_entries_are_never_time_gated(self) -> None:
        entries = [
            ("untimed", self._timed(None, b"h")),
            ("timed-late", self._timed("2026:04:05 20:00:00", b"i")),
        ]
        self.assertEqual(len(cluster_signatures(entries)), 1)

    def test_image_signature_reads_capture_time_from_exif_sub_ifd(self) -> None:
        path = self.root / "stamped.jpg"
        Image.new("RGB", (48, 32), (90, 110, 130)).save(
            path, quality=90, exif=self._exif_blob("2026:04:03 12:25:49")
        )

        signature = image_signature(path)

        self.assertEqual(signature["capture_time"], "2026:04:03 12:25:49")

    def _timed(
        self,
        capture_time: str | None,
        token: bytes,
        perceptual_hash: int = 0,
        difference_hash: int = 0xF0F0F0F0,
    ) -> dict:
        return {
            "width": 100,
            "height": 80,
            "pixels": token * 8,
            "difference_hash": difference_hash,
            "perceptual_hash": perceptual_hash,
            "palette": [1 / 65] * 65,
            "capture_time": capture_time,
        }

    def _exif_blob(self, capture_time: str) -> bytes:
        """Minimal TIFF EXIF payload with DateTimeOriginal in the Exif sub-IFD."""
        data = capture_time.encode("ascii") + b"\x00"
        ifd0_offset = 8
        exif_ifd_offset = ifd0_offset + 18
        tiff = b"II" + struct.pack("<H", 42) + struct.pack("<I", ifd0_offset)
        ifd0 = struct.pack("<H", 1)
        ifd0 += struct.pack("<HHII", 34665, 4, 1, exif_ifd_offset)
        ifd0 += struct.pack("<I", 0)
        value_offset = exif_ifd_offset + 18
        exif_ifd = struct.pack("<H", 1)
        exif_ifd += struct.pack("<HHII", 36867, 2, len(data), value_offset)
        exif_ifd += struct.pack("<I", 0)
        return b"Exif\x00\x00" + tiff + ifd0 + exif_ifd + data

    def _save(self, filename: str, image: Image.Image) -> Path:
        path = self.root / filename
        image.save(path, quality=90)
        return path


if __name__ == "__main__":
    unittest.main()
