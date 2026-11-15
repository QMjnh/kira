"""Google sign-in for the official APIs Kira uses: Drive backup and Google Photos uploads.

One OAuth consent covers both. Tokens are encrypted with Windows DPAPI for the
current user. ``drive.file`` only reaches files Kira created; Photos access is
append-only plus read access to what Kira itself uploaded.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import http.client
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .media import CHUNK_COPY_SIZE, atomic_json_write

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
PHOTOS_APPEND_SCOPE = "https://www.googleapis.com/auth/photoslibrary.appendonly"
PHOTOS_APP_READ_SCOPE = "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata"
PICKER_SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
SCOPES = (DRIVE_SCOPE, PHOTOS_APPEND_SCOPE, PHOTOS_APP_READ_SCOPE, PICKER_SCOPE)
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"


class GoogleError(Exception):
    pass


if os.name == "nt":
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    def _dpapi(function: object, data: bytes, description: str | None) -> bytes:
        """Run a Windows DPAPI protect/unprotect call over ``data`` for the current user."""
        source = ctypes.create_string_buffer(data)
        incoming = _DataBlob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
        outgoing = _DataBlob()
        if not function(ctypes.byref(incoming), description, None, None, None, 0, ctypes.byref(outgoing)):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(outgoing.pbData, outgoing.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(outgoing.pbData)


def _windows_protect(data: bytes) -> bytes:
    return _dpapi(ctypes.windll.crypt32.CryptProtectData, data, "Kira Google")


def _windows_unprotect(data: bytes) -> bytes:
    return _dpapi(ctypes.windll.crypt32.CryptUnprotectData, data, None)


def protect_json(path: Path, payload: dict) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    protected = _windows_protect(raw) if os.name == "nt" else raw
    atomic_json_write(path, {"encrypted": os.name == "nt", "data": base64.b64encode(protected).decode("ascii")})
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def unprotect_json(path: Path) -> dict:
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    raw = base64.b64decode(wrapper["data"])
    if wrapper.get("encrypted"):
        if os.name != "nt":
            raise GoogleError("Google credentials belong to another desktop")
        raw = _windows_unprotect(raw)
    return json.loads(raw)


def error_message(body: bytes, status: int) -> str:
    try:
        payload = json.loads(body)
        error = payload.get("error", payload)
        if isinstance(error, dict):
            return str(error.get("message") or error.get("error_description") or f"Google request failed ({status})")
        return str(error)
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return f"Google request failed ({status})"


class GoogleAuth:
    def __init__(self, data_root: Path, app_root: Path) -> None:
        self.root = data_root
        self.credentials_path = app_root / "google-oauth-client.json"
        self.token_path = data_root / "google-token.json"
        self.lock = threading.RLock()
        self.pending: dict[str, dict] = {}

    def _client(self) -> dict:
        client_id = os.environ.get("KIRA_GOOGLE_CLIENT_ID", "").strip()
        if client_id:
            return {"client_id": client_id, "client_secret": os.environ.get("KIRA_GOOGLE_CLIENT_SECRET", "").strip()}
        if not self.credentials_path.exists():
            raise GoogleError(f"Add Google OAuth credentials at {self.credentials_path}")
        try:
            payload = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            client = payload.get("installed") or payload.get("web") or payload
            return {"client_id": str(client["client_id"]), "client_secret": str(client.get("client_secret", ""))}
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise GoogleError("Google OAuth credential file is invalid") from exc

    def _load(self) -> dict:
        if not self.token_path.exists():
            raise GoogleError("Connect your Google account first")
        try:
            return unprotect_json(self.token_path)
        except GoogleError:
            raise
        except Exception as exc:
            raise GoogleError("Stored Google credentials are unreadable; connect again") from exc

    def granted(self) -> set[str]:
        try:
            return set(str(self._load().get("scope", "")).split())
        except GoogleError:
            return set()

    def status(self) -> dict:
        try:
            self._client()
            configured = True
        except GoogleError:
            configured = False
        granted = self.granted()
        return {
            "configured": configured,
            "connected": bool(granted),
            "drive": DRIVE_SCOPE in granted,
            "photos_upload": PHOTOS_APPEND_SCOPE in granted,
            "photos_picker": PICKER_SCOPE in granted,
            "needs_consent": bool(granted) and not set(SCOPES) <= granted,
        }

    def start(self, redirect_uri: str) -> str:
        client = self._client()
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with self.lock:
            self.pending[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "expires": time.time() + 600}
        query = urlencode(
            {
                "client_id": client["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{AUTH_ENDPOINT}?{query}"

    def owns_state(self, state: str) -> bool:
        with self.lock:
            return state in self.pending

    def finish(self, code: str, state: str) -> None:
        with self.lock:
            pending = self.pending.pop(state, None)
        if not pending or pending["expires"] < time.time():
            raise GoogleError("Google sign-in expired or could not be verified")
        client = self._client()
        token = request_json(
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
            raise GoogleError("Google did not return an access token")
        token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
        protect_json(self.token_path, token)

    def access_token(self, scope: str) -> str:
        with self.lock:
            token = self._load()
            if scope not in str(token.get("scope", "")).split():
                raise GoogleError("Reconnect your Google account to allow Drive backup and Photos uploads")
            if token.get("access_token") and float(token.get("expires_at", 0)) > time.time():
                return str(token["access_token"])
            refresh = token.get("refresh_token")
            if not refresh:
                raise GoogleError("Google sign-in expired; connect again")
            client = self._client()
            refreshed = request_json(
                TOKEN_ENDPOINT,
                method="POST",
                form={
                    "client_id": client["client_id"],
                    "client_secret": client["client_secret"],
                    "refresh_token": refresh,
                    "grant_type": "refresh_token",
                },
            )
            token.update(refreshed)
            token["refresh_token"] = refresh
            token["expires_at"] = time.time() + int(refreshed.get("expires_in", 3600)) - 60
            protect_json(self.token_path, token)
            return str(token["access_token"])

    def disconnect(self) -> None:
        try:
            token = self._load()
            revoke = token.get("refresh_token") or token.get("access_token")
            if revoke:
                request_json(REVOKE_ENDPOINT, method="POST", form={"token": revoke})
        except GoogleError:
            pass
        self.token_path.unlink(missing_ok=True)


def request_json(
    url: str,
    method: str = "GET",
    token: str | None = None,
    payload: dict | None = None,
    form: dict | None = None,
    timeout: int = 60,
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
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
        return json.loads(body) if body else {}
    except HTTPError as exc:
        raise GoogleError(error_message(exc.read(), exc.code)) from exc
    except (URLError, OSError) as exc:
        raise GoogleError(f"Could not reach Google: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GoogleError("Google returned an invalid response") from exc


def send_file(
    url: str, path: Path, headers: dict[str, str], method: str = "POST", offset: int = 0
) -> tuple[int, dict[str, str], bytes]:
    """Stream a file (from ``offset``) as a request body. Returns status, headers and body."""
    parsed = urlparse(url)
    connection = http.client.HTTPSConnection(parsed.netloc, timeout=300)
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    size = path.stat().st_size
    try:
        connection.putrequest(method, target)
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.putheader("Content-Length", str(size - offset))
        connection.endheaders()
        with path.open("rb") as source:
            source.seek(offset)
            while chunk := source.read(CHUNK_COPY_SIZE):
                connection.send(chunk)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
    except OSError as exc:
        raise GoogleError(f"Upload of {path.name} was interrupted: {exc}") from exc
    finally:
        connection.close()
