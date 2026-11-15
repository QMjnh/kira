"""File types, safe names, folder browsing, and media identity shared across Kira."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from functools import lru_cache
from http import HTTPStatus
from pathlib import Path

from PIL import Image, ImageOps

from .errors import KiraError

CHUNK_COPY_SIZE = 4 * 1024 * 1024
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
EDIT_SUFFIX = re.compile(
    r"(?:[\s_-]+(?:(?:edited?|edit|final|copy|lr|lightroom|google)(?:[\s_-]*v?\d+)?|v\d+)|\s*\(\d+\))$", re.IGNORECASE
)
RAW_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".cr2",
    ".cr3",
    ".dcr",
    ".dng",
    ".erf",
    ".fff",
    ".iiq",
    ".kdc",
    ".mef",
    ".mos",
    ".mrw",
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".raf",
    ".raw",
    ".rw2",
    ".rwl",
    ".sr2",
    ".srf",
    ".srw",
}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}
PREVIEW_EXTENSIONS = JPEG_EXTENSIONS | {".png", ".webp", ".avif"}
PHOTO_EXTENSIONS = RAW_EXTENSIONS | PREVIEW_EXTENSIONS | {".tif", ".tiff", ".jxl", ".heic", ".heif"}
VIDEO_EXTENSIONS = {
    ".3g2",
    ".3gp",
    ".asf",
    ".avi",
    ".divx",
    ".m2t",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mmv",
    ".mod",
    ".mov",
    ".mp4",
    ".mpg",
    ".mts",
    ".tod",
    ".wmv",
}
MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS
SIDECAR_EXTENSIONS = {".xmp", ".aae"}
PROJECT_EXTENSIONS = {".psd", ".psb", ".xcf", ".kra", ".ora", ".afphoto"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def safe_filename(value: str, fallback: str = "unnamed-file") -> str:
    name = Path(value.replace("\\", "/")).name.strip().rstrip(". ")
    name = INVALID_FILENAME.sub("_", name)
    if not name or name in {".", ".."}:
        name = fallback
    stem = Path(name).stem[:150]
    suffix = Path(name).suffix[:20]
    return f"{stem}{suffix}"


def safe_download_name(value: str) -> str:
    return safe_filename(value).encode("ascii", "ignore").decode("ascii") or "download"


def match_key(filename: str) -> str:
    stem = Path(filename).stem.strip()
    previous = None
    while stem != previous:
        previous = stem
        stem = EDIT_SUFFIX.sub("", stem).strip(" _-")
    return stem.casefold()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_COPY_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=4096)
def _media_identity(resolved: str, size: int, mtime_ns: int, suffix: str) -> tuple[str, str]:
    path = Path(resolved)
    identity: tuple[str, str] | None = None
    if suffix in PHOTO_EXTENSIONS:
        try:
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened)
                mode = "RGBA" if "A" in image.getbands() else "RGB"
                converted = image.convert(mode)
                digest = hashlib.sha256()
                digest.update(f"{converted.width}x{converted.height}\0{mode}\0".encode("ascii"))
                for top in range(0, converted.height, 256):
                    strip = converted.crop((0, top, converted.width, min(top + 256, converted.height)))
                    digest.update(strip.tobytes())
                identity = ("pixels", digest.hexdigest())
        except (OSError, ValueError):
            pass
    if identity is None:
        identity = ("bytes", sha256_file(path))
    return identity


def media_identity(path: Path) -> tuple[str, str]:
    """Return an exact visual identity for photos and a byte identity otherwise.

    Decoding the pixels deliberately ignores filenames, JPEG encoding, and metadata.
    This catches visually identical Google Photos downloads while keeping any image
    whose pixels were actually edited. RAW files and videos use exact file bytes.
    """
    stat = path.stat()
    return _media_identity(str(path.resolve()), stat.st_size, stat.st_mtime_ns, path.suffix.casefold())


def natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def available_roots() -> list[dict[str, str]]:
    if os.name == "nt":
        roots = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            path = Path(f"{letter}:\\")
            if path.exists():
                roots.append({"name": f"{letter}: drive", "path": str(path)})
        return roots
    return [{"name": "/", "path": "/"}]


def resolve_directory(value: str) -> Path:
    if not value:
        raise KiraError(HTTPStatus.BAD_REQUEST, "A folder path is required")
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise KiraError(HTTPStatus.NOT_FOUND, "Folder not found") from exc
    if not path.is_dir():
        raise KiraError(HTTPStatus.BAD_REQUEST, "Path is not a folder")
    return path


def browse_directories(value: str) -> dict:
    if not value:
        return {"current": "", "parent": None, "breadcrumbs": [], "directories": available_roots()}
    current = resolve_directory(value)
    directories: list[dict[str, str]] = []
    try:
        children = sorted(
            (child for child in current.iterdir() if child.is_dir()), key=lambda child: natural_key(child.name)
        )
        for child in children:
            try:
                directories.append({"name": child.name, "path": str(child.resolve())})
            except OSError:
                continue
    except PermissionError as exc:
        raise KiraError(HTTPStatus.FORBIDDEN, "Windows denied access to this folder") from exc
    parent = None if current.parent == current else str(current.parent)
    breadcrumbs: list[dict[str, str]] = []
    parts = current.parts
    for index, part in enumerate(parts):
        target = Path(*parts[: index + 1])
        label = part.rstrip("\\/") or part
        breadcrumbs.append({"name": label, "path": str(target)})
    return {"current": str(current), "parent": parent, "breadcrumbs": breadcrumbs, "directories": directories}


def clean_folder_name(value: str) -> str:
    name = INVALID_FILENAME.sub("_", str(value).strip()).rstrip(" .")
    if not name or name in {".", ".."}:
        raise KiraError(HTTPStatus.BAD_REQUEST, "Enter a folder name")
    reserved = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
    if name.split(".")[0].casefold() in reserved:
        raise KiraError(HTTPStatus.BAD_REQUEST, f"{name} is a reserved Windows device name")
    if len(name) > 120:
        raise KiraError(HTTPStatus.BAD_REQUEST, "Folder names are limited to 120 characters")
    return name


def reveal_directory(path_value: str) -> None:
    current = resolve_directory(path_value)
    if os.name != "nt":
        raise KiraError(HTTPStatus.BAD_REQUEST, "Opening File Explorer requires Windows")
    os.startfile(str(current))  # noqa: S606 - user-requested Explorer reveal
