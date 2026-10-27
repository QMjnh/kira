"""Collect a Google Photos session from a browser opened expressly for sign-in."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event
from urllib.parse import urlparse

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


class GoogleSignInBrowser:
    """Own a temporary browser profile; never attach to a personal browser."""

    def __init__(self, root: Path, cancelled: Event, *, headless: bool = False) -> None:
        self.root = root
        self.cancelled = cancelled
        self.headless = headless
        self.process = None
        self.socket = None
        self.profile = None
        self.request_id = 0

    def __enter__(self) -> GoogleSignInBrowser:
        if self.cancelled.is_set():
            raise GoogleSignInError("Sign-in cancelled.")
        executable = find_browser()
        if not executable:
            raise GoogleSignInError("Install Google Chrome or Microsoft Edge to sign in to Google Photos.")
        self.profile = tempfile.TemporaryDirectory(prefix=".kira-google-signin-", dir=self.root)
        arguments = [
            executable,
            f"--user-data-dir={self.profile.name}",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--new-window",
        ]
        if self.headless:
            arguments.extend(("--headless=new", "--disable-gpu"))
        arguments.append("https://photos.google.com/")
        try:
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
                    return self
                except (OSError, ValueError, IndexError, websocket.WebSocketException):
                    if self.process.poll() is not None:
                        break
                    self.cancelled.wait(0.2)
            raise GoogleSignInError("Could not open the Google sign-in window. Try again.")
        except BaseException:
            self.close()
            raise

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
        targets = self.command("Target.getTargets").get("targetInfos", [])
        for target in targets:
            url = urlparse(target.get("url", ""))
            if target.get("type") != "page" or url.scheme != "https" or url.hostname != "photos.google.com":
                continue
            match = re.match(r"/u/(\d+)(?:/|$)", url.path)
            account_index = int(match[1]) if match else 0
            cookies = self.command("Storage.getCookies").get("cookies", [])
            google_cookies = []
            for cookie in cookies:
                domain = str(cookie.get("domain", "")).lstrip(".").casefold()
                if domain == "google.com" or domain.endswith(".google.com"):
                    google_cookies.append({**cookie, "expirationDate": cookie.get("expires")})
            if any(cookie.get("name") in {"SID", "__Secure-1PSID", "__Secure-3PSID"} for cookie in google_cookies):
                return google_cookies, account_index
        return None

    def close(self) -> None:
        if self.socket:
            try:
                self.command("Browser.close")
            except (OSError, ValueError, GoogleSignInError, websocket.WebSocketException):
                pass
            finally:
                self.socket.close()
                self.socket = None
        if self.process:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
        if self.profile:
            self.profile.cleanup()
            self.profile = None

    def __exit__(self, *_args) -> None:
        self.close()
