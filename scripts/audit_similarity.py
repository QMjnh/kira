"""Reproduce current matcher limitations without real photos or cloud requests.

Run from the repository root:
    python scripts/audit_similarity.py --report docs/similarity-audit-results.md

These are diagnostic probes, not accuracy measurements or a replacement matcher.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from kira import visual
from kira.google_photos_web import GooglePhotosWebService
from kira.media import media_identity, sha256_file


def generated_scene(size: tuple[int, int] = (800, 600)) -> Image.Image:
    """Seeded, textured illustration with varied local geometry and colors."""
    rng = random.Random(20261027)
    image = Image.new("RGB", size)
    width, height = size
    image.putdata([
        (int(35 + 140 * x / width), int(40 + 150 * y / height),
         int(130 + 70 * (x + y) / (width + height)))
        for y in range(height) for x in range(width)
    ])
    draw = ImageDraw.Draw(image)
    for _ in range(110):
        x, y = rng.randrange(width - 80), rng.randrange(height - 80)
        box = (x, y, x + rng.randrange(12, 90), y + rng.randrange(12, 90))
        color = tuple(rng.randrange(20, 236) for _ in range(3))
        if rng.random() < 0.5:
            draw.rectangle(box, fill=color)
        else:
            draw.ellipse(box, fill=color)
    draw.text((25, height - 35), "KIRA: deterministic synthetic probe", fill="white")
    return image


def candidate(signature: dict, name: str) -> dict:
    return {"signature": signature, "paths": [name], "name_keys": {Path(name).stem}}


def controlled_signature(token: bytes, phash: int, dhash: int,
                         palette: list[float] | None = None) -> dict:
    return {
        "width": 100, "height": 80, "pixels": token * 8,
        "perceptual_hash": phash, "difference_hash": dhash,
        "palette": palette if palette is not None else [1 / 65] * 65,
        "capture_time": None,
    }


class FakePayloads:
    @staticmethod
    def GetBatchMediaInfo(keys: list[str]) -> tuple:
        return ("metadata", keys)

    @staticmethod
    def GetItemInfo(key: str) -> SimpleNamespace:
        return SimpleNamespace(payload_id=key)


class FakeMetadataClient:
    """In-memory responses only; deliberately offers no network session."""
    def send_api_request(self, payload: object) -> object:
        if isinstance(payload, tuple):
            return SimpleNamespace(data=[SimpleNamespace(
                media_key="remote-1", file_name="renamed_export.jpg",
                timestamp=None, timezone_offset=None,
            )])
        return [SimpleNamespace(
            response_id=item.payload_id,
            data=SimpleNamespace(download_original_url="https://example.invalid/no-request"),
        ) for item in payload]


def run_audit(root: Path) -> dict:
    service = object.__new__(GooglePhotosWebService)
    base = generated_scene()
    original_path = root / "source.jpg"
    base.save(original_path, quality=95)
    original = visual.image_signature(original_path)
    watermark = base.copy()
    ImageDraw.Draw(watermark).rectangle((0, 480, 800, 600), fill="white")
    transformations = {
        "JPEG recompression": (base, 70),
        "Resize to 20%": (base.resize((160, 120)), 90),
        "Center crop retaining 75% width/height": (base.crop((100, 75, 700, 525)), 90),
        "Center crop retaining 50% width/height": (base.crop((200, 150, 600, 450)), 90),
        "Exposure 50%": (ImageEnhance.Brightness(base).enhance(0.5), 90),
        "Grayscale edit": (ImageOps.grayscale(base).convert("RGB"), 90),
        "Saturation 25%": (ImageEnhance.Color(base).enhance(0.25), 90),
        "Rotation 3 degrees": (base.rotate(3, resample=Image.Resampling.BICUBIC), 90),
        "Rotation 90 degrees": (base.transpose(Image.Transpose.ROTATE_90), 90),
        "Horizontal flip": (ImageOps.mirror(base), 90),
        "White overlay on bottom 20%": (watermark, 90),
    }
    variants = []
    variant_signatures = {}
    for index, (name, (image, quality)) in enumerate(transformations.items()):
        path = root / f"variant-{index}.jpg"
        image.save(path, quality=quality)
        signature = visual.image_signature(path)
        variant_signatures[name] = signature
        metrics = visual.similarity_metrics(original, signature)
        gate = visual.dimensions_compatible(
            (original["width"], original["height"]),
            (signature["width"], signature["height"]),
        )
        tier = visual.match_tier(metrics)
        pair_clustered = len(visual.cluster_signatures([
            ("original", original), ("variant", signature),
        ])) == 1
        variants.append({
            "transformation": name, "byte_equal": sha256_file(path) == sha256_file(original_path),
            "tier_if_compared": tier, "dimension_gate_allows": gate,
            "web_pair_eligible_with_metadata": bool(gate and tier),
            "compare_pair_clustered": pair_clustered,
            "metrics": {key: round(value, 4) for key, value in metrics.items()},
        })

    # Isolate the current hash implementation from image decoding/preprocessing.
    top = Image.new("L", (32, 32))
    top.putdata([(x * 7 + y * 3) % 256 for y in range(32) for x in range(32)])
    changed_bottom = top.copy()
    for y in range(8, 32):
        for x in range(32):
            changed_bottom.putpixel((x, y), 255 - top.getpixel((x, y)))
    hash_probe = {
        "changed_input_pixels": sum(a != b for a, b in zip(top.getdata(), changed_bottom.getdata())),
        "same_hash_after_changing_bottom_24_of_32_rows":
            visual._perceptual_hash(top) == visual._perceptual_hash(changed_bottom),
    }

    # The full candidate-resolution stage changes its fallback at >6 groups.
    narrowed_counts = {}
    distinct_groups = []
    for index in range(7):
        path = root / f"original-{index}.jpg"
        ImageEnhance.Brightness(base).enhance(1 + index * 0.03).save(path, quality=95)
        distinct_groups.append(candidate(visual.image_signature(path), path.name))
    assert len({group["signature"]["pixels"] for group in distinct_groups}) == 7
    for count in (6, 7):
        groups = distinct_groups[:count]
        resolved = service._resolve_visual_downloads(
            FakeMetadataClient(), FakePayloads,
            [({"media_key": "remote-1"}, groups)],
        )
        narrowed_counts[str(count)] = sum(len(groups) for _, groups in resolved)

    # Controlled signatures demonstrate connected-component behavior, not accuracy.
    a = controlled_signature(b"a", 0, 0)
    b = controlled_signature(b"b", (1 << 18) - 1, (1 << 64) - 1)
    c = controlled_signature(b"c", (1 << 36) - 1, (1 << 128) - 1)
    chain_probe = {
        "A_B_pass_pair_gate": visual._try_cluster_pair(a, b, None),
        "B_C_pass_pair_gate": visual._try_cluster_pair(b, c, None),
        "A_C_pass_pair_gate": visual._try_cluster_pair(a, c, None),
        "clusters": visual.cluster_signatures([("A", a), ("B", b), ("C", c)]),
    }

    # Exercise the edited-tier runner-up margin using actual generated images.
    remote_sig = original
    best = variant_signatures["Exposure 50%"]
    runner_up_path = root / "exposure-and-saturation.jpg"
    runner_up_image = ImageEnhance.Color(base).enhance(0.6)
    ImageEnhance.Brightness(runner_up_image).enhance(0.5).save(runner_up_path, quality=90)
    runner_up = visual.image_signature(runner_up_path)
    matches = {}
    service._record_visual_match(matches, {"media_key": "remote-1"}, [
        candidate(best, "best.jpg"), candidate(runner_up, "runner-up.jpg"),
    ], remote_sig)
    margin_probe = {
        "best_palette_similarity": visual.palette_similarity(remote_sig, best),
        "runner_up_palette_similarity": visual.palette_similarity(remote_sig, runner_up),
        "best_tier": visual.match_tier(visual.similarity_metrics(remote_sig, best)),
        "runner_up_tier": visual.match_tier(visual.similarity_metrics(remote_sig, runner_up)),
        "palette_gap": visual.palette_similarity(remote_sig, best) - visual.palette_similarity(remote_sig, runner_up),
        "perceptual_distance_gap": abs(visual.perceptual_hash_distance(remote_sig, best)
                                       - visual.perceptual_hash_distance(remote_sig, runner_up)),
        "returned_match_count": len(matches),
    }

    same_pixels = []
    for index in range(2):
        path = root / f"metadata-{index}.jpg"
        exif = Image.Exif()
        exif[0x010E] = f"distinct description {index}"
        base.save(path, quality=95, exif=exif)
        same_pixels.append(path)
    metadata_probe = {
        "sha256_equal": sha256_file(same_pixels[0]) == sha256_file(same_pixels[1]),
        "decoded_pixel_identity_equal": media_identity(same_pixels[0]) == media_identity(same_pixels[1]),
    }
    return {
        "variants": variants, "hash_probe": hash_probe,
        "renamed_missing_capture_candidate_counts": narrowed_counts,
        "transitive_cluster_probe": chain_probe,
        "edited_margin_probe": margin_probe,
        "metadata_probe": metadata_probe,
    }


def render_report(results: dict) -> str:
    lines = [
        "# Current similarity implementation: diagnostic results", "",
        "Run date: October 27, 2026. Application code is unchanged.", "",
        "Reproduce: `python scripts/audit_similarity.py --report docs/similarity-audit-results.md`.", "",
        "These are deterministic synthetic mechanism probes, not measured accuracy on real photography. "
        "No personal images, live accounts, or cloud requests are used. "
        "All variants below are deliberately derived from one generated source. "
        "The web eligibility column assumes metadata narrowing already retained the pair; "
        "it is not an end-to-end Google Photos result.", "",
        "| Known transformation | Byte equal | Pair tier if compared | Size gate allows | Web pair eligible | Culling pair grouped |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    def flag(value: bool) -> str:
        return "yes" if value else "no"
    for row in results["variants"]:
        lines.append("| " + " | ".join([
            row["transformation"], flag(row["byte_equal"]), row["tier_if_compared"] or "none",
            flag(row["dimension_gate_allows"]), flag(row["web_pair_eligible_with_metadata"]),
            flag(row["compare_pair_clustered"]),
        ]) + " |")
    lines.extend([
        "", "## Isolated mechanism probes", "",
        "- Hash coverage: " + json.dumps(results["hash_probe"], sort_keys=True) + ".",
        "- Candidate narrowing, retained local groups for a renamed remote file with missing capture time: "
        + json.dumps(results["renamed_missing_capture_candidate_counts"], sort_keys=True) + ".",
        "- Transitive grouping: " + json.dumps(results["transitive_cluster_probe"], sort_keys=True) + ".",
        "- Edited-tier ranking: " + json.dumps(results["edited_margin_probe"], sort_keys=True) + ".",
        "- Byte identity versus full decoded-pixel identity: "
        + json.dumps(results["metadata_probe"], sort_keys=True) + ".",
        "", "## Per-pair metrics", "",
        "| Transformation | Mean RGB difference | dHash distance | Current perceptual hash distance | Palette overlap |",
        "| --- | --- | --- | --- | --- |",
    ])
    for row in results["variants"]:
        m = row["metrics"]
        lines.append(f"| {row['transformation']} | {m['color']} | {m['difference_hash']} | "
                     f"{m['perceptual_hash']} | {m['palette']} |")
    lines.extend([
        "", "## Limits", "",
        "The probes establish reproducible failure mechanisms, not their frequency in a user's collection. "
        "A generated illustration cannot validate a replacement matcher, infer historical ancestry, "
        "or establish safe automatic association thresholds. Real export compatibility, supported formats, "
        "retrieval coverage, false positives, and user review effort still require representative evaluation.", "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="kira-similarity-audit-") as temporary:
        results = run_audit(Path(temporary))
    report = render_report(results)
    if args.report:
        args.report.write_text(report, encoding="utf-8", newline="\n")
    print(report)


if __name__ == "__main__":
    main()
