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
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps

from .errors import KiraError
from .media import (
    CHUNK_COPY_SIZE,
    PHOTO_EXTENSIONS,
    atomic_json_write,
    match_key,
    resolve_directory,
    safe_filename,
    scan_photo_directory,
    unique_destination,
    utc_now,
)


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
                raise KiraError(HTTPStatus.BAD_REQUEST, "Job has no source folder")
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
