"""Everything one running Kira holds: settings, the library and the services around it."""

from __future__ import annotations

import json
import secrets
from http import HTTPStatus
from pathlib import Path

from .drive import DriveBackup
from .errors import KiraError
from .google_auth import GoogleAuth
from .google_photos_web import GooglePhotosWebService
from .gphotos import PhotosSync
from .ipad import Outbox
from .library import Library
from .media import atomic_json_write, utc_now
from .sources import SourceBrowser
from .tasks import TaskRunner


class KiraApp:
    def __init__(self, data_root: Path, app_root: Path) -> None:
        self.root = data_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "config.json"
        self.config = self._load_config()
        self.pair_code = f"{secrets.randbelow(1_000_000):06d}"
        self.pair_secret = secrets.token_urlsafe(24)
        library_root = self.config.get("library_root")
        self.library = Library(self.root, Path(library_root) if library_root else None)
        self.auth = GoogleAuth(self.root, app_root)
        self.web = GooglePhotosWebService(self.root)
        self.drive = DriveBackup(self.library, self.auth)
        self.photos = PhotosSync(self.library, self.auth, self.web)
        self.outbox = Outbox(self.library)
        self.sources = SourceBrowser(self.library)
        self.tasks = TaskRunner()

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

    def set_library_root(self, value: str) -> dict:
        path = Path(str(value).strip()).expanduser()
        if not path.is_absolute():
            raise KiraError(HTTPStatus.BAD_REQUEST, "Choose a full folder path, for example G:\\Kira Library")
        if path.resolve() == self.root or path.resolve().is_relative_to(self.root):
            raise KiraError(HTTPStatus.BAD_REQUEST, "The library cannot live inside Kira's working folder")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise KiraError(HTTPStatus.BAD_REQUEST, f"Could not use {path}: {exc}") from exc
        if self.library.online:
            self.library.flush()
        self.library.open(path)
        self.config["library_root"] = str(path)
        atomic_json_write(self.config_path, self.config)
        return self.library.overview()

    def close(self) -> None:
        try:
            self.web.close()
        finally:
            self.library.close()
