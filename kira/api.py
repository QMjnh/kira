"""HTTP layer: request dispatch, authentication, and API route handlers."""
from __future__ import annotations

import ipaddress
import json
import mimetypes
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
from .errors import KiraError
from .media import (
    PREVIEW_EXTENSIONS,
    browse_directories,
    move_assets_between_directories,
    move_culling_assets,
    safe_download_name,
    scan_photo_directory,
)
from .store import KiraStore
from google_photos import GooglePhotosError, GooglePhotosService

MAX_JSON_BODY = 1024 * 1024
CHUNK_COPY_SIZE = 4 * 1024 * 1024
APP_ROOT = Path(__file__).resolve().parent.parent

# (pattern, methods, handler, requires_local). "{name}" segments become params.
AUTHED_ROUTES: list[tuple[tuple[str, ...], tuple[str, ...], str, bool]] = [
    (("api", "google", "status"), ("GET",), "_h_google_status", True),
    (("api", "google", "oauth", "start"), ("POST",), "_h_google_oauth_start", True),
    (("api", "google", "disconnect"), ("POST",), "_h_google_disconnect", True),
    (("api", "google", "web-session"), ("POST",), "_h_google_web_session_import", True),
    (("api", "google", "web-session"), ("DELETE",), "_h_google_web_session_delete", True),
    (("api", "google", "albums"), ("GET",), "_h_google_albums", True),
    (("api", "google", "match-folder"), ("POST",), "_h_google_match_folder", True),
    (("api", "google", "organize"), ("POST",), "_h_google_organize", True),
    (("api", "google", "picker", "sessions"), ("POST",), "_h_picker_session_create", True),
    (("api", "google", "picker", "sessions", "{session_id}"), ("GET",), "_h_picker_session_get", True),
    (("api", "google", "imports"), ("POST",), "_h_google_imports", True),
    (("api", "google", "uploads"), ("POST",), "_h_google_uploads", True),
    (("api", "google", "operations", "{operation_id}"), ("GET",), "_h_google_operation", True),
    (("api", "local", "browse"), ("GET",), "_h_local_browse", True),
    (("api", "local", "scan"), ("GET",), "_h_local_scan", True),
    (("api", "local", "cull"), ("POST",), "_h_local_cull", True),
    (("api", "local", "move"), ("POST",), "_h_local_move", True),
    (("api", "local", "preview"), ("GET", "HEAD"), "_h_local_preview", True),
    (("api", "jobs", "from-selection"), ("POST",), "_h_job_from_selection", True),
    (("api", "jobs"), ("GET",), "_h_jobs_list", False),
    (("api", "jobs"), ("POST",), "_h_jobs_create", False),
    (("api", "jobs", "{job_id}"), ("GET",), "_h_job_get", False),
    (("api", "jobs", "{job_id}"), ("DELETE",), "_h_job_delete", True),
    (("api", "jobs", "{job_id}", "organize"), ("POST",), "_h_job_organize", True),
    (("api", "jobs", "{job_id}", "uploads", "start"), ("POST",), "_h_upload_start", False),
    (("api", "jobs", "{job_id}", "uploads", "{upload_id}", "chunk"), ("PUT",), "_h_upload_chunk", False),
    (("api", "jobs", "{job_id}", "uploads", "{upload_id}", "complete"), ("POST",), "_h_upload_complete", False),
    (("api", "jobs", "{job_id}", "{kind}", "{record_id}", "download"), ("GET", "HEAD"), "_h_file_download", False),
    (("api", "jobs", "{job_id}", "bundle.zip"), ("GET", "HEAD"), "_h_bundle", False),
]

STATIC_PATHS = {"/", "/index.html", "/app.js", "/styles.css"}


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
    for expected, actual in zip(pattern, segments):
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


class KiraHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: KiraStore, static_root: Path) -> None:
        self.store = store
        self.static_root = static_root
        self.google_photos = GooglePhotosService(store.root, APP_ROOT)
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
            if path in STATIC_PATHS and method in {"GET", "HEAD"}:
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
                self._method_not_allowed()
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

    # -- Google Photos handlers -------------------------------------------------

    def _h_google_status(self, params: dict, query: dict) -> None:
        self._send_json(self.server.google_photos.status())

    def _h_google_oauth_start(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        port = self.server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}"
        self._send_json(self.server.google_photos.start_oauth(redirect_uri))

    def _h_google_disconnect(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        self._send_json(self.server.google_photos.disconnect())

    def _h_google_web_session_import(self, params: dict, query: dict) -> None:
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
            self.server.google_photos.import_web_session(Path(cookies_path), account_index)
        )

    def _h_google_web_session_delete(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        self._send_json(self.server.google_photos.disconnect_web_session())

    def _h_google_albums(self, params: dict, query: dict) -> None:
        self._send_json(self.server.google_photos.albums())

    def _h_google_match_folder(self, params: dict, query: dict) -> None:
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

    def _h_google_organize(self, params: dict, query: dict) -> None:
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

    def _h_picker_session_create(self, params: dict, query: dict) -> None:
        body = self._read_json()
        self._send_json(
            self.server.google_photos.create_picker_session(int(body.get("max_items", 2000))),
            status=HTTPStatus.CREATED,
        )

    def _h_picker_session_get(self, params: dict, query: dict) -> None:
        self._send_json(self.server.google_photos.picker_session(params["session_id"]))

    def _h_google_imports(self, params: dict, query: dict) -> None:
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

    def _h_google_uploads(self, params: dict, query: dict) -> None:
        body = self._read_json()
        asset_ids = _string_list(body, "asset_ids")
        scan = scan_photo_directory(str(body.get("source_directory", "")))
        selected = set(asset_ids)
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

    def _h_google_operation(self, params: dict, query: dict) -> None:
        self._send_json(self.server.google_photos.operation(params["operation_id"]))

    # -- Local library handlers -------------------------------------------------

    def _h_local_browse(self, params: dict, query: dict) -> None:
        self._send_json(browse_directories(query.get("path", [""])[0]))

    def _h_local_scan(self, params: dict, query: dict) -> None:
        self._send_json(scan_photo_directory(query.get("path", [""])[0]))

    def _h_local_cull(self, params: dict, query: dict) -> None:
        body = self._read_json()
        self._send_json(
            move_culling_assets(
                str(body.get("source_directory", "")),
                _string_list(body, "asset_ids"),
                str(body.get("action", "")),
                str(body.get("group_name", "")),
            )
        )

    def _h_local_move(self, params: dict, query: dict) -> None:
        body = self._read_json()
        self._send_json(
            move_assets_between_directories(
                str(body.get("source_directory", "")),
                str(body.get("destination_directory", "")),
                _string_list(body, "asset_ids"),
            )
        )

    def _h_local_preview(self, params: dict, query: dict) -> None:
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
            self.command == "HEAD",
            content_type="image/jpeg" if thumbnail != preview else None,
            disposition="inline",
            cache_control="private, max-age=3600",
        )

    # -- Job handlers -------------------------------------------------------------

    def _h_job_from_selection(self, params: dict, query: dict) -> None:
        body = self._read_json()
        result = self.server.store.create_job_from_selection(
            str(body.get("name", "")),
            str(body.get("source_directory", "")),
            _string_list(body, "selected_ids"),
            str(body.get("source_format", "")),
        )
        self._send_json(result, status=HTTPStatus.CREATED)

    def _h_jobs_list(self, params: dict, query: dict) -> None:
        self._send_json({"jobs": self.server.store.list_jobs()})

    def _h_jobs_create(self, params: dict, query: dict) -> None:
        body = self._read_json()
        self._send_json(self.server.store.create_job(str(body.get("name", ""))), status=201)

    def _h_job_get(self, params: dict, query: dict) -> None:
        self._send_json(self.server.store.get_job(params["job_id"]))

    def _h_job_delete(self, params: dict, query: dict) -> None:
        self._send_json(self.server.store.delete_job(params["job_id"]))

    def _h_job_organize(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        self._send_json(self.server.store.organize_source_folder(params["job_id"]))

    def _h_upload_start(self, params: dict, query: dict) -> None:
        body = self._read_json()
        result = self.server.store.start_upload(
            params["job_id"],
            str(body.get("kind", "")),
            str(body.get("filename", "")),
            int(body.get("size", -1)),
            int(body.get("last_modified", 0)),
        )
        self._send_json(result)

    def _h_upload_chunk(self, params: dict, query: dict) -> None:
        offset = int(query.get("offset", ["0"])[0])
        length = self._content_length()
        result = self.server.store.append_upload(
            params["job_id"], params["upload_id"], offset, length, self.rfile
        )
        self._send_json(result)

    def _h_upload_complete(self, params: dict, query: dict) -> None:
        self._discard_optional_body()
        result = self.server.store.complete_upload(params["job_id"], params["upload_id"])
        self._send_json(result)

    def _h_file_download(self, params: dict, query: dict) -> None:
        if params["kind"] not in {"files", "returns"}:
            raise KiraError(HTTPStatus.NOT_FOUND, "Not found")
        kind = "originals" if params["kind"] == "files" else "returns"
        file_path, record = self.server.store.resolve_file(params["job_id"], kind, params["record_id"])
        self._serve_file(file_path, record["filename"], self.command == "HEAD")

    def _h_bundle(self, params: dict, query: dict) -> None:
        file_path, filename = self.server.store.create_bundle(params["job_id"])
        self._serve_file(file_path, filename, self.command == "HEAD", content_type="application/zip")

    # -- Pairing, static files, and response helpers ------------------------------

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
