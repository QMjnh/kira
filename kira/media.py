"""Filesystem scanning, culling moves, and media identity for photo folders."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from functools import lru_cache
from http import HTTPStatus
from pathlib import Path

from PIL import Image, ImageOps

from .errors import KiraError

CHUNK_COPY_SIZE = 4 * 1024 * 1024
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
EDIT_SUFFIX = re.compile(
    r"(?:[\s_-]+(?:(?:edited?|edit|final|copy|lr|lightroom|google)(?:[\s_-]*v?\d+)?|v\d+)|\s*\(\d+\))$",
    re.IGNORECASE,
)
RAW_EXTENSIONS = {
    ".3fr", ".arw", ".cr2", ".cr3", ".dcr", ".dng", ".erf", ".fff",
    ".iiq", ".kdc", ".mef", ".mos", ".mrw", ".nef", ".nrw", ".orf",
    ".pef", ".raf", ".raw", ".rw2", ".rwl", ".sr2", ".srf", ".srw",
}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}
PREVIEW_EXTENSIONS = JPEG_EXTENSIONS | {".png", ".webp", ".avif"}
PHOTO_EXTENSIONS = RAW_EXTENSIONS | PREVIEW_EXTENSIONS | {".tif", ".tiff", ".jxl", ".heic", ".heif"}
VIDEO_EXTENSIONS = {
    ".3g2", ".3gp", ".asf", ".avi", ".divx", ".m2t", ".m2ts", ".m4v",
    ".mkv", ".mmv", ".mod", ".mov", ".mp4", ".mpg", ".mts", ".tod", ".wmv",
}
MEDIA_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def safe_filename(value: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip().rstrip(". ")
    name = INVALID_FILENAME.sub("_", name)
    if not name or name in {".", ".."}:
        name = "unnamed-file"
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


def unique_destination(folder: Path, filename: str) -> Path:
    candidate = folder / safe_filename(filename)
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    index = 2
    while True:
        versioned = folder / f"{stem}__v{index}{suffix}"
        if not versioned.exists():
            return versioned
        index += 1


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
    return _media_identity(
        str(path.resolve()), stat.st_size, stat.st_mtime_ns, path.suffix.casefold()
    )


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
            (child for child in current.iterdir() if child.is_dir()),
            key=lambda child: natural_key(child.name),
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
    return {
        "current": str(current),
        "parent": parent,
        "breadcrumbs": breadcrumbs,
        "directories": directories,
    }


def culling_context(current: Path) -> dict:
    role = "inbox"
    group_name = None
    if current.name.casefold() in {"select", "unselect"}:
        root = current.parent
        role = current.name.casefold()
    elif current.parent.name.casefold() == "compare_groups":
        root = current.parent.parent
        role = "group"
        group_name = current.name
    elif current.name.casefold() == "compare_groups":
        root = current.parent
        role = "groups"
    else:
        root = current

    folders = [{"name": "Inbox", "path": str(root), "role": "inbox"}]
    select = root / "select"
    unselect = root / "unselect"
    groups_root = root / "compare_groups"
    if select.is_dir():
        folders.append({"name": "Select", "path": str(select), "role": "select"})
    if unselect.is_dir():
        folders.append({"name": "Unselect", "path": str(unselect), "role": "unselect"})
    if groups_root.is_dir():
        try:
            groups = sorted(
                (child for child in groups_root.iterdir() if child.is_dir()),
                key=lambda child: natural_key(child.name),
            )
        except PermissionError:
            groups = []
        folders.extend(
            {
                "name": f"Compare: {group.name}",
                "group_name": group.name,
                "path": str(group),
                "role": "group",
            }
            for group in groups
        )
    return {
        "root": str(root),
        "role": role,
        "group_name": group_name,
        "folders": folders,
    }


def safe_group_name(value: str) -> str:
    cleaned = INVALID_FILENAME.sub("_", str(value).strip()).strip(" .")[:80]
    if not cleaned or cleaned in {".", ".."}:
        raise KiraError(HTTPStatus.BAD_REQUEST, "Enter a name for the comparison group")
    if cleaned.casefold() in {"select", "unselect", "compare_groups"}:
        raise KiraError(HTTPStatus.BAD_REQUEST, "Choose a more specific comparison group name")
    return cleaned


def choose_assets(source_directory: str, asset_ids: list[str], context_message: str) -> tuple[Path, list[dict]]:
    """Resolve a directory and the assets matching the given ids, or fail."""
    current = resolve_directory(source_directory)
    scan = scan_photo_directory(str(current))
    selected = {str(item) for item in asset_ids}
    chosen = [asset for asset in scan["assets"] if asset["id"] in selected]
    if not chosen:
        raise KiraError(HTTPStatus.BAD_REQUEST, "Select at least one photo")
    if len(chosen) != len(selected):
        raise KiraError(
            HTTPStatus.CONFLICT,
            f"The folder changed; scan it again before {context_message}",
        )
    return current, chosen


def move_culling_assets(
    source_directory: str,
    asset_ids: list[str],
    action: str,
    group_name: str = "",
) -> dict:
    current, chosen = choose_assets(source_directory, asset_ids, "moving photos")

    context = culling_context(current)
    root = Path(context["root"])
    if action == "select":
        destination = root / "select"
    elif action == "unselect":
        destination = root / "unselect"
    elif action == "restore":
        destination = root
    elif action == "group":
        destination = root / "compare_groups" / safe_group_name(group_name)
    else:
        raise KiraError(HTTPStatus.BAD_REQUEST, "Unknown culling action")
    destination.mkdir(parents=True, exist_ok=True)

    planned: list[tuple[Path, Path]] = []
    planned_targets: set[Path] = set()
    duplicate_files: list[dict[str, str]] = []
    renamed_files: list[dict[str, str]] = []
    destination_identities: dict[tuple[str, str], Path] = {}
    try:
        existing_files = [
            path
            for path in destination.iterdir()
            if path.is_file() and path.suffix.casefold() in MEDIA_EXTENSIONS
        ]
    except PermissionError as exc:
        raise KiraError(HTTPStatus.FORBIDDEN, "Windows denied access to the destination folder") from exc
    for path in existing_files:
        destination_identities.setdefault(media_identity(path), path)

    moved_assets = 0
    duplicate_assets = 0
    for asset in chosen:
        records = asset["raw_files"] + asset["jpeg_files"] + asset.get("video_files", []) + asset["other_files"]
        unique_records: list[tuple[dict, Path, tuple[str, str]]] = []
        for record in records:
            source = Path(record["path"])
            identity = media_identity(source)
            duplicate_of = destination_identities.get(identity)
            if duplicate_of is not None and source.resolve() != duplicate_of.resolve():
                duplicate_files.append({"source": str(source), "duplicate_of": str(duplicate_of)})
                continue
            unique_records.append((record, source, identity))

        if not unique_records:
            duplicate_assets += 1
            continue

        direct_targets = [destination / source.name for _, source, _ in unique_records]
        has_name_conflict = any(
            target.exists() and source.resolve() != target.resolve()
            for (_, source, _), target in zip(unique_records, direct_targets)
        )
        variant_index = 2
        targets = direct_targets
        if has_name_conflict:
            while True:
                targets = [
                    destination / f"{source.stem}__variant{variant_index}{source.suffix}"
                    for _, source, _ in unique_records
                ]
                if not any(target.exists() or target in planned_targets for target in targets):
                    break
                variant_index += 1

        asset_planned = False
        for (_, source, identity), target in zip(unique_records, targets):
            if source.resolve() == target.resolve():
                continue
            planned.append((source, target))
            planned_targets.add(target)
            destination_identities.setdefault(identity, target)
            asset_planned = True
            if target.name != source.name:
                renamed_files.append({"source": source.name, "destination": target.name})
        if asset_planned:
            moved_assets += 1

    moved: list[tuple[Path, Path]] = []
    deleted_duplicates: list[Path] = []
    try:
        for source, target in planned:
            shutil.move(str(source), str(target))
            moved.append((source, target))
        for duplicate in duplicate_files:
            source = Path(duplicate["source"])
            source.unlink()
            deleted_duplicates.append(source)
    except OSError as exc:
        for source, target in reversed(moved):
            try:
                if target.exists() and not source.exists():
                    shutil.move(str(target), str(source))
            except OSError:
                pass
        raise KiraError(HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not organize photo group: {exc}") from exc

    refresh_directory = current
    group_deleted = False
    if current.parent.name.casefold() == "compare_groups":
        try:
            group_is_empty = not any(current.iterdir())
        except OSError:
            group_is_empty = False
        if group_is_empty:
            current.rmdir()
            refresh_directory = root
            group_deleted = True

    refreshed = scan_photo_directory(str(refresh_directory))
    refreshed["culling"] = culling_context(refresh_directory)
    refreshed["moved_assets"] = moved_assets
    refreshed["moved_files"] = len(moved)
    refreshed["duplicate_assets"] = duplicate_assets
    refreshed["duplicate_files"] = duplicate_files
    refreshed["deleted_duplicate_files"] = len(deleted_duplicates)
    refreshed["renamed_files"] = renamed_files
    refreshed["destination"] = str(destination)
    refreshed["destination_group_name"] = destination.name if action == "group" else None
    refreshed["group_deleted"] = group_deleted
    return refreshed


def move_assets_between_directories(
    source_directory: str,
    destination_directory: str,
    asset_ids: list[str],
) -> dict:
    source, chosen = choose_assets(source_directory, asset_ids, "moving photos")
    destination = resolve_directory(destination_directory)
    if source == destination:
        raise KiraError(HTTPStatus.BAD_REQUEST, "Choose a different destination folder")

    planned: list[tuple[Path, Path]] = []
    for asset in chosen:
        records = asset["raw_files"] + asset["jpeg_files"] + asset.get("video_files", []) + asset["other_files"]
        for record in records:
            file_source = Path(record["path"])
            file_target = destination / file_source.name
            if file_source.resolve() == file_target.resolve():
                continue
            if file_target.exists():
                raise KiraError(
                    HTTPStatus.CONFLICT,
                    f"{file_target.name} already exists in {destination}. Nothing was moved.",
                )
            planned.append((file_source, file_target))

    moved: list[tuple[Path, Path]] = []
    try:
        for file_source, file_target in planned:
            shutil.move(str(file_source), str(file_target))
            moved.append((file_source, file_target))
    except OSError as exc:
        for file_source, file_target in reversed(moved):
            try:
                if file_target.exists() and not file_source.exists():
                    shutil.move(str(file_target), str(file_source))
            except OSError:
                pass
        raise KiraError(HTTPStatus.INTERNAL_SERVER_ERROR, f"Could not move photo group: {exc}") from exc

    return {
        "source": scan_photo_directory(str(source)),
        "destination": scan_photo_directory(str(destination)),
        "moved_assets": len(chosen),
        "moved_files": len(moved),
    }


def scan_photo_directory(value: str) -> dict:
    current = resolve_directory(value)
    groups: dict[str, dict] = {}
    try:
        files = sorted(
            (item for item in current.iterdir() if item.is_file() and item.suffix.casefold() in MEDIA_EXTENSIONS),
            key=lambda item: natural_key(item.name),
        )
    except PermissionError as exc:
        raise KiraError(HTTPStatus.FORBIDDEN, "Windows denied access to this folder") from exc

    for path in files:
        stem_key = match_key(path.name)
        group = groups.setdefault(
            stem_key,
            {
                "stem": path.stem,
                "raw_paths": [],
                "jpeg_paths": [],
                "video_paths": [],
                "preview_paths": [],
                "other_paths": [],
            },
        )
        suffix = path.suffix.casefold()
        if suffix in RAW_EXTENSIONS:
            group["raw_paths"].append(path)
        elif suffix in VIDEO_EXTENSIONS:
            group["video_paths"].append(path)
        elif suffix in JPEG_EXTENSIONS:
            group["jpeg_paths"].append(path)
            group["preview_paths"].append(path)
        elif suffix in PREVIEW_EXTENSIONS:
            group["preview_paths"].append(path)
            group["other_paths"].append(path)
        else:
            group["other_paths"].append(path)

    assets: list[dict] = []
    for group in groups.values():
        all_paths = group["raw_paths"] + group["jpeg_paths"] + group["video_paths"] + group["other_paths"]
        asset_id = hashlib.sha256(f"{current}\0{group['stem'].casefold()}".encode("utf-8")).hexdigest()[:16]
        preview = group["preview_paths"][0] if group["preview_paths"] else None
        file_stats = {path: path.stat() for path in all_paths}
        assets.append(
            {
                "id": asset_id,
                "stem": group["stem"],
                "raw_files": [{"filename": path.name, "path": str(path), "size": file_stats[path].st_size} for path in group["raw_paths"]],
                "jpeg_files": [{"filename": path.name, "path": str(path), "size": file_stats[path].st_size} for path in group["jpeg_paths"]],
                "video_files": [{"filename": path.name, "path": str(path), "size": file_stats[path].st_size} for path in group["video_paths"]],
                "other_files": [{"filename": path.name, "path": str(path), "size": file_stats[path].st_size} for path in group["other_paths"]],
                "preview_path": str(preview) if preview else None,
                "preview_version": file_stats[preview].st_mtime_ns if preview else None,
                "total_size": sum(file_stats[path].st_size for path in all_paths),
            }
        )
    assets.sort(key=lambda asset: natural_key(asset["stem"]))
    return {"directory": str(current), "assets": assets, "culling": culling_context(current)}
