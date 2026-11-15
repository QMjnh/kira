"""The photo library: one folder per shoot, originals and their edits side by side.

Every file that reaches Kira, by any route, goes through :meth:`Library.ingest_file`.
It decides whether the file is already known, a new capture, another original of a
known capture (RAW + JPEG), an edit, or a lower-quality copy, and files it next to
the photo it belongs to. Nothing is discarded while that decision is uncertain: the
file waits in the review queue.

Each shoot folder carries a ``_kira.json`` manifest, which is the source of truth
and survives the loss of Kira's working data. Folders stay usable without Kira::

    <library>/2026/2026-07 NYC/
        DSC01234.ARW            original (read-only)
        DSC01234.JPG            original (read-only)
        DSC01234_edit1.jpg      edit (read-only)
        DSC01234.ARW.xmp        sidecar, keeps its exact name
        _rejects/               culled photos, never deleted
        _copies/                recompressed duplicates
        _kira.json
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from http import HTTPStatus
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from . import visual
from .errors import KiraError
from .media import (
    CHUNK_COPY_SIZE,
    EDIT_SUFFIX,
    INVALID_FILENAME,
    MEDIA_EXTENSIONS,
    RAW_EXTENSIONS,
    VIDEO_EXTENSIONS,
    atomic_json_write,
    clean_folder_name,
    match_key,
    media_identity,
    natural_key,
    safe_filename,
    sha256_file,
    utc_now,
)
from .metadata import embedded_preview, file_kind, origin_of, read_metadata
from .statedb import StateDB

try:  # Optional: PyAV pulls a frame out of videos for their thumbnails.
    import av
except ImportError:  # pragma: no cover - videos then show without a thumbnail
    av = None

# Library files are the user's own; panoramas and stitched scans exceed Pillow's bomb guard.
Image.MAX_IMAGE_PIXELS = None

MANIFEST_NAME = "_kira.json"
REJECTS_DIR = "_rejects"
COPIES_DIR = "_copies"
TRASH_DIR = "_trash"
UNMATCHED_DIR = "_unmatched"
STAGING_DIR = ".kira-staging"
MANIFEST_SCHEMA = 1
MEDIA_ROLES = {"original", "edit", "copy", "live"}
READ_ONLY_ROLES = {"original", "edit", "copy", "live"}
REJECT_SOURCE_DIRS = {"unselect", "unselected", "unselected_jpeg", "rejects", "_rejects"}
SKIPPED_SOURCE_DIRS = {
    "$recycle.bin",
    "system volume information",
    ".kira-staging",
    "_trash",
    "_unmatched",
    ".trashes",
    ".spotlight-v100",
}
TEMPORARY_SUFFIXES = {".tmp", ".part", ".crdownload", ".download", ".kira-download"}
SETTLE_SECONDS = 2.0
SAVE_EVERY = 25
MAX_VISUAL_POOL = 4000
LOOSE_NAME_MATCH = 20
LIVE_PAIR_SECONDS = 5


def format_group(name: str) -> str:
    suffix = Path(name).suffix.casefold()
    if suffix in RAW_EXTENSIONS:
        return "raw"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return "still"


def base_stem(name: str) -> str:
    """The capture's stem with edit suffixes removed, keeping the original case."""
    stem = Path(name).stem.strip()
    if Path(stem).suffix.casefold() in MEDIA_EXTENSIONS:
        stem = Path(stem).stem
    previous = None
    while stem != previous:
        previous = stem
        stem = EDIT_SUFFIX.sub("", stem).strip(" _-")
    stem = INVALID_FILENAME.sub("_", stem).strip(". ")
    return stem or "photo"


def stem_key(name: str) -> str:
    """Case-insensitive key shared by every file name that belongs to one capture."""
    return match_key(base_stem(name) + ".x")


def _capture_seconds(value: str) -> float | None:
    try:
        return time.mktime(time.strptime(value, "%Y:%m:%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def _set_read_only(path: Path, read_only: bool) -> None:
    try:
        os.chmod(path, stat.S_IREAD if read_only else stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass


class Library:
    def __init__(self, data_root: Path, root: Path | None = None) -> None:
        self.data_root = data_root.resolve()
        self.thumbnails_root = self.data_root / "thumbnails"
        self.cache_root = self.data_root / "manifest-cache"
        self.thumbnails_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.state = StateDB(self.data_root / "kira.sqlite3")
        self.lock = threading.RLock()
        self.thumbnail_slots = threading.Semaphore(2)
        self.root: Path | None = None
        self.shoots: dict[str, dict] = {}
        self.by_sha: dict[str, tuple[str, str, str]] = {}
        self.by_time: dict[str, set[tuple[str, str]]] = {}
        self._signatures: dict[str, tuple[int, int] | None] = {}
        self.online = False
        if root is not None:
            self.open(root)

    # -- Loading and saving ---------------------------------------------------------

    def open(self, root: Path) -> None:
        """Load every shoot manifest, or the cached copies when the drive is unplugged."""
        with self.lock:
            self.root = Path(root)
            self.shoots.clear()
            self.by_sha.clear()
            self.by_time.clear()
            self.online = self.root.is_dir()
            if self.online:
                for name in (TRASH_DIR, UNMATCHED_DIR, STAGING_DIR):
                    (self.root / name).mkdir(exist_ok=True)
                manifests = sorted(
                    path
                    for path in self.root.glob(f"*/*/{MANIFEST_NAME}")
                    if not path.parent.parent.name.startswith(("_", "."))
                )
                seen: set[str] = set()
                for path in manifests:
                    shoot = self._read_manifest(path)
                    if shoot is None or shoot["id"] in seen:
                        continue
                    seen.add(shoot["id"])
                    shoot["path"] = path.parent
                    self._adopt(shoot)
                    self._write_cache(shoot)
                for cached in self.cache_root.glob("*.json"):
                    if cached.stem not in seen:
                        cached.unlink(missing_ok=True)
            else:
                for cached in sorted(self.cache_root.glob("*.json")):
                    shoot = self._read_manifest(cached)
                    if shoot is not None:
                        shoot["path"] = self.root / str(shoot.get("relative_path", ""))
                        self._adopt(shoot)

    def _read_manifest(self, path: Path) -> dict | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
            return None
        if int(payload.get("schema", 1)) > MANIFEST_SCHEMA:
            raise KiraError(HTTPStatus.CONFLICT, "This library was created by a newer Kira version")
        photos = payload.get("photos", [])
        payload["photos"] = {photo["id"]: photo for photo in photos if isinstance(photo, dict) and photo.get("id")}
        payload.setdefault("loose", [])
        return payload

    def _adopt(self, shoot: dict) -> None:
        self.shoots[shoot["id"]] = shoot
        for photo in shoot["photos"].values():
            for record in photo["files"]:
                self._index_file(shoot, photo, record)
            self._index_time(shoot, photo)

    def _index_file(self, shoot: dict, photo: dict, record: dict) -> None:
        if record["role"] in MEDIA_ROLES and not record.get("missing"):
            self.by_sha.setdefault(record["sha256"], (shoot["id"], photo["id"], record["name"]))

    def _index_time(self, shoot: dict, photo: dict) -> None:
        captured = photo["capture"].get("time")
        if captured:
            self.by_time.setdefault(captured, set()).add((shoot["id"], photo["id"]))

    def _manifest_payload(self, shoot: dict) -> dict:
        payload = {key: value for key, value in shoot.items() if key not in {"path", "photos", "dirty"}}
        payload["schema"] = MANIFEST_SCHEMA
        payload["photos"] = sorted(shoot["photos"].values(), key=lambda photo: natural_key(photo["stem"]))
        return payload

    def _write_cache(self, shoot: dict) -> None:
        payload = self._manifest_payload(shoot)
        payload["relative_path"] = shoot["path"].relative_to(self.root).as_posix()
        atomic_json_write(self.cache_root / f"{shoot['id']}.json", payload)

    def _save(self, shoot: dict) -> None:
        shoot["updated_at"] = utc_now()
        atomic_json_write(shoot["path"] / MANIFEST_NAME, self._manifest_payload(shoot))
        self._write_cache(shoot)
        shoot["dirty"] = 0

    def _require_online(self) -> Path:
        if self.root is None:
            raise KiraError(HTTPStatus.CONFLICT, "Choose a library folder first")
        if not self.root.is_dir():
            self.online = False
            raise KiraError(HTTPStatus.SERVICE_UNAVAILABLE, "The library drive is not connected")
        if not self.online:
            self.open(self.root)
        return self.root

    # -- Lookups --------------------------------------------------------------------

    def shoot(self, shoot_id: str) -> dict:
        shoot = self.shoots.get(str(shoot_id))
        if shoot is None:
            raise KiraError(HTTPStatus.NOT_FOUND, "Shoot not found")
        return shoot

    def photo(self, shoot_id: str, photo_id: str) -> tuple[dict, dict]:
        shoot = self.shoot(shoot_id)
        photo = shoot["photos"].get(str(photo_id))
        if photo is None:
            raise KiraError(HTTPStatus.NOT_FOUND, "Photo not found")
        return shoot, photo

    def file_record(self, photo: dict, name: str) -> dict:
        for record in photo["files"]:
            if record["name"] == name:
                return record
        raise KiraError(HTTPStatus.NOT_FOUND, "File not found")

    def file_path(self, shoot: dict, photo: dict, record: dict) -> Path:
        if record["role"] == "copy":
            return shoot["path"] / COPIES_DIR / record["name"]
        if photo.get("rejected"):
            return shoot["path"] / REJECTS_DIR / record["name"]
        return shoot["path"] / record["name"]

    def relative_path(self, shoot: dict, photo: dict, record: dict) -> str:
        return self.file_path(shoot, photo, record).relative_to(self.root).as_posix()

    def all_files(self):
        """Yield (shoot, photo, record) for every tracked file that is present."""
        for shoot in list(self.shoots.values()):
            for photo in list(shoot["photos"].values()):
                for record in list(photo["files"]):
                    if not record.get("missing"):
                        yield shoot, photo, record

    @staticmethod
    def effective_final(photo: dict) -> str | None:
        """The pinned Final, otherwise the newest edit; unedited photos have none."""
        names = {record["name"] for record in photo["files"] if record["role"] in {"original", "edit"}}
        if photo.get("final") in names:
            return photo["final"]
        edits = [record for record in photo["files"] if record["role"] == "edit" and not record.get("missing")]
        if edits:
            return max(edits, key=lambda record: record.get("edit", 0))["name"]
        return None

    def _stem_keys(self, photo: dict) -> set[str]:
        keys = {stem_key(photo["stem"] + ".x")}
        for record in photo["files"]:
            if record["role"] in MEDIA_ROLES and record.get("original_name"):
                keys.add(stem_key(record["original_name"]))
        return keys

    def _shoot_stems(self, shoot: dict) -> set[str]:
        return {photo["stem"].casefold() for photo in shoot["photos"].values()}

    def _unique_stem(self, shoot: dict, wanted: str) -> str:
        taken = self._shoot_stems(shoot)
        stem = wanted
        index = 2
        while stem.casefold() in taken:
            stem = f"{wanted}-{index}"
            index += 1
        return stem

    # -- Shoots ---------------------------------------------------------------------

    def create_shoot(self, name: str, year: str | None = None) -> dict:
        root = self._require_online()
        clean = clean_folder_name(name)
        if clean.startswith(("_", ".")):
            raise KiraError(HTTPStatus.BAD_REQUEST, "Shoot names cannot start with _ or .")
        with self.lock:
            folder_year = str(year or datetime.now().year)
            if not re.fullmatch(r"\d{4}", folder_year):
                raise KiraError(HTTPStatus.BAD_REQUEST, "Year must have four digits")
            folder = root / folder_year / clean
            if folder.exists():
                raise KiraError(HTTPStatus.CONFLICT, f"A shoot named {clean} already exists in {folder_year}")
            folder.mkdir(parents=True)
            shoot = {
                "id": uuid.uuid4().hex[:12],
                "name": clean,
                "year": folder_year,
                "created_at": utc_now(),
                "photos": {},
                "loose": [],
                "path": folder,
            }
            self.shoots[shoot["id"]] = shoot
            self._save(shoot)
            return shoot

    def find_shoot(self, name: str) -> dict | None:
        wanted = str(name).strip().casefold()
        for shoot in self.shoots.values():
            if shoot["name"].casefold() == wanted:
                return shoot
        return None

    def rename_shoot(self, shoot_id: str, name: str) -> dict:
        self._require_online()
        with self.lock:
            shoot = self.shoot(shoot_id)
            clean = clean_folder_name(name)
            if clean.startswith(("_", ".")):
                raise KiraError(HTTPStatus.BAD_REQUEST, "Shoot names cannot start with _ or .")
            target = shoot["path"].parent / clean
            if target != shoot["path"]:
                if target.exists():
                    raise KiraError(HTTPStatus.CONFLICT, f"{clean} already exists")
                try:
                    shoot["path"].rename(target)
                except OSError as exc:
                    raise KiraError(HTTPStatus.CONFLICT, f"Could not rename the shoot folder: {exc}") from exc
                shoot["path"] = target
            shoot["name"] = clean
            self._save(shoot)
            return shoot

    # -- Views ----------------------------------------------------------------------

    def shoot_summary(self, shoot: dict) -> dict:
        photos = list(shoot["photos"].values())
        kept = [photo for photo in photos if not photo.get("rejected")]
        times = sorted(photo["capture"]["time"] for photo in photos if photo["capture"].get("time"))
        cover = next((photo for photo in sorted(kept, key=self._photo_order) if self._cover(photo)), None)
        return {
            "id": shoot["id"],
            "name": shoot["name"],
            "year": shoot["year"],
            "path": str(shoot["path"]),
            "photo_count": len(kept),
            "rejected_count": len(photos) - len(kept),
            "edited_count": sum(1 for photo in kept if self.effective_final(photo)),
            "first_capture": times[0] if times else "",
            "last_capture": times[-1] if times else "",
            "cover": {"photo_id": cover["id"], "file": self._cover(cover)} if cover else None,
        }

    @staticmethod
    def _photo_order(photo: dict) -> tuple:
        return (photo["capture"].get("time") or "9999", natural_key(photo["stem"]))

    def _cover(self, photo: dict) -> str | None:
        """The file whose thumbnail represents the photo: Final, else a still original, RAW, then video."""
        final = self.effective_final(photo)
        if final and Path(final).suffix.casefold() not in RAW_EXTENSIONS | VIDEO_EXTENSIONS:
            return final
        present = [record for record in photo["files"] if not record.get("missing")]
        for group in ("still", "raw"):
            for record in present:
                if record["role"] == "original" and format_group(record["name"]) == group:
                    return record["name"]
        for record in present:
            if record["role"] == "edit" and format_group(record["name"]) == "still":
                return record["name"]
        for record in present:
            if record["role"] == "original" and format_group(record["name"]) == "video":
                return record["name"]
        return None

    def photo_view(self, photo: dict) -> dict:
        return {
            "id": photo["id"],
            "stem": photo["stem"],
            "kind": photo.get("kind", "photo"),
            "origin": photo.get("origin", "other"),
            "capture_time": photo["capture"].get("time", ""),
            "model": photo["capture"].get("model", ""),
            "rejected": bool(photo.get("rejected")),
            "tags": list(photo.get("tags", [])),
            "final": self.effective_final(photo),
            "final_pinned": bool(photo.get("final")),
            "cover": self._cover(photo),
            "files": [
                {
                    key: record.get(key)
                    for key in ("name", "role", "edit", "size", "sha256", "source", "original_name", "missing")
                    if record.get(key) is not None
                }
                for record in photo["files"]
            ],
        }

    def shoot_view(self, shoot_id: str) -> dict:
        with self.lock:
            shoot = self.shoot(shoot_id)
            view = self.shoot_summary(shoot)
            view["photos"] = [
                self.photo_view(photo) for photo in sorted(shoot["photos"].values(), key=self._photo_order)
            ]
            view["loose"] = [record["name"] for record in shoot.get("loose", [])]
            return view

    def overview(self) -> dict:
        with self.lock:
            shoots = [self.shoot_summary(shoot) for shoot in self.shoots.values()]
            shoots.sort(key=lambda item: (item["last_capture"] or item["year"], item["name"]), reverse=True)
            return {
                "configured": self.root is not None,
                "online": bool(self.root and self.root.is_dir()),
                "root": str(self.root) if self.root else "",
                "shoots": shoots,
                "review_count": len(self.review_items()),
                "trash_count": self.state.execute("SELECT COUNT(*) AS n FROM trash")[0]["n"],
            }

    # -- Ingest ---------------------------------------------------------------------

    def staging_path(self, name: str) -> Path:
        """A fresh path on the library drive. The extension is kept so the file can be read."""
        return self._require_online() / STAGING_DIR / f"{uuid.uuid4().hex}{Path(name).suffix}"

    def _stage(self, source: Path, name: str) -> tuple[Path, str, int]:
        """Copy a file onto the library drive, hashing the stream and verifying the copy."""
        staged = self.staging_path(name)
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as incoming, staged.open("xb") as outgoing:
                while chunk := incoming.read(CHUNK_COPY_SIZE):
                    digest.update(chunk)
                    size += len(chunk)
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            value = digest.hexdigest()
            if sha256_file(staged) != value:
                raise KiraError(HTTPStatus.CONFLICT, f"The copy of {source.name} could not be verified")
            return staged, value, size
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def ingest_file(
        self,
        source: Path,
        shoot_id: str,
        *,
        source_kind: str,
        original_name: str | None = None,
        hints: set[str] | None = None,
        consume: bool = False,
        in_place: bool = False,
        rejected: bool = False,
        batch: dict | None = None,
        provenance: dict | None = None,
        digest: str | None = None,
    ) -> dict:
        """File one incoming file. Returns what it was and where it went.

        ``consume`` takes ownership of a file already staged on the library drive.
        ``in_place`` adopts an untracked file that already sits in the shoot folder.
        ``digest`` is the file's SHA-256 when the caller already hashed it.
        """
        self._require_online()
        name = safe_filename(original_name or source.name)
        kind = file_kind(name)
        if kind == "other":
            return {"name": name, "status": "ignored", "detail": "not a photo, video, sidecar or project file"}
        if consume or in_place:
            staged, digest, size = source, digest or sha256_file(source), source.stat().st_size
        else:
            staged, digest, size = self._stage(source, name)
        placed = False
        discard = not consume and not in_place
        try:
            with self.lock:
                shoot = self.shoot(shoot_id)
                known = self.by_sha.get(digest) if kind in {"photo", "video"} else None
                if known:
                    known_shoot, known_photo = self.photo(known[0], known[1])
                    if batch is not None:
                        batch[(str(source.parent), stem_key(name))] = (known_shoot["id"], known_photo["id"])
                    if provenance:
                        self._record_provenance(digest, provenance)
                    if in_place:
                        self._to_trash(shoot, staged, digest)
                        placed = True
                    discard = not in_place
                    return {
                        "name": name,
                        "status": "duplicate",
                        "shoot_id": known_shoot["id"],
                        "photo_id": known_photo["id"],
                        "file": known[2],
                        "detail": f"already in the library as {known[2]}",
                    }
                metadata = read_metadata(staged) if kind in {"photo", "video"} else {"kind": kind}
                metadata["kind"] = kind
                if kind in {"sidecar", "project"}:
                    decision = self._classify_component(name, source, shoot, batch)
                elif kind == "video":
                    decision = self._classify_video(name, source, metadata, shoot, batch)
                else:
                    decision = self._classify_photo(staged, name, metadata, shoot, hints, source_kind)
                result = self._apply(
                    decision, staged, name, digest, size, metadata, shoot, source_kind, rejected, in_place
                )
                placed = True
                if batch is not None and result.get("photo_id"):
                    batch[(str(source.parent), stem_key(name))] = (result["shoot_id"], result["photo_id"])
                if provenance and kind in {"photo", "video"}:
                    self._record_provenance(digest, provenance)
                return result
        finally:
            # A file handed over with ``consume`` stays put if filing it failed, so it can be retried.
            if not placed and discard:
                staged.unlink(missing_ok=True)

    def _record_provenance(self, digest: str, provenance: dict) -> None:
        if provenance.get("gphotos_media_key"):
            self.state.execute(
                "INSERT OR REPLACE INTO gphotos VALUES (?,?,?,?,?,?,?)",
                (
                    digest,
                    "present",
                    provenance["gphotos_media_key"],
                    provenance.get("gphotos_dedup_key") or None,
                    int(bool(provenance.get("gphotos_archived"))),
                    "download",
                    utc_now(),
                ),
            )

    # -- Deciding what a file is ------------------------------------------------------

    def _candidates(self, metadata: dict, kind: str) -> list[tuple[dict, dict]]:
        """Photos taken at the same moment on a compatible camera."""
        found: list[tuple[dict, dict]] = []
        for shoot_id, photo_id in sorted(self.by_time.get(metadata.get("capture_time", ""), ())):
            shoot = self.shoots.get(shoot_id)
            photo = shoot["photos"].get(photo_id) if shoot else None
            if photo is None or photo.get("kind", "photo") != kind:
                continue
            capture = photo["capture"]
            if metadata.get("model") and capture.get("model") and metadata["model"] != capture["model"]:
                continue
            if metadata.get("subsec") and capture.get("subsec") and metadata["subsec"] != capture["subsec"]:
                continue
            found.append((shoot, photo))
        return found

    def _reference(self, shoot: dict, photo: dict) -> dict | None:
        """The file that best shows what a photo looks like."""
        present = [record for record in photo["files"] if not record.get("missing")]
        for role, group in (("original", "still"), ("original", "raw"), ("edit", "still")):
            for record in present:
                if record["role"] == role and format_group(record["name"]) == group:
                    return record
        return None

    @staticmethod
    def _raw_preview(path: Path) -> Image.Image | None:
        """The JPEG embedded in a RAW file, turned upright like its camera JPEG would be."""
        embedded = embedded_preview(path)
        if embedded is None:
            return None
        image = Image.open(BytesIO(embedded))
        if image.getexif().get(0x0112, 1) != 1:
            return ImageOps.exif_transpose(image)
        turn = {3: 180, 6: 270, 8: 90}.get(read_metadata(path).get("orientation", 1))
        return image.rotate(turn, expand=True) if turn else image

    @staticmethod
    def _video_frame(path: Path) -> Image.Image | None:
        """A keyframe about a second in (past any fade from black), turned upright."""
        if av is None:
            return None
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            stream.codec_context.skip_frame = "NONKEY"
            if (container.duration or 0) > 2 * av.time_base:
                container.seek(av.time_base)
            frame = next(container.decode(stream), None)
            if frame is None:
                return None
            image = frame.to_image()
            return image.rotate(frame.rotation, expand=True) if frame.rotation else image

    def _signature_of(self, path: Path) -> tuple[int, int] | None:
        try:
            if path.suffix.casefold() in RAW_EXTENSIONS:
                preview = self._raw_preview(path)
                return visual.version_signature(preview) if preview else None
            return visual.version_signature(path)
        except Exception:  # noqa: BLE001 - an undecodable image simply cannot be compared
            return None

    def _photo_signature(self, shoot: dict, photo: dict) -> tuple[int, int] | None:
        record = self._reference(shoot, photo)
        if record is None:
            return None
        digest = record["sha256"]
        if digest in self._signatures:
            return self._signatures[digest]
        rows = self.state.execute("SELECT frequency, difference FROM signatures WHERE sha256=?", (digest,))
        if rows:
            signature = (int(rows[0]["frequency"]), int(rows[0]["difference"]))
        else:
            signature = self._signature_of(self.file_path(shoot, photo, record))
            if signature:
                self.state.execute(
                    "INSERT OR REPLACE INTO signatures VALUES (?,?,?)", (digest, str(signature[0]), str(signature[1]))
                )
        self._signatures[digest] = signature
        return signature

    def _ranked(self, signature: tuple[int, int] | None, pool: list[tuple[dict, dict]]) -> list[tuple]:
        """Pool members that can be compared, closest first: (distance, shoot, photo)."""
        if signature is None:
            return []
        rows = []
        for shoot, photo in pool:
            other = self._photo_signature(shoot, photo)
            if other is not None:
                rows.append((visual.version_distance(signature, other), shoot, photo))
        rows.sort(key=lambda row: row[0])
        return rows

    def _closest(self, signature: tuple[int, int] | None, pool: list[tuple[dict, dict]]) -> tuple | None:
        """The one pool member a file clearly derives from, or None when unsure."""
        ranked = self._ranked(signature, pool)
        if not ranked or ranked[0][0][0] > visual.EDIT_PHASH:
            return None
        if len(ranked) > 1 and ranked[1][0][0] - ranked[0][0][0] < visual.EDIT_MARGIN:
            return None
        return ranked[0]

    def _original_pixels(self, photo: dict) -> int:
        return max(
            (
                int(record.get("width", 0)) * int(record.get("height", 0))
                for record in photo["files"]
                if record["role"] == "original"
            ),
            default=0,
        )

    def _pixel_identical(self, path: Path, shoot: dict, photo: dict) -> dict | None:
        """An original of the photo holding exactly the same decoded pixels, if any."""
        try:
            incoming = media_identity(path)
        except OSError:
            return None
        if incoming[0] != "pixels":
            return None
        for record in photo["files"]:
            if record["role"] != "original" or record.get("missing") or format_group(record["name"]) != "still":
                continue
            try:
                if media_identity(self.file_path(shoot, photo, record)) == incoming:
                    return record
            except OSError:
                continue
        return None

    def _classify_photo(
        self, path: Path, name: str, metadata: dict, shoot: dict, hints: set[str] | None, source_kind: str
    ) -> dict:
        name_key = stem_key(name)
        edit_like = bool(metadata.get("edited")) or source_kind == "ipad"
        pixels = int(metadata.get("width", 0)) * int(metadata.get("height", 0))
        cache: dict[str, tuple[int, int] | None] = {}

        def signature() -> tuple[int, int] | None:
            if "value" not in cache:
                cache["value"] = self._signature_of(path)
            return cache["value"]

        candidates = self._candidates(metadata, "photo")
        if hints:
            hinted = [pair for pair in candidates if pair[1]["id"] in hints]
            candidates = hinted or candidates
        if candidates:
            named = [pair for pair in candidates if name_key in self._stem_keys(pair[1])]
            if edit_like:
                if len(named) == 1:
                    return self._attach(named[0], "edit", "same capture time and name")
                if len(candidates) == 1:
                    return self._attach(candidates[0], "edit", "same capture time")
                closest = self._closest(signature(), candidates)
                if closest:
                    return self._attach(closest[1:], "edit", "same capture time, closest match")
                return self._review(candidates, "Several photos were taken in the same second")
            if len(named) == 1:
                return self._classify_rendition(path, name, metadata, named[0], signature)
            for pair in candidates:
                if self._pixel_identical(path, *pair):
                    return self._attach(pair, "copy", "same pixels, different metadata")
            smaller = [pair for pair in candidates if pixels and pixels < self._original_pixels(pair[1])]
            closest = self._closest(signature(), smaller)
            if closest:
                role = "copy" if metadata.get("shared") or visual.same_pixels(closest[0]) else "edit"
                return self._attach(closest[1:], role, "smaller rendition of the same capture")
            return {"action": "new", "role": "original"}

        if hints:
            pool = [
                (owner, photo)
                for owner in self.shoots.values()
                for photo in owner["photos"].values()
                if photo["id"] in hints
            ]
        elif not metadata.get("capture_time"):
            pool = [(shoot, photo) for photo in shoot["photos"].values() if photo.get("kind", "photo") == "photo"]
        else:
            return {"action": "new", "role": "edit" if metadata.get("edited") else "original"}

        named = [pair for pair in pool if name_key in self._stem_keys(pair[1])]
        if len(named) == 1:
            ranked = self._ranked(signature(), named)
            if not ranked:
                return self._attach(named[0], "edit", "same name")
            distance = ranked[0][0]
            if distance[0] <= LOOSE_NAME_MATCH:
                if metadata.get("shared") or (not edit_like and visual.same_pixels(distance)):
                    return self._attach(named[0], "copy", "same name and same picture")
                return self._attach(named[0], "edit", "same name")
            return self._review(named, "Same name as an existing photo, but it looks different")
        if len(pool) > MAX_VISUAL_POOL:
            return {"action": "new", "role": "original"}
        ranked = self._ranked(signature(), pool)
        if ranked:
            best = ranked[0]
            runner_up = ranked[1][0] if len(ranked) > 1 else None
            if visual.same_pixels(best[0]) and not (runner_up and visual.same_pixels(runner_up)):
                role = "edit" if edit_like else "copy"
                return self._attach(best[1:], role, "same picture without capture metadata")
            if best[0][0] <= visual.EDIT_PHASH and (
                runner_up is None or runner_up[0] - best[0][0] >= visual.EDIT_MARGIN
            ):
                if source_kind == "ipad":
                    return self._attach(best[1:], "edit", "closest match among the photos sent to the iPad")
                return self._review([best[1:]], "Looks like an edit of an existing photo")
        if source_kind == "ipad":
            return self._review([row[1:] for row in ranked[:3]], "Could not match this return to a photo")
        return {"action": "new", "role": "original"}

    def _classify_rendition(
        self, path: Path, name: str, metadata: dict, pair: tuple[dict, dict], signature: Callable
    ) -> dict:
        """An unedited-looking file with the same capture time and name as a known photo."""
        shoot, photo = pair
        group = format_group(name)
        originals = [record for record in photo["files"] if record["role"] == "original" and not record.get("missing")]
        same_group = [record for record in originals if format_group(record["name"]) == group]
        has_raw = any(format_group(record["name"]) == "raw" for record in originals)
        if not originals or (not same_group and (group == "raw" or has_raw)):
            return self._attach(pair, "original", "another original of the same capture")
        reference = (same_group or originals)[0]
        identical = self._pixel_identical(path, shoot, photo) is not None
        distance = None
        other = self._photo_signature(shoot, photo)
        mine = signature()
        if mine is not None and other is not None:
            distance = visual.version_distance(mine, other)
        if identical or (distance is not None and visual.same_pixels(distance)):
            pixels = int(metadata.get("width", 0)) * int(metadata.get("height", 0))
            known = int(reference.get("width", 0)) * int(reference.get("height", 0))
            if same_group and not metadata.get("shared") and pixels > known > 0:
                decision = self._attach(pair, "original", "higher-resolution original of the same capture")
                decision["demote"] = reference["name"]
                return decision
            return self._attach(pair, "copy", "same picture, recompressed or with different metadata")
        if metadata.get("shared"):
            return self._attach(pair, "copy", "shared copy of the same capture")
        if distance is None:
            return self._review([pair], "Same capture as an existing photo, but the two files cannot be compared")
        return self._attach(pair, "edit", "same capture, different pixels")

    def _classify_video(self, name: str, source: Path, metadata: dict, shoot: dict, batch: dict | None) -> dict:
        name_key = stem_key(name)
        owners: list[tuple[dict, dict]] = []
        if batch and (str(source.parent), name_key) in batch:
            owners.append(self.photo(*batch[(str(source.parent), name_key)]))
        owners.extend((shoot, photo) for photo in shoot["photos"].values() if name_key in self._stem_keys(photo))
        video_time = _capture_seconds(metadata.get("capture_time", ""))
        for owner, photo in owners:
            if photo.get("kind", "photo") != "photo" or any(record["role"] == "live" for record in photo["files"]):
                continue
            photo_time = _capture_seconds(photo["capture"].get("time", ""))
            if video_time is None or photo_time is None or abs(video_time - photo_time) <= LIVE_PAIR_SECONDS:
                return self._attach((owner, photo), "live", "Live Photo video")
        return {"action": "new", "role": "original"}

    def _classify_component(self, name: str, source: Path, shoot: dict, batch: dict | None) -> dict:
        name_key = stem_key(name)
        if batch and (str(source.parent), name_key) in batch:
            return self._attach(self.photo(*batch[(str(source.parent), name_key)]), file_kind(name), "same name")
        owners = [photo for photo in shoot["photos"].values() if name_key in self._stem_keys(photo)]
        if len(owners) == 1:
            return self._attach((shoot, owners[0]), file_kind(name), "same name")
        return {"action": "loose"}

    @staticmethod
    def _attach(pair: tuple, role: str, reason: str) -> dict:
        return {"action": "attach", "shoot": pair[0], "photo": pair[1], "role": role, "reason": reason}

    @staticmethod
    def _review(pairs: list[tuple], reason: str) -> dict:
        return {"action": "review", "candidates": [(pair[0], pair[1]) for pair in pairs], "reason": reason}

    # -- Placing a file ---------------------------------------------------------------

    def _apply(
        self,
        decision: dict,
        staged: Path,
        name: str,
        digest: str,
        size: int,
        metadata: dict,
        shoot: dict,
        source_kind: str,
        rejected: bool,
        in_place: bool,
    ) -> dict:
        action = decision["action"]
        if action == "review":
            return self._queue_review(decision, staged, name, digest, metadata, shoot, source_kind)
        if action == "loose":
            target = self._free_path(shoot["path"], name)
            self._move(staged, target)
            shoot.setdefault("loose", []).append(self._record(target, target.name, "loose", digest, name, source_kind))
            self._touch(shoot)
            return {"name": name, "status": "loose", "shoot_id": shoot["id"], "file": target.name}

        if action == "new":
            owner = shoot
            photo = {
                "id": uuid.uuid4().hex[:12],
                "stem": self._unique_stem(shoot, base_stem(name)),
                "kind": "video" if metadata["kind"] == "video" else "photo",
                "origin": origin_of(metadata),
                "capture": {},
                "rejected": bool(rejected),
                "final": None,
                "tags": [],
                "files": [],
            }
            shoot["photos"][photo["id"]] = photo
            status = "new"
        else:
            owner, photo = decision["shoot"], decision["photo"]
            status = decision["role"]
        role = decision["role"]

        if decision.get("demote"):
            self._demote(owner, photo, decision["demote"])
        extension = Path(name).suffix
        if role in {"sidecar", "project"}:
            source_stem = base_stem(name)
            rest = name[len(source_stem) :] if name.casefold().startswith(source_stem.casefold()) else extension
            file_name = f"{photo['stem']}{rest}"
            existing = next((item for item in photo["files"] if item["name"].casefold() == file_name.casefold()), None)
            if existing:
                # Sidecars and project files change in place; the previous state goes to the trash.
                previous = self.file_path(owner, photo, existing)
                if previous.exists() and previous != staged:
                    self._to_trash(owner, previous, existing["sha256"])
                photo["files"].remove(existing)
        elif role == "edit":
            number = max((int(item.get("edit", 0)) for item in photo["files"]), default=0) + 1
            file_name = f"{photo['stem']}_edit{number}{extension.lower()}"
        elif role == "copy":
            number = sum(1 for item in photo["files"] if item["role"] == "copy") + 1
            file_name = f"{photo['stem']}_copy{number}{extension.lower()}"
        else:
            file_name = f"{photo['stem']}{extension}"
            if any(item["name"].casefold() == file_name.casefold() for item in photo["files"]):
                role = "edit"
                number = max((int(item.get("edit", 0)) for item in photo["files"]), default=0) + 1
                file_name = f"{photo['stem']}_edit{number}{extension.lower()}"

        folder = owner["path"]
        if role == "copy":
            folder = folder / COPIES_DIR
        elif photo.get("rejected"):
            folder = folder / REJECTS_DIR
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / file_name
        if target.exists() and target != staged:
            target = self._free_path(folder, file_name)
            file_name = target.name
        if target != staged:
            self._move(staged, target)
        record = self._record(target, file_name, role, digest, name, source_kind)
        if role == "edit":
            record["edit"] = number
        for key in ("width", "height", "software"):
            if metadata.get(key):
                record[key] = metadata[key]
        photo["files"].append(record)
        if role in READ_ONLY_ROLES:
            _set_read_only(target, True)
        if metadata.get("capture_time") and role in {"original", "edit", "copy"}:
            capture = photo["capture"]
            capture.setdefault("time", metadata["capture_time"])
            for key in ("subsec", "model", "make", "offset"):
                if metadata.get(key) and not capture.get(key):
                    capture[key] = metadata[key]
            self._index_time(owner, photo)
        if role == "original" and photo.get("origin", "other") == "other":
            photo["origin"] = origin_of(metadata)
        self._index_file(owner, photo, record)
        self._touch(owner)
        return {
            "name": name,
            "status": status,
            "role": role,
            "shoot_id": owner["id"],
            "photo_id": photo["id"],
            "file": file_name,
            "detail": decision.get("reason", "new photo"),
        }

    def _record(self, path: Path, name: str, role: str, digest: str, original_name: str, source_kind: str) -> dict:
        info = path.stat()
        return {
            "name": name,
            "role": role,
            "sha256": digest,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "added_at": utc_now(),
            "source": source_kind,
            "original_name": original_name,
        }

    @staticmethod
    def _free_path(folder: Path, name: str) -> Path:
        candidate = folder / name
        index = 2
        while candidate.exists():
            candidate = folder / f"{Path(name).stem}-{index}{Path(name).suffix}"
            index += 1
        return candidate

    @staticmethod
    def _move(source: Path, target: Path) -> None:
        """Rename without ever replacing an existing file."""
        if target.exists():
            raise KiraError(HTTPStatus.CONFLICT, f"{target.name} already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(source, target)
        except OSError:
            shutil.move(str(source), str(target))

    def _touch(self, shoot: dict) -> None:
        shoot["dirty"] = shoot.get("dirty", 0) + 1

    def _demote(self, shoot: dict, photo: dict, name: str) -> None:
        """Turn a lower-quality original into a hidden copy to make room for a better one."""
        record = self.file_record(photo, name)
        source = self.file_path(shoot, photo, record)
        number = sum(1 for item in photo["files"] if item["role"] == "copy") + 1
        record["role"] = "copy"
        record["name"] = f"{photo['stem']}_copy{number}{Path(name).suffix.lower()}"
        target = self._free_path(shoot["path"] / COPIES_DIR, record["name"])
        record["name"] = target.name
        self._move(source, target)
        if photo.get("final") == name:
            photo["final"] = None

    def _to_trash(self, shoot: dict, path: Path, digest: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self._free_path(self.root / TRASH_DIR / stamp / shoot["name"], path.name)
        _set_read_only(path, False)
        relative = path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else path.name
        self._move(path, target)
        self.state.execute(
            "INSERT INTO trash VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex[:12], shoot["id"], relative, str(target), digest, utc_now()),
        )
        return target

    # -- Review queue ---------------------------------------------------------------

    def _queue_review(
        self, decision: dict, staged: Path, name: str, digest: str, metadata: dict, shoot: dict, source_kind: str
    ) -> dict:
        review_id = uuid.uuid4().hex[:12]
        target = self.root / UNMATCHED_DIR / f"{review_id}_{name}"
        _set_read_only(staged, False)
        self._move(staged, target)
        payload = {
            "name": name,
            "sha256": digest,
            "source": source_kind,
            "shoot_id": shoot["id"],
            "reason": decision["reason"],
            "capture_time": metadata.get("capture_time", ""),
            "edited": bool(metadata.get("edited")) or source_kind == "ipad",
            "candidates": [
                {"shoot_id": owner["id"], "photo_id": photo["id"]} for owner, photo in decision["candidates"]
            ],
        }
        self.state.execute(
            "INSERT INTO review (id, created_at, kind, path, payload) VALUES (?,?,?,?,?)",
            (review_id, utc_now(), "unmatched", str(target), json.dumps(payload)),
        )
        return {"name": name, "status": "review", "review_id": review_id, "detail": decision["reason"]}

    def review_items(self) -> list[dict]:
        items = []
        for row in self.state.execute("SELECT * FROM review WHERE status='open' ORDER BY created_at"):
            payload = json.loads(row["payload"])
            candidates = []
            for candidate in payload.get("candidates", []):
                shoot = self.shoots.get(candidate["shoot_id"])
                photo = shoot["photos"].get(candidate["photo_id"]) if shoot else None
                if photo:
                    candidates.append(
                        {
                            "shoot_id": shoot["id"],
                            "shoot_name": shoot["name"],
                            "photo_id": photo["id"],
                            "stem": photo["stem"],
                            "cover": self._cover(photo),
                        }
                    )
            items.append({**payload, "id": row["id"], "created_at": row["created_at"], "candidates": candidates})
        return items

    def review_path(self, review_id: str) -> Path:
        rows = self.state.execute("SELECT path FROM review WHERE id=? AND status='open'", (review_id,))
        if not rows:
            raise KiraError(HTTPStatus.NOT_FOUND, "Review item not found")
        return Path(rows[0]["path"])

    def resolve_review(
        self, review_id: str, action: str, shoot_id: str | None = None, photo_id: str | None = None
    ) -> dict:
        """Settle a waiting file: an edit or copy of a photo, a new photo, or discard it."""
        self._require_online()
        with self.lock:
            rows = self.state.execute("SELECT * FROM review WHERE id=? AND status='open'", (review_id,))
            if not rows:
                raise KiraError(HTTPStatus.NOT_FOUND, "Review item not found")
            payload = json.loads(rows[0]["payload"])
            path = Path(rows[0]["path"])
            if not path.is_file():
                self.state.execute("UPDATE review SET status='lost' WHERE id=?", (review_id,))
                raise KiraError(HTTPStatus.NOT_FOUND, "The waiting file is no longer on disk")
            name, digest = payload["name"], payload["sha256"]
            metadata = read_metadata(path)
            if action in {"edit", "copy"}:
                owner, photo = self.photo(str(shoot_id), str(photo_id))
                decision = self._attach((owner, photo), action, "confirmed in review")
                shoot = owner
            elif action == "new":
                shoot = self.shoot(str(shoot_id or payload["shoot_id"]))
                decision = {"action": "new", "role": "edit" if payload.get("edited") else "original"}
            elif action == "discard":
                shoot = self.shoot(payload["shoot_id"]) if payload["shoot_id"] in self.shoots else None
                self._to_trash(shoot or {"id": "", "name": "review"}, path, digest)
                self.state.execute("UPDATE review SET status='discarded' WHERE id=?", (review_id,))
                return {"status": "discarded"}
            else:
                raise KiraError(HTTPStatus.BAD_REQUEST, "Unknown review action")
            result = self._apply(
                decision, path, name, digest, path.stat().st_size, metadata, shoot, payload["source"], False, False
            )
            self.state.execute("UPDATE review SET status='resolved' WHERE id=?", (review_id,))
            self._save(self.shoot(result["shoot_id"]))
            return result

    # -- Importing a folder or card ---------------------------------------------------

    @staticmethod
    def _source_key(path: Path) -> str:
        info = path.stat()
        return f"{path}|{info.st_size}|{info.st_mtime_ns}"

    def _source_files(self, source: Path) -> list[Path]:
        files: list[Path] = []
        for folder, directories, names in os.walk(source):
            directories[:] = [
                name for name in directories if name.casefold() not in SKIPPED_SOURCE_DIRS and not name.startswith(".")
            ]
            for name in names:
                if file_kind(name) != "other" and not name.startswith((".", "~")):
                    files.append(Path(folder) / name)
        order = {"photo": 0, "video": 1, "project": 2, "sidecar": 3}
        files.sort(
            key=lambda path: (
                order[file_kind(path)],
                str(path.parent).casefold(),
                natural_key(path.stem),
                format_group(path.name) != "raw",
            )
        )
        return files

    def import_folder(
        self, source: Path, shoot_id: str, *, source_kind: str = "folder", progress: Callable | None = None
    ) -> dict:
        """Copy everything new from a card or folder into a shoot. The source is never changed."""
        root = self._require_online()
        source = source.resolve()
        if not source.is_dir():
            raise KiraError(HTTPStatus.NOT_FOUND, "Import folder not found")
        if source == root or source.is_relative_to(root) or root.is_relative_to(source):
            raise KiraError(HTTPStatus.BAD_REQUEST, "Choose a folder outside the Kira library")
        self.shoot(shoot_id)
        files = self._source_files(source)
        report = {"total": len(files), "counts": {}, "failed": [], "review": 0, "shoot_id": shoot_id}
        batch: dict = {}
        touched: set[str] = {shoot_id}
        for index, path in enumerate(files, start=1):
            try:
                key = self._source_key(path)
                receipt = self.state.execute("SELECT sha256 FROM receipts WHERE source_key=?", (key,))
                known = self.by_sha.get(receipt[0]["sha256"]) if receipt else None
                if known:
                    batch[(str(path.parent), stem_key(path.name))] = (known[0], known[1])
                    result = {"status": "duplicate"}
                else:
                    rejected = any(
                        part.casefold() in REJECT_SOURCE_DIRS for part in path.relative_to(source).parts[:-1]
                    )
                    result = self.ingest_file(path, shoot_id, source_kind=source_kind, rejected=rejected, batch=batch)
                    digest = self._digest_of(result)
                    if digest:
                        self.state.execute("INSERT OR REPLACE INTO receipts VALUES (?,?,?)", (key, digest, utc_now()))
                    if result.get("shoot_id"):
                        touched.add(result["shoot_id"])
                status = result["status"]
                report["counts"][status] = report["counts"].get(status, 0) + 1
                if status == "review":
                    report["review"] += 1
            except (KiraError, OSError) as exc:
                report["failed"].append({"name": path.name, "error": getattr(exc, "message", str(exc))})
            if progress:
                progress(index, len(files), path.name)
            if index % SAVE_EVERY == 0:
                self._flush(touched)
        self._flush(touched)
        return report

    def _digest_of(self, result: dict) -> str | None:
        if result.get("photo_id") and result.get("file") and result["status"] != "loose":
            try:
                _, photo = self.photo(result["shoot_id"], result["photo_id"])
                return self.file_record(photo, result["file"])["sha256"]
            except KiraError:
                return None
        return None

    def _flush(self, shoot_ids: set[str]) -> None:
        with self.lock:
            for shoot_id in shoot_ids:
                shoot = self.shoots.get(shoot_id)
                if shoot and shoot.get("dirty"):
                    self._save(shoot)

    def flush(self) -> None:
        self._flush(set(self.shoots))

    def check_source(self, source: Path) -> dict:
        """Is everything on a card or folder already safely in the library?"""
        source = source.resolve()
        if not source.is_dir():
            raise KiraError(HTTPStatus.NOT_FOUND, "Folder not found")
        files = self._source_files(source)
        imported = 0
        missing: list[str] = []
        digests: list[str] = []
        for path in files:
            receipt = self.state.execute("SELECT sha256 FROM receipts WHERE source_key=?", (self._source_key(path),))
            if receipt and receipt[0]["sha256"] in self.by_sha:
                imported += 1
                digests.append(receipt[0]["sha256"])
            elif file_kind(path) in {"photo", "video"}:
                missing.append(path.name)
            else:
                imported += 1
        return {"total": len(files), "imported": imported, "not_imported": missing[:50], "sha256": digests}

    # -- Rescanning a shoot folder ----------------------------------------------------

    def rescan(self, shoot_id: str) -> dict:
        """Notice files saved into the shoot folder by an editor, and files that changed or vanished."""
        self._require_online()
        report = {"added": [], "changed": [], "missing": [], "waiting": []}
        with self.lock:
            shoot = self.shoot(shoot_id)
            tracked: dict[Path, tuple[dict | None, dict]] = {}
            for photo in shoot["photos"].values():
                for record in photo["files"]:
                    tracked[self.file_path(shoot, photo, record)] = (photo, record)
            for record in shoot.get("loose", []):
                tracked[shoot["path"] / record["name"]] = (None, record)

            for path, (photo, record) in list(tracked.items()):
                try:
                    info = path.stat()
                except OSError:
                    if not record.get("missing"):
                        record["missing"] = True
                        report["missing"].append(record["name"])
                        self._touch(shoot)
                    continue
                if record.get("missing"):
                    if sha256_file(path) == record["sha256"]:
                        record.pop("missing")
                        record["size"], record["mtime_ns"] = info.st_size, info.st_mtime_ns
                        if photo:
                            self._index_file(shoot, photo, record)
                        self._touch(shoot)
                    continue
                if info.st_size == record["size"] and info.st_mtime_ns == record["mtime_ns"]:
                    continue
                if time.time() - info.st_mtime < SETTLE_SECONDS:
                    report["waiting"].append(record["name"])
                    continue
                digest = sha256_file(path)
                if digest != record["sha256"] and photo and record["role"] in {"original", "edit"}:
                    self._overwritten(shoot, photo, record, path, digest)
                    report["changed"].append(record["name"])
                else:
                    if digest != record["sha256"]:
                        report["changed"].append(record["name"])
                    record.update(sha256=digest, size=info.st_size, mtime_ns=info.st_mtime_ns)
                self._touch(shoot)

            known = {path for path in tracked}
            untracked: list[Path] = []
            for folder in (shoot["path"], shoot["path"] / REJECTS_DIR):
                if not folder.is_dir():
                    continue
                for path in folder.iterdir():
                    if (
                        path.is_file()
                        and path not in known
                        and path.name != MANIFEST_NAME
                        and not path.name.startswith((".", "~"))
                        and path.suffix.casefold() not in TEMPORARY_SUFFIXES
                        and file_kind(path) != "other"
                    ):
                        untracked.append(path)
            order = {"photo": 0, "video": 1, "project": 2, "sidecar": 3}
            untracked.sort(key=lambda path: (order[file_kind(path)], natural_key(path.name)))
        for path in untracked:
            try:
                if time.time() - path.stat().st_mtime < SETTLE_SECONDS:
                    report["waiting"].append(path.name)
                    continue
                rejected = path.parent.name == REJECTS_DIR
                result = self.ingest_file(path, shoot_id, source_kind="desktop", in_place=True, rejected=rejected)
                report["added"].append(result)
            except (KiraError, OSError) as exc:
                report["waiting"].append(f"{path.name}: {getattr(exc, 'message', exc)}")
        self._flush(set(self.shoots))
        return report

    def _overwritten(self, shoot: dict, photo: dict, record: dict, path: Path, digest: str) -> None:
        """An editor saved over a tracked file: keep the new bytes as a new edit."""
        number = max((int(item.get("edit", 0)) for item in photo["files"]), default=0) + 1
        name = f"{photo['stem']}_edit{number}{path.suffix.lower()}"
        target = self._free_path(path.parent, name)
        _set_read_only(path, False)
        self._move(path, target)
        edit = self._record(target, target.name, "edit", digest, record["name"], "desktop")
        edit["edit"] = number
        photo["files"].append(edit)
        _set_read_only(target, True)
        self._index_file(shoot, photo, edit)
        if self.by_sha.get(record["sha256"], ("", "", ""))[2] == record["name"]:
            self.by_sha.pop(record["sha256"], None)
        record["missing"] = True

    def restore_file(self, shoot_id: str, photo_id: str, name: str, source: Path) -> None:
        """Put back a tracked file that went missing, from a verified copy of its bytes."""
        self._require_online()
        with self.lock:
            shoot, photo = self.photo(shoot_id, photo_id)
            record = self.file_record(photo, name)
            if sha256_file(source) != record["sha256"]:
                raise KiraError(HTTPStatus.CONFLICT, "The restored copy does not match the original file")
            target = self.file_path(shoot, photo, record)
            if target.exists():
                raise KiraError(HTTPStatus.CONFLICT, f"{name} already exists")
            self._move(source, target)
            info = target.stat()
            record.update(size=info.st_size, mtime_ns=info.st_mtime_ns)
            record.pop("missing", None)
            if record["role"] in READ_ONLY_ROLES:
                _set_read_only(target, True)
            self._index_file(shoot, photo, record)
            self._save(shoot)

    # -- Culling, Finals, tags and deletion ---------------------------------------------

    def set_rejected(self, shoot_id: str, photo_ids: list[str], rejected: bool) -> dict:
        self._require_online()
        with self.lock:
            shoot = self.shoot(shoot_id)
            moved = 0
            for photo_id in photo_ids:
                photo = shoot["photos"].get(str(photo_id))
                if photo is None or bool(photo.get("rejected")) == bool(rejected):
                    continue
                plan = []
                for record in photo["files"]:
                    if record["role"] == "copy" or record.get("missing"):
                        continue
                    source = self.file_path(shoot, photo, record)
                    folder = shoot["path"] / REJECTS_DIR if rejected else shoot["path"]
                    if (folder / record["name"]).exists():
                        raise KiraError(HTTPStatus.CONFLICT, f"{record['name']} already exists in the destination")
                    plan.append((source, folder / record["name"]))
                done: list[tuple[Path, Path]] = []
                try:
                    for source, target in plan:
                        self._move(source, target)
                        done.append((source, target))
                except (OSError, KiraError) as exc:
                    for source, target in reversed(done):
                        self._move(target, source)
                    raise KiraError(HTTPStatus.CONFLICT, f"Could not move {photo['stem']}: {exc}") from exc
                photo["rejected"] = bool(rejected)
                moved += 1
            self._save(shoot)
            return {"moved": moved}

    def set_final(self, shoot_id: str, photo_id: str, name: str | None) -> dict:
        self._require_online()
        with self.lock:
            shoot, photo = self.photo(shoot_id, photo_id)
            if name is not None:
                record = self.file_record(photo, name)
                if record["role"] not in {"original", "edit"} or format_group(name) == "video":
                    raise KiraError(HTTPStatus.BAD_REQUEST, "Only an original or an edit can be the Final")
            photo["final"] = name
            self._save(shoot)
            return self.photo_view(photo)

    def update_tags(self, shoot_id: str, photo_ids: list[str], add: list[str], remove: list[str]) -> dict:
        self._require_online()
        adds = [re.sub(r"\s+", " ", str(tag).strip())[:40] for tag in add if str(tag).strip()]
        removes = {str(tag).strip().casefold() for tag in remove}
        with self.lock:
            shoot = self.shoot(shoot_id)
            for photo_id in photo_ids:
                photo = shoot["photos"].get(str(photo_id))
                if photo is None:
                    continue
                tags = [tag for tag in photo.get("tags", []) if tag.casefold() not in removes]
                for tag in adds:
                    if tag.casefold() not in {existing.casefold() for existing in tags}:
                        tags.append(tag)
                photo["tags"] = tags[:32]
            self._save(shoot)
            return {"updated": len(photo_ids)}

    def delete_file(self, shoot_id: str, photo_id: str, name: str | None = None) -> dict:
        """Move one version, or a whole photo, to the library trash. Nothing is erased."""
        self._require_online()
        with self.lock:
            shoot, photo = self.photo(shoot_id, photo_id)
            records = [self.file_record(photo, name)] if name else list(photo["files"])
            for record in records:
                path = self.file_path(shoot, photo, record)
                if path.exists():
                    self._to_trash(shoot, path, record["sha256"])
                if self.by_sha.get(record["sha256"], ("", "", ""))[2] == record["name"]:
                    self.by_sha.pop(record["sha256"], None)
                photo["files"].remove(record)
                if photo.get("final") == record["name"]:
                    photo["final"] = None
            if not any(record["role"] in MEDIA_ROLES for record in photo["files"]):
                for record in list(photo["files"]):
                    path = self.file_path(shoot, photo, record)
                    if path.exists():
                        self._to_trash(shoot, path, record["sha256"])
                captured = photo["capture"].get("time")
                if captured:
                    self.by_time.get(captured, set()).discard((shoot["id"], photo["id"]))
                del shoot["photos"][photo["id"]]
            self._save(shoot)
            return {"trashed": len(records)}

    def delete_shoot(self, shoot_id: str) -> dict:
        """Move a whole shoot folder into the library trash. Nothing is erased until Empty trash."""
        root = self._require_online()
        with self.lock:
            shoot = self.shoot(shoot_id)
            folder = shoot["path"]
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = self._free_path(root / TRASH_DIR / stamp, f"{shoot['year']} {shoot['name']}")
            files = [path for path in folder.rglob("*") if path.is_file()] if folder.is_dir() else []
            if folder.is_dir():
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    folder.rename(target)
                except OSError as exc:
                    raise KiraError(
                        HTTPStatus.CONFLICT, f"Could not move the shoot folder (is a file open in another app?): {exc}"
                    ) from exc
            digests = {
                self.relative_path(shoot, photo, record): record["sha256"]
                for photo in shoot["photos"].values()
                for record in photo["files"]
            }
            now = utc_now()
            self.state.executemany(
                "INSERT INTO trash VALUES (?,?,?,?,?,?)",
                [
                    (
                        uuid.uuid4().hex[:12],
                        shoot_id,
                        path.relative_to(root).as_posix(),
                        str(target / path.relative_to(folder)),
                        digests.get(path.relative_to(root).as_posix(), ""),
                        now,
                    )
                    for path in files
                ],
            )
            self.state.execute("DELETE FROM outbox WHERE shoot_id=? AND state='waiting'", (shoot_id,))
            for digest, (owner, _, _) in list(self.by_sha.items()):
                if owner == shoot_id:
                    del self.by_sha[digest]
            for keys in self.by_time.values():
                keys.difference_update({key for key in keys if key[0] == shoot_id})
            del self.shoots[shoot_id]
            for other in self.shoots.values():  # another shoot may hold a file the deleted one indexed first
                for photo in other["photos"].values():
                    for record in photo["files"]:
                        self._index_file(other, photo, record)
            (self.cache_root / f"{shoot_id}.json").unlink(missing_ok=True)
            try:
                folder.parent.rmdir()  # the year folder, only if it is now empty
            except OSError:
                pass
            return {"trashed": len(files), "trash_folder": str(target)}

    def trash_items(self) -> list[dict]:
        rows = self.state.execute(
            "SELECT trash.*, trash_reasons.reason FROM trash LEFT JOIN trash_reasons ON trash_reasons.trash_id = trash.id"
            " ORDER BY deleted_at DESC"
        )
        return [dict(row) for row in rows]

    def empty_trash(self) -> dict:
        """Permanently erase everything in the library trash. Only ever called on an explicit click."""
        root = self._require_online()
        rows = self.trash_items()
        trash = root / TRASH_DIR
        removed = 0
        for path in sorted(trash.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file():
                _set_read_only(path, False)
                path.unlink()
                removed += 1
            else:
                try:
                    path.rmdir()
                except OSError:
                    pass
        self.state.execute("DELETE FROM trash")
        self.state.execute("DELETE FROM trash_reasons")
        return {"removed": removed, "items": rows}

    # -- Previews -------------------------------------------------------------------

    def preview(self, shoot_id: str, photo_id: str, name: str, requested_size: int) -> Path:
        """A cached JPEG preview. Cached previews keep working while the drive is unplugged."""
        shoot, photo = self.photo(shoot_id, photo_id)
        record = self.file_record(photo, name)
        return self._preview_of(self.file_path(shoot, photo, record), record["sha256"], requested_size)

    def review_preview(self, review_id: str, requested_size: int) -> Path:
        path = self.review_path(review_id)
        rows = self.state.execute("SELECT payload FROM review WHERE id=?", (review_id,))
        return self._preview_of(path, json.loads(rows[0]["payload"])["sha256"], requested_size)

    def _preview_of(self, source: Path, digest: str, requested_size: int) -> Path:
        size = 1600 if requested_size > 640 else 480
        destination = self.thumbnails_root / f"{digest}-{size}.jpg"
        if destination.exists():
            return destination
        if not source.is_file():
            raise KiraError(HTTPStatus.NOT_FOUND, "Preview is not available while the library drive is unplugged")
        with self.thumbnail_slots:
            if destination.exists():
                return destination
            temporary = destination.with_name(f"{destination.stem}-{threading.get_ident()}.tmp")
            try:
                suffix = source.suffix.casefold()
                if suffix in VIDEO_EXTENSIONS:
                    opened = self._video_frame(source)
                    if opened is None:
                        raise KiraError(HTTPStatus.NOT_FOUND, "This video has no frame to show")
                elif suffix in RAW_EXTENSIONS:
                    opened = self._raw_preview(source)
                    if opened is None:
                        raise KiraError(HTTPStatus.NOT_FOUND, "This RAW file has no embedded preview")
                else:
                    opened = Image.open(source)
                    opened.draft("RGB", (size * 2, size * 2))
                with opened:
                    image = ImageOps.exif_transpose(opened)
                    image.thumbnail((size, size), Image.Resampling.LANCZOS)
                    if image.mode != "RGB":
                        image = image.convert("RGB")
                    image.save(temporary, format="JPEG", quality=84)
                os.replace(temporary, destination)
            except KiraError:
                raise
            except Exception as exc:  # noqa: BLE001 - undecodable formats have no preview
                raise KiraError(HTTPStatus.NOT_FOUND, f"No preview for {source.name}") from exc
            finally:
                temporary.unlink(missing_ok=True)
        return destination

    def close(self) -> None:
        try:
            if self.online:
                self.flush()
        finally:
            self.state.close()
