"""HTTP layer: request dispatch, authentication, and API route handlers."""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import qrcode
import qrcode.image.svg

from . import APP_NAME, APP_VERSION
from .app import KiraApp
from .errors import KiraError
from .google_auth import GoogleError
from .media import CHUNK_COPY_SIZE, browse_directories, reveal_directory, safe_download_name, safe_filename
from .metadata import file_kind

MAX_JSON_BODY = 1024 * 1024
MAX_DRAINED_BODY = 16 * 1024 * 1024
MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
APP_ROOT = Path(__file__).resolve().parent.parent

# (pattern, methods, handler, requires_local). "{name}" segments become params.
# Routes that are not desktop-only are the ones the iPad and its Shortcuts use.
AUTHED_ROUTES: list[tuple[tuple[str, ...], tuple[str, ...], str, bool]] = [
    (("api", "library"), ("GET",), "_h_library", False),
    (("api", "library", "root"), ("POST",), "_h_library_root", True),
    (("api", "browse"), ("GET",), "_h_browse", True),
    (("api", "shoots"), ("POST",), "_h_shoot_create", True),
    (("api", "shoots", "{shoot_id}"), ("GET",), "_h_shoot", False),
    (("api", "shoots", "{shoot_id}", "rename"), ("POST",), "_h_shoot_rename", True),
    (("api", "shoots", "{shoot_id}", "trash"), ("POST",), "_h_shoot_trash", True),
    (("api", "shoots", "{shoot_id}", "rescan"), ("POST",), "_h_shoot_rescan", True),
    (("api", "shoots", "{shoot_id}", "reveal"), ("POST",), "_h_shoot_reveal", True),
    (("api", "shoots", "{shoot_id}", "reject"), ("POST",), "_h_shoot_reject", False),
    (("api", "shoots", "{shoot_id}", "final"), ("POST",), "_h_shoot_final", False),
    (("api", "shoots", "{shoot_id}", "tags"), ("POST",), "_h_shoot_tags", False),
    (("api", "shoots", "{shoot_id}", "delete"), ("POST",), "_h_shoot_delete", True),
    (("api", "shoots", "{shoot_id}", "upload"), ("POST",), "_h_shoot_upload", False),
    (
        ("api", "shoots", "{shoot_id}", "photos", "{photo_id}", "{name}", "preview"),
        ("GET", "HEAD"),
        "_h_preview",
        False,
    ),
    (("api", "shoots", "{shoot_id}", "photos", "{photo_id}", "{name}"), ("GET", "HEAD"), "_h_file", False),
    (("api", "import"), ("POST",), "_h_import", True),
    (("api", "import", "check"), ("POST",), "_h_import_check", True),
    (("api", "review"), ("GET",), "_h_review_list", False),
    (("api", "review", "{review_id}", "preview"), ("GET", "HEAD"), "_h_review_preview", False),
    (("api", "review", "{review_id}"), ("POST",), "_h_review_resolve", False),
    (("api", "source"), ("GET",), "_h_source", True),
    (("api", "source", "preview"), ("GET", "HEAD"), "_h_source_preview", True),
    (("api", "source", "file"), ("GET", "HEAD"), "_h_source_file", True),
    (("api", "source", "scan"), ("POST",), "_h_source_scan", True),
    (("api", "source", "links"), ("POST",), "_h_source_links", True),
    (("api", "source", "trash-copies"), ("POST",), "_h_source_trash_copies", True),
    (("api", "trash"), ("GET",), "_h_trash", True),
    (("api", "trash", "empty"), ("POST",), "_h_trash_empty", True),
    (("api", "tasks"), ("GET",), "_h_tasks", False),
    (("api", "tasks", "{task_id}"), ("GET",), "_h_task", False),
    (("api", "tasks", "{task_id}", "cancel"), ("POST",), "_h_task_cancel", True),
    (("api", "ipad", "send"), ("POST",), "_h_ipad_send", False),
    (("api", "ipad", "outbox"), ("GET",), "_h_ipad_outbox", False),
    (("api", "ipad", "outbox", "clear"), ("POST",), "_h_ipad_outbox_clear", False),
    (("api", "ipad", "outbox", "{item_id}"), ("GET", "HEAD"), "_h_ipad_outbox_file", False),
    (("api", "ipad", "outbox", "{item_id}"), ("DELETE",), "_h_ipad_outbox_remove", False),
    (("api", "ipad", "receive"), ("POST", "PUT"), "_h_ipad_receive", False),
    (("api", "ipad", "setup"), ("GET",), "_h_ipad_setup", True),
    (("api", "google", "status"), ("GET",), "_h_google_status", True),
    (("api", "google", "connect"), ("POST",), "_h_google_connect", True),
    (("api", "google", "disconnect"), ("POST",), "_h_google_disconnect", True),
    (("api", "google", "web-signin"), ("POST",), "_h_web_signin_start", True),
    (("api", "google", "web-signin"), ("GET",), "_h_web_signin_status", True),
    (("api", "google", "web-signin"), ("DELETE",), "_h_web_signin_cancel", True),
    (("api", "google", "web-session"), ("DELETE",), "_h_web_session_delete", True),
    (("api", "backup", "run"), ("POST",), "_h_backup_run", True),
    (("api", "backup", "restore"), ("POST",), "_h_backup_restore", True),
    (("api", "photos", "check"), ("POST",), "_h_photos_check", True),
    (("api", "photos", "upload-finals"), ("POST",), "_h_photos_upload", True),
    (("api", "photos", "cleanup", "plan"), ("POST",), "_h_cleanup_plan", True),
    (("api", "photos", "cleanup"), ("GET",), "_h_cleanup_get", True),
    (("api", "photos", "cleanup", "apply"), ("POST",), "_h_cleanup_apply", True),
    (("api", "photos", "picker"), ("POST",), "_h_picker_start", True),
    (("api", "photos", "picker", "{session_id}"), ("GET",), "_h_picker_status", True),
    (("api", "photos", "picker", "{session_id}", "import"), ("POST",), "_h_picker_import", True),
]

STATIC_PATHS = {"/", "/index.html", "/app.js", "/sha256.js", "/styles.css"}
MEDIA_ROOT = APP_ROOT / "media"
MEDIA_FILES = {"/media/kira_light.png", "/media/kira_k_light.png"}


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


def _match_route(pattern: tuple[str, ...], segments: list[str]) -> dict[str, str] | None:
    if len(pattern) != len(segments):
        return None
    params: dict[str, str] = {}
    for expected, actual in zip(pattern, segments, strict=False):
        if expected.startswith("{") and expected.endswith("}"):
            params[expected[1:-1]] = actual
        elif expected != actual:
            return None
    return params


def _string_list(body: dict, key: str) -> list[str]:
    values = body.get(key, [])
    if not isinstance(values, list):
        raise KiraError(HTTPStatus.BAD_REQUEST, f"{key} must be a list")
    return [str(item) for item in values]


def _query(query: dict, key: str, default: str = "") -> str:
    return query.get(key, [default])[0]


def _inline_type(name: str) -> str | None:
    """The type to play a file in the page with; browsers refuse QuickTime's own type for H.264/HEVC .mov."""
    return "video/mp4" if Path(name).suffix.casefold() == ".mov" else None


def _sniff_extension(path: Path) -> str:
    """Tell a file's type from its first bytes, for uploads whose name lost its extension."""
    with path.open("rb") as handle:
        head = handle.read(16)
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head[:4] in {b"II*\x00", b"MM\x00*"}:
        return ".tif"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"heic", b"heix", b"mif1", b"msf1", b"hevc"}:
            return ".heic"
        if brand == b"avif":
            return ".avif"
        return ".mov" if brand == b"qt  " else ".mp4"
    if head.startswith(b"8BPS"):
        return ".psd"
    return ""


class KiraHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: KiraApp, static_root: Path) -> None:
        self.app = app
        self.static_root = static_root
        super().__init__(address, KiraRequestHandler)

    def server_close(self) -> None:
        try:
            self.app.close()
        finally:
            try:
                super().server_close()
            finally:
                lease = getattr(self, "writer_lease", None)
                if lease:
                    lease.close()

    def process_request(self, request, client_address) -> None:
        lease = getattr(self, "writer_lease", None)
        if lease:
            lease.retain_request()
        try:
            super().process_request(request, client_address)
        except BaseException:
            if lease:
                lease.release_request()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            lease = getattr(self, "writer_lease", None)
            if lease:
                lease.release_request()


class KiraRequestHandler(BaseHTTPRequestHandler):
    server: KiraHTTPServer
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> KiraApp:
        return self.server.app

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
        self._body_read = False
        try:
            if path == "/" and method == "GET" and "state" in query and ("code" in query or "error" in query):
                self._google_oauth_callback(query)
                return
            if path in STATIC_PATHS | MEDIA_FILES and method in {"GET", "HEAD"}:
                self._serve_static(path)
                return
            if path == "/api/system" and method == "GET":
                self._send_json({"name": APP_NAME, "version": APP_VERSION})
                return
            if path == "/api/bootstrap" and method == "GET":
                self._bootstrap()
                return
            if path == "/api/pair-qr.svg" and method in {"GET", "HEAD"}:
                self._pair_qr()
                return
            if path == "/api/pair" and method == "POST":
                self._pair()
                return

            self._require_auth(query)
            segments = [unquote(segment) for segment in path.strip("/").split("/")]

            seen_methods: set[str] = set()
            for pattern, methods, handler_name, local_only in AUTHED_ROUTES:
                params = _match_route(pattern, segments)
                if params is None:
                    continue
                seen_methods.update(methods)
                if method not in methods:
                    continue
                if local_only:
                    self._require_local()
                getattr(self, handler_name)(params, query)
                return
            if seen_methods:
                raise KiraError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
            raise KiraError(HTTPStatus.NOT_FOUND, "Not found")
        except KiraError as exc:
            self._drain_unread(method)
            self._send_json({"error": exc.message, **exc.extra}, status=exc.status)
        except GoogleError as exc:
            print(f"[Google] {method} {path}: {' '.join(str(exc).splitlines())}", flush=True)
            self._drain_unread(method)
            if path == "/" and "state" in query:
                self._send_html(
                    "<!doctype html><meta charset='utf-8'><title>Kira</title>"
                    "<body style='font:16px system-ui;padding:40px'><h1>Could not connect</h1>"
                    f"<p>{html.escape(str(exc))}</p></body>",
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

    def _drain_unread(self, method: str) -> None:
        """A rejected upload may still have an unread body. Drain it so the connection can be reused."""
        if method not in {"PUT", "POST"} or self._body_read:
            return
        try:
            unread = int(self.headers.get("Content-Length", "0"))
            if 0 < unread <= MAX_DRAINED_BODY:
                self.rfile.read(unread)
            elif unread > MAX_DRAINED_BODY:
                self.close_connection = True
        except (OSError, ValueError):
            self.close_connection = True

    # -- Library --------------------------------------------------------------------

    def _h_library(self, params: dict, query: dict) -> None:
        overview = self.app.library.overview()
        overview["outbox_waiting"] = len(self.app.outbox.items("waiting"))
        self._send_json(overview)

    def _h_library_root(self, params: dict, query: dict) -> None:
        self._send_json(self.app.set_library_root(str(self._read_json().get("path", ""))))

    def _h_browse(self, params: dict, query: dict) -> None:
        self._send_json(browse_directories(_query(query, "path")))

    def _h_shoot_create(self, params: dict, query: dict) -> None:
        body = self._read_json()
        shoot = self.app.library.create_shoot(str(body.get("name", "")), body.get("year") or None)
        self._send_json(self.app.library.shoot_summary(shoot), status=HTTPStatus.CREATED)

    def _h_shoot(self, params: dict, query: dict) -> None:
        self._send_json(self.app.library.shoot_view(params["shoot_id"]))

    def _h_shoot_rename(self, params: dict, query: dict) -> None:
        shoot = self.app.library.rename_shoot(params["shoot_id"], str(self._read_json().get("name", "")))
        self._send_json(self.app.library.shoot_summary(shoot))

    def _h_shoot_trash(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        self._send_json(self.app.library.delete_shoot(params["shoot_id"]))

    def _h_shoot_rescan(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        self._send_json(self.app.library.rescan(params["shoot_id"]))

    def _h_shoot_reveal(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        reveal_directory(str(self.app.library.shoot(params["shoot_id"])["path"]))
        self._send_json({"ok": True})

    def _h_shoot_reject(self, params: dict, query: dict) -> None:
        body = self._read_json()
        rejected = body.get("rejected", True)
        if not isinstance(rejected, bool):
            raise KiraError(HTTPStatus.BAD_REQUEST, "rejected must be true or false")
        self._send_json(self.app.library.set_rejected(params["shoot_id"], _string_list(body, "photo_ids"), rejected))

    def _h_shoot_final(self, params: dict, query: dict) -> None:
        body = self._read_json()
        name = body.get("name")
        self._send_json(
            self.app.library.set_final(params["shoot_id"], str(body.get("photo_id", "")), str(name) if name else None)
        )

    def _h_shoot_tags(self, params: dict, query: dict) -> None:
        body = self._read_json()
        self._send_json(
            self.app.library.update_tags(
                params["shoot_id"],
                _string_list(body, "photo_ids"),
                _string_list(body, "add"),
                _string_list(body, "remove"),
            )
        )

    def _h_shoot_delete(self, params: dict, query: dict) -> None:
        body = self._read_json()
        name = body.get("name")
        self._send_json(
            self.app.library.delete_file(params["shoot_id"], str(body.get("photo_id", "")), str(name) if name else None)
        )

    def _h_shoot_upload(self, params: dict, query: dict) -> None:
        """One file dropped onto a shoot in the browser. The body is the raw file."""
        shoot_id = params["shoot_id"]
        self.app.library.shoot(shoot_id)
        staged, name = self._receive_body(self._upload_name(query))
        try:
            result = self.app.library.ingest_file(
                staged, shoot_id, source_kind="upload", original_name=name, consume=True
            )
            self.app.library.flush()
        finally:
            staged.unlink(missing_ok=True)
        self._send_json(result)

    def _h_preview(self, params: dict, query: dict) -> None:
        size = int(_query(query, "size", "480") or 480)
        path = self.app.library.preview(params["shoot_id"], params["photo_id"], params["name"], size)
        self._serve_file(
            path,
            f"{Path(params['name']).stem}.jpg",
            self.command == "HEAD",
            "image/jpeg",
            "inline",
            "private, max-age=86400",
        )

    def _h_file(self, params: dict, query: dict) -> None:
        shoot, photo = self.app.library.photo(params["shoot_id"], params["photo_id"])
        record = self.app.library.file_record(photo, params["name"])
        path = self.app.library.file_path(shoot, photo, record)
        if not path.is_file():
            raise KiraError(HTTPStatus.SERVICE_UNAVAILABLE, "The library drive is not connected")
        if _query(query, "inline"):
            self._serve_file(path, record["name"], self.command == "HEAD", _inline_type(record["name"]), "inline")
        else:
            self._serve_file(path, record["name"], self.command == "HEAD")

    # -- Import ---------------------------------------------------------------------

    def _shoot_for(self, body: dict) -> str:
        shoot_id = str(body.get("shoot_id", "")).strip()
        if shoot_id:
            return self.app.library.shoot(shoot_id)["id"]
        name = str(body.get("shoot_name", "")).strip()
        if not name:
            raise KiraError(HTTPStatus.BAD_REQUEST, "Choose a shoot or name a new one")
        existing = self.app.library.find_shoot(name)
        return (existing or self.app.library.create_shoot(name, body.get("year") or None))["id"]

    def _h_import(self, params: dict, query: dict) -> None:
        body = self._read_json()
        source = Path(str(body.get("source", "")).strip())
        if not str(source).strip(". ") or not source.is_dir():
            raise KiraError(HTTPStatus.NOT_FOUND, "Import folder not found")
        shoot_id = self._shoot_for(body)
        library = self.app.library
        task = self.app.tasks.start(
            "import",
            f"Import {source.name or source}",
            lambda task: library.import_folder(source, shoot_id, progress=task.progress),
        )
        self._send_json(task, status=HTTPStatus.ACCEPTED)

    def _h_import_check(self, params: dict, query: dict) -> None:
        result = self.app.library.check_source(Path(str(self._read_json().get("source", ""))))
        digests = result.pop("sha256")
        state = self.app.library.state
        backed = {row["sha256"] for row in state.execute("SELECT sha256 FROM drive_files")}
        in_photos = {row["sha256"] for row in state.execute("SELECT sha256 FROM gphotos WHERE state='present'")}
        result["backed_up"] = sum(1 for digest in digests if digest in backed or digest in in_photos)
        result["safe_to_wipe"] = (
            not result["not_imported"] and result["backed_up"] == len(digests) and result["total"] > 0
        )
        self._send_json(result)

    # -- Browsing a card or folder ---------------------------------------------------

    def _h_source(self, params: dict, query: dict) -> None:
        self._send_json(self.app.sources.listing(_query(query, "path"), _query(query, "recursive") == "1"))

    def _h_source_preview(self, params: dict, query: dict) -> None:
        size = int(_query(query, "size", "480") or 480)
        path = self.app.sources.preview(_query(query, "path"), size)
        self._serve_file(
            path, f"{Path(_query(query, 'path')).stem}.jpg", self.command == "HEAD", "image/jpeg", "inline"
        )

    def _h_source_file(self, params: dict, query: dict) -> None:
        path = Path(_query(query, "path"))
        if not path.is_file():
            raise KiraError(HTTPStatus.NOT_FOUND, "File not found")
        self._serve_file(path, path.name, self.command == "HEAD", _inline_type(path.name), "inline")

    def _h_source_scan(self, params: dict, query: dict) -> None:
        source = Path(str(self._read_json().get("path", "")).strip())
        sources = self.app.sources
        task = self.app.tasks.start(
            "scan",
            f"Check {source.name or source}",
            lambda task: sources.scan(source, progress=task.progress, cancelled=task.cancelled),
        )
        self._send_json(task, status=HTTPStatus.ACCEPTED)

    def _h_source_links(self, params: dict, query: dict) -> None:
        self._send_json(self.app.sources.write_links(str(self._read_json().get("path", ""))))

    def _h_source_trash_copies(self, params: dict, query: dict) -> None:
        self._send_json(self.app.sources.trash_library_copies(str(self._read_json().get("path", ""))))

    # -- Review and trash -------------------------------------------------------------

    def _h_review_list(self, params: dict, query: dict) -> None:
        self._send_json({"items": self.app.library.review_items()})

    def _h_review_preview(self, params: dict, query: dict) -> None:
        size = int(_query(query, "size", "480") or 480)
        path = self.app.library.review_preview(params["review_id"], size)
        self._serve_file(path, "preview.jpg", self.command == "HEAD", "image/jpeg", "inline")

    def _h_review_resolve(self, params: dict, query: dict) -> None:
        body = self._read_json()
        self._send_json(
            self.app.library.resolve_review(
                params["review_id"],
                str(body.get("action", "")),
                str(body.get("shoot_id") or "") or None,
                str(body.get("photo_id") or "") or None,
            )
        )

    def _h_trash(self, params: dict, query: dict) -> None:
        self._send_json({"items": self.app.library.trash_items()})

    def _h_trash_empty(self, params: dict, query: dict) -> None:
        body = self._read_json()
        if body.get("confirm") is not True:
            raise KiraError(HTTPStatus.BAD_REQUEST, "Emptying the trash needs confirm: true")
        self._send_json(self.app.library.empty_trash())

    # -- Tasks ----------------------------------------------------------------------

    def _h_tasks(self, params: dict, query: dict) -> None:
        self._send_json({"tasks": self.app.tasks.list()})

    def _h_task(self, params: dict, query: dict) -> None:
        self._send_json(self.app.tasks.get(params["task_id"]))

    def _h_task_cancel(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        self._send_json(self.app.tasks.cancel(params["task_id"]))

    # -- iPad -----------------------------------------------------------------------

    def _h_ipad_send(self, params: dict, query: dict) -> None:
        body = self._read_json()
        prefer = "jpeg" if body.get("format") == "jpeg" else "raw"
        self._send_json(self.app.outbox.send(str(body.get("shoot_id", "")), _string_list(body, "photo_ids"), prefer))

    def _h_ipad_outbox(self, params: dict, query: dict) -> None:
        items = self.app.outbox.items(_query(query, "state") or None)
        base = self._ipad_url()
        token = quote(self.app.token, safe="")
        for item in items:
            item["url"] = f"{base}/api/ipad/outbox/{item['id']}?token={token}"
        if _query(query, "format") == "urls":
            # A plain list of download links is the easiest thing for a Shortcut to loop over.
            body = "\n".join(item["url"] for item in items if item["state"] == "waiting")
            self._send_bytes(body.encode("utf-8"), "text/plain; charset=utf-8")
            return
        self._send_json({"items": items})

    def _h_ipad_outbox_file(self, params: dict, query: dict) -> None:
        path, name = self.app.outbox.file(params["item_id"])
        self._serve_file(path, name, self.command == "HEAD")
        if self.command == "GET" and not self.headers.get("Range"):
            self.app.outbox.mark_sent(params["item_id"])

    def _h_ipad_outbox_remove(self, params: dict, query: dict) -> None:
        self.app.outbox.remove(params["item_id"])
        self._send_json({"ok": True})

    def _h_ipad_outbox_clear(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        self._send_json(self.app.outbox.clear_sent())

    def _h_ipad_receive(self, params: dict, query: dict) -> None:
        """An edit coming back from the iPad, sent by the Shortcut or the browser."""
        staged, name = self._receive_body(self._upload_name(query))
        try:
            result = self.app.outbox.receive(staged, name)
        finally:
            staged.unlink(missing_ok=True)
        if _query(query, "format") == "text":
            message = f"{name}: {result.get('detail') or result['status']}"
            self._send_bytes(message.encode("utf-8"), "text/plain; charset=utf-8")
            return
        self._send_json(result)

    def _h_ipad_setup(self, params: dict, query: dict) -> None:
        base = self._ipad_url()
        token = quote(self.app.token, safe="")
        self._send_json(
            {
                "address": base,
                "outbox_urls": f"{base}/api/ipad/outbox?format=urls&token={token}",
                "receive_url": f"{base}/api/ipad/receive?format=text&token={token}&name=",
                "pair_url": self._pair_url(base),
            }
        )

    # -- Google ---------------------------------------------------------------------

    def _h_google_status(self, params: dict, query: dict) -> None:
        self._send_json(
            {
                "account": self.app.auth.status(),
                "drive": self.app.drive.status(),
                "photos": self.app.photos.status(),
                "missing": len(self.app.drive.missing()) if self.app.library.root else 0,
            }
        )

    def _h_google_connect(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        port = self.server.server_address[1]
        self._send_json({"url": self.app.auth.start(f"http://127.0.0.1:{port}/")})

    def _h_google_disconnect(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        self.app.auth.disconnect()
        self._send_json(self.app.auth.status())

    def _h_web_signin_start(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        self._send_json(self.app.web.start_sign_in())

    def _h_web_signin_status(self, params: dict, query: dict) -> None:
        self._send_json(self.app.web.status())

    def _h_web_signin_cancel(self, params: dict, query: dict) -> None:
        self._send_json(self.app.web.cancel_sign_in())

    def _h_web_session_delete(self, params: dict, query: dict) -> None:
        self._send_json(self.app.web.disconnect())

    def _start(self, kind: str, title: str, work) -> None:
        self._discard_optional_body()
        self._send_json(self.app.tasks.start(kind, title, work), status=HTTPStatus.ACCEPTED)

    def _h_backup_run(self, params: dict, query: dict) -> None:
        self._start("drive-backup", "Back up to Google Drive", self.app.drive.backup)

    def _h_backup_restore(self, params: dict, query: dict) -> None:
        self._start("drive-restore", "Restore missing files from Drive", self.app.drive.restore)

    def _h_photos_check(self, params: dict, query: dict) -> None:
        self._start("photos-check", "Check what Google Photos holds", self.app.photos.check)

    def _h_photos_upload(self, params: dict, query: dict) -> None:
        self._start("photos-upload", "Upload Finals to Google Photos", self.app.photos.upload_finals)

    def _h_cleanup_plan(self, params: dict, query: dict) -> None:
        self._start("photos-cleanup-plan", "Find copies to hide in Google Photos", self.app.photos.cleanup_plan)

    def _h_cleanup_get(self, params: dict, query: dict) -> None:
        self._send_json(json.loads(self.app.library.state.get("gphotos_cleanup_plan") or '{"items": []}'))

    def _h_cleanup_apply(self, params: dict, query: dict) -> None:
        keys = _string_list(self._read_json(), "media_keys")
        photos = self.app.photos
        self._send_json(
            self.app.tasks.start(
                "photos-cleanup", "Hide copies in Google Photos", lambda task: photos.cleanup_apply(keys, task)
            ),
            status=HTTPStatus.ACCEPTED,
        )

    def _h_picker_start(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        self._send_json(self.app.photos.picker_start())

    def _h_picker_status(self, params: dict, query: dict) -> None:
        self._send_json(self.app.photos.picker_status(params["session_id"]))

    def _h_picker_import(self, params: dict, query: dict) -> None:
        shoot_id = self._shoot_for(self._read_json())
        session_id = params["session_id"]
        photos = self.app.photos
        self._send_json(
            self.app.tasks.start(
                "picker-import",
                "Import from Google Photos",
                lambda task: photos.picker_import(session_id, shoot_id, task),
            ),
            status=HTTPStatus.ACCEPTED,
        )

    # -- Request helpers -----------------------------------------------------------------

    def _upload_name(self, query: dict) -> str:
        raw = _query(query, "name") or unquote(self.headers.get("X-File-Name", ""))
        if not raw:
            disposition = self.headers.get("Content-Disposition", "")
            match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
            raw = unquote(match.group(1)) if match else ""
        if not raw:
            raw = f"ipad-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        return safe_filename(Path(raw).name)

    def _receive_body(self, name: str) -> tuple[Path, str]:
        """Stream the request body onto the library drive, checking an optional sender SHA-256."""
        length = self._content_length()
        if length <= 0:
            raise KiraError(HTTPStatus.BAD_REQUEST, "The upload is empty")
        if length > MAX_UPLOAD_BYTES:
            raise KiraError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "The file is too large")
        staged = self.app.library.staging_path(name)
        digest = hashlib.sha256()
        remaining = length
        self._body_read = True
        try:
            with staged.open("xb") as output:
                while remaining:
                    chunk = self.rfile.read(min(CHUNK_COPY_SIZE, remaining))
                    if not chunk:
                        raise KiraError(HTTPStatus.BAD_REQUEST, "The upload was interrupted")
                    digest.update(chunk)
                    output.write(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            expected = (self.headers.get("X-Kira-Sha256") or "").strip().lower()
            if expected and expected != digest.hexdigest():
                raise KiraError(HTTPStatus.CONFLICT, "The file was damaged on the way; send it again")
            if file_kind(name) == "other":
                # iOS Shortcuts often send a name without its extension; tell the type from the bytes.
                extension = _sniff_extension(staged)
                if extension:
                    name = f"{name}{extension}"
                    renamed = staged.with_suffix(extension)
                    staged.rename(renamed)
                    staged = renamed
            return staged, name
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def _google_oauth_callback(self, query: dict[str, list[str]]) -> None:
        self._require_local()
        error = _query(query, "error")
        if error:
            raise GoogleError(f"Google sign-in was cancelled: {error}")
        self.app.auth.finish(_query(query, "code"), _query(query, "state"))
        self._send_html(
            "<!doctype html><meta charset='utf-8'><title>Kira</title>"
            "<body style='font:16px system-ui;padding:40px'>"
            "<h1>Google connected</h1>"
            "<p>You can close this window and return to Kira.</p>"
            "<script>setTimeout(() => window.close(), 1200)</script></body>"
        )

    def _bootstrap(self) -> None:
        if not self._is_loopback():
            raise KiraError(HTTPStatus.FORBIDDEN, "Bootstrap is only available on the desktop")
        ipad_url = self._ipad_url()
        self._send_json(
            {
                "token": self.app.token,
                "pair_code": self.app.pair_code,
                "ipad_url": ipad_url,
                "pair_url": self._pair_url(ipad_url),
                "pair_qr_url": "/api/pair-qr.svg",
                "data_dir": str(self.app.root),
                "version": APP_VERSION,
            }
        )

    def _is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _require_local(self) -> None:
        if not self._is_loopback():
            raise KiraError(HTTPStatus.FORBIDDEN, "This action is only available on the desktop")

    def _ipad_url(self) -> str:
        return f"http://{discover_local_ip()}:{self.server.server_address[1]}"

    def _pair_url(self, ipad_url: str) -> str:
        return f"{ipad_url}/?pair={quote(self.app.pair_secret, safe='')}"

    def _pair_qr(self) -> None:
        if not self._is_loopback():
            raise KiraError(HTTPStatus.FORBIDDEN, "Pairing QR is only displayed on the desktop")
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=4,
            image_factory=qrcode.image.svg.SvgPathFillImage,
        )
        qr.add_data(self._pair_url(self._ipad_url()))
        qr.make(fit=True)
        self._send_bytes(qr.make_image().to_string(encoding="utf-8"), "image/svg+xml; charset=utf-8")

    def _pair(self) -> None:
        body = self._read_json()
        supplied_code = str(body.get("code", "")).strip()
        supplied_secret = str(body.get("secret", "")).strip()
        code_matches = bool(supplied_code) and secrets.compare_digest(supplied_code, self.app.pair_code)
        secret_matches = bool(supplied_secret) and secrets.compare_digest(supplied_secret, self.app.pair_secret)
        if not code_matches and not secret_matches:
            raise KiraError(HTTPStatus.UNAUTHORIZED, "Pairing code is incorrect")
        self._send_json({"token": self.app.token})

    def _require_auth(self, query: dict[str, list[str]]) -> None:
        supplied = self.headers.get("X-Kira-Token") or query.get("token", [""])[0]
        if not supplied or not secrets.compare_digest(supplied, self.app.token):
            raise KiraError(HTTPStatus.UNAUTHORIZED, "Pair this device with Kira")

    def _read_json(self) -> dict:
        length = self._content_length()
        if length > MAX_JSON_BODY:
            raise KiraError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "JSON body is too large")
        raw = self.rfile.read(length)
        self._body_read = True
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

    def _serve_static(self, path: str) -> None:
        if path in MEDIA_FILES:
            target = MEDIA_ROOT / path.rsplit("/", 1)[1]
        else:
            filename = "index.html" if path in {"/", "/index.html"} else path.lstrip("/")
            target = self.server.static_root / filename
        if not target.exists():
            raise KiraError(HTTPStatus.NOT_FOUND, "Not found")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self._send_bytes(target.read_bytes(), content_type)

    def _send_bytes(self, data: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        """Send a small, uncached in-memory response."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_html(self, markup: str, status: int = HTTPStatus.OK) -> None:
        self._send_bytes(markup.encode("utf-8"), "text/html; charset=utf-8", status)

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
        self.send_header(
            "Content-Disposition", f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"
        )
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
        self._send_bytes(encoded, "application/json; charset=utf-8", status)
