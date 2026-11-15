"""Google Photos: where you look at photos, and the cloud home of phone photos.

* **Check** asks Google Photos which exact files it already holds (phone photos
  and edits synced from phones). A phone photo counts as backed up only after
  this check, and only then does Drive backup skip it.
* **Upload Finals** puts each photo's Final into a per-shoot album through the
  official API. Files Google Photos already holds are never uploaded again.
* **Clean up** hides (Archives) copies that duplicate a Final: non-Final edits
  and phone originals that were edited. It only runs on an explicit click,
  never deletes anything, and uses the unofficial web client.
* **Pick from Google Photos** imports chosen phone photos into a shoot, then adds them to
  the shoot's album and Archives them, so the picker stops offering photos already in Kira.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .errors import KiraError
from .google_auth import (
    PHOTOS_APPEND_SCOPE,
    PICKER_SCOPE,
    GoogleAuth,
    GoogleError,
    error_message,
    request_json,
    send_file,
)
from .google_photos_web import GooglePhotosWebService
from .library import Library, format_group
from .media import CHUNK_COPY_SIZE, RAW_EXTENSIONS, VIDEO_EXTENSIONS, utc_now
from .tasks import ordered_parallel

LIBRARY_API = "https://photoslibrary.googleapis.com/v1"
PICKER_API = "https://photospicker.googleapis.com/v1"
UPLOADABLE = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".webp", ".tif", ".tiff", ".gif", ".avif"}
CLEANUP_REASONS = {
    "edit": "Edit that is not the Final",
    "phone_original": "Phone original; its edit is the Final",
    "copy": "Lower-quality copy of a library photo",
    "old_final": "Earlier Final uploaded by Kira",
}


def google_hash_of(path: Path) -> str:
    """The content hash Google Photos uses to recognise exact files: base64 SHA-1."""
    digest = hashlib.sha1()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_COPY_SIZE):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


class PhotosSync:
    def __init__(self, library: Library, auth: GoogleAuth, web: GooglePhotosWebService) -> None:
        self.library = library
        self.auth = auth
        self.web = web
        self.state = library.state

    # -- Hash lookups ------------------------------------------------------------------

    def _google_hash(self, path: Path, digest: str) -> str:
        rows = self.state.execute("SELECT google_hash FROM ghash WHERE sha256=?", (digest,))
        if rows:
            return rows[0]["google_hash"]
        value = google_hash_of(path)
        self.state.execute("INSERT OR REPLACE INTO ghash VALUES (?,?)", (digest, value))
        return value

    def _lookup(self, entries: list[tuple[Path, str]], task=None, label: str = "") -> dict[str, dict]:
        """sha256 -> Google media item, for those of ``entries`` that Google Photos holds."""
        hashes: dict[str, str] = {}
        for index, (path, digest) in enumerate(entries, start=1):
            if task and task.cancelled.is_set():
                break
            if task:
                task.progress(index - 1, len(entries), f"{label}{path.name}")
            try:
                hashes[self._google_hash(path, digest)] = digest
            except OSError:
                continue
        found = self.web.lookup_hashes(list(hashes))
        return {hashes[value]: item for value, item in found.items()}

    def _present(self, digest: str) -> dict | None:
        rows = self.state.execute("SELECT * FROM gphotos WHERE sha256=?", (digest,))
        return dict(rows[0]) if rows and rows[0]["state"] == "present" else None

    # -- Check ------------------------------------------------------------------------

    def _checkable(self) -> list[tuple[Path, str]]:
        entries: dict[str, Path] = {}
        with self.library.lock:
            for shoot, photo, record in self.library.all_files():
                if record["role"] not in {"original", "edit", "copy", "live"}:
                    continue
                phone = photo.get("origin") == "phone" or record.get("source") in {"gphotos", "phone"}
                if phone or record["role"] == "edit":
                    entries.setdefault(record["sha256"], self.library.file_path(shoot, photo, record))
        return [(path, digest) for digest, path in entries.items()]

    def check(self, task=None) -> dict:
        """Record which library files Google Photos holds byte for byte."""
        self.library._require_online()
        entries = self._checkable()
        found = self._lookup(entries, task, "Checking ")
        now = utc_now()
        rows = []
        for _, digest in entries:
            item = found.get(digest)
            previous = self._present(digest)
            archived = int(bool(previous and previous["archived"]))
            if item:
                rows.append((digest, "present", item["media_key"], item["dedup_key"], archived, "lookup", now))
            else:
                rows.append((digest, "absent", None, None, 0, "lookup", now))
        self.state.executemany("INSERT OR REPLACE INTO gphotos VALUES (?,?,?,?,?,?,?)", rows)
        self.state.set("gphotos_last_check", now)
        missing_phone = self.phone_not_backed_up()
        return {"checked": len(entries), "present": len(found), "phone_not_in_photos": len(missing_phone)}

    def phone_not_backed_up(self) -> list[dict]:
        """Phone originals Google Photos does not hold exactly; Drive backup keeps these."""
        items = []
        with self.library.lock:
            for shoot, photo, record in self.library.all_files():
                if photo.get("origin") == "phone" and record["role"] == "original":
                    rows = self.state.execute("SELECT state FROM gphotos WHERE sha256=?", (record["sha256"],))
                    if rows and rows[0]["state"] == "absent":
                        items.append({"shoot_id": shoot["id"], "photo_id": photo["id"], "name": record["name"]})
        return items

    # -- Upload Finals ------------------------------------------------------------------

    def pending_finals(self, shoot_id: str | None = None) -> tuple[list[tuple[dict, dict, dict]], list[dict]]:
        uploads = {row["sha256"] for row in self.state.execute("SELECT sha256 FROM gphotos_uploads")}
        todo, skipped = [], []
        with self.library.lock:
            shoots = [self.library.shoot(shoot_id)] if shoot_id else list(self.library.shoots.values())
            for shoot in shoots:
                for photo in shoot["photos"].values():
                    if photo.get("rejected"):
                        continue
                    final = self.library.effective_final(photo)
                    if not final:
                        continue
                    record = self.library.file_record(photo, final)
                    if record.get("missing") or record["sha256"] in uploads or self._present(record["sha256"]):
                        continue
                    if photo.get("origin") == "phone" and record["role"] == "original":
                        continue
                    suffix = Path(final).suffix.casefold()
                    if suffix not in UPLOADABLE:
                        reason = "RAW Finals need a JPEG export" if suffix in RAW_EXTENSIONS else "unsupported type"
                        if suffix in VIDEO_EXTENSIONS:
                            reason = "videos are not uploaded"
                        skipped.append({"shoot": shoot["name"], "file": final, "reason": reason})
                        continue
                    todo.append((shoot, photo, record))
        return todo, skipped

    def upload_finals(self, task=None, shoot_id: str | None = None) -> dict:
        self.library._require_online()
        todo, skipped = self.pending_finals(shoot_id)
        report = {"uploaded": 0, "skipped": skipped, "failed": []}
        token = self.auth.access_token(PHOTOS_APPEND_SCOPE)
        for index, (shoot, photo, record) in enumerate(todo, start=1):
            if task and task.cancelled.is_set():
                break
            if task:
                task.progress(index - 1, len(todo), record["name"])
            path = self.library.file_path(shoot, photo, record)
            try:
                album_id = self._album(shoot, token)
                item_id = self._upload(path, album_id, token, f"{shoot['name']} / {photo['stem']}")
                now = utc_now()
                self.state.execute(
                    "INSERT OR REPLACE INTO gphotos_uploads VALUES (?,?,?,?)",
                    (record["sha256"], item_id, album_id, now),
                )
                self.state.execute(
                    "INSERT OR REPLACE INTO gphotos VALUES (?,?,?,?,?,?,?)",
                    (record["sha256"], "present", None, None, 0, "upload", now),
                )
                report["uploaded"] += 1
            except (GoogleError, OSError) as exc:
                report["failed"].append({"file": record["name"], "error": str(exc)})
                if "quota" in str(exc).casefold() or "connect" in str(exc).casefold():
                    break
        if task:
            task.progress(len(todo), len(todo), "")
        return report

    def _album(self, shoot: dict, token: str) -> str:
        rows = self.state.execute("SELECT album_id FROM gphotos_albums WHERE shoot_id=?", (shoot["id"],))
        if rows:
            return rows[0]["album_id"]
        created = request_json(
            f"{LIBRARY_API}/albums", method="POST", token=token, payload={"album": {"title": shoot["name"]}}
        )
        self.state.execute(
            "INSERT OR REPLACE INTO gphotos_albums VALUES (?,?,?)", (shoot["id"], created["id"], shoot["name"])
        )
        return created["id"]

    @staticmethod
    def _upload(path: Path, album_id: str, token: str, description: str) -> str:
        status, _, body = send_file(
            f"{LIBRARY_API}/uploads",
            path,
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
                "X-Goog-Upload-Content-Type": _mime(path),
                "X-Goog-Upload-File-Name": quote(path.name),
                "X-Goog-Upload-Protocol": "raw",
            },
        )
        if status != 200 or not body:
            raise GoogleError(error_message(body, status))
        created = request_json(
            f"{LIBRARY_API}/mediaItems:batchCreate",
            method="POST",
            token=token,
            payload={
                "albumId": album_id,
                "newMediaItems": [
                    {
                        "description": description[:1000],
                        "simpleMediaItem": {"uploadToken": body.decode("utf-8"), "fileName": path.name},
                    }
                ],
            },
        )
        result = (created.get("newMediaItemResults") or [{}])[0]
        item = result.get("mediaItem")
        if not item:
            raise GoogleError(result.get("status", {}).get("message") or "Google Photos did not accept the upload")
        return str(item["id"])

    # -- Clean up -----------------------------------------------------------------------

    def cleanup_plan(self, task=None) -> dict:
        """List what clean-up would hide. Nothing changes until :meth:`cleanup_apply`."""
        self.library._require_online()
        uploads = {row["sha256"] for row in self.state.execute("SELECT sha256 FROM gphotos_uploads")}
        candidates: list[dict] = []
        with self.library.lock:
            for shoot in self.library.shoots.values():
                for photo in shoot["photos"].values():
                    final = self.library.effective_final(photo)
                    if not final:
                        continue
                    final_record = self.library.file_record(photo, final)
                    for record in photo["files"]:
                        if record.get("missing") or record["name"] == final:
                            continue
                        if record["sha256"] == final_record["sha256"]:
                            continue
                        reason = None
                        if record["role"] == "edit":
                            reason = "old_final" if record["sha256"] in uploads else "edit"
                        elif record["role"] == "original" and photo.get("origin") == "phone":
                            reason = "phone_original" if final_record["role"] == "edit" else None
                        elif record["role"] == "copy" and format_group(record["name"]) == "still":
                            reason = "copy"
                        if reason:
                            candidates.append(
                                {
                                    "shoot_id": shoot["id"],
                                    "shoot_name": shoot["name"],
                                    "photo_id": photo["id"],
                                    "stem": photo["stem"],
                                    "name": record["name"],
                                    "sha256": record["sha256"],
                                    "reason": reason,
                                    "reason_text": CLEANUP_REASONS[reason],
                                    "path": self.library.file_path(shoot, photo, record),
                                }
                            )
        found = self._lookup([(item["path"], item["sha256"]) for item in candidates], task, "Looking up ")
        items = []
        for item in candidates:
            match = found.get(item["sha256"])
            if not match:
                continue
            previous = self._present(item["sha256"])
            if previous and previous["archived"]:
                continue
            item.pop("path")
            items.append({**item, **match})
        self.state.set("gphotos_cleanup_plan", json.dumps({"created_at": utc_now(), "items": items}))
        return {"items": items, "created_at": utc_now()}

    def cleanup_apply(self, media_keys: list[str], task=None) -> dict:
        """Archive the chosen items from the last plan. Archived items stay in your account."""
        plan = json.loads(self.state.get("gphotos_cleanup_plan") or '{"items": []}')
        chosen = [item for item in plan["items"] if item["media_key"] in set(media_keys)]
        if not chosen:
            raise KiraError(HTTPStatus.BAD_REQUEST, "Nothing selected; review the clean-up list again")
        archived: list[str] = []
        for start in range(0, len(chosen), GooglePhotosWebService.MAX_MEDIA_ITEMS):
            batch = chosen[start : start + GooglePhotosWebService.MAX_MEDIA_ITEMS]
            if task:
                task.progress(start, len(chosen), f"Archiving {len(batch)} items")
            archived += self.web.archive(batch)
        done = set(archived)
        now = utc_now()
        self.state.executemany(
            "INSERT OR REPLACE INTO gphotos VALUES (?,?,?,?,1,?,?)",
            [
                (item["sha256"], "present", item["media_key"], item["dedup_key"], "lookup", now)
                for item in chosen
                if item["media_key"] in done
            ],
        )
        plan["items"] = [item for item in plan["items"] if item["media_key"] not in done]
        self.state.set("gphotos_cleanup_plan", json.dumps(plan))
        return {"archived": len(done)}

    # -- Pick from Google Photos -------------------------------------------------------------

    def picker_start(self) -> dict:
        token = self.auth.access_token(PICKER_SCOPE)
        session = request_json(f"{PICKER_API}/sessions", method="POST", token=token, payload={})
        return {"id": session["id"], "picker_uri": session["pickerUri"], "ready": False}

    def picker_status(self, session_id: str) -> dict:
        token = self.auth.access_token(PICKER_SCOPE)
        session = request_json(f"{PICKER_API}/sessions/{quote(session_id)}", token=token)
        return {
            "id": session_id,
            "picker_uri": session.get("pickerUri", ""),
            "ready": bool(session.get("mediaItemsSet")),
        }

    def picker_import(self, session_id: str, shoot_id: str, task=None) -> dict:
        self.library._require_online()
        self.library.shoot(shoot_id)
        token = self.auth.access_token(PICKER_SCOPE)
        items, page = [], ""
        while True:
            query = urlencode({"sessionId": session_id, "pageSize": 100, **({"pageToken": page} if page else {})})
            response = request_json(f"{PICKER_API}/mediaItems?{query}", token=token)
            items += response.get("mediaItems", [])
            page = response.get("nextPageToken", "")
            if not page:
                break
        report = {"total": len(items), "counts": {}, "failed": [], "shoot_id": shoot_id}
        imported: list[dict] = []

        def fetch(item: dict) -> tuple[Path, str]:
            """Download one picked item and hash it; runs on a worker thread."""
            media = item["mediaFile"]
            suffix = "=dv" if item.get("type") == "VIDEO" else "=d"
            staged = self.library.staging_path(_picked_name(item))
            try:
                _download(f"{media['baseUrl']}{suffix}", self.auth.access_token(PICKER_SCOPE), staged)
                return staged, google_hash_of(staged)
            except BaseException:
                staged.unlink(missing_ok=True)
                raise

        cancelled = task.cancelled if task else None
        downloads = ordered_parallel(
            items, fetch, cancelled=cancelled, discard=lambda done: done[0].unlink(missing_ok=True)
        )
        for index, (item, fetched, error) in enumerate(downloads, start=1):
            name = _picked_name(item)
            if task:
                task.progress(index - 1, len(items), name)
            if error is not None:
                report["failed"].append({"name": name, "error": getattr(error, "message", str(error))})
                continue
            staged, google_hash = fetched
            try:
                result = self.library.ingest_file(
                    staged, shoot_id, source_kind="gphotos", original_name=name, consume=True
                )
                report["counts"][result["status"]] = report["counts"].get(result["status"], 0) + 1
                if result["status"] != "ignored":
                    imported.append({"name": name, "time_ms": _rfc3339_ms(item.get("createTime")), "hash": google_hash})
            except (KiraError, OSError) as exc:
                report["failed"].append({"name": name, "error": getattr(exc, "message", str(exc))})
            finally:
                staged.unlink(missing_ok=True)
            if cancelled and cancelled.is_set():
                downloads.close()
                break
        self.library.flush()
        try:
            request_json(f"{PICKER_API}/sessions/{quote(session_id)}", method="DELETE", token=token)
        except GoogleError:
            pass
        if imported:
            if task:
                task.progress(len(items), len(items), "Adding to the album and archiving in Google Photos")
            report["google"] = self._file_away_picked(imported, self.library.shoot(shoot_id)["name"])
        return report

    def _file_away_picked(self, imported: list[dict], album_title: str) -> dict:
        """Put imported photos in the shoot's album and Archive them, so the picker stops offering them.

        Picker IDs are not web media keys, so items are found by exact hash first, then by original
        filename and capture time (picker downloads lose location data, which changes the hash).
        Nothing is deleted; anything not found confidently is left alone and listed.
        """
        outcome = {"album": album_title, "archived": 0, "not_found": [], "error": None}
        if not self.web.status().get("connected"):
            outcome["error"] = "Sign in to Photos web (Cloud page) so imports are filed into albums and archived."
            return outcome
        try:
            by_hash = self.web.lookup_hashes([entry["hash"] for entry in imported])
            matched, remaining = {}, []
            for entry in imported:
                hit = by_hash.get(entry["hash"])
                if hit:
                    matched[hit["media_key"]] = hit
                elif entry["time_ms"] is not None:
                    remaining.append(entry)
                else:
                    outcome["not_found"].append(entry["name"])
            by_name = self.web.find_by_name_and_time(remaining)
            for index, entry in enumerate(remaining):
                if index in by_name:
                    matched[by_name[index]["media_key"]] = by_name[index]
                else:
                    outcome["not_found"].append(entry["name"])
            keys = list(matched)
            for start in range(0, len(keys), GooglePhotosWebService.MAX_MEDIA_ITEMS):
                batch = keys[start : start + GooglePhotosWebService.MAX_MEDIA_ITEMS]
                self.web.organize(batch, album_title, archive=True)
                outcome["archived"] += len(batch)
        except GoogleError as exc:
            outcome["error"] = getattr(exc, "message", str(exc))
        return outcome

    # -- Status -----------------------------------------------------------------------

    def status(self) -> dict:
        auth = self.auth.status()
        web = self.web.status()
        pending, skipped = self.pending_finals() if self.library.root else ([], [])
        plan = json.loads(self.state.get("gphotos_cleanup_plan") or '{"items": []}')
        return {
            "upload_connected": auth["photos_upload"],
            "picker_connected": auth["photos_picker"],
            "web_signed_in": bool(web.get("connected") or web.get("signed_in")),
            "web": web,
            "pending_finals": len(pending),
            "skipped_finals": len(skipped),
            "cleanup_waiting": len(plan.get("items", [])),
            "last_check": self.state.get("gphotos_last_check"),
            "phone_not_in_photos": len(self.phone_not_backed_up()) if self.library.root else 0,
        }


def _picked_name(item: dict) -> str:
    return item.get("mediaFile", {}).get("filename") or f"{item.get('id', 'photo')}.jpg"


def _rfc3339_ms(value: object) -> int | None:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _mime(path: Path) -> str:
    suffix = path.suffix.casefold()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".png": "image/png",
        ".webp": "image/webp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".gif": "image/gif",
        ".avif": "image/avif",
    }.get(suffix, "application/octet-stream")


def _download(url: str, token: str, destination: Path, attempts: int = 3) -> None:
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"Authorization": f"Bearer {token}"})
            with urlopen(request, timeout=300) as response, destination.open("wb") as output:
                while chunk := response.read(CHUNK_COPY_SIZE):
                    output.write(chunk)
            return
        except HTTPError as exc:
            if exc.code < 500 or attempt == attempts - 1:
                raise GoogleError(error_message(exc.read(), exc.code)) from exc
        except URLError as exc:
            if attempt == attempts - 1:
                raise GoogleError(f"Could not download from Google Photos: {exc}") from exc
        time.sleep(1 + attempt)
