"""Job manifests, resumable uploads, and transfer-bundle creation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import uuid
import zipfile
from http import HTTPStatus
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps

from .errors import KiraError
from .preservation import Preservation
from .media import (
    CHUNK_COPY_SIZE,
    atomic_json_write,
    match_key,
    resolve_directory,
    safe_filename,
    scan_photo_directory,
    sha256_file,
    unique_destination,
    utc_now,
)

MAX_TAGS_PER_ASSET = 32
MAX_TAG_LENGTH = 40


def normalize_tag(value: object) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())[:MAX_TAG_LENGTH].strip()
    if not cleaned:
        raise KiraError(HTTPStatus.BAD_REQUEST, "Enter a non-empty tag")
    return cleaned


class KiraStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.jobs_root = self.root / "jobs"
        self.thumbnails_root = self.root / "thumbnails"
        self.config_path = self.root / "config.json"
        self.tags_path = self.root / "tags.json"
        self.lock = threading.RLock()
        self.bundle_lock = threading.Lock()
        self.thumbnail_slots = threading.Semaphore(2)
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.thumbnails_root.mkdir(parents=True, exist_ok=True)
        self.preservation = Preservation(self.root / "library")
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

    # -- Photo tags ---------------------------------------------------------------

    def _load_tags(self) -> dict[str, list[str]]:
        if not self.tags_path.exists():
            return {}
        try:
            payload = json.loads(self.tags_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        tags: dict[str, list[str]] = {}
        for asset_id, names in payload.items():
            if not isinstance(names, list):
                continue
            merged: list[str] = []
            seen: set[str] = set()
            for name in names:
                key = str(name).strip().casefold()
                if key and key not in seen:
                    seen.add(key)
                    merged.append(str(name).strip())
            if merged:
                tags[str(asset_id)] = merged
        return tags

    def all_tags(self) -> list[dict]:
        counts: dict[str, int] = {}
        for names in self._load_tags().values():
            for name in names:
                counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
        return [
            {"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda item: item[0])
        ]

    def update_asset_tags(self, asset_ids: list[str], add_values: list, remove_values: list) -> dict:
        adds = [normalize_tag(value) for value in add_values]
        removes = [str(value or "").strip() for value in remove_values]
        ids = [str(item) for item in asset_ids]
        if not ids:
            raise KiraError(HTTPStatus.BAD_REQUEST, "Select at least one photo")
        if not adds and not removes:
            raise KiraError(HTTPStatus.BAD_REQUEST, "Provide a tag to add or remove")
        remove_keys = {value.casefold() for value in removes if value}
        with self.lock:
            tags = self._load_tags()
            assignments: dict[str, list[str]] = {}
            for asset_id in ids:
                existing = [name for name in tags.get(asset_id, []) if name.casefold() not in remove_keys]
                merged: list[str] = []
                seen: set[str] = set(existing_name.casefold() for existing_name in existing)
                merged.extend(existing)
                for name in adds:
                    key = name.casefold()
                    if key not in seen:
                        seen.add(key)
                        merged.append(name)
                if len(merged) > MAX_TAGS_PER_ASSET:
                    raise KiraError(
                        HTTPStatus.BAD_REQUEST,
                        f"A photo can hold at most {MAX_TAGS_PER_ASSET} tags",
                    )
                assignments[asset_id] = merged
                if merged:
                    tags[asset_id] = merged
                else:
                    tags.pop(asset_id, None)
            atomic_json_write(self.tags_path, tags)
        return {"assignments": assignments, "tags": self.all_tags()}

    def annotate_scan(self, scan: dict) -> None:
        """Attach stored tag lists to every scanned asset in place."""
        tags = self._load_tags()
        for asset in scan.get("assets", []):
            asset["tags"] = list(tags.get(str(asset.get("id")), []))

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
            for child in ("returns", "uploads", ".cache"):
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
            raise KiraError(
                HTTPStatus.CONFLICT,
                "The folder changed; scan it again before creating the job",
            )

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
                sources = (([photo_candidates[0]] if photo_candidates else [])
                           + asset.get("video_files", []) + asset["other_files"])
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
        expected_sha256: str | None = None,
    ) -> dict:
        if kind != "returns":
            raise KiraError(HTTPStatus.BAD_REQUEST, "Upload kind must be returns")
        if size < 0:
            raise KiraError(HTTPStatus.BAD_REQUEST, "File size is invalid")
        if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                                           or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)):
            raise KiraError(HTTPStatus.BAD_REQUEST, "A valid SHA-256 checksum is required")
        clean_name = safe_filename(filename)
        # A content-bound identity supports safe retries. Older clients without
        # a sender digest get a new session, never a metadata-only resume.
        identity = expected_sha256 or uuid.uuid4().hex
        resume_source = f"{job_id}\0{kind}\0{clean_name}\0{size}\0{identity}"
        upload_id = hashlib.sha256(resume_source.encode("utf-8")).hexdigest()[:24]
        uploads = self._job_dir(job_id) / "uploads"
        metadata_path = uploads / f"{upload_id}.json"
        part_path = uploads / f"{upload_id}.part"

        with self.lock:
            self._load_manifest(job_id)
            if metadata_path.exists():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    metadata = {}
                if metadata.get("status") == "complete":
                    record = self.complete_upload(job_id, upload_id)
                    return {
                        "upload_id": upload_id,
                        "offset": size,
                        "size": size,
                        "complete": True,
                        "record": record,
                    }
                if metadata.get("status") == "publishing":
                    record = self.complete_upload(job_id, upload_id)
                    return {"upload_id": upload_id, "offset": size, "size": size,
                            "complete": True, "record": record}
                if metadata.get("status") == "checksum_failed":
                    # The whole-file sender check proved this staged stream is
                    # not the selected file. A new selection can resend it.
                    part_path.unlink(missing_ok=True)
            metadata = {
                "id": upload_id,
                "job_id": job_id,
                "kind": kind,
                "filename": clean_name,
                "size": size,
                "last_modified": last_modified,
                "created_at": utc_now(),
                "status": "uploading",
                "expected_sha256": expected_sha256,
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
            if metadata.get("status") != "uploading":
                raise KiraError(HTTPStatus.CONFLICT, "Upload is already received", expected_offset=expected_size)
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
        if not re.fullmatch(r"[a-f0-9]{24}", upload_id):
            raise KiraError(HTTPStatus.NOT_FOUND, "Upload not found")
        uploads = self._job_dir(job_id) / "uploads"
        metadata_path = uploads / f"{upload_id}.json"
        part_path = uploads / f"{upload_id}.part"
        if not metadata_path.exists():
            raise KiraError(HTTPStatus.NOT_FOUND, "Upload not found")

        with self.lock:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            manifest = self._load_manifest(job_id)
            saved = metadata.get("record")
            if saved:
                record = next((r for r in manifest["returns"] if r["id"] == saved["id"]), saved)
                # Legacy completed uploads are adopted only after verifying bytes.
                object_path = self.preservation.object_path(record["sha256"])
                if not object_path.exists():
                    self.preservation.retain(Path(record["storage_path"]), record["sha256"])
                self.preservation.receipt(job_id, "returns", record)
                destination = Path(record["storage_path"])
                operation = f"return:{job_id}:{upload_id}:{destination}"
                self.preservation.publish(operation, record["sha256"], destination)
                if not any(r["id"] == record["id"] for r in manifest["returns"]):
                    manifest["returns"].append(record)
                    self._save_manifest(manifest)
                metadata.update(status="complete", record=record)
                atomic_json_write(metadata_path, metadata)
                part_path.unlink(missing_ok=True)
                return record
            expected_size = int(metadata["size"])
            actual_size = part_path.stat().st_size if part_path.exists() else 0
            if not part_path.exists() or actual_size != expected_size:
                raise KiraError(
                    HTTPStatus.CONFLICT,
                    "Upload is incomplete",
                    expected_size=expected_size,
                    actual_size=actual_size,
                )

            try:
                retained = self.preservation.retain(part_path, metadata.get("expected_sha256"))
            except KiraError as exc:
                if "checksum" in str(exc):
                    metadata["status"] = "checksum_failed"
                    atomic_json_write(metadata_path, metadata)
                raise
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
            record = {
                "id": uuid.uuid4().hex[:12],
                "filename": destination.name,
                "original_filename": metadata["filename"],
                "size": actual_size,
                "sha256": retained["sha256"],
                "storage_path": str(destination),
                "created_at": utc_now(),
                "verification": "sender_and_receiver" if metadata.get("expected_sha256") else "receiver_only",
            }

            self._attach_return_match(manifest, record)
            # Record the chosen output and keep staging before external writes.
            # A failed commit can retry the same operation and destination.
            metadata.update(status="publishing", record=record)
            atomic_json_write(metadata_path, metadata)
            self.preservation.receipt(job_id, "returns", record)
            self.preservation.publish(f"return:{job_id}:{upload_id}:{destination}", record["sha256"], destination)
            manifest["returns"].append(record)
            if manifest.get("source_directory"):
                manifest["postprocess"] = {"status": "pending"}
            self._save_manifest(manifest)
            metadata["status"] = "complete"
            metadata["record"] = record
            atomic_json_write(metadata_path, metadata)
            part_path.unlink(missing_ok=True)
            return record

    def _attach_return_match(self, manifest: dict, returned: dict) -> None:
        key = match_key(returned["original_filename"])
        matches = [item for item in manifest["files"] if match_key(item["original_filename"]) == key]
        if len(matches) == 1:
            returned["match_status"] = "matched"
            returned["association_status"] = "suggested"
            returned["match_method"] = "filename"
            returned["matched_file_id"] = matches[0]["id"]
            returned["matched_filename"] = matches[0]["filename"]
        elif len(matches) > 1:
            returned["match_status"] = "ambiguous"
            returned["candidate_file_ids"] = [item["id"] for item in matches]
        else:
            returned["match_status"] = "unmatched"

    def resolve_file(self, job_id: str, kind: str, record_id: str) -> tuple[Path, dict]:
        with self.lock:
            manifest = self._load_manifest(job_id)
            collection_name = "files" if kind == "originals" else "returns"
            for record in manifest[collection_name]:
                if record["id"] != record_id:
                    continue
                stored_path = record["source_path"] if kind == "originals" else record["storage_path"]
                if not record.get("sha256") or not self.preservation.object_path(record["sha256"]).exists():
                    source = Path(stored_path)
                    if not source.exists():
                        raise KiraError(HTTPStatus.NOT_FOUND, "File is missing from disk")
                    retained = self.preservation.retain(source, record.get("sha256"))
                    record.update(sha256=retained["sha256"], size=retained["size"])
                path = self.preservation.verified_object(record["sha256"])
                record["revision_id"] = self.preservation.receipt(job_id, collection_name, record)
                if kind == "originals":
                    record["snapshot_path"] = str(path)
                self._save_manifest(manifest)
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
            sources: list[tuple[dict, Path]] = []
            signature_parts: list[str] = []
            for record in manifest["files"]:
                source = Path(record["source_path"])
                if record.get("sha256") and self.preservation.object_path(record["sha256"]).exists():
                    snapshot = self.preservation.verified_object(record["sha256"])
                else:
                    if not source.exists():
                        raise KiraError(HTTPStatus.NOT_FOUND, f"Source file is missing: {record['filename']}")
                    retained = self.preservation.retain(source, record.get("sha256"))
                    record.update(sha256=retained["sha256"], size=retained["size"])
                    snapshot = Path(retained["path"])
                record["snapshot_path"] = str(snapshot)
                record["bundle_filename"] = record.get("bundle_filename", record["original_filename"])
                record["revision_id"] = self.preservation.receipt(job_id, "files", record)
                sources.append((record, snapshot))
                signature_parts.append(f"{record['id']}\0{record['bundle_filename']}\0{record['sha256']}")
            signature = hashlib.sha256("\n".join(signature_parts).encode("utf-8")).hexdigest()
            cached_digest = manifest.get("bundle_sha256")
            valid_cache = (cache_path.is_file() and manifest.get("bundle_signature") == signature
                           and cached_digest and sha256_file(cache_path) == cached_digest)
            if not valid_cache:
                temp = cache_path.with_suffix(".zip.tmp")
                with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                    for record, source in sources:
                        digest = hashlib.sha256()
                        info = zipfile.ZipInfo.from_file(source, arcname=record["bundle_filename"])
                        info.compress_type = zipfile.ZIP_STORED
                        with source.open("rb") as incoming, archive.open(info, "w", force_zip64=True) as outgoing:
                            while chunk := incoming.read(CHUNK_COPY_SIZE):
                                digest.update(chunk)
                                outgoing.write(chunk)
                        if digest.hexdigest() != record["sha256"]:
                            raise KiraError(HTTPStatus.CONFLICT, "A retained original failed verification")
                with temp.open("rb+") as handle:
                    os.fsync(handle.fileno())
                cached_digest = sha256_file(temp)
                os.replace(temp, cache_path)
            with self.lock:
                # Merge only our snapshot fields into the newest manifest. An
                # upload or organization may have committed during packaging.
                current = self._load_manifest(job_id)
                by_id = {r["id"]: r for r, _ in sources}
                for record in current["files"]:
                    snapshot = by_id.get(record["id"])
                    if snapshot:
                        for key in ("sha256", "size", "snapshot_path", "bundle_filename", "revision_id"):
                            record[key] = snapshot[key]
                current["bundle_signature"] = signature
                current["bundle_sha256"] = cached_digest
                current["bundle_verified_at"] = utc_now()
                self._save_manifest(current)
            return cache_path, f"{safe_filename(manifest['name'])}-originals.zip"

    def organize_source_folder(self, job_id: str) -> dict:
        from .organization import organize
        return organize(self, job_id)

    def delete_job(self, job_id: str) -> dict:
        # Wait for an in-progress bundle build so its temporary file is never
        # removed underneath the sequential packaging pass.
        with self.bundle_lock:
            with self.lock:
                folder = self._job_dir(job_id)
                if not folder.resolve().is_relative_to(self.jobs_root.resolve()):
                    raise KiraError(HTTPStatus.CONFLICT, "Job path is outside working storage")
                manifest = self._load_manifest(job_id)
                for path in (folder / "uploads").glob("*.json"):
                    metadata = json.loads(path.read_text(encoding="utf-8"))
                    part = path.with_suffix(".part")
                    if metadata.get("status") != "complete" and (
                        metadata.get("record") or (part.exists() and part.stat().st_size)
                    ):
                        raise KiraError(HTTPStatus.CONFLICT, "A received or partial upload still needs recovery; finish it before removing this job")
                for part in (folder / "uploads").glob("*.part"):
                    if not part.with_suffix(".json").exists() and part.stat().st_size:
                        raise KiraError(HTTPStatus.CONFLICT, "Unrecorded upload bytes need recovery before this job can be removed")
                for kind, path_key in (("files", "source_path"), ("returns", "storage_path")):
                    for record in manifest[kind]:
                        if not record.get("sha256") or not self.preservation.object_path(record["sha256"]).exists():
                            retained = self.preservation.retain(Path(record[path_key]), record.get("sha256"))
                            record.update(sha256=retained["sha256"], size=retained["size"])
                        self.preservation.receipt(job_id, kind, record)
                history_folder = self.preservation.root / "jobs"
                history_folder.mkdir(parents=True, exist_ok=True)
                atomic_json_write(history_folder / f"{job_id}.json", manifest)
                preserved = manifest.get("source_directory")
                shutil.rmtree(folder)
                return {"deleted": True, "job_id": job_id, "preserved_source_directory": preserved,
                        "history_preserved": True, "checkpoint_count": len(self.preservation.history(job_id))}
