"""Google Drive backup: a one-way, never-overwriting mirror of the library folders.

Drive holds exact bytes of everything Kira keeps, except phone photos that Google
Photos has already been confirmed to hold (their cloud home is Photos). Rules:

* Every upload is verified against Drive's own SHA-256 of the stored bytes.
* A file that changed locally is uploaded as a new Drive file; the old one is
  renamed with a timestamp, never replaced.
* A file that disappears locally is never deleted from Drive. It can be restored.
* A file that only moved (for example into ``_rejects``) is moved on Drive too.
"""

from __future__ import annotations

import json
import time
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .errors import KiraError
from .google_auth import DRIVE_SCOPE, GoogleAuth, GoogleError, error_message, request_json, send_file
from .library import MANIFEST_NAME, Library
from .media import CHUNK_COPY_SIZE, sha256_file, utc_now
from .tasks import ordered_parallel

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
ROOT_FOLDER_NAME = "Kira Library"
PHONE_ROLES = {"original", "live"}


class DriveBackup:
    def __init__(self, library: Library, auth: GoogleAuth) -> None:
        self.library = library
        self.auth = auth
        self.state = library.state

    # -- Planning -------------------------------------------------------------------

    def _in_photos(self, digest: str) -> bool:
        rows = self.library.state.execute("SELECT state FROM gphotos WHERE sha256=?", (digest,))
        return bool(rows) and rows[0]["state"] == "present"

    def wanted(self) -> dict[str, tuple[Path, str]]:
        """Every file that should be on Drive: relative path -> (local path, sha256)."""
        wanted: dict[str, tuple[Path, str]] = {}
        with self.library.lock:
            for shoot, photo, record in self.library.all_files():
                phone = photo.get("origin") == "phone" and record["role"] in PHONE_ROLES
                if phone and self._in_photos(record["sha256"]):
                    continue
                if record["role"] == "loose":
                    continue
                path = self.library.file_path(shoot, photo, record)
                wanted[self.library.relative_path(shoot, photo, record)] = (path, record["sha256"])
            for shoot in self.library.shoots.values():
                for record in shoot.get("loose", []):
                    if not record.get("missing"):
                        path = shoot["path"] / record["name"]
                        wanted[path.relative_to(self.library.root).as_posix()] = (path, record["sha256"])
        return wanted

    def status(self) -> dict:
        auth = self.auth.status()
        if not self.library.root:
            return {"connected": auth["drive"], "pending": 0, "backed_up": 0, "last_run": ""}
        wanted = self.wanted()
        stored = {row["relative_path"]: row["sha256"] for row in self.state.execute("SELECT * FROM drive_files")}
        pending = [path for path, (_, digest) in wanted.items() if stored.get(path) != digest]
        return {
            "connected": auth["drive"],
            "pending": len(pending),
            "pending_bytes": sum(_size(wanted[path][0]) for path in pending),
            "backed_up": len(wanted) - len(pending),
            "last_run": self.state.get("drive_last_run"),
            "last_error": self.state.get("drive_last_error"),
        }

    # -- Drive calls ----------------------------------------------------------------

    def _token(self) -> str:
        return self.auth.access_token(DRIVE_SCOPE)

    def _folder(self, relative: str) -> str:
        """The Drive folder id for a library-relative folder, created on demand."""
        key = relative.strip("/")
        rows = self.state.execute("SELECT folder_id FROM drive_folders WHERE relative_path=?", (key,))
        if rows:
            return rows[0]["folder_id"]
        if key:
            parent = self._folder(_parent(key))
            name = PurePosixPath(key).name
        else:
            parent, name = None, ROOT_FOLDER_NAME
        metadata = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            metadata["parents"] = [parent]
        created = request_json(f"{DRIVE_API}/files?fields=id", method="POST", token=self._token(), payload=metadata)
        self.state.execute("INSERT OR REPLACE INTO drive_folders VALUES (?,?)", (key, created["id"]))
        return created["id"]

    def _verify_root(self) -> None:
        """Forget cached folder ids if the Kira folder was deleted on Drive."""
        rows = self.state.execute("SELECT folder_id FROM drive_folders WHERE relative_path=''")
        if not rows:
            return
        try:
            meta = request_json(
                f"{DRIVE_API}/files/{rows[0]['folder_id']}?fields=id,trashed", token=self._token(), timeout=30
            )
            if not meta.get("trashed"):
                return
        except GoogleError as exc:
            if "not found" not in str(exc).casefold():
                raise
        self.state.execute("DELETE FROM drive_folders")
        self.state.execute("DELETE FROM drive_files")

    def _upload(self, path: Path, name: str, parent: str, digest: str) -> str:
        metadata = json.dumps({"name": name, "parents": [parent]}).encode("utf-8")
        request = Request(
            f"{DRIVE_UPLOAD}?uploadType=resumable&fields=id",
            data=metadata,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(path.stat().st_size),
            },
        )
        try:
            with urlopen(request, timeout=60) as response:
                session = response.headers["Location"]
        except HTTPError as exc:
            raise GoogleError(error_message(exc.read(), exc.code)) from exc
        except (URLError, OSError) as exc:
            raise GoogleError(f"Could not reach Google Drive: {exc}") from exc
        status, _, body = send_file(session, path, {"Content-Type": "application/octet-stream"}, method="PUT")
        if status not in {200, 201}:
            raise GoogleError(error_message(body, status))
        file_id = json.loads(body)["id"]
        stored = request_json(f"{DRIVE_API}/files/{file_id}?fields=id,sha256Checksum,size", token=self._token())
        if str(stored.get("sha256Checksum", "")).lower() != digest:
            raise GoogleError(f"Drive's copy of {name} does not match the local file")
        return file_id

    def _rename_old(self, file_id: str, name: str) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = PurePosixPath(name)
        request_json(
            f"{DRIVE_API}/files/{file_id}?fields=id",
            method="PATCH",
            token=self._token(),
            payload={"name": f"{path.stem}.{stamp}{path.suffix}"},
        )

    def _move(self, file_id: str, old: str, new: str) -> None:
        query = urlencode(
            {"addParents": self._folder(_parent(new)), "removeParents": self._folder(_parent(old)), "fields": "id"}
        )
        request_json(
            f"{DRIVE_API}/files/{file_id}?{query}",
            method="PATCH",
            token=self._token(),
            payload={"name": PurePosixPath(new).name},
        )

    # -- Backup ---------------------------------------------------------------------

    def backup(self, task=None) -> dict:
        """Bring Drive up to date with the library. Safe to stop and run again at any time."""
        root = self.library._require_online()
        self.library.flush()
        self._verify_root()
        wanted = self.wanted()
        manifests = {
            shoot["path"].joinpath(MANIFEST_NAME).relative_to(root).as_posix(): shoot["path"] / MANIFEST_NAME
            for shoot in self.library.shoots.values()
            if (shoot["path"] / MANIFEST_NAME).exists()
        }
        stored = {row["relative_path"]: dict(row) for row in self.state.execute("SELECT * FROM drive_files")}
        by_sha: dict[str, list[str]] = {}
        for relative, row in stored.items():
            by_sha.setdefault(row["sha256"], []).append(relative)
        todo = [
            relative for relative, (_, digest) in wanted.items() if stored.get(relative, {}).get("sha256") != digest
        ]
        report = {"uploaded": 0, "moved": 0, "manifests": 0, "failed": [], "skipped_phone": 0}
        total = len(todo) + len(manifests)
        self.state.set("drive_last_error", "")
        for index, relative in enumerate(sorted(todo), start=1):
            if task and task.cancelled.is_set():
                break
            path, digest = wanted[relative]
            if task:
                task.progress(index - 1, total, relative)
            try:
                if not path.is_file():
                    raise KiraError(HTTPStatus.NOT_FOUND, "missing on the library drive")
                if sha256_file(path) != digest:
                    raise KiraError(HTTPStatus.CONFLICT, "changed since Kira last checked it; rescan the shoot")
                moved_from = next((old for old in by_sha.get(digest, []) if old not in wanted and old in stored), None)
                if moved_from:
                    self._move(stored[moved_from]["file_id"], moved_from, relative)
                    file_id = stored.pop(moved_from)["file_id"]
                    by_sha[digest].remove(moved_from)
                    self.state.execute("DELETE FROM drive_files WHERE relative_path=?", (moved_from,))
                    report["moved"] += 1
                else:
                    previous = stored.get(relative)
                    if previous:
                        self._rename_old(previous["file_id"], relative)
                    file_id = self._upload(path, PurePosixPath(relative).name, self._folder(_parent(relative)), digest)
                    report["uploaded"] += 1
                self.state.execute(
                    "INSERT OR REPLACE INTO drive_files VALUES (?,?,?,?)", (relative, file_id, digest, utc_now())
                )
            except (GoogleError, KiraError, OSError) as exc:
                message = getattr(exc, "message", str(exc))
                report["failed"].append({"file": relative, "error": message})
                self.state.set("drive_last_error", f"{relative}: {message}")
                if isinstance(exc, GoogleError) and _fatal(message):
                    break
        for offset, (relative, path) in enumerate(sorted(manifests.items()), start=len(todo) + 1):
            if task and task.cancelled.is_set():
                break
            if task:
                task.progress(offset - 1, total, relative)
            try:
                self._sync_manifest(relative, path)
                report["manifests"] += 1
            except (GoogleError, OSError) as exc:
                report["failed"].append({"file": relative, "error": str(exc)})
        if task:
            task.progress(total, total, "")
        self.state.set("drive_last_run", utc_now())
        return report

    def _sync_manifest(self, relative: str, path: Path) -> None:
        """Manifests are Kira's own bookkeeping, so they are updated in place on Drive."""
        digest = sha256_file(path)
        rows = self.state.execute("SELECT * FROM drive_files WHERE relative_path=?", (relative,))
        if rows and rows[0]["sha256"] == digest:
            return
        if rows:
            status, _, body = send_file(
                f"{DRIVE_UPLOAD}/{rows[0]['file_id']}?uploadType=media&fields=id",
                path,
                {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"},
                method="PATCH",
            )
            if status == 404:
                self.state.execute("DELETE FROM drive_files WHERE relative_path=?", (relative,))
                return self._sync_manifest(relative, path)
            if status not in {200, 201}:
                raise GoogleError(error_message(body, status))
            file_id = rows[0]["file_id"]
        else:
            file_id = self._upload(path, MANIFEST_NAME, self._folder(_parent(relative)), digest)
        self.state.execute(
            "INSERT OR REPLACE INTO drive_files VALUES (?,?,?,?)", (relative, file_id, digest, utc_now())
        )

    # -- Restore --------------------------------------------------------------------

    def missing(self) -> list[dict]:
        """Tracked files gone from the library drive that Drive can put back."""
        found = []
        with self.library.lock:
            for shoot in self.library.shoots.values():
                for photo in shoot["photos"].values():
                    for record in photo["files"]:
                        if not record.get("missing"):
                            continue
                        rows = self.state.execute(
                            "SELECT file_id FROM drive_files WHERE sha256=? LIMIT 1", (record["sha256"],)
                        )
                        found.append(
                            {
                                "shoot_id": shoot["id"],
                                "photo_id": photo["id"],
                                "name": record["name"],
                                "restorable": bool(rows),
                            }
                        )
        return found

    def restore(self, task=None) -> dict:
        self.library._require_online()
        items = [item for item in self.missing() if item["restorable"]]
        report = {"restored": 0, "failed": []}

        def fetch(item: dict) -> Path:
            """Download one file from Drive into staging; runs on a worker thread."""
            _, photo = self.library.photo(item["shoot_id"], item["photo_id"])
            digest = self.library.file_record(photo, item["name"])["sha256"]
            rows = self.state.execute("SELECT file_id FROM drive_files WHERE sha256=? LIMIT 1", (digest,))
            staged = self.library.staging_path(item["name"])
            try:
                self._download(rows[0]["file_id"], staged)
                return staged
            except BaseException:
                staged.unlink(missing_ok=True)
                raise

        cancelled = task.cancelled if task else None
        downloads = ordered_parallel(
            items, fetch, cancelled=cancelled, discard=lambda staged: staged.unlink(missing_ok=True)
        )
        for index, (item, staged, error) in enumerate(downloads, start=1):
            if task:
                task.progress(index - 1, len(items), item["name"])
            if error is not None:
                report["failed"].append({"file": item["name"], "error": getattr(error, "message", str(error))})
                continue
            try:
                self.library.restore_file(item["shoot_id"], item["photo_id"], item["name"], staged)
                report["restored"] += 1
            except (KiraError, OSError) as exc:
                report["failed"].append({"file": item["name"], "error": getattr(exc, "message", str(exc))})
            finally:
                staged.unlink(missing_ok=True)
            if cancelled and cancelled.is_set():
                downloads.close()
                break
        return report

    def _download(self, file_id: str, destination: Path) -> None:
        request = Request(
            f"{DRIVE_API}/files/{quote(file_id)}?alt=media", headers={"Authorization": f"Bearer {self._token()}"}
        )
        try:
            with urlopen(request, timeout=300) as response, destination.open("xb") as output:
                while chunk := response.read(CHUNK_COPY_SIZE):
                    output.write(chunk)
        except HTTPError as exc:
            raise GoogleError(error_message(exc.read(), exc.code)) from exc
        except URLError as exc:
            raise GoogleError(f"Could not reach Google Drive: {exc}") from exc


def _parent(relative: str) -> str:
    parent = str(PurePosixPath(relative).parent)
    return "" if parent == "." else parent


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _fatal(message: str) -> bool:
    lowered = message.casefold()
    return any(word in lowered for word in ("connect", "quota", "storage", "reconnect", "expired", "reach google"))
