from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qs, quote, unquote, urlparse

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if VENDOR_DIR.exists():
    sys.path.insert(0, str(VENDOR_DIR))

import qrcode
import qrcode.image.svg
from google_photos import GooglePhotosError, GooglePhotosService
try:
    from PIL import Image, ImageOps
except ImportError:  # Kira can still run, but previews fall back to full JPEGs.
    Image = None
    ImageOps = None


APP_NAME = "Kira"
APP_VERSION = "0.9.6"
CHUNK_COPY_SIZE = 4 * 1024 * 1024
MAX_JSON_BODY = 1024 * 1024
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
    cleaned = safe_filename(value)
    ascii_name = cleaned.encode("ascii", "ignore").decode("ascii") or "download"
    return ascii_name.replace('"', "_")


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
    if Image is not None and suffix in PHOTO_EXTENSIONS:
        try:
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened) if ImageOps is not None else opened.copy()
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


def move_culling_assets(
    source_directory: str,
    asset_ids: list[str],
    action: str,
    group_name: str = "",
) -> dict:
    current = resolve_directory(source_directory)
    scan = scan_photo_directory(str(current))
    selected = {str(asset_id) for asset_id in asset_ids}
    chosen = [asset for asset in scan["assets"] if asset["id"] in selected]
    if not chosen:
        raise KiraError(HTTPStatus.BAD_REQUEST, "Select at least one photo")
    if len(chosen) != len(selected):
        raise KiraError(HTTPStatus.CONFLICT, "The folder changed; scan it again before moving photos")

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
    source = resolve_directory(source_directory)
    destination = resolve_directory(destination_directory)
    if source == destination:
        raise KiraError(HTTPStatus.BAD_REQUEST, "Choose a different destination folder")

    scan = scan_photo_directory(str(source))
    selected = {str(asset_id) for asset_id in asset_ids}
    chosen = [asset for asset in scan["assets"] if asset["id"] in selected]
    if not chosen:
        raise KiraError(HTTPStatus.BAD_REQUEST, "Select at least one photo")
    if len(chosen) != len(selected):
        raise KiraError(HTTPStatus.CONFLICT, "The folder changed; scan it again before moving photos")

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


def discover_local_ip() -> str:
    candidates: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # A public destination makes Windows choose the real default-route
            # adapter (usually Wi-Fi), instead of a Hyper-V/WSL virtual switch.
            # UDP connect does not send data and does not require internet access.
            probe.connect(("8.8.8.8", 80))
            candidates.append(probe.getsockname()[0])
    except OSError:
        pass

    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidates.append(result[4][0])
    except OSError:
        pass

    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.is_private and not address.is_loopback and not address.is_link_local:
            return candidate
    return "127.0.0.1"


class KiraError(Exception):
    def __init__(self, status: int, message: str, **extra: object) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


class KiraStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.jobs_root = self.root / "jobs"
        self.thumbnails_root = self.root / "thumbnails"
        self.config_path = self.root / "config.json"
        self.lock = threading.RLock()
        self.bundle_lock = threading.Lock()
        self.thumbnail_slots = threading.Semaphore(2)
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.thumbnails_root.mkdir(parents=True, exist_ok=True)
        self.config = self._load_config()
        self.pair_code = f"{secrets.randbelow(1_000_000):06d}"
        self.pair_secret = secrets.token_urlsafe(24)

    @property
    def token(self) -> str:
        return str(self.config["token"])

    def _load_config(self) -> dict:
        if self.config_path.exists():
            try:
                payload = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(payload.get("token"), str) and len(payload["token"]) >= 24:
                    return payload
            except (OSError, json.JSONDecodeError):
                pass
        payload = {"token": secrets.token_urlsafe(32), "created_at": utc_now()}
        atomic_json_write(self.config_path, payload)
        return payload

    def _job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", job_id):
            raise KiraError(HTTPStatus.NOT_FOUND, "Job not found")
        return self.jobs_root / job_id

    def _manifest_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "manifest.json"

    def _load_manifest(self, job_id: str) -> dict:
        path = self._manifest_path(job_id)
        if not path.exists():
            raise KiraError(HTTPStatus.NOT_FOUND, "Job not found")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KiraError(HTTPStatus.INTERNAL_SERVER_ERROR, "Job manifest is unreadable") from exc

    def _save_manifest(self, manifest: dict) -> None:
        manifest["updated_at"] = utc_now()
        atomic_json_write(self._manifest_path(manifest["id"]), manifest)

    def preview_thumbnail(self, source: Path, requested_size: int) -> Path:
        if Image is None or ImageOps is None:
            return source
        size = 1600 if requested_size > 640 else 480
        stat = source.stat()
        cache_key = hashlib.sha256(
            f"{source.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}\0{size}".encode("utf-8")
        ).hexdigest()
        destination = self.thumbnails_root / f"{cache_key}.jpg"
        if destination.exists():
            return destination
        with self.thumbnail_slots:
            if destination.exists():
                return destination
            temp = destination.with_name(f"{destination.stem}-{threading.get_ident()}.tmp")
            try:
                with Image.open(source) as image:
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((size, size), Image.Resampling.LANCZOS)
                    if image.mode != "RGB":
                        image = image.convert("RGB")
                    image.save(temp, format="JPEG", quality=82)
                os.replace(temp, destination)
            finally:
                if temp.exists():
                    temp.unlink()
        return destination

    def create_job(self, name: str) -> dict:
        clean_name = str(name).strip()[:120]
        if not clean_name:
            raise KiraError(HTTPStatus.BAD_REQUEST, "A job name is required")
        with self.lock:
            while True:
                job_id = uuid.uuid4().hex[:12]
                folder = self._job_dir(job_id)
                if not folder.exists():
                    break
            for child in ("originals", "returns", "uploads", ".cache"):
                (folder / child).mkdir(parents=True, exist_ok=True)
            timestamp = utc_now()
            manifest = {
                "id": job_id,
                "name": clean_name,
                "created_at": timestamp,
                "updated_at": timestamp,
                "files": [],
                "returns": [],
            }
            self._save_manifest(manifest)
            return self.job_summary(manifest)

    def job_summary(self, manifest: dict) -> dict:
        matched = sum(1 for item in manifest["returns"] if item.get("match_status") == "matched")
        return {
            "id": manifest["id"],
            "name": manifest["name"],
            "created_at": manifest["created_at"],
            "updated_at": manifest["updated_at"],
            "file_count": len(manifest["files"]),
            "return_count": len(manifest["returns"]),
            "matched_count": matched,
            "postprocess_status": manifest.get("postprocess", {}).get("status", "not_available"),
        }

    def list_jobs(self) -> list[dict]:
        jobs: list[dict] = []
        with self.lock:
            for path in self.jobs_root.glob("*/manifest.json"):
                try:
                    manifest = json.loads(path.read_text(encoding="utf-8"))
                    jobs.append(self.job_summary(manifest))
                except (OSError, json.JSONDecodeError, KeyError, TypeError):
                    continue
        return sorted(jobs, key=lambda item: item["created_at"], reverse=True)

    def get_job(self, job_id: str) -> dict:
        with self.lock:
            return self._load_manifest(job_id)

    def create_job_from_selection(
        self,
        name: str,
        source_directory: str,
        selected_ids: list[str],
        source_format: str,
    ) -> dict:
        if source_format not in {"raw", "jpeg"}:
            raise KiraError(HTTPStatus.BAD_REQUEST, "Choose RAW or JPEG as the edit source")
        scan = scan_photo_directory(source_directory)
        selected = {str(item) for item in selected_ids}
        chosen = [asset for asset in scan["assets"] if asset["id"] in selected]
        if not chosen:
            raise KiraError(HTTPStatus.BAD_REQUEST, "Select at least one photo")
        if len(chosen) != len(selected):
            raise KiraError(HTTPStatus.CONFLICT, "The folder changed; scan it again before creating the job")

        summary = self.create_job(name)
        job_id = summary["id"]
        with self.lock:
            manifest = self._load_manifest(job_id)
            manifest["source_directory"] = scan["directory"]
            manifest["edit_source_format"] = source_format
            manifest["postprocess"] = {"status": "pending"}
            manifest["assets"] = []
            for asset in scan["assets"]:
                stored_asset = {
                    "id": asset["id"],
                    "stem": asset["stem"],
                    "selected": asset["id"] in selected,
                    "raw_files": asset["raw_files"],
                    "jpeg_files": asset["jpeg_files"],
                    "video_files": asset.get("video_files", []),
                    "other_files": asset["other_files"],
                }
                manifest["assets"].append(stored_asset)
                if not stored_asset["selected"]:
                    continue
                photo_candidates = asset["raw_files"] if source_format == "raw" else asset["jpeg_files"]
                has_photo = bool(asset["raw_files"] or asset["jpeg_files"])
                if has_photo and not photo_candidates:
                    shutil.rmtree(self._job_dir(job_id))
                    label = "RAW" if source_format == "raw" else "JPEG"
                    raise KiraError(
                        HTTPStatus.BAD_REQUEST,
                        f"{asset['stem']} does not have a {label} file. Change the edit source or deselect it.",
                    )
                sources = ([photo_candidates[0]] if photo_candidates else []) + asset.get("video_files", [])
                if not sources:
                    shutil.rmtree(self._job_dir(job_id))
                    raise KiraError(HTTPStatus.BAD_REQUEST, f"{asset['stem']} has no transferable media")
                stored_asset["edit_file_ids"] = []
                for candidate in sources:
                    source = Path(candidate["path"])
                    record = {
                        "id": uuid.uuid4().hex[:12],
                        "asset_id": asset["id"],
                        "filename": source.name,
                        "original_filename": source.name,
                        "source_path": str(source),
                        "referenced": True,
                        "size": source.stat().st_size,
                        # Hash while building the transfer ZIP so large sources are
                        # read once instead of once here and again for packaging.
                        "sha256": None,
                        "created_at": utc_now(),
                    }
                    manifest["files"].append(record)
                    stored_asset["edit_file_ids"].append(record["id"])
                stored_asset["edit_file_id"] = stored_asset["edit_file_ids"][0]
            self._save_manifest(manifest)
            return self.job_summary(manifest)

    def start_upload(
        self,
        job_id: str,
        kind: str,
        filename: str,
        size: int,
        last_modified: int,
    ) -> dict:
        if kind != "returns":
            raise KiraError(HTTPStatus.BAD_REQUEST, "Upload kind must be returns")
        if size < 0:
            raise KiraError(HTTPStatus.BAD_REQUEST, "File size is invalid")
        self._load_manifest(job_id)
        clean_name = safe_filename(filename)
        resume_source = f"{job_id}\0{kind}\0{clean_name}\0{size}\0{last_modified}"
        upload_id = hashlib.sha256(resume_source.encode("utf-8")).hexdigest()[:24]
        uploads = self._job_dir(job_id) / "uploads"
        metadata_path = uploads / f"{upload_id}.json"
        part_path = uploads / f"{upload_id}.part"

        with self.lock:
            if metadata_path.exists():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    metadata = {}
                if metadata.get("status") == "complete":
                    return {
                        "upload_id": upload_id,
                        "offset": size,
                        "size": size,
                        "complete": True,
                        "record": metadata.get("record"),
                    }
            metadata = {
                "id": upload_id,
                "job_id": job_id,
                "kind": kind,
                "filename": clean_name,
                "size": size,
                "last_modified": last_modified,
                "created_at": utc_now(),
                "status": "uploading",
            }
            if part_path.exists() and part_path.stat().st_size > size:
                part_path.unlink()
            if size == 0 and not part_path.exists():
                part_path.touch()
            offset = part_path.stat().st_size if part_path.exists() else 0
            atomic_json_write(metadata_path, metadata)
        return {"upload_id": upload_id, "offset": offset, "size": size, "complete": False}

    def append_upload(
        self,
        job_id: str,
        upload_id: str,
        offset: int,
        content_length: int,
        source: BinaryIO,
    ) -> dict:
        if not re.fullmatch(r"[a-f0-9]{24}", upload_id):
            raise KiraError(HTTPStatus.NOT_FOUND, "Upload not found")
        uploads = self._job_dir(job_id) / "uploads"
        metadata_path = uploads / f"{upload_id}.json"
        part_path = uploads / f"{upload_id}.part"
        if not metadata_path.exists():
            raise KiraError(HTTPStatus.NOT_FOUND, "Upload not found")

        with self.lock:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            expected_size = int(metadata["size"])
            current = part_path.stat().st_size if part_path.exists() else 0
            if offset != current:
                raise KiraError(HTTPStatus.CONFLICT, "Upload offset changed", expected_offset=current)
            if current + content_length > expected_size:
                raise KiraError(HTTPStatus.BAD_REQUEST, "Chunk exceeds declared file size")
            remaining = content_length
            with part_path.open("ab") as destination:
                while remaining:
                    chunk = source.read(min(CHUNK_COPY_SIZE, remaining))
                    if not chunk:
                        raise KiraError(HTTPStatus.BAD_REQUEST, "Upload ended before the chunk was complete")
                    destination.write(chunk)
                    remaining -= len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            new_offset = part_path.stat().st_size
        return {"upload_id": upload_id, "offset": new_offset, "size": expected_size}

    def complete_upload(self, job_id: str, upload_id: str) -> dict:
        uploads = self._job_dir(job_id) / "uploads"
        metadata_path = uploads / f"{upload_id}.json"
        part_path = uploads / f"{upload_id}.part"
        if not metadata_path.exists():
            raise KiraError(HTTPStatus.NOT_FOUND, "Upload not found")

        with self.lock:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("status") == "complete":
                return metadata["record"]
            expected_size = int(metadata["size"])
            actual_size = part_path.stat().st_size if part_path.exists() else 0
            if actual_size != expected_size:
                raise KiraError(
                    HTTPStatus.CONFLICT,
                    "Upload is incomplete",
                    expected_size=expected_size,
                    actual_size=actual_size,
                )

            digest = hashlib.sha256()
            with part_path.open("rb") as handle:
                while chunk := handle.read(CHUNK_COPY_SIZE):
                    digest.update(chunk)

            manifest = self._load_manifest(job_id)
            if manifest.get("source_directory"):
                source_root = resolve_directory(str(manifest["source_directory"]))
                edited_folder = source_root / "selected"
                if Path(metadata["filename"]).suffix.casefold() == ".zip":
                    folder = edited_folder / ".kira-incoming"
                else:
                    folder = edited_folder
                folder.mkdir(parents=True, exist_ok=True)
            else:
                folder = self._job_dir(job_id) / "returns"
                folder.mkdir(parents=True, exist_ok=True)
            destination = unique_destination(folder, metadata["filename"])
            os.replace(part_path, destination)
            record = {
                "id": uuid.uuid4().hex[:12],
                "filename": destination.name,
                "original_filename": metadata["filename"],
                "size": actual_size,
                "sha256": digest.hexdigest(),
                "storage_path": str(destination),
                "created_at": utc_now(),
            }

            self._attach_return_match(manifest, record)
            manifest["returns"].append(record)
            if manifest.get("source_directory"):
                manifest["postprocess"] = {"status": "pending"}
            self._save_manifest(manifest)
            metadata["status"] = "complete"
            metadata["record"] = record
            atomic_json_write(metadata_path, metadata)
            return record

    def _attach_return_match(self, manifest: dict, returned: dict) -> None:
        key = match_key(returned["original_filename"])
        matches = [item for item in manifest["files"] if match_key(item["original_filename"]) == key]
        if len(matches) == 1:
            returned["match_status"] = "matched"
            returned["matched_file_id"] = matches[0]["id"]
            returned["matched_filename"] = matches[0]["filename"]
        elif len(matches) > 1:
            returned["match_status"] = "ambiguous"
            returned["candidate_file_ids"] = [item["id"] for item in matches]
        else:
            returned["match_status"] = "unmatched"

    def resolve_file(self, job_id: str, kind: str, record_id: str) -> tuple[Path, dict]:
        manifest = self._load_manifest(job_id)
        collection_name = "files" if kind == "originals" else "returns"
        for record in manifest[collection_name]:
            if record["id"] == record_id:
                stored_path = record["source_path"] if kind == "originals" else record["storage_path"]
                path = Path(stored_path)
                if not path.exists():
                    raise KiraError(HTTPStatus.NOT_FOUND, "File is missing from disk")
                return path, record
        raise KiraError(HTTPStatus.NOT_FOUND, "File not found")

    def create_bundle(self, job_id: str) -> tuple[Path, str]:
        # Package one bundle at a time so the hard drive stays responsive, but
        # do not hold the manifest lock during multi-gigabyte sequential I/O.
        with self.bundle_lock:
            with self.lock:
                manifest = self._load_manifest(job_id)
                if not manifest["files"]:
                    raise KiraError(HTTPStatus.BAD_REQUEST, "This job has no files")
                cache_path = self._job_dir(job_id) / ".cache" / "originals.zip"
            sources: list[tuple[dict, Path, os.stat_result]] = []
            signature_parts: list[str] = []
            for record in manifest["files"]:
                source = Path(record["source_path"])
                if not source.exists():
                    raise KiraError(HTTPStatus.NOT_FOUND, f"Source file is missing: {record['filename']}")
                stat = source.stat()
                sources.append((record, source, stat))
                signature_parts.append(f"{source.resolve()}\0{record['filename']}\0{stat.st_size}\0{stat.st_mtime_ns}")
            signature = hashlib.sha256("\n".join(signature_parts).encode("utf-8")).hexdigest()
            if not cache_path.exists() or manifest.get("bundle_signature") != signature:
                temp = cache_path.with_suffix(".zip.tmp")
                with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                    for record, source, _ in sources:
                        digest = hashlib.sha256()
                        info = zipfile.ZipInfo.from_file(source, arcname=record["filename"])
                        info.compress_type = zipfile.ZIP_STORED
                        with source.open("rb") as incoming, archive.open(info, "w", force_zip64=True) as outgoing:
                            while chunk := incoming.read(CHUNK_COPY_SIZE):
                                digest.update(chunk)
                                outgoing.write(chunk)
                        record["sha256"] = digest.hexdigest()
                os.replace(temp, cache_path)
                manifest["bundle_signature"] = signature
                manifest["bundle_verified_at"] = utc_now()
                with self.lock:
                    self._save_manifest(manifest)
            return cache_path, f"{safe_filename(manifest['name'])}-originals.zip"

    def organize_source_folder(self, job_id: str) -> dict:
        with self.lock:
            manifest = self._load_manifest(job_id)
            source_value = manifest.get("source_directory")
            if not source_value:
                raise KiraError(
                    HTTPStatus.BAD_REQUEST,
                    "This older job was not created from a browsed source folder",
                )
            source_root = resolve_directory(str(source_value))
            previous = manifest.get("postprocess", {})
            if previous.get("status") == "complete":
                report_path = Path(str(previous.get("report_path", "")))
                if report_path.exists():
                    return previous

            destinations = {
                "raw": source_root / "selected" / "raw",
                "unselected_jpeg": source_root / "unselected_jpeg",
                "pre_edit": source_root / "selected" / "pre-edit",
                "edited": source_root / "selected",
            }
            for folder in destinations.values():
                folder.mkdir(parents=True, exist_ok=True)

            moved: list[dict] = []
            copied: list[dict] = []
            errors: list[str] = []
            moved_paths: dict[str, str] = {}

            def move_source_item(item: dict, destination_folder: Path) -> None:
                source = Path(str(item.get("path", "")))
                expected = destination_folder / safe_filename(str(item.get("filename", source.name)))
                try:
                    if source.exists() and source.resolve() == expected.resolve():
                        item["path"] = str(source)
                        return
                    if not source.exists() and expected.exists():
                        item["path"] = str(expected)
                        return
                    if not source.exists():
                        errors.append(f"Missing source file: {source}")
                        return
                    destination = unique_destination(destination_folder, item.get("filename", source.name))
                    shutil.move(str(source), str(destination))
                    item["path"] = str(destination)
                    item["filename"] = destination.name
                    moved_paths[str(source)] = str(destination)
                    moved.append({"from": str(source), "to": str(destination)})
                except OSError as exc:
                    errors.append(f"Could not move {source}: {exc}")

            for asset in manifest.get("assets", []):
                for item in asset.get("raw_files", []):
                    move_source_item(item, destinations["raw"])
                jpeg_destination = destinations["pre_edit"] if asset.get("selected") else destinations["unselected_jpeg"]
                for item in asset.get("jpeg_files", []):
                    move_source_item(item, jpeg_destination)

            # Selected originals are references to the source files, not copies.
            # Keep those references usable after the in-place organization move.
            for record in manifest.get("files", []):
                source_path = str(record.get("source_path", ""))
                if source_path in moved_paths:
                    record["source_path"] = moved_paths[source_path]
                    record["filename"] = Path(moved_paths[source_path]).name

            for record in manifest.get("returns", []):
                existing = [Path(value) for value in record.get("organized_paths", [])]
                if existing and all(path.exists() for path in existing):
                    if all(path.parent.resolve() == destinations["edited"].resolve() for path in existing):
                        continue
                    migrated: list[str] = []
                    for old_path in existing:
                        destination = unique_destination(destinations["edited"], old_path.name)
                        shutil.move(str(old_path), str(destination))
                        migrated.append(str(destination))
                        moved.append({"from": str(old_path), "to": str(destination)})
                    record["organized_paths"] = migrated
                    record["storage_path"] = migrated[0] if len(migrated) == 1 else record.get("storage_path")
                    continue
                stored_path = record.get("storage_path")
                source = Path(str(stored_path)) if stored_path else self._job_dir(job_id) / "returns" / record["filename"]
                organized_paths: list[str] = []
                try:
                    if source.suffix.casefold() == ".zip":
                        if not source.exists():
                            errors.append(f"Missing returned edit archive: {source}")
                            continue
                        with zipfile.ZipFile(source) as archive:
                            for member in archive.infolist():
                                if member.is_dir():
                                    continue
                                filename = safe_filename(Path(member.filename.replace("\\", "/")).name)
                                if Path(filename).suffix.casefold() not in PHOTO_EXTENSIONS:
                                    continue
                                destination = unique_destination(destinations["edited"], filename)
                                with archive.open(member) as incoming, destination.open("wb") as outgoing:
                                    shutil.copyfileobj(incoming, outgoing, length=CHUNK_COPY_SIZE)
                                organized_paths.append(str(destination))
                                copied.append({"from": f"{source}!/{member.filename}", "to": str(destination)})
                        # The archive is only an upload staging file. Its extracted
                        # edits now live directly in the selected folder.
                        source.unlink()
                        incoming_folder = source.parent
                        if incoming_folder.name == ".kira-incoming" and not any(incoming_folder.iterdir()):
                            incoming_folder.rmdir()
                    else:
                        if not source.exists():
                            errors.append(f"Missing returned edit: {source}")
                            continue
                        # New jobs place individual Lightroom exports directly in
                        # the final edited folder when their upload completes.
                        if source.parent.resolve() == destinations["edited"].resolve():
                            organized_paths.append(str(source))
                        else:
                            destination = unique_destination(destinations["edited"], record["filename"])
                            try:
                                inside_source = source.resolve().is_relative_to(source_root.resolve())
                            except (OSError, RuntimeError):
                                inside_source = False
                            if inside_source:
                                shutil.move(str(source), str(destination))
                                record["storage_path"] = str(destination)
                                moved.append({"from": str(source), "to": str(destination)})
                            else:
                                shutil.copy2(source, destination)
                                copied.append({"from": str(source), "to": str(destination)})
                            organized_paths.append(str(destination))
                    record["organized_paths"] = organized_paths
                except (OSError, zipfile.BadZipFile) as exc:
                    errors.append(f"Could not process returned file {source}: {exc}")

            completed_at = utc_now()
            report_path = source_root / f"KIRA-ORGANIZED-{job_id}.json"
            report = {
                "kira_job_id": job_id,
                "job_name": manifest["name"],
                "completed_at": completed_at,
                "source_directory": str(source_root),
                "folders": {key: str(value) for key, value in destinations.items()},
                "moved": moved,
                "copied": copied,
                "errors": errors,
            }
            atomic_json_write(report_path, report)
            result = {
                "status": "complete" if not errors else "complete_with_warnings",
                "completed_at": completed_at,
                "source_directory": str(source_root),
                "report_path": str(report_path),
                "moved_count": len(moved),
                "copied_count": len(copied),
                "errors": errors,
            }
            manifest["postprocess"] = result
            self._save_manifest(manifest)
            return result

    def delete_job(self, job_id: str) -> dict:
        # Wait for an in-progress bundle build so its temporary file is never
        # removed underneath the sequential packaging pass.
        with self.bundle_lock:
            with self.lock:
                folder = self._job_dir(job_id)
                manifest = self._load_manifest(job_id)
                preserved = manifest.get("source_directory")
                shutil.rmtree(folder)
                return {"deleted": True, "job_id": job_id, "preserved_source_directory": preserved}


class KiraHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: KiraStore, static_root: Path) -> None:
        self.store = store
        self.static_root = static_root
        self.google_photos = GooglePhotosService(store.root, static_root.parent)
        super().__init__(address, KiraRequestHandler)


class KiraRequestHandler(BaseHTTPRequestHandler):
    server: KiraHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.client_address[0]} {format % args}")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/" and method == "GET" and "state" in query and ("code" in query or "error" in query):
                self._google_oauth_callback(query)
                return
            if path in {"/", "/index.html", "/app.js", "/styles.css"} and method in {"GET", "HEAD"}:
                self._serve_static(path, method == "HEAD")
                return
            if path == "/api/system" and method == "GET":
                self._send_json({"name": APP_NAME, "version": APP_VERSION})
                return
            if path == "/api/bootstrap" and method == "GET":
                self._bootstrap()
                return
            if path == "/api/pair-qr.svg" and method in {"GET", "HEAD"}:
                self._pair_qr(method == "HEAD")
                return
            if path == "/api/pair" and method == "POST":
                self._pair()
                return

            self._require_auth(query)
            segments = [unquote(segment) for segment in path.strip("/").split("/")]
            if segments[:2] == ["api", "google"]:
                self._require_local()
                if segments == ["api", "google", "status"] and method == "GET":
                    self._send_json(self.server.google_photos.status())
                    return
                if segments == ["api", "google", "oauth", "start"] and method == "POST":
                    self._discard_optional_body()
                    port = self.server.server_address[1]
                    redirect_uri = f"http://127.0.0.1:{port}"
                    self._send_json(self.server.google_photos.start_oauth(redirect_uri))
                    return
                if segments == ["api", "google", "disconnect"] and method == "POST":
                    self._discard_optional_body()
                    self._send_json(self.server.google_photos.disconnect())
                    return
                if segments == ["api", "google", "web-session"] and method == "POST":
                    body = self._read_json()
                    cookies_path = str(body.get("cookies_path", "")).strip()
                    if not cookies_path:
                        raise KiraError(HTTPStatus.BAD_REQUEST, "cookies_path is required")
                    try:
                        account_index = int(body.get("account_index", 0))
                    except (TypeError, ValueError) as exc:
                        raise KiraError(
                            HTTPStatus.BAD_REQUEST,
                            "account_index must be a number between 0 and 99",
                        ) from exc
                    self._send_json(
                        self.server.google_photos.import_web_session(
                            Path(cookies_path), account_index
                        )
                    )
                    return
                if segments == ["api", "google", "web-session"] and method == "DELETE":
                    self._discard_optional_body()
                    self._send_json(self.server.google_photos.disconnect_web_session())
                    return
                if segments == ["api", "google", "albums"] and method == "GET":
                    self._send_json(self.server.google_photos.albums())
                    return
                if segments == ["api", "google", "match-folder"] and method == "POST":
                    body = self._read_json()
                    album_title = str(body.get("album_title", "")).strip()
                    if not album_title:
                        raise KiraError(HTTPStatus.BAD_REQUEST, "album_title is required")
                    if not isinstance(body.get("archive"), bool):
                        raise KiraError(HTTPStatus.BAD_REQUEST, "archive must be an explicit JSON boolean")
                    scan = scan_photo_directory(str(body.get("source_directory", "")))
                    paths = [
                        Path(item["path"])
                        for asset in scan["assets"]
                        for group in ("raw_files", "jpeg_files", "video_files", "other_files")
                        for item in asset[group]
                    ]
                    operation = self.server.google_photos.start_match_folder(
                        paths,
                        album_title,
                        body["archive"],
                    )
                    if operation.get("status") == "failed":
                        raise KiraError(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            operation.get("error") or "Google Photos folder matching failed",
                        )
                    self._send_json(operation)
                    return
                if segments == ["api", "google", "organize"] and method == "POST":
                    body = self._read_json()
                    media_ids = body.get("media_ids", [])
                    if not isinstance(media_ids, list):
                        raise KiraError(HTTPStatus.BAD_REQUEST, "media_ids must be a list")
                    album_title = str(body.get("album_title", "")).strip()
                    if not album_title:
                        raise KiraError(HTTPStatus.BAD_REQUEST, "album_title is required")
                    if not isinstance(body.get("archive"), bool):
                        raise KiraError(HTTPStatus.BAD_REQUEST, "archive must be an explicit JSON boolean")
                    self._send_json(
                        self.server.google_photos.start_organize(
                            media_ids,
                            album_title,
                            body["archive"],
                        ),
                        status=HTTPStatus.ACCEPTED,
                    )
                    return
                if segments == ["api", "google", "picker", "sessions"] and method == "POST":
                    body = self._read_json()
                    self._send_json(
                        self.server.google_photos.create_picker_session(int(body.get("max_items", 2000))),
                        status=HTTPStatus.CREATED,
                    )
                    return
                if len(segments) == 5 and segments[:4] == ["api", "google", "picker", "sessions"] and method == "GET":
                    self._send_json(self.server.google_photos.picker_session(segments[4]))
                    return
                if segments == ["api", "google", "imports"] and method == "POST":
                    body = self._read_json()
                    session_id = str(body.get("session_id", "")).strip()
                    if not session_id:
                        raise KiraError(HTTPStatus.BAD_REQUEST, "Google Picker session is required")
                    archive = body.get("archive", True)
                    if not isinstance(archive, bool):
                        raise KiraError(HTTPStatus.BAD_REQUEST, "archive must be an explicit JSON boolean")
                    destination_folder = str(body.get("destination_folder", "")).strip()
                    operation = self.server.google_photos.start_import(
                        session_id,
                        self.server.google_photos.resolve_import_destination(destination_folder)
                        if destination_folder
                        else None,
                        body.get("download_mode", "automatic"),
                        body.get("zip_threshold", 50),
                        str(body.get("album_title", "")).strip() or None,
                        archive,
                    )
                    self._send_json(operation, status=HTTPStatus.ACCEPTED)
                    return
                if segments == ["api", "google", "uploads"] and method == "POST":
                    body = self._read_json()
                    asset_ids = body.get("asset_ids", [])
                    if not isinstance(asset_ids, list):
                        raise KiraError(HTTPStatus.BAD_REQUEST, "asset_ids must be a list")
                    scan = scan_photo_directory(str(body.get("source_directory", "")))
                    selected = {str(asset_id) for asset_id in asset_ids}
                    chosen = [asset for asset in scan["assets"] if asset["id"] in selected]
                    if not chosen or len(chosen) != len(selected):
                        raise KiraError(HTTPStatus.CONFLICT, "The folder changed; scan it again before uploading")
                    paths = [
                        Path(item["path"])
                        for asset in chosen
                        for item in asset["jpeg_files"] + asset.get("video_files", [])
                    ]
                    if not paths:
                        raise KiraError(HTTPStatus.BAD_REQUEST, "Select at least one JPEG or video")
                    self._send_json(
                        self.server.google_photos.start_upload(paths),
                        status=HTTPStatus.ACCEPTED,
                    )
                    return
                if len(segments) == 4 and segments[:3] == ["api", "google", "operations"] and method == "GET":
                    self._send_json(self.server.google_photos.operation(segments[3]))
                    return
                self._method_not_allowed()
                return
            if segments == ["api", "local", "browse"] and method == "GET":
                self._require_local()
                self._send_json(browse_directories(query.get("path", [""])[0]))
                return
            if segments == ["api", "local", "scan"] and method == "GET":
                self._require_local()
                self._send_json(scan_photo_directory(query.get("path", [""])[0]))
                return
            if segments == ["api", "local", "cull"] and method == "POST":
                self._require_local()
                body = self._read_json()
                asset_ids = body.get("asset_ids", [])
                if not isinstance(asset_ids, list):
                    raise KiraError(HTTPStatus.BAD_REQUEST, "asset_ids must be a list")
                self._send_json(
                    move_culling_assets(
                        str(body.get("source_directory", "")),
                        [str(asset_id) for asset_id in asset_ids],
                        str(body.get("action", "")),
                        str(body.get("group_name", "")),
                    )
                )
                return
            if segments == ["api", "local", "move"] and method == "POST":
                self._require_local()
                body = self._read_json()
                asset_ids = body.get("asset_ids", [])
                if not isinstance(asset_ids, list):
                    raise KiraError(HTTPStatus.BAD_REQUEST, "asset_ids must be a list")
                self._send_json(
                    move_assets_between_directories(
                        str(body.get("source_directory", "")),
                        str(body.get("destination_directory", "")),
                        [str(asset_id) for asset_id in asset_ids],
                    )
                )
                return
            if segments == ["api", "local", "preview"] and method in {"GET", "HEAD"}:
                self._require_local()
                try:
                    preview = Path(query.get("path", [""])[0]).resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise KiraError(HTTPStatus.NOT_FOUND, "Preview file not found") from exc
                if not preview.is_file() or preview.suffix.casefold() not in PREVIEW_EXTENSIONS:
                    raise KiraError(HTTPStatus.BAD_REQUEST, "Preview is not a supported image")
                try:
                    requested_size = int(query.get("size", ["480"])[0])
                except ValueError:
                    requested_size = 480
                thumbnail = self.server.store.preview_thumbnail(preview, requested_size)
                self._serve_file(
                    thumbnail,
                    f"{preview.stem}-preview.jpg",
                    method == "HEAD",
                    content_type="image/jpeg" if thumbnail != preview else None,
                    disposition="inline",
                    cache_control="private, max-age=3600",
                )
                return
            if segments == ["api", "jobs", "from-selection"] and method == "POST":
                self._require_local()
                body = self._read_json()
                selected_ids = body.get("selected_ids", [])
                if not isinstance(selected_ids, list):
                    raise KiraError(HTTPStatus.BAD_REQUEST, "selected_ids must be a list")
                result = self.server.store.create_job_from_selection(
                    str(body.get("name", "")),
                    str(body.get("source_directory", "")),
                    [str(item) for item in selected_ids],
                    str(body.get("source_format", "")),
                )
                self._send_json(result, status=HTTPStatus.CREATED)
                return
            if segments == ["api", "jobs"]:
                if method == "GET":
                    self._send_json({"jobs": self.server.store.list_jobs()})
                elif method == "POST":
                    body = self._read_json()
                    self._send_json(self.server.store.create_job(str(body.get("name", ""))), status=201)
                else:
                    self._method_not_allowed()
                return

            if len(segments) >= 3 and segments[:2] == ["api", "jobs"]:
                job_id = segments[2]
                if len(segments) == 3 and method == "GET":
                    self._send_json(self.server.store.get_job(job_id))
                    return
                if len(segments) == 3 and method == "DELETE":
                    self._require_local()
                    self._send_json(self.server.store.delete_job(job_id))
                    return
                if len(segments) == 4 and segments[3] == "organize" and method == "POST":
                    self._require_local()
                    self._discard_optional_body()
                    self._send_json(self.server.store.organize_source_folder(job_id))
                    return
                if len(segments) == 5 and segments[3:] == ["uploads", "start"] and method == "POST":
                    body = self._read_json()
                    result = self.server.store.start_upload(
                        job_id,
                        str(body.get("kind", "")),
                        str(body.get("filename", "")),
                        int(body.get("size", -1)),
                        int(body.get("last_modified", 0)),
                    )
                    self._send_json(result)
                    return
                if len(segments) == 6 and segments[3] == "uploads":
                    upload_id = segments[4]
                    action = segments[5]
                    if action == "chunk" and method == "PUT":
                        offset = int(query.get("offset", ["0"])[0])
                        length = self._content_length()
                        result = self.server.store.append_upload(job_id, upload_id, offset, length, self.rfile)
                        self._send_json(result)
                        return
                    if action == "complete" and method == "POST":
                        self._discard_optional_body()
                        result = self.server.store.complete_upload(job_id, upload_id)
                        self._send_json(result)
                        return
                if len(segments) == 6 and segments[3] in {"files", "returns"} and segments[5] == "download":
                    kind = "originals" if segments[3] == "files" else "returns"
                    file_path, record = self.server.store.resolve_file(job_id, kind, segments[4])
                    self._serve_file(file_path, record["filename"], method == "HEAD")
                    return
                if len(segments) == 4 and segments[3] == "bundle.zip" and method in {"GET", "HEAD"}:
                    file_path, filename = self.server.store.create_bundle(job_id)
                    self._serve_file(file_path, filename, method == "HEAD", content_type="application/zip")
                    return

            raise KiraError(HTTPStatus.NOT_FOUND, "Not found")
        except KiraError as exc:
            if method == "PUT":
                # A rejected binary request may still have an unread chunk. Drain
                # it so a retry can safely reuse the same HTTP connection.
                try:
                    unread = int(self.headers.get("Content-Length", "0"))
                    if 0 < unread <= 16 * 1024 * 1024:
                        self.rfile.read(unread)
                    elif unread > 16 * 1024 * 1024:
                        self.close_connection = True
                except (OSError, ValueError):
                    self.close_connection = True
            self._send_json({"error": exc.message, **exc.extra}, status=exc.status)
        except GooglePhotosError as exc:
            print(f"[Google Photos] {method} {path}: {' '.join(str(exc).splitlines())}", flush=True)
            if path == "/" and "state" in query:
                self._send_html(
                    "<!doctype html><meta charset='utf-8'><title>Kira</title>"
                    f"<body style='font:16px system-ui;padding:40px'><h1>Could not connect</h1><p>{self._html_escape(str(exc))}</p></body>",
                    status=HTTPStatus.BAD_REQUEST,
                )
            else:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_GATEWAY)
        except (ValueError, TypeError, json.JSONDecodeError):
            self._send_json({"error": "Invalid request"}, status=HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # pragma: no cover - last-resort server protection
            print(f"Unhandled request error: {exc!r}")
            self._send_json({"error": "Internal server error"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _google_oauth_callback(self, query: dict[str, list[str]]) -> None:
        self._require_local()
        error = query.get("error", [""])[0]
        if error:
            raise GooglePhotosError(f"Google sign-in was cancelled: {error}")
        self.server.google_photos.finish_oauth(
            query.get("code", [""])[0], query.get("state", [""])[0]
        )
        self._send_html(
            "<!doctype html><meta charset='utf-8'><title>Kira</title>"
            "<body style='font:16px system-ui;padding:40px'>"
            "<h1>Google Photos connected</h1>"
            "<p>You can close this window and return to Kira.</p>"
            "<script>setTimeout(() => window.close(), 1200)</script></body>"
        )

    def _require_local(self) -> None:
        if not self._is_loopback():
            raise KiraError(HTTPStatus.FORBIDDEN, "This action is only available on the Dell")

    def _bootstrap(self) -> None:
        if not self._is_loopback():
            raise KiraError(HTTPStatus.FORBIDDEN, "Bootstrap is only available on the Dell")
        port = self.server.server_address[1]
        local_ip = discover_local_ip()
        ipad_url = f"http://{local_ip}:{port}"
        pair_url = f"{ipad_url}/?pair={quote(self.server.store.pair_secret, safe='')}"
        self._send_json(
            {
                "token": self.server.store.token,
                "pair_code": self.server.store.pair_code,
                "ipad_url": ipad_url,
                "pair_url": pair_url,
                "pair_qr_url": "/api/pair-qr.svg",
                "data_dir": str(self.server.store.root),
                "version": APP_VERSION,
            }
        )

    def _pair_qr(self, head_only: bool) -> None:
        if not self._is_loopback():
            raise KiraError(HTTPStatus.FORBIDDEN, "Pairing QR is only displayed on the Dell")
        port = self.server.server_address[1]
        local_ip = discover_local_ip()
        pair_url = f"http://{local_ip}:{port}/?pair={quote(self.server.store.pair_secret, safe='')}"
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=4,
            image_factory=qrcode.image.svg.SvgPathFillImage,
        )
        qr.add_data(pair_url)
        qr.make(fit=True)
        encoded = qr.make_image().to_string(encoding="utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            self.wfile.write(encoded)

    def _pair(self) -> None:
        body = self._read_json()
        supplied_code = str(body.get("code", "")).strip()
        supplied_secret = str(body.get("secret", "")).strip()
        code_matches = bool(supplied_code) and secrets.compare_digest(supplied_code, self.server.store.pair_code)
        secret_matches = bool(supplied_secret) and secrets.compare_digest(
            supplied_secret, self.server.store.pair_secret
        )
        if not code_matches and not secret_matches:
            raise KiraError(HTTPStatus.UNAUTHORIZED, "Pairing code is incorrect")
        self._send_json({"token": self.server.store.token})

    def _require_auth(self, query: dict[str, list[str]]) -> None:
        supplied = self.headers.get("X-Kira-Token") or query.get("token", [""])[0]
        if not supplied or not secrets.compare_digest(supplied, self.server.store.token):
            raise KiraError(HTTPStatus.UNAUTHORIZED, "Pair this device with Kira")

    def _read_json(self) -> dict:
        length = self._content_length()
        if length > MAX_JSON_BODY:
            raise KiraError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "JSON body is too large")
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise KiraError(HTTPStatus.BAD_REQUEST, "JSON object expected")
        return payload

    def _content_length(self) -> int:
        value = self.headers.get("Content-Length")
        if value is None:
            raise KiraError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
        length = int(value)
        if length < 0:
            raise KiraError(HTTPStatus.BAD_REQUEST, "Content-Length is invalid")
        return length

    def _discard_optional_body(self) -> None:
        value = self.headers.get("Content-Length")
        if value is None:
            return
        length = int(value)
        if length < 0 or length > MAX_JSON_BODY:
            raise KiraError(HTTPStatus.BAD_REQUEST, "Request body is invalid")
        if length:
            self.rfile.read(length)

    def _serve_static(self, path: str, head_only: bool) -> None:
        filename = "index.html" if path in {"/", "/index.html"} else path.lstrip("/")
        target = self.server.static_root / filename
        if not target.exists():
            raise KiraError(HTTPStatus.NOT_FOUND, "Not found")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    @staticmethod
    def _html_escape(value: str) -> str:
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def _send_html(self, html: str, status: int = HTTPStatus.OK) -> None:
        encoded = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _serve_file(
        self,
        path: Path,
        filename: str,
        head_only: bool,
        content_type: str | None = None,
        disposition: str = "attachment",
        cache_control: str = "private, no-store",
    ) -> None:
        total_size = path.stat().st_size
        start = 0
        end = total_size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                raise KiraError(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid byte range")
            first, last = match.groups()
            if first:
                start = int(first)
                end = int(last) if last else total_size - 1
            elif last:
                suffix_length = int(last)
                start = max(0, total_size - suffix_length)
            if start >= total_size or end < start:
                raise KiraError(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Byte range is outside the file")
            end = min(end, total_size - 1)
            status = HTTPStatus.PARTIAL_CONTENT

        length = max(0, end - start + 1)
        guessed_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        ascii_name = safe_download_name(filename)
        encoded_name = quote(filename, safe="")
        self.send_response(status)
        self.send_header("Content-Type", guessed_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}")
        self.send_header("Cache-Control", cache_control)
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
        self.end_headers()
        if head_only or not length:
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(CHUNK_COPY_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _method_not_allowed(self) -> None:
        raise KiraError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")


def build_server(host: str, port: int, data_dir: Path) -> KiraHTTPServer:
    static_root = Path(__file__).resolve().parent / "web"
    return KiraHTTPServer((host, port), KiraStore(data_dir), static_root)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kira local photo transfer MVP")
    parser.add_argument("--host", default="0.0.0.0", help="Interface to listen on")
    parser.add_argument("--port", type=int, default=8787, help="TCP port")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "kira-data",
        help="Folder where Kira stores jobs and returned edits",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not open the Dell dashboard")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = build_server(args.host, args.port, args.data_dir)
    actual_port = server.server_address[1]
    local_url = f"http://127.0.0.1:{actual_port}"
    ipad_url = f"http://{discover_local_ip()}:{actual_port}"
    print()
    print(f"Kira {APP_VERSION} is running")
    print(f"Dell dashboard: {local_url}")
    print(f"iPad address:   {ipad_url}")
    print(f"Pairing code:   {server.store.pair_code}")
    print(f"Data folder:    {server.store.root}")
    print("Press Ctrl+C to stop Kira.")
    print()
    if not args.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(local_url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping Kira...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
