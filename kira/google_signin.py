"""Collect a Google Photos session from a browser opened expressly for sign-in."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event

import websocket


class GoogleSignInError(Exception):
    pass


def find_browser() -> str | None:
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        folder = os.environ.get(variable)
        if folder:
            for relative in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
                candidate = Path(folder) / relative
                if candidate.is_file():
                    return str(candidate)
    for name in ("google-chrome", "chromium", "chromium-browser", "msedge"):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    return None


# Straight to Google's account sign-in, landing in Photos afterwards, instead of the Photos
# landing page with its own "Go to Google Photos" button.
SIGN_IN_URL = "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fphotos.google.com%2F"


class GoogleSignInBrowser:
    """Own a temporary browser profile; never attach to a personal browser.

    Google refuses sign-in from a browser started with remote debugging, so the visible window is a plain
    browser. Once the user closes it, the same profile is reopened headless on loopback just to read cookies.
    """

    def __init__(self, root: Path, cancelled: Event) -> None:
        self.root = root
        self.cancelled = cancelled
        self.executable = None
        self.process = None
        self.socket = None
        self.profile = None
        self.request_id = 0

    def __enter__(self) -> GoogleSignInBrowser:
        if self.cancelled.is_set():
            raise GoogleSignInError("Sign-in cancelled.")
        self.executable = find_browser()
        if not self.executable:
            raise GoogleSignInError("Install Google Chrome or Microsoft Edge to sign in to Google Photos.")
        self.profile = tempfile.TemporaryDirectory(prefix=".kira-google-signin-", dir=self.root)
        try:
            self.process = subprocess.Popen(
                [*self._base_arguments(), "--new-window", SIGN_IN_URL],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except BaseException:
            self.close()
            raise
        return self

    def _base_arguments(self) -> list[str]:
        return [
            self.executable,
            f"--user-data-dir={self.profile.name}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            # Bound sessions tie Google's short-lived cookies to a key inside this throwaway
            # profile, so Kira could never renew them and the sign-in would die within hours.
            "--disable-features=EnableBoundSessionCredentials,DeviceBoundSessionCredentials",
        ]

    def wait_until_closed(self, deadline: float) -> bool:
        """Wait for the user to close the sign-in window; False on timeout or cancel."""
        while not self.cancelled.is_set() and time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.process = None
                return True
            self.cancelled.wait(0.5)
        return False

    def _open_reader(self) -> None:
        (Path(self.profile.name) / "DevToolsActivePort").unlink(missing_ok=True)
        arguments = [
            *self._base_arguments(),
            "--headless=new",
            "--disable-gpu",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "about:blank",
        ]
        self.process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        active_port = Path(self.profile.name) / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        while not self.cancelled.is_set() and time.monotonic() < deadline:
            try:
                lines = active_port.read_text(encoding="utf-8").splitlines()
                port = int(lines[0])
                endpoint = lines[1]
                if not 0 < port < 65536 or not endpoint.startswith("/devtools/browser/"):
                    raise ValueError
                self.socket = websocket.create_connection(
                    f"ws://127.0.0.1:{port}{endpoint}",
                    timeout=5,
                    suppress_origin=True,
                    http_no_proxy=["127.0.0.1", "localhost"],
                )
                return
            except (OSError, ValueError, IndexError, websocket.WebSocketException):
                if self.process.poll() is not None:
                    break
                self.cancelled.wait(0.2)
        raise GoogleSignInError("Could not read the Google sign-in. Try again.")

    def command(self, method: str, params: dict | None = None) -> dict:
        self.request_id += 1
        request_id = self.request_id
        self.socket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.socket.settimeout(max(0.1, deadline - time.monotonic()))
            response = json.loads(self.socket.recv())
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise GoogleSignInError("Could not read the Google sign-in window. Try again.")
            return response.get("result", {})
        raise GoogleSignInError("The Google sign-in window stopped responding. Try again.")

    def session(self) -> tuple[list[dict], int] | None:
        """Read the signed-in Google cookies from the closed sign-in profile."""
        self._open_reader()
        try:
            cookies = self.command("Storage.getCookies").get("cookies", [])
        finally:
            self._close_reader()
        return self.google_session(cookies)

    @staticmethod
    def google_session(cookies: list[dict]) -> tuple[list[dict], int] | None:
        google_cookies = []
        for cookie in cookies:
            domain = str(cookie.get("domain", "")).lstrip(".").casefold()
            if domain == "google.com" or domain.endswith(".google.com"):
                google_cookies.append({**cookie, "expirationDate": cookie.get("expires")})
        if any(cookie.get("name") in {"SID", "__Secure-1PSID", "__Secure-3PSID"} for cookie in google_cookies):
            # A fresh profile has exactly the accounts signed in here, and the first is /u/0.
            return google_cookies, 0
        return None

    def _close_reader(self) -> None:
        if self.socket:
            try:
                self.command("Browser.close")
            except (OSError, ValueError, GoogleSignInError, websocket.WebSocketException):
                pass
            finally:
                self.socket.close()
                self.socket = None

    def close(self) -> None:
        self._close_reader()
        if self.process:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
            self.process = None
        if self.profile:
            self.profile.cleanup()
            self.profile = None

    def __exit__(self, *_args) -> None:
        self.close()
