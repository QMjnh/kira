from __future__ import annotations

import base64
import concurrent.futures
import ctypes
import hashlib
import http.client
import json
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
import zipfile
from ctypes import wintypes
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PICKER_SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
UPLOAD_SCOPE = "https://www.googleapis.com/auth/photoslibrary.appendonly"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
PICKER_ENDPOINT = "https://photospicker.googleapis.com/v1"
LIBRARY_HOST = "photoslibrary.googleapis.com"
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PHOTO_SUFFIXES = {
    ".avif", ".bmp", ".gif", ".heic", ".heif", ".ico", ".jpeg", ".jpg",
    ".png", ".tif", ".tiff", ".webp", ".dng", ".cr2", ".cr3", ".nef",
    ".arw", ".orf", ".raf", ".rw2", ".pef", ".srw", ".raw",
}
VIDEO_SUFFIXES = {
    ".3g2", ".3gp", ".asf", ".avi", ".divx", ".m2t", ".m2ts", ".m4v",
    ".mkv", ".mmv", ".mod", ".mov", ".mp4", ".mpg", ".mts", ".tod", ".wmv",
}
MEDIA_SUFFIXES = PHOTO_SUFFIXES | VIDEO_SUFFIXES
VARIANT_SUFFIX = re.compile(
    r"(?:[\s_-]+(?:(?:edited?|edit|final|copy|lr|lightroom|google)(?:[\s_-]*v?\d+)?|v\d+)|\s*\(\d+\))$",
    re.IGNORECASE,
)
IMPORT_DOWNLOAD_MODES = {"automatic", "zip", "files"}
DEFAULT_ZIP_THRESHOLD = 50
MIN_ZIP_THRESHOLD = 2
MAX_ZIP_THRESHOLD = 2000
MAX_CONCURRENT_DOWNLOADS = 10


class GooglePhotosError(Exception):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _windows_protect(data: bytes) -> bytes:
    source = ctypes.create_string_buffer(data)
    incoming = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(incoming), "Kira Google Photos", None, None, None, 0, ctypes.byref(outgoing)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.pbData)


def _windows_unprotect(data: bytes) -> bytes:
    source = ctypes.create_string_buffer(data)
    incoming = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(incoming), None, None, None, None, 0, ctypes.byref(outgoing)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.pbData)


def _safe_filename(value: str) -> str:
    name = Path(str(value).replace("\\", "/")).name.strip().rstrip(". ")
    name = INVALID_FILENAME.sub("_", name)
    if not name or name in {".", ".."}:
        name = "google-photo"
    return f"{Path(name).stem[:150]}{Path(name).suffix[:20]}"


def _unique_destination(folder: Path, filename: str, marker: str = "v") -> Path:
    candidate = folder / _safe_filename(filename)
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        versioned = folder / f"{candidate.stem}__{marker}{index}{candidate.suffix}"
        if not versioned.exists():
            return versioned
        index += 1


def _name_key(filename: str) -> str:
    stem = Path(filename).stem.strip()
    previous = None
    while stem != previous:
        previous = stem
        stem = VARIANT_SUFFIX.sub("", stem).strip(" _-")
    return stem.casefold()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _google_remote_hash(path: Path) -> str:
    """Return the content hash format used by Google Photos' remote-match endpoint."""
    return _local_content_hashes(path)[1]


def _local_content_hashes(path: Path) -> tuple[str, str]:
    """Return Kira's persistent SHA-256 and Google Photos' remote-match SHA-1."""
    local_digest = hashlib.sha256()
    digest = hashlib.sha1()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            local_digest.update(chunk)
            digest.update(chunk)
    return local_digest.hexdigest(), base64.b64encode(digest.digest()).decode("ascii")


class GooglePhotosService:
    """Small server-side Google Photos client for Kira's local desktop workflow."""

    def __init__(self, data_root: Path, app_root: Path | None = None) -> None:
        from google_photos_web import GooglePhotosWebService

        self.root = data_root.resolve()
        self.app_root = (app_root or Path(__file__).resolve().parent).resolve()
        self.credentials_path = self.app_root / "google-oauth-client.json"
        self.token_path = self.root / "google-token.json"
        self.inbox = self.root / "google-photos-inbox"
        self.operations_root = self.root / "google-operations"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.operations_root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.oauth_states: dict[str, dict] = {}
        self.operations: dict[str, dict] = {}
        self.web = GooglePhotosWebService(self.root, cookie_export_root=self.app_root)

    def _client(self) -> dict:
        client_id = os.environ.get("KIRA_GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("KIRA_GOOGLE_CLIENT_SECRET", "").strip()
        if client_id:
            return {"client_id": client_id, "client_secret": client_secret}
        if not self.credentials_path.exists():
            raise GooglePhotosError(f"Add Google OAuth credentials at {self.credentials_path}")
        try:
            payload = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            client = payload.get("installed") or payload.get("web") or payload
            if not isinstance(client.get("client_id"), str):
                raise ValueError
            return {
                "client_id": client["client_id"],
                "client_secret": str(client.get("client_secret", "")),
            }
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise GooglePhotosError("Google OAuth credential file is invalid") from exc

    def status(self) -> dict:
        try:
            self._client()
            configured = True
        except GooglePhotosError:
            configured = False
        return {
            "configured": configured,
            "connected": self.token_path.exists(),
            "credentials_path": str(self.credentials_path),
            "inbox": str(self.inbox),
            "organizer": self.web.status(),
        }

    def resolve_import_destination(self, folder: str) -> Path:
        """Resolve an import destination, accepting the legacy /inbox alias."""
        value = str(folder or "").strip().replace("\\", "/")
        normalized = value.casefold().rstrip("/")
        if not value or normalized in {"", ".", "/inbox", "inbox"}:
            target = self.inbox
        elif normalized.startswith("/inbox/") or normalized.startswith("inbox/"):
            parts = [part for part in value.split("/") if part and part != "."]
            target = self.inbox.joinpath(*parts[1:])
        else:
            # Imports may be placed in an existing collection outside Kira's
            # data directory when a fully qualified path is supplied.
            candidate = Path(value)
            target = candidate if candidate.is_absolute() else self.inbox / candidate

        target = target.resolve()
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _save_tokens(self, payload: dict) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        protected = _windows_protect(raw) if os.name == "nt" else raw
        wrapper = {
            "encrypted": os.name == "nt",
            "data": base64.b64encode(protected).decode("ascii"),
        }
        temp = self.token_path.with_suffix(".tmp")
        temp.write_text(json.dumps(wrapper), encoding="utf-8")
        os.replace(temp, self.token_path)
        if os.name != "nt":
            try:
                os.chmod(self.token_path, 0o600)
            except OSError:
                pass

    def _load_tokens(self) -> dict:
        if not self.token_path.exists():
            raise GooglePhotosError("Connect Google Photos first")
        try:
            wrapper = json.loads(self.token_path.read_text(encoding="utf-8"))
            raw = base64.b64decode(wrapper["data"])
            if wrapper.get("encrypted"):
                if os.name != "nt":
                    raise GooglePhotosError("Google credentials belong to another computer")
                raw = _windows_unprotect(raw)
            return json.loads(raw)
        except GooglePhotosError:
            raise
        except Exception as exc:
            raise GooglePhotosError("Stored Google credentials are unreadable; reconnect Google Photos") from exc

    def start_oauth(self, redirect_uri: str) -> dict:
        client = self._client()
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with self.lock:
            self.oauth_states[state] = {
                "verifier": verifier,
                "redirect_uri": redirect_uri,
                "expires_at": time.time() + 600,
            }
        query = urlencode(
            {
                "client_id": client["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": f"{PICKER_SCOPE} {UPLOAD_SCOPE}",
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return {"authorization_url": f"{AUTH_ENDPOINT}?{query}"}

    def finish_oauth(self, code: str, state: str) -> None:
        with self.lock:
            pending = self.oauth_states.pop(state, None)
        if not pending or pending["expires_at"] < time.time():
            raise GooglePhotosError("Google sign-in expired or could not be verified")
        client = self._client()
        token = self._request_json(
            TOKEN_ENDPOINT,
            method="POST",
            form={
                "code": code,
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "redirect_uri": pending["redirect_uri"],
                "grant_type": "authorization_code",
                "code_verifier": pending["verifier"],
            },
        )
        if not token.get("access_token"):
            raise GooglePhotosError("Google did not return an access token")
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
        self._save_tokens(token)

    def _access_token(self) -> str:
        with self.lock:
            token = self._load_tokens()
            if token.get("access_token") and float(token.get("expires_at", 0)) > time.time():
                return str(token["access_token"])
            refresh_token = token.get("refresh_token")
            if not refresh_token:
                raise GooglePhotosError("Google sign-in expired; reconnect Google Photos")
            client = self._client()
            refreshed = self._request_json(
                TOKEN_ENDPOINT,
                method="POST",
                form={
                    "client_id": client["client_id"],
                    "client_secret": client["client_secret"],
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            token.update(refreshed)
            token["refresh_token"] = refresh_token
            token["expires_at"] = time.time() + int(refreshed.get("expires_in", 3600)) - 60
            self._save_tokens(token)
            return str(token["access_token"])

    def disconnect(self) -> dict:
        try:
            token = self._load_tokens()
            revocation_token = token.get("refresh_token") or token.get("access_token")
            if revocation_token:
                self._request_json(REVOKE_ENDPOINT, method="POST", form={"token": revocation_token})
        except GooglePhotosError:
            pass
        if self.token_path.exists():
            self.token_path.unlink()
        return {"connected": False}

    def import_web_session(self, cookies_path: Path, account_index: int = 0) -> dict:
        return self.web.import_session(cookies_path, account_index)

    def disconnect_web_session(self) -> dict:
        return self.web.disconnect()

    def albums(self) -> dict:
        return {"albums": self.web.list_albums()}

    def create_picker_session(self, max_items: int = 2000) -> dict:
        max_items = max(1, min(int(max_items), 2000))
        return self._request_json(
            f"{PICKER_ENDPOINT}/sessions",
            method="POST",
            token=self._access_token(),
            payload={"pickingConfig": {"maxItemCount": str(max_items)}},
        )

    def picker_session(self, session_id: str) -> dict:
        return self._request_json(
            f"{PICKER_ENDPOINT}/sessions/{session_id}", token=self._access_token()
        )

    def _list_picked_items(self, session_id: str) -> list[dict]:
        items: list[dict] = []
        page_token = ""
        while True:
            query = {"sessionId": session_id, "pageSize": "100"}
            if page_token:
                query["pageToken"] = page_token
            result = self._request_json(
                f"{PICKER_ENDPOINT}/mediaItems?{urlencode(query)}", token=self._access_token()
            )
            items.extend(result.get("mediaItems", []))
            page_token = str(result.get("nextPageToken", ""))
            if not page_token:
                return items

    def start_import(
        self,
        session_id: str,
        destination: Path | None = None,
        download_mode: object = "automatic",
        zip_threshold: object = DEFAULT_ZIP_THRESHOLD,
        album_title: str | None = None,
        archive: bool | None = None,
    ) -> dict:
        target = (destination or self.inbox).resolve()
        if not target.is_dir():
            raise GooglePhotosError("Choose an existing local collection folder")
        mode = str(download_mode).strip().casefold()
        threshold = int(zip_threshold)
        if mode not in IMPORT_DOWNLOAD_MODES:
            raise GooglePhotosError("Download method must be Automatic, Always ZIP, or Never ZIP")
        if threshold < MIN_ZIP_THRESHOLD or threshold > MAX_ZIP_THRESHOLD:
            raise GooglePhotosError(
                f"ZIP threshold must be between {MIN_ZIP_THRESHOLD} and {MAX_ZIP_THRESHOLD}"
            )
        title = str(album_title or "").strip()
        if archive is None:
            archive = bool(title)
        if archive and not title:
            raise GooglePhotosError("An album name is required when automatic organization is enabled")
        if archive and not self.web.status().get("connected"):
            raise GooglePhotosError(
                "Connect the Google Photos web session before enabling automatic album and Archive"
            )
        operation = self._new_operation("import", directory=target)
        self._update_operation(
            operation["id"],
            album_title=title or None,
            archive=bool(archive),
            organize_after_import=bool(archive),
        )
        threading.Thread(
            target=self._run_import,
            args=(operation["id"], session_id, target, mode, threshold, title, bool(archive)),
            daemon=True,
        ).start()
        return operation

    def start_upload(self, paths: list[Path]) -> dict:
        supported = [path.resolve() for path in paths if path.suffix.casefold() in MEDIA_SUFFIXES]
        if not supported:
            raise GooglePhotosError("Select at least one supported photo or video")
        operation = self._new_operation("upload", total=len(supported))
        threading.Thread(
            target=self._run_upload, args=(operation["id"], supported), daemon=True
        ).start()
        return operation

    def start_organize(
        self,
        media_keys: list[str],
        album_title: str,
        archive: bool = False,
    ) -> dict:
        keys = list(dict.fromkeys(str(key).strip() for key in media_keys if str(key).strip()))
        if not keys:
            raise GooglePhotosError("At least one Google media ID is required")
        operation = self._new_operation("organize", total=len(keys))
        self._update_operation(
            operation["id"],
            album_title=str(album_title).strip(),
            archive=bool(archive),
        )
        threading.Thread(
            target=self._run_organize,
            args=(operation["id"], keys, str(album_title), bool(archive)),
            daemon=True,
        ).start()
        return self.operation(operation["id"])

    def start_match_folder(
        self,
        paths: list[Path],
        album_title: str,
        archive: bool = True,
    ) -> dict:
        supported = list(
            dict.fromkeys(
                path.resolve() for path in paths if path.suffix.casefold() in MEDIA_SUFFIXES
            )
        )
        title = str(album_title).strip()
        if not supported:
            raise GooglePhotosError("The selected folder has no supported photos or videos")
        if not title:
            raise GooglePhotosError("Album name is required")
        operation = self._new_operation("match_folder", total=len(supported))
        self._update_operation(
            operation["id"],
            source_directory=str(supported[0].parent),
            album_title=title,
            archive=bool(archive),
            scanned=0,
            matched=0,
            matched_local_files=0,
            organized=0,
            unmatched=0,
        )
        self._run_match_folder(operation["id"], supported, title, bool(archive))
        return self.operation(operation["id"])

    def _new_operation(self, kind: str, total: int = 0, directory: Path | None = None) -> dict:
        operation = {
            "id": uuid.uuid4().hex[:16],
            "kind": kind,
            "status": "starting",
            "total": total,
            "completed": 0,
            "duplicates": 0,
            "possible_edits": 0,
            "related_variants": 0,
            "failed": 0,
            "files": [],
            "error": None,
            "directory": str(directory or self.inbox) if kind == "import" else None,
        }
        with self.lock:
            self.operations[operation["id"]] = operation
            self._save_operation(operation)
        return dict(operation)

    def operation(self, operation_id: str) -> dict:
        with self.lock:
            operation = self.operations.get(operation_id)
            if operation:
                return dict(operation)
        path = self.operations_root / f"{operation_id}.json"
        if not path.exists():
            raise GooglePhotosError("Google Photos operation not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_operation(self, operation: dict) -> None:
        path = self.operations_root / f"{operation['id']}.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(operation, indent=2), encoding="utf-8")
        os.replace(temp, path)

    def _update_operation(self, operation_id: str, **changes: object) -> dict:
        with self.lock:
            operation = self.operations[operation_id]
            operation.update(changes)
            self._save_operation(operation)
            return dict(operation)

    def _append_result(self, operation_id: str, result: dict, succeeded: bool) -> None:
        with self.lock:
            operation = self.operations[operation_id]
            operation["files"].append(result)
            classification = result.get("classification")
            if classification == "exact_duplicate":
                operation["duplicates"] += 1
            elif classification == "possible_edit":
                operation["possible_edits"] += 1
                operation["completed"] += 1
            elif classification == "related_variant":
                operation["related_variants"] += 1
                operation["completed"] += 1
            else:
                operation["completed" if succeeded else "failed"] += 1
            self._save_operation(operation)

    def _log_operation_error(
        self,
        operation_id: str,
        error: object,
        item: str | None = None,
    ) -> None:
        with self.lock:
            kind = str(self.operations.get(operation_id, {}).get("kind", "operation"))
        detail = " ".join(str(error).splitlines()).strip() or type(error).__name__
        item_label = f" [{item}]" if item else ""
        print(f"[Google Photos] {kind} {operation_id}{item_label}: {detail}", flush=True)

    def _existing_media(self, destination: Path) -> list[dict]:
        records: list[dict] = []
        try:
            paths = destination.rglob("*")
            for path in paths:
                if path.is_file() and path.suffix.casefold() in MEDIA_SUFFIXES:
                    stat = path.stat()
                    records.append(
                        {
                            "path": path,
                            "name": path.name.casefold(),
                            "name_key": _name_key(path.name),
                            "size": stat.st_size,
                            "sha256": None,
                        }
                    )
        except (OSError, PermissionError) as exc:
            raise GooglePhotosError(f"Could not inspect the collection folder: {exc}") from exc
        return records

    def _classify_download(
        self, incoming: Path, filename: str, size: int, digest: str, existing: list[dict]
    ) -> tuple[str, Path | None]:
        for record in existing:
            if record["size"] != size:
                continue
            if record["sha256"] is None:
                record["sha256"] = _sha256_file(record["path"])
            if record["sha256"] == digest:
                return "exact_duplicate", record["path"]
        same_name = [record for record in existing if record["name"] == filename.casefold()]
        if same_name:
            return "possible_edit", same_name[0]["path"]
        related = [record for record in existing if record["name_key"] == _name_key(filename)]
        if related:
            return "related_variant", related[0]["path"]
        return "new", None

    def _download_picked_item(self, index: int, item: dict, token: str, folder: Path) -> dict:
        media = item.get("mediaFile") or {}
        filename = _safe_filename(str(media.get("filename", "google-photo")))
        media_type = str(item.get("type", "PHOTO"))
        base_url = str(media.get("baseUrl", ""))
        if media_type not in {"PHOTO", "VIDEO"} or not base_url:
            raise GooglePhotosError("Google returned an invalid media item")
        path = folder / f".{index:05d}-{uuid.uuid4().hex}.kira-download"
        parameter = "dv" if media_type == "VIDEO" else "d"
        size, digest = self._download_file(f"{base_url}={parameter}", token, path)
        return {
            "item": item,
            "filename": filename,
            "media_type": media_type,
            "path": path,
            "size": size,
            "sha256": digest,
            # Picker IDs are scoped to the Picker API and cannot be used by the
            # Google Photos web client.  Preserve Google's content-hash format
            # so the corresponding web media key can be resolved after import.
            "google_content_hash": _google_remote_hash(path),
            "archive_name": f"{index:05d}/{filename}",
        }

    def _integrate_download(
        self, operation_id: str, download: dict, target: Path, existing: list[dict]
    ) -> None:
        incoming = Path(download["path"])
        filename = download["filename"]
        classification, related_path = self._classify_download(
            incoming, filename, download["size"], download["sha256"], existing
        )
        result = {
            "filename": filename,
            "media_type": download["media_type"].casefold(),
            "google_media_id": str(download["item"].get("id", "")),
            "google_content_hash": download["google_content_hash"],
            "classification": classification,
            "size": download["size"],
            "sha256": download["sha256"],
        }
        if classification == "exact_duplicate":
            incoming.unlink(missing_ok=True)
            result.update(status="skipped", duplicate_of=str(related_path), local_path=str(related_path))
            self._append_result(operation_id, result, True)
            return

        final = _unique_destination(target, filename, marker="google")
        os.replace(incoming, final)
        existing.append(
            {
                "path": final,
                "name": final.name.casefold(),
                "name_key": _name_key(final.name),
                "size": download["size"],
                "sha256": download["sha256"],
            }
        )
        result.update(
            filename=final.name,
            status="complete",
            related_to=str(related_path) if related_path else None,
            local_path=str(final),
        )
        self._append_result(operation_id, result, True)

    def _resolve_import_media_keys(self, files: list[dict]) -> tuple[list[str], int, int]:
        """Resolve Picker downloads to the internal media keys used by the web client."""
        imported = [
            item
            for item in files
            if item.get("status") in {"complete", "skipped"}
            and str(item.get("google_content_hash", "")).strip()
        ]
        hashes = list(
            dict.fromkeys(str(item["google_content_hash"]).strip() for item in imported)
        )
        if not hashes:
            return [], 0, 0

        matches = self.web.find_remote_matches(hashes)
        matched_hashes = {
            str(match.get("content_hash", "")).strip()
            for match in matches
            if str(match.get("media_key", "")).strip()
        }
        media_keys = list(
            dict.fromkeys(
                str(match.get("media_key", "")).strip()
                for match in matches
                if str(match.get("content_hash", "")).strip() in hashes
                and str(match.get("media_key", "")).strip()
            )
        )
        matched_items = sum(
            str(item["google_content_hash"]).strip() in matched_hashes for item in imported
        )

        unresolved_photos = [
            Path(str(item.get("local_path", "")))
            for item in imported
            if str(item["google_content_hash"]).strip() not in matched_hashes
            and item.get("media_type") == "photo"
            and str(item.get("local_path", "")).strip()
        ]
        visual_matcher = getattr(self.web, "find_visual_matches", None)
        if unresolved_photos and callable(visual_matcher):
            visual_matches, _visual_errors = visual_matcher(unresolved_photos)
            visually_matched_paths = {
                str(path)
                for match in visual_matches
                if str(match.get("media_key", "")).strip()
                for path in match.get("local_files", [])
            }
            media_keys.extend(
                str(match.get("media_key", "")).strip()
                for match in visual_matches
                if str(match.get("media_key", "")).strip()
            )
            media_keys = list(dict.fromkeys(media_keys))
            matched_items += sum(str(path) in visually_matched_paths for path in unresolved_photos)

        return media_keys, matched_items, len(imported)

    def _run_import(
        self,
        operation_id: str,
        session_id: str,
        destination: Path | None = None,
        requested_mode: str = "automatic",
        zip_threshold: int = DEFAULT_ZIP_THRESHOLD,
        album_title: str = "",
        archive: bool = True,
    ) -> None:
        try:
            target = (destination or self.inbox).resolve()
            existing = self._existing_media(target)
            items = self._list_picked_items(session_id)
            zip_mode = requested_mode == "zip" or (
                requested_mode == "automatic" and len(items) >= zip_threshold
            )
            self._update_operation(
                operation_id,
                status="running",
                total=len(items),
                download_mode="zip" if zip_mode else "files",
                zip_threshold=zip_threshold,
            )
            token = self._access_token()

            with tempfile.TemporaryDirectory(prefix=".kira-google-", dir=target) as temporary:
                temporary_root = Path(temporary)
                downloads = []
                if items:
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(MAX_CONCURRENT_DOWNLOADS, len(items))
                    ) as executor:
                        futures = {
                            executor.submit(
                                self._download_picked_item,
                                index,
                                item,
                                token,
                                temporary_root,
                            ): item
                            for index, item in enumerate(items)
                        }
                        for future in concurrent.futures.as_completed(futures):
                            source_item = futures[future]
                            filename = str(source_item.get("mediaFile", {}).get("filename") or "Google item")
                            try:
                                downloads.append(future.result())
                            except Exception as exc:
                                self._log_operation_error(operation_id, exc, filename)
                                self._append_result(
                                    operation_id,
                                    {
                                        "filename": filename,
                                        "google_media_id": str(source_item.get("id", "")),
                                        "stage": "download",
                                        "status": "failed",
                                        "error": str(exc),
                                    },
                                    False,
                                )

                if zip_mode and downloads:
                    archive_path = temporary_root / "google-photos.zip"
                    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
                        for download in downloads:
                            archive.write(download["path"], download["archive_name"])
                    extracted = temporary_root / "extracted"
                    with zipfile.ZipFile(archive_path) as archive:
                        archive.extractall(extracted)
                    for download in downloads:
                        download["path"] = extracted / download["archive_name"]

                for download in downloads:
                    self._integrate_download(operation_id, download, target, existing)

            current = self.operation(operation_id)
            status = "complete" if not current["failed"] else "complete_with_errors"
            if album_title and current["files"]:
                self._update_operation(operation_id, phase="organizing_google_photos", organize_status="running")
                matched_items = 0
                try:
                    media_keys, matched_items, importable_items = self._resolve_import_media_keys(
                        current["files"]
                    )
                    if not media_keys:
                        raise GooglePhotosError(
                            "Downloaded media could not be matched back to Google Photos for "
                            "album and Archive"
                        )
                    organized = self.web.organize(media_keys, album_title, archive)
                    unmatched_items = importable_items - matched_items
                    self._update_operation(
                        operation_id,
                        album=organized["album"],
                        archived=bool(organized["archived"]),
                        organized=len(organized["items"]),
                        organize_matched=matched_items,
                        organize_unmatched=unmatched_items,
                        organize_status="partial" if unmatched_items else "complete",
                    )
                    if unmatched_items:
                        status = "complete_with_errors"
                except Exception as exc:
                    self._log_operation_error(operation_id, exc, "automatic album and Archive")
                    self._update_operation(
                        operation_id,
                        organize_status="failed",
                        organize_error=str(exc),
                        organize_matched=matched_items,
                        organized=0,
                    )
                    status = "complete_with_errors"
            self._update_operation(operation_id, status=status)
            try:
                self._request_json(
                    f"{PICKER_ENDPOINT}/sessions/{session_id}", method="DELETE", token=self._access_token()
                )
            except GooglePhotosError as exc:
                self._log_operation_error(operation_id, exc, "picker session cleanup")
        except Exception as exc:
            self._log_operation_error(operation_id, exc)
            self._update_operation(operation_id, status="failed", error=str(exc))

    def _download_file(self, url: str, token: str, destination: Path) -> tuple[int, str]:
        request = Request(url, headers={"Authorization": f"Bearer {token}"})
        part = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
        digest = hashlib.sha256()
        size = 0
        try:
            with urlopen(request, timeout=120) as response, part.open("wb") as output:
                while chunk := response.read(4 * 1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(part, destination)
            return size, digest.hexdigest()
        except (HTTPError, URLError, OSError) as exc:
            raise GooglePhotosError(f"Download failed: {exc}") from exc
        finally:
            if part.exists():
                part.unlink()

    def _run_upload(self, operation_id: str, paths: list[Path]) -> None:
        try:
            self._update_operation(operation_id, status="running", phase="hashing_local_files")
            for path in paths:
                try:
                    if not path.is_file():
                        raise GooglePhotosError("Local file is missing")
                    token = self._access_token()
                    upload_token = self._upload_bytes(path, token)
                    created = self._request_json(
                        "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate",
                        method="POST",
                        token=token,
                        payload={
                            "newMediaItems": [
                                {"simpleMediaItem": {"fileName": path.name, "uploadToken": upload_token}}
                            ]
                        },
                    )
                    result = (created.get("newMediaItemResults") or [{}])[0]
                    if result.get("status", {}).get("code"):
                        raise GooglePhotosError(result["status"].get("message", "Google rejected the photo"))
                    self._append_result(
                        operation_id,
                        {"filename": path.name, "status": "complete", "mediaItem": result.get("mediaItem", {})},
                        True,
                    )
                except Exception as exc:
                    self._log_operation_error(operation_id, exc, path.name)
                    self._append_result(
                        operation_id,
                        {"filename": path.name, "status": "failed", "error": str(exc)},
                        False,
                    )
            current = self.operation(operation_id)
            status = "complete" if not current["failed"] else "complete_with_errors"
            self._update_operation(operation_id, status=status)
        except Exception as exc:
            self._log_operation_error(operation_id, exc)
            self._update_operation(operation_id, status="failed", error=str(exc))

    def _run_organize(
        self,
        operation_id: str,
        media_keys: list[str],
        album_title: str,
        archive: bool,
    ) -> None:
        try:
            self._update_operation(operation_id, status="running")
            result = self.web.organize(media_keys, album_title, archive)
            for item in result["items"]:
                self._append_result(operation_id, item, True)
            self._update_operation(
                operation_id,
                status="complete",
                album=result["album"],
                archived=bool(result["archived"]),
            )
        except Exception as exc:
            self._log_operation_error(operation_id, exc)
            self._update_operation(operation_id, status="failed", error=str(exc))

    def _run_match_folder(
        self,
        operation_id: str,
        paths: list[Path],
        album_title: str,
        archive: bool,
    ) -> None:
        try:
            self._update_operation(operation_id, status="running")
            paths_by_hash: dict[str, list[Path]] = {}
            for index, path in enumerate(paths, start=1):
                try:
                    if not path.is_file():
                        raise GooglePhotosError("Local file is missing")
                    _local_sha256, content_hash = _local_content_hashes(path)
                    paths_by_hash.setdefault(content_hash, []).append(path)
                except Exception as exc:
                    self._log_operation_error(operation_id, exc, path.name)
                    self._append_result(
                        operation_id,
                        {
                            "filename": path.name,
                            "local_files": [str(path)],
                            "stage": "local_hash",
                            "status": "failed",
                            "error": str(exc),
                        },
                        False,
                    )
                finally:
                    self._update_operation(operation_id, scanned=index)

            self._update_operation(operation_id, phase="matching_google_photos")
            matches: list[dict] = []
            for match in self.web.find_remote_matches(list(paths_by_hash)):
                match["match_method"] = "google_remote_sha1"
                match["local_files"] = [
                    str(path) for path in paths_by_hash.get(match["content_hash"], [])
                ]
                matches.append(match)

            byte_matched_paths = {
                local_file for match in matches for local_file in match.get("local_files", [])
            }
            visual_candidates = [
                path
                for values in paths_by_hash.values()
                for path in values
                if str(path) not in byte_matched_paths and path.suffix.casefold() in PHOTO_SUFFIXES
            ]
            if visual_candidates:
                visual_matches, visual_errors = self.web.find_visual_matches(visual_candidates)
                for error in visual_errors:
                    filename = str(error.get("filename") or "visual match")
                    exc = GooglePhotosError(str(error.get("error") or "Visual matching failed"))
                    self._log_operation_error(operation_id, exc, filename)
                    self._append_result(
                        operation_id,
                        {
                            "filename": filename,
                            "local_files": error.get("local_files", []),
                            "stage": "visual_match",
                            "status": "failed",
                            "error": str(exc),
                        },
                        False,
                    )
                for match in visual_matches:
                    local_files = match.get("local_files", [])
                    local_path = Path(local_files[0]) if local_files else None
                    if local_path is not None:
                        match["content_hash"] = next(
                            (
                                content_hash
                                for content_hash, values in paths_by_hash.items()
                                if local_path in values
                            ),
                            "",
                        )
                    match["match_method"] = "visual_content"
                    matches.append(match)

            matches_by_media: dict[str, dict] = {}
            for match in matches:
                media_key = match["media_key"]
                existing = matches_by_media.get(media_key)
                if existing is None:
                    matches_by_media[media_key] = match
                    continue
                existing["local_files"] = list(
                    dict.fromkeys(existing.get("local_files", []) + match.get("local_files", []))
                )
                if match["match_method"] not in existing["match_method"].split("+"):
                    existing["match_method"] += f"+{match['match_method']}"

            matches = list(matches_by_media.values())
            matched_paths = {
                local_file for match in matches for local_file in match.get("local_files", [])
            }
            matched_local_files = len(matched_paths)
            hashed_local_files = sum(len(values) for values in paths_by_hash.values())
            media_keys = list(dict.fromkeys(item["media_key"] for item in matches))
            if not media_keys:
                current = self.operation(operation_id)
                self._update_operation(
                    operation_id,
                    status="complete" if not current["failed"] else "complete_with_errors",
                    matched=0,
                    matched_local_files=0,
                    organized=0,
                    unmatched=hashed_local_files,
                    album=None,
                    archived=False,
                    phase="complete",
                )
                return

            self._update_operation(
                operation_id,
                phase="organizing_google_photos",
                matched=len(media_keys),
                matched_local_files=matched_local_files,
                unmatched=hashed_local_files - matched_local_files,
            )
            resolved_album = self.web.ensure_album(album_title)
            organized = self.web.organize(
                media_keys,
                album_title,
                archive,
                resolved_album=resolved_album,
            )
            album = organized["album"]
            for item in organized["items"]:
                media_key = item["google_media_key"]
                match = matches_by_media[media_key]
                item["content_hash"] = match["content_hash"]
                item["match_method"] = match["match_method"]
                item["local_files"] = match["local_files"]
                self._append_result(operation_id, item, True)
            successful = len(organized["items"])
            current = self.operation(operation_id)
            self._update_operation(
                operation_id,
                status="complete" if not current["failed"] else "complete_with_errors",
                matched=len(media_keys),
                organized=successful,
                matched_local_files=matched_local_files,
                unmatched=hashed_local_files - matched_local_files,
                album=album,
                archived=bool(archive and successful),
                archived_count=successful if archive else 0,
                phase="complete",
            )
        except Exception as exc:
            self._log_operation_error(operation_id, exc)
            self._update_operation(operation_id, status="failed", error=str(exc))

    def _upload_bytes(self, path: Path, token: str) -> str:
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        connection = http.client.HTTPSConnection(LIBRARY_HOST, timeout=120)
        try:
            connection.putrequest("POST", "/v1/uploads")
            connection.putheader("Authorization", f"Bearer {token}")
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("X-Goog-Upload-Content-Type", mime_type)
            connection.putheader("X-Goog-Upload-Protocol", "raw")
            connection.putheader("Content-Length", str(path.stat().st_size))
            connection.endheaders()
            with path.open("rb") as source:
                while chunk := source.read(4 * 1024 * 1024):
                    connection.send(chunk)
            response = connection.getresponse()
            body = response.read()
            if response.status < 200 or response.status >= 300:
                raise GooglePhotosError(self._error_message(body, response.status))
            return body.decode("utf-8")
        except OSError as exc:
            raise GooglePhotosError(f"Upload failed: {exc}") from exc
        finally:
            connection.close()

    def _request_json(
        self,
        url: str,
        method: str = "GET",
        token: str | None = None,
        payload: dict | None = None,
        form: dict | None = None,
    ) -> dict:
        headers = {"Accept": "application/json"}
        data = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                body = response.read()
            return json.loads(body) if body else {}
        except HTTPError as exc:
            body = exc.read()
            raise GooglePhotosError(self._error_message(body, exc.code)) from exc
        except (URLError, OSError) as exc:
            raise GooglePhotosError(f"Could not reach Google Photos: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise GooglePhotosError("Google returned an invalid response") from exc

    @staticmethod
    def _error_message(body: bytes, status: int) -> str:
        try:
            payload = json.loads(body)
            error = payload.get("error", payload)
            if isinstance(error, dict):
                return str(error.get("message") or error.get("error_description") or f"Google request failed ({status})")
            return str(error)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return f"Google request failed ({status})"
