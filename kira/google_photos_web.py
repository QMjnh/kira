"""Album and Archive automation through the vendored ``gpwc`` Google Photos web client."""

from __future__ import annotations

import base64
import concurrent.futures
import importlib
import json
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.cookiejar import MozillaCookieJar
from pathlib import Path

from . import visual
from .google_auth import GoogleError, _windows_protect, _windows_unprotect
from .google_signin import GoogleSignInBrowser, GoogleSignInError, find_browser
from .media import atomic_json_write, match_key


class GooglePhotosWebError(GoogleError):
    pass


class GooglePhotosWebService:
    """Focused adapter for album assignment and Archive through Google Photos' web client."""

    MAX_COOKIE_BYTES = 5 * 1024 * 1024
    MAX_MEDIA_ITEMS = 500
    MAX_HASHES = 5000
    HASH_BATCH_SIZE = 100
    INFO_BATCH_SIZE = 100
    VERIFY_ATTEMPTS = 10
    VERIFY_INTERVAL_SECONDS = 0.5
    VISUAL_NARROW_FALLBACK_LIMIT = 6
    # Google retires the __Secure-*PSIDTS cookies every few hours; a browser swaps in fresh ones
    # through RotateCookies. Kira does the same while it runs and saves whatever Google sends back.
    ROTATE_URL = "https://accounts.google.com/RotateCookies"
    ROTATE_EVERY_SECONDS = 10 * 60
    BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

    def __init__(
        self, data_root: Path, client_factory: object | None = None, payloads_module: object | None = None
    ) -> None:
        self.root = data_root.resolve()
        self.session_path = self.root / "google-web-session.json"
        self._client_factory = client_factory
        self._payloads_module = payloads_module
        self._signin_lock = threading.RLock()
        self._signin_cancelled = threading.Event()
        self._signin_thread = None
        self._signin_state = {"status": "idle"}
        self._thumbnail_lock = threading.Lock()
        self._thumbnail_session: tuple[int, object] | None = None
        self._cookie_lock = threading.Lock()
        self._rotated_at: float | None = None
        self._keepalive_stop = threading.Event()
        self._keepalive_thread: threading.Thread | None = None
        # Set once Google turns the saved session away, so status stops calling it connected.
        self._session_expired = False

    def status(self) -> dict:
        metadata = self._session_metadata()
        try:
            self._dependency()
            available = True
        except GooglePhotosWebError:
            available = False
        readable = self._session_readable()
        return {
            "available": available,
            "connected": readable and not self._session_expired,
            "expired": readable and self._session_expired,
            "account": metadata.get("account"),
            "account_index": metadata.get("account_index", 0),
            "session_path": str(self.session_path),
            "browser_available": find_browser() is not None,
            "sign_in": self.sign_in_status(),
        }

    def sign_in_status(self) -> dict:
        with self._signin_lock:
            return dict(self._signin_state)

    def start_sign_in(self) -> dict:
        self._dependency()
        if not find_browser():
            raise GooglePhotosWebError("Install Google Chrome or Microsoft Edge to sign in to Google Photos.")
        with self._signin_lock:
            if self._signin_thread and self._signin_thread.is_alive():
                return dict(self._signin_state)
            cancelled = threading.Event()
            self._signin_cancelled = cancelled
            self._signin_state = {"status": "starting"}
            self._signin_thread = threading.Thread(target=self._run_sign_in, args=(cancelled,), daemon=True)
            self._signin_thread.start()
            return dict(self._signin_state)

    def cancel_sign_in(self) -> dict:
        with self._signin_lock:
            self._signin_cancelled.set()
            if self._signin_state["status"] in {"starting", "waiting", "verifying"}:
                self._signin_state = {"status": "cancelled"}
            return dict(self._signin_state)

    def close(self) -> None:
        self.cancel_sign_in()
        self._keepalive_stop.set()
        if self._signin_thread:
            self._signin_thread.join(timeout=10)

    def start_keepalive(self) -> None:
        """Refresh the saved sign-in in the background so it outlasts Google's short cookie life."""

        def run() -> None:
            while True:
                try:
                    self.refresh_session()
                    self.verify_session()
                except Exception:
                    pass
                if self._keepalive_stop.wait(self.ROTATE_EVERY_SECONDS):
                    return

        if self._keepalive_thread is None:
            self._keepalive_thread = threading.Thread(target=run, name="kira-google-keepalive", daemon=True)
            self._keepalive_thread.start()

    def refresh_session(self, force: bool = False) -> bool:
        """Ask Google for fresh session cookies and save them; True when the saved sign-in changed.

        Runs at most once per ROTATE_EVERY_SECONDS unless forced. Any failure leaves the saved
        session as it was, so a later sign-in check reports the real state.
        """
        if self._client_factory is not None or not self.session_path.exists():
            return False  # an injected client (tests) never talks to Google
        with self._cookie_lock:
            now = time.monotonic()
            if not force and self._rotated_at is not None and now - self._rotated_at < self.ROTATE_EVERY_SECONDS:
                return False
            self._rotated_at = now
        try:
            raw, _ = self._load_session()
            session = self._cookie_session(raw)
            response = session.post(
                self.ROTATE_URL,
                headers={
                    "Content-Type": "application/json",
                    # Google answers 403 unless the call looks like it came from its own sign-in page.
                    "Origin": "https://accounts.google.com",
                    "User-Agent": self.BROWSER_USER_AGENT,
                },
                data='[000,"-0000000000000000000"]',
                timeout=15,
            )
        except Exception:
            return False
        if response.status_code == 401:
            self._session_expired = True
        if response.status_code != 200:
            return False
        return self._save_cookies(session.cookies)

    def verify_session(self) -> bool | None:
        """Load the Photos home page with the saved cookies; False when Google wants a new sign-in.

        None when there is nothing to check or Google could not be reached, leaving the last answer.
        """
        if self._client_factory is not None or not self.session_path.exists():
            return None
        try:
            raw, account_index = self._load_session()
            session = self._cookie_session(raw)
            base = f"/u/{account_index}/" if account_index else "/"
            response = session.get(f"https://photos.google.com{base}", timeout=30)
        except GooglePhotosWebError:
            self._session_expired = True
            return False
        except Exception:
            return None
        self._session_expired = 'data-id="_gd"' not in response.text
        return not self._session_expired

    def _cookie_session(self, raw: bytes) -> object:
        """A plain requests session carrying the saved cookies, with no page load."""
        self._dependency()
        requests = importlib.import_module("requests")
        with self._temporary_cookie_file(raw) as path:
            jar = MozillaCookieJar(str(path))
            jar.load(ignore_discard=True, ignore_expires=True)
        session = requests.Session()
        session.cookies.update(jar)
        return session

    def _save_cookies(self, cookies: object) -> bool:
        """Fold cookies Google set during a session back into the saved sign-in.

        Only Google cookies whose value changed are written, so ordinary requests leave the file
        alone. Returns True when something was saved.
        """
        with self._cookie_lock:
            try:
                raw, _ = self._load_session()
                wrapper = json.loads(self.session_path.read_text(encoding="utf-8"))
            except (GooglePhotosWebError, OSError, ValueError):
                return False
            with self._temporary_cookie_file(raw) as path:
                jar = MozillaCookieJar(str(path))
                jar.load(ignore_discard=True, ignore_expires=True)
                saved = {(c.domain, c.path, c.name): c.value for c in jar}
                changed = False
                for cookie in cookies:
                    domain = cookie.domain.casefold().lstrip(".")
                    if domain != "google.com" and not domain.endswith(".google.com"):
                        continue
                    if saved.get((cookie.domain, cookie.path, cookie.name)) != cookie.value:
                        jar.set_cookie(cookie)
                        changed = True
                if not changed:
                    return False
                jar.save(ignore_discard=True, ignore_expires=True)
                updated = path.read_bytes()
            protected = _windows_protect(updated) if wrapper.get("encrypted") else updated
            wrapper["data"] = base64.b64encode(protected).decode("ascii")
            self._write_session(wrapper)
            return True

    def _run_sign_in(self, cancelled: threading.Event) -> None:
        try:
            with GoogleSignInBrowser(self.root, cancelled) as browser:
                with self._signin_lock:
                    if cancelled.is_set():
                        return
                    self._signin_state = {"status": "waiting"}
                closed = browser.wait_until_closed(time.monotonic() + 900)
                if cancelled.is_set():
                    return
                if not closed:
                    raise GoogleSignInError("Sign-in timed out. Click Sign in to Photos web and try again.")
                with self._signin_lock:
                    self._signin_state = {"status": "verifying"}
                session = browser.session()
            if session is None:
                raise GoogleSignInError(
                    "The window closed before Google sign-in finished. Sign in to Google Photos, then close the window."
                )
            cookies, account_index = session
            raw = self._normalize_cookie_export(json.dumps(cookies).encode("utf-8"))
            try:
                wrapper = self._verified_session(raw, account_index)
            except GooglePhotosWebError as exc:
                raise GoogleSignInError("Signed in, but Google Photos did not accept the session. Try again.") from exc
            with self._signin_lock:
                if cancelled.is_set():
                    return
                self._write_session(wrapper)
                self._signin_state = {"status": "completed"}
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, GoogleSignInError)
                else "The Google sign-in window closed or could not be verified. Try again."
            )
            with self._signin_lock:
                if not cancelled.is_set():
                    self._signin_state = {"status": "failed", "error": message}

    def check(self) -> None:
        """Verify the stored cookie JSON can be decrypted and used."""
        if not self.session_path.exists():
            raise GooglePhotosWebError("Sign in to Google Photos to connect album and Archive automation.")
        with self._client():
            pass

    def _session_readable(self) -> bool:
        if not self.session_path.exists():
            return False
        try:
            self._load_session()
            return True
        except GooglePhotosWebError:
            return False

    def import_session(self, cookies_path: Path, account_index: int = 0) -> dict:
        self.cancel_sign_in()
        try:
            source = cookies_path.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise GooglePhotosWebError("Cookie file not found") from exc
        if not source.is_file():
            raise GooglePhotosWebError("Cookie path is not a file")
        if account_index < 0 or account_index > 99:
            raise GooglePhotosWebError("Google account index must be between 0 and 99")
        try:
            if source.stat().st_size > self.MAX_COOKIE_BYTES:
                raise GooglePhotosWebError("Cookie file is too large")
            raw = source.read_bytes()
        except GooglePhotosWebError:
            raise
        except OSError as exc:
            raise GooglePhotosWebError(f"Could not read cookie file: {exc}") from exc
        normalized = self._normalize_cookie_export(raw)
        if b"google.com" not in normalized.lower():
            raise GooglePhotosWebError("Cookie file does not contain a Google browser session")

        wrapper = self._verified_session(normalized, account_index)
        with self._signin_lock:
            self._write_session(wrapper)
        return self.status()

    def _verified_session(self, normalized: bytes, account_index: int) -> dict:
        account = ""
        client_factory, _ = self._dependency()
        with self._temporary_cookie_file(normalized) as temporary:
            client = None
            try:
                client = client_factory(temporary, account_index=account_index)
                account = str(getattr(client, "global_data", {}).get("oPEP7c", "")).strip()
            except IndexError as exc:
                raise GooglePhotosWebError("Google Photos cookie export is expired or not signed in") from exc
            except Exception as exc:
                raise GooglePhotosWebError(f"Google Photos web session could not be verified: {exc}") from exc
            finally:
                self._close_client(client)

        protected = _windows_protect(normalized) if os.name == "nt" else normalized
        return {
            "version": 1,
            "encrypted": os.name == "nt",
            "account": account or None,
            "account_index": account_index,
            "data": base64.b64encode(protected).decode("ascii"),
        }

    @staticmethod
    def _normalize_cookie_export(raw: bytes) -> bytes:
        stripped = raw.lstrip()
        if not stripped.startswith((b"{", b"[")):
            return raw

        try:
            parsed = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GooglePhotosWebError("Cookie JSON export is unreadable") from exc
        entries = parsed.get("cookies") if isinstance(parsed, dict) else parsed
        if not isinstance(entries, list):
            raise GooglePhotosWebError("Cookie JSON export must contain a cookies list")

        lines = ["# Netscape HTTP Cookie File", "# Converted privately by Kira; do not edit."]
        google_entries = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            domain = str(entry.get("domain") or entry.get("host") or "").strip()
            normalized_domain = domain.casefold().lstrip(".")
            if normalized_domain != "google.com" and not normalized_domain.endswith(".google.com"):
                continue
            name = str(entry.get("name") or "")
            value = str(entry.get("value") or "")
            path = str(entry.get("path") or "/")
            if not name:
                continue
            if any(character in field for field in (domain, name, value, path) for character in "\t\r\n"):
                raise GooglePhotosWebError("Cookie JSON export contains an unsupported field")
            host_only = bool(entry.get("hostOnly", not domain.startswith(".")))
            include_subdomains = "FALSE" if host_only else "TRUE"
            secure = "TRUE" if bool(entry.get("secure")) else "FALSE"
            expiration = 0
            if not bool(entry.get("session")) and entry.get("expirationDate") is not None:
                try:
                    expiration = max(0, int(float(entry["expirationDate"])))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise GooglePhotosWebError("Cookie JSON export has an invalid expiration") from exc
            stored_domain = f"#HttpOnly_{domain}" if bool(entry.get("httpOnly")) else domain
            lines.append("\t".join((stored_domain, include_subdomains, path, secure, str(expiration), name, value)))
            google_entries += 1
        if not google_entries:
            raise GooglePhotosWebError("Cookie JSON export contains no Google cookies")
        return ("\n".join(lines) + "\n").encode("utf-8")

    def disconnect(self) -> dict:
        with self._signin_lock:
            self.cancel_sign_in()
            self.session_path.unlink(missing_ok=True)
        return self.status()

    def list_albums(self) -> list[dict]:
        with self._client() as (client, payloads):
            albums = self._list_albums(client, payloads)
        return sorted(albums, key=lambda album: (album["title"].casefold(), album["media_key"]))

    def find_remote_matches(self, hashes: list[str]) -> list[dict]:
        requested = list(dict.fromkeys(str(value).strip() for value in hashes if str(value).strip()))
        if not requested:
            return []
        if len(requested) > self.MAX_HASHES:
            raise GooglePhotosWebError(f"Match at most {self.MAX_HASHES} local files at once")
        matches: dict[tuple[str, str], dict] = {}
        with self._client() as (client, payloads):
            for start in range(0, len(requested), self.HASH_BATCH_SIZE):
                batch = requested[start : start + self.HASH_BATCH_SIZE]
                try:
                    response = client.send_api_request(payloads.GetRemoteMatchesByHash(batch))
                    items = getattr(response, "data", None) or []
                except Exception as exc:
                    raise GooglePhotosWebError(f"Could not match local files in Google Photos: {exc}") from exc
                for item in items:
                    content_hash = str(getattr(item, "hash", "") or "")
                    media_key = str(getattr(item, "media_key", "") or "")
                    dedup_key = str(getattr(item, "dedup_key", "") or "")
                    if content_hash in batch and media_key and dedup_key:
                        matches[(content_hash, media_key)] = {"content_hash": content_hash, "media_key": media_key}
        return list(matches.values())

    def lookup_hashes(self, hashes: list[str]) -> dict[str, dict]:
        """Which exact files Google Photos holds: content hash -> media key and dedup key."""
        requested = list(dict.fromkeys(value for value in hashes if value))
        found: dict[str, dict] = {}
        if not requested:
            return found
        with self._client() as (client, payloads):
            for start in range(0, len(requested), self.HASH_BATCH_SIZE):
                batch = requested[start : start + self.HASH_BATCH_SIZE]
                try:
                    response = client.send_api_request(payloads.GetRemoteMatchesByHash(batch))
                    items = getattr(response, "data", None) or []
                except Exception as exc:
                    raise GooglePhotosWebError(f"Could not look up files in Google Photos: {exc}") from exc
                for item in items:
                    content_hash = str(getattr(item, "hash", "") or "")
                    media_key = str(getattr(item, "media_key", "") or "")
                    dedup_key = str(getattr(item, "dedup_key", "") or "")
                    if content_hash in batch and media_key and dedup_key:
                        found[content_hash] = {"media_key": media_key, "dedup_key": dedup_key}
        return found

    def find_by_name_and_time(self, wanted: list[dict]) -> dict[int, dict]:
        """Find library items by original filename and capture time (UTC epoch ms).

        Used when hashes cannot match, e.g. picker downloads that Google stripped of location.
        Returns ``{index into wanted: {media_key, dedup_key}}``; ambiguous entries are left out.
        """
        if not wanted:
            return {}
        day = 24 * 60 * 60 * 1000
        oldest = min(entry["time_ms"] for entry in wanted) - day
        candidates: dict[int, list[dict]] = {}
        with self._client() as (client, payloads):
            page_id, timestamp, seen_pages = None, None, set()
            while True:
                try:
                    page = client.send_api_request(
                        payloads.GetLibraryPageByTakenDate(
                            timestamp=timestamp, page_id=page_id, source="both", page_size=500
                        )
                    ).data
                except Exception as exc:
                    raise GooglePhotosWebError(f"Could not scan the Google Photos library: {exc}") from exc
                for item in page.items:
                    taken = self._epoch_ms(item.timestamp)
                    if not item.media_key or taken is None:
                        continue
                    for index, entry in enumerate(wanted):
                        if abs(taken - entry["time_ms"]) <= day:
                            candidates.setdefault(index, []).append(
                                {"media_key": str(item.media_key), "dedup_key": str(item.dedup_key or ""), "ms": taken}
                            )
                token = (str(page.next_page_id or ""), str(page.last_item_timestamp or ""))
                last = self._epoch_ms(page.last_item_timestamp)
                if not token[0] or token in seen_pages or (last is not None and last < oldest):
                    break
                seen_pages.add(token)
                page_id, timestamp = page.next_page_id, page.last_item_timestamp

            keys = list(dict.fromkeys(item["media_key"] for items in candidates.values() for item in items))
            names: dict[str, str] = {}
            for start in range(0, len(keys), self.INFO_BATCH_SIZE):
                try:
                    response = client.send_api_request(
                        payloads.GetBatchMediaInfo(keys[start : start + self.INFO_BATCH_SIZE])
                    )
                except Exception as exc:
                    raise GooglePhotosWebError(f"Could not read Google Photos filenames: {exc}") from exc
                for metadata in getattr(response, "data", None) or []:
                    names[str(getattr(metadata, "media_key", ""))] = str(getattr(metadata, "file_name", "") or "")

        found: dict[int, dict] = {}
        for index, items in candidates.items():
            name = wanted[index]["name"].casefold()
            same_name = [item for item in items if names.get(item["media_key"], "").casefold() == name]
            close = [item for item in same_name if abs(item["ms"] - wanted[index]["time_ms"]) <= 2000]
            chosen = close if len(close) == 1 else same_name if len(same_name) == 1 else []
            if chosen and chosen[0]["dedup_key"]:
                found[index] = {"media_key": chosen[0]["media_key"], "dedup_key": chosen[0]["dedup_key"]}
        return found

    @staticmethod
    def _epoch_ms(value: object) -> int | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number > 100_000_000_000_000:
            return int(number / 1000)
        if number > 100_000_000_000:
            return int(number)
        return int(number * 1000)

    def archive(self, items: list[dict]) -> list[str]:
        """Hide items from the Photos timeline (nothing is deleted). Returns confirmed media keys."""
        items = [item for item in items if item.get("media_key") and item.get("dedup_key")][: self.MAX_MEDIA_ITEMS]
        if not items:
            return []
        with self._client() as (client, payloads):
            try:
                client.send_api_request(payloads.SetArchive([item["dedup_key"] for item in items]))
            except Exception as exc:
                raise GooglePhotosWebError(f"Google Photos Archive failed: {exc}") from exc
            keys = [item["media_key"] for item in items]
            self._verify_archive(client, payloads, keys)
            return keys

    def find_visual_matches(self, paths: list[Path]) -> tuple[list[dict], list[dict]]:
        """Match photos by decoded visual content, independent of names and encoding."""
        errors: list[dict] = []
        local_groups: dict[tuple, dict] = {}
        for path in paths:
            try:
                signature = visual.image_signature(path)
                key = (signature["width"], signature["height"], signature["pixels"])
                group = local_groups.setdefault(key, {"signature": signature, "paths": [], "name_keys": set()})
                group["paths"].append(str(path))
                group["name_keys"].add(match_key(path.name))
            except Exception as exc:
                errors.append(
                    {
                        "filename": path.name,
                        "local_files": [str(path)],
                        "error": f"Could not decode local image for content matching: {exc}",
                    }
                )

        if not local_groups:
            return [], errors

        local_groups_list = list(local_groups.values())

        matches: dict[str, dict] = {}
        with self._client() as (client, payloads):
            remote_candidates: list[tuple[dict, list[dict]]] = []
            for remote in self._list_library(client, payloads):
                if remote["video"] or remote["partial"] or not remote["owned"]:
                    continue
                candidates = [
                    group
                    for group in local_groups_list
                    if visual.dimensions_compatible(
                        (group["signature"]["width"], group["signature"]["height"]), (remote["width"], remote["height"])
                    )
                ]
                if not candidates:
                    continue
                remote_candidates.append((remote, candidates))

            remote_candidates = self._resolve_visual_downloads(client, payloads, remote_candidates)

            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
                futures = {
                    executor.submit(self._remote_image_signature, client, remote): (remote, candidates)
                    for remote, candidates in remote_candidates
                }
                for future in concurrent.futures.as_completed(futures):
                    remote, candidates = futures[future]
                    try:
                        signature = future.result()
                    except Exception as exc:
                        errors.append(
                            {
                                "filename": "Google Photos library item",
                                "local_files": [],
                                "error": f"Could not inspect one Google Photos thumbnail: {exc}",
                            }
                        )
                        continue
                    self._record_visual_match(matches, remote, candidates, signature)
        return list(matches.values()), errors

    def _remote_image_signature(self, client: object, remote: dict) -> dict:
        try:
            response = client.session.get(remote["download_url"], timeout=60)
            if response.status_code < 200 or response.status_code >= 300:
                raise GooglePhotosWebError(f"original download returned HTTP {response.status_code}")
            return visual.image_signature(response.content)
        except GooglePhotosWebError:
            raise
        except Exception as exc:
            raise GooglePhotosWebError(f"original download failed ({type(exc).__name__})") from exc

    def _resolve_visual_downloads(
        self, client: object, payloads: object, candidates: list[tuple[dict, list[dict]]]
    ) -> list[tuple[dict, list[dict]]]:
        by_media = {remote["media_key"]: (remote, groups) for remote, groups in candidates}
        narrowed: dict[str, tuple[dict, list[dict]]] = {}
        keys = list(by_media)
        for start in range(0, len(keys), 100):
            batch = keys[start : start + 100]
            try:
                response = client.send_api_request(payloads.GetBatchMediaInfo(batch))
                metadata_items = getattr(response, "data", None) or []
            except Exception as exc:
                raise GooglePhotosWebError(
                    f"Could not read Google Photos filenames for content matching: {exc}"
                ) from exc
            for metadata in metadata_items:
                media_key = str(getattr(metadata, "media_key", "") or "")
                pair = by_media.get(media_key)
                if pair is None:
                    continue
                remote, groups = pair
                remote_name = match_key(str(getattr(metadata, "file_name", "") or ""))
                remote_capture = self._remote_capture_time(
                    getattr(metadata, "timestamp", None), getattr(metadata, "timezone_offset", None)
                )
                compatible = [
                    group
                    for group in groups
                    if remote_name in group["name_keys"]
                    or (remote_capture and remote_capture == group["signature"].get("capture_time"))
                ]
                if not compatible and len(groups) <= self.VISUAL_NARROW_FALLBACK_LIMIT:
                    compatible = list(groups)
                if compatible:
                    narrowed[media_key] = (remote, compatible)

        narrowed_keys = list(narrowed)
        resolved: list[tuple[dict, list[dict]]] = []
        for start in range(0, len(narrowed_keys), 100):
            requests = [(key, payloads.GetItemInfo(key)) for key in narrowed_keys[start : start + 100]]
            try:
                responses = client.send_api_request([payload for _, payload in requests])
            except Exception as exc:
                raise GooglePhotosWebError(
                    f"Could not prepare Google Photos originals for content matching: {exc}"
                ) from exc
            by_id = {response.response_id: response for response in responses}
            for media_key, payload in requests:
                data = getattr(by_id.get(payload.payload_id), "data", None)
                download_url = str(getattr(data, "download_original_url", "") or "")
                if not download_url:
                    continue
                remote, groups = narrowed[media_key]
                remote["download_url"] = download_url
                resolved.append((remote, groups))
        return resolved

    @staticmethod
    def _remote_capture_time(timestamp: object, timezone_offset: object) -> str | None:
        try:
            value = float(timestamp)
            if value > 100_000_000_000_000:
                seconds = value / 1_000_000
            elif value > 100_000_000_000:
                seconds = value / 1_000
            else:
                seconds = value
            offset = int(timezone_offset or 0)
            if abs(offset) <= 24 * 60:
                offset *= 60
            captured = datetime.fromtimestamp(seconds, UTC) + timedelta(seconds=offset)
            return captured.strftime("%Y:%m:%d %H:%M:%S")
        except (OSError, OverflowError, TypeError, ValueError):
            return None

    def _record_visual_match(
        self, matches: dict[str, dict], remote: dict, candidates: list[dict], signature: dict
    ) -> None:
        exact_rows: list[tuple[dict, dict]] = []
        variant_rows: list[tuple[dict, dict]] = []
        for candidate in candidates:
            metrics = visual.similarity_metrics(signature, candidate["signature"])
            tier = visual.match_tier(metrics)
            if tier == "reencoded":
                exact_rows.append((metrics, candidate))
            elif tier == "edited":
                variant_rows.append((metrics, candidate))

        if exact_rows:
            ranked = sorted(exact_rows, key=lambda value: (value[0]["color"], value[0]["difference_hash"]))
            best_metrics, best = ranked[0]
            if len(ranked) > 1:
                next_metrics = ranked[1][0]
                if (
                    next_metrics["color"] - best_metrics["color"] < 1.0
                    and next_metrics["difference_hash"] - best_metrics["difference_hash"] < 3
                ):
                    return
            matches[remote["media_key"]] = {
                "media_key": remote["media_key"],
                "local_files": list(best["paths"]),
                "visual_match_tier": "reencoded",
                "visual_color_distance": round(best_metrics["color"], 3),
                "visual_hash_distance": best_metrics["difference_hash"],
            }
            return

        if not variant_rows:
            return
        ranked = sorted(variant_rows, key=lambda value: (-value[0]["palette"], value[0]["perceptual_hash"]))
        best_metrics, best = ranked[0]
        if len(ranked) > 1:
            next_metrics = ranked[1][0]
            if (
                next_metrics["palette"] - best_metrics["palette"] < 0.01
                and abs(next_metrics["perceptual_hash"] - best_metrics["perceptual_hash"]) < 3
            ):
                return
        matches[remote["media_key"]] = {
            "media_key": remote["media_key"],
            "local_files": list(best["paths"]),
            "visual_match_tier": "edited",
            "visual_palette_similarity": round(best_metrics["palette"], 4),
            "visual_phash_distance": best_metrics["perceptual_hash"],
        }

    def _list_library(self, client: object, payloads: object) -> list[dict]:
        items: dict[str, dict] = {}
        page_id = None
        timestamp = None
        seen_pages: set[tuple[str, str]] = set()
        while True:
            try:
                response = client.send_api_request(
                    payloads.GetLibraryPageByTakenDate(
                        timestamp=timestamp, page_id=page_id, source="both", page_size=500
                    )
                )
                page = response.data
            except Exception as exc:
                raise GooglePhotosWebError(f"Could not scan the Google Photos library: {exc}") from exc
            for item in page.items:
                media_key = str(item.media_key or "")
                thumbnail_url = str(item.thumbnail_url or "")
                if media_key and thumbnail_url:
                    items[media_key] = {
                        "media_key": media_key,
                        "dedup_key": str(item.dedup_key or ""),
                        "thumbnail_url": thumbnail_url,
                        "width": int(item.res_width or 0),
                        "height": int(item.res_height or 0),
                        "video": item.video_duration is not None,
                        "partial": bool(item.is_partial_upload),
                        "owned": bool(item.is_owned),
                        "timestamp": getattr(item, "timestamp", None),
                        "timezone_offset": getattr(item, "timezone_offset", None),
                    }
            next_page = str(page.next_page_id or "")
            next_timestamp = str(page.last_item_timestamp or "")
            token = (next_page, next_timestamp)
            if not next_page or token in seen_pages:
                return list(items.values())
            seen_pages.add(token)
            page_id = next_page
            timestamp = page.last_item_timestamp

    def library_items(self, progress=None, cancelled: threading.Event | None = None) -> list[dict]:
        """Every item in the Google Photos library, newest first. Metadata only; nothing is downloaded."""
        items: dict[str, dict] = {}
        page_id, timestamp, seen_pages = None, None, set()
        with self._client() as (client, payloads):
            while not (cancelled and cancelled.is_set()):
                try:
                    page = client.send_api_request(
                        payloads.GetLibraryPageByTakenDate(
                            timestamp=timestamp, page_id=page_id, source="both", page_size=500
                        )
                    ).data
                except Exception as exc:
                    raise GooglePhotosWebError(f"Could not scan the Google Photos library: {exc}") from exc
                for item in page.items:
                    media_key = str(item.media_key or "")
                    if not media_key:
                        continue
                    items[media_key] = {
                        "media_key": media_key,
                        "dedup_key": str(item.dedup_key or ""),
                        "taken_ms": self._epoch_ms(item.timestamp),
                        "local_time": self._remote_capture_time(item.timestamp, item.timezone_offset),
                        "thumbnail_url": str(item.thumbnail_url or ""),
                        "width": int(item.res_width or 0),
                        "height": int(item.res_height or 0),
                        "video": item.video_duration is not None,
                        "archived": bool(item.is_archived),
                        "owned": bool(item.is_owned),
                    }
                if progress:
                    progress(len(items))
                token = (str(page.next_page_id or ""), str(page.last_item_timestamp or ""))
                if not token[0] or token in seen_pages:
                    break
                seen_pages.add(token)
                page_id, timestamp = page.next_page_id, page.last_item_timestamp
        return list(items.values())

    def media_details(
        self, media_keys: list[str], progress=None, cancelled: threading.Event | None = None
    ) -> dict[str, dict]:
        """Original file name, byte size and storage quality of each item, a batch per request."""
        details: dict[str, dict] = {}
        with self._client() as (client, payloads):
            for start in range(0, len(media_keys), self.INFO_BATCH_SIZE):
                if cancelled and cancelled.is_set():
                    break
                batch = media_keys[start : start + self.INFO_BATCH_SIZE]
                try:
                    response = client.send_api_request(payloads.GetBatchMediaInfo(batch))
                except Exception as exc:
                    raise GooglePhotosWebError(f"Could not read Google Photos file details: {exc}") from exc
                for metadata in getattr(response, "data", None) or []:
                    key = str(getattr(metadata, "media_key", "") or "")
                    if key:
                        details[key] = {
                            "file_name": str(getattr(metadata, "file_name", "") or ""),
                            "size": int(getattr(metadata, "size", 0) or 0),
                            "original_quality": bool(getattr(metadata, "is_original_quality", False)),
                        }
                if progress:
                    progress(min(start + len(batch), len(media_keys)), len(media_keys))
        return details

    def thumbnail(self, url: str, size: int) -> bytes:
        """One of Google's own thumbnails. Google refuses them without the signed-in session."""
        crop = "-c" if size <= 480 else ""
        self.refresh_session()
        try:
            response = self._thumbnails().get(f"{url}=w{size}-h{size}{crop}", timeout=30)
        except Exception as exc:
            raise GooglePhotosWebError(f"Could not load the Google Photos thumbnail: {exc}") from exc
        if response.status_code in {401, 403}:
            self._session_expired = True
            raise GooglePhotosWebError("Google Photos web session expired; click Sign in to Photos web")
        if response.status_code != 200 or not response.headers.get("Content-Type", "").startswith("image/"):
            raise GooglePhotosWebError(f"Google Photos thumbnail returned HTTP {response.status_code}")
        return response.content

    def _thumbnails(self) -> object:
        """A plain cookie session for thumbnails, rebuilt whenever the saved sign-in changes.

        The full web client loads the Photos home page on every start, far too slow for a grid
        of thumbnails; image links need only the cookies.
        """
        with self._thumbnail_lock:
            try:
                stamp = self.session_path.stat().st_mtime_ns
            except OSError as exc:
                raise GooglePhotosWebError("Sign in to Photos web to see Google Photos thumbnails") from exc
            if self._thumbnail_session is None or self._thumbnail_session[0] != stamp:
                raw, _ = self._load_session()
                self._thumbnail_session = (stamp, self._cookie_session(raw))
            return self._thumbnail_session[1]

    def ensure_album(self, album_title: str) -> dict:
        """Resolve one exact album once, creating it when it does not exist."""
        title = self._validated_album_title(album_title)
        with self._client() as (client, payloads):
            return self._ensure_album(client, payloads, title)

    def organize(
        self, media_keys: list[str], album_title: str, archive: bool = False, resolved_album: dict | None = None
    ) -> dict:
        keys = list(dict.fromkeys(str(key).strip() for key in media_keys if str(key).strip()))
        title = self._validated_album_title(album_title)
        if not keys:
            raise GooglePhotosWebError("At least one Google media ID is required")
        if len(keys) > self.MAX_MEDIA_ITEMS:
            raise GooglePhotosWebError(f"Organize at most {self.MAX_MEDIA_ITEMS} items at once")

        with self._client() as (client, payloads):
            album = dict(resolved_album) if resolved_album else self._ensure_album(client, payloads, title)
            return self._organize_with_client(client, payloads, keys, title, archive, album)

    def _organize_with_client(
        self, client: object, payloads: object, keys: list[str], title: str, archive: bool, album: dict
    ) -> dict:
        if not str(album.get("media_key", "")).strip():
            raise GooglePhotosWebError("Google Photos album ID is missing")
        if str(album.get("title", "")).strip().casefold() != title.casefold():
            raise GooglePhotosWebError("Resolved Google Photos album does not match the requested name")
        info = self._item_info(client, payloads, keys)

        try:
            if album.get("shared"):
                client.send_api_request(payloads.AddItemsToExistingSharedAlbum(keys, album["media_key"]))
            else:
                client.send_api_request(payloads.AddItemsToExistingAlbum(keys, album["media_key"]))
        except Exception as exc:
            raise GooglePhotosWebError(f"Could not add media to the Google Photos album: {exc}") from exc

        verified_items = self._verify_album(client, payloads, keys, album["media_key"])
        if archive:
            try:
                client.send_api_request(payloads.SetArchive([info[key]["dedup_key"] for key in keys]))
            except Exception as exc:
                raise GooglePhotosWebError(f"Media was added to the album, but Archive failed: {exc}") from exc
            self._verify_archive(client, payloads, keys)

        return {
            "album": {"title": title, "media_key": album["media_key"]},
            "archived": archive,
            "items": [
                {
                    "google_media_key": key,
                    "google_dedup_key": info[key]["dedup_key"],
                    "album_media_key": verified_items[index]["album_media_key"],
                    "album_added": True,
                    "archived": archive,
                    "status": "complete",
                }
                for index, key in enumerate(keys)
            ],
        }

    @staticmethod
    def _validated_album_title(album_title: str) -> str:
        title = str(album_title).strip()
        if not title:
            raise GooglePhotosWebError("Album name is required")
        if len(title) > 500:
            raise GooglePhotosWebError("Album name must be 500 characters or fewer")
        return title

    def _ensure_album(self, client: object, payloads: object, title: str) -> dict:
        exact = [
            album for album in self._list_albums(client, payloads) if album["title"].casefold() == title.casefold()
        ]
        if exact:
            return exact[0]

        try:
            client.send_api_request(payloads.CreateAlbum(title))
        except Exception as exc:
            raise GooglePhotosWebError(f"Could not create the Google Photos album: {exc}") from exc

        for attempt in range(5):
            exact = [
                album for album in self._list_albums(client, payloads) if album["title"].casefold() == title.casefold()
            ]
            if exact:
                return exact[0]
            if attempt < 4:
                time.sleep(0.25 * (2**attempt))
        raise GooglePhotosWebError("Google created the album but did not return its ID yet; wait a moment and retry")

    def _dependency(self) -> tuple[object, object]:
        if self._client_factory is not None and self._payloads_module is not None:
            return self._client_factory, self._payloads_module
        try:
            gpwc = importlib.import_module("gpwc")
        except (ImportError, OSError) as exc:
            raise GooglePhotosWebError("Google Photos web client is not installed") from exc
        return gpwc.Client, gpwc.payloads

    def _session_metadata(self) -> dict:
        if not self.session_path.exists():
            return {}
        try:
            wrapper = json.loads(self.session_path.read_text(encoding="utf-8"))
            return {"account": wrapper.get("account"), "account_index": int(wrapper.get("account_index", 0))}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _write_session(self, wrapper: dict) -> None:
        try:
            atomic_json_write(self.session_path, wrapper)
            self._session_expired = False
            if os.name != "nt":
                try:
                    os.chmod(self.session_path, 0o600)
                except OSError:
                    pass
        except OSError as exc:
            raise GooglePhotosWebError(f"Could not store Google Photos web session: {exc}") from exc

    def _load_session(self) -> tuple[bytes, int]:
        if not self.session_path.exists():
            raise GooglePhotosWebError("Sign in to Google Photos first")
        try:
            wrapper = json.loads(self.session_path.read_text(encoding="utf-8"))
            if isinstance(wrapper, dict) and "data" not in wrapper and ("cookies" in wrapper or "url" in wrapper):
                raise GooglePhotosWebError(
                    "google-web-session.json is a cookie export, not an imported Kira session. "
                    "Paste that file's path into Cookie export path and click Import session."
                )
            raw = base64.b64decode(wrapper["data"])
            if wrapper.get("encrypted"):
                if os.name != "nt":
                    raise GooglePhotosWebError("Google web session belongs to another desktop")
                raw = _windows_unprotect(raw)
            return raw, int(wrapper.get("account_index", 0))
        except GooglePhotosWebError:
            raise
        except Exception as exc:
            raise GooglePhotosWebError("Stored Google Photos web session is unreadable; import it again") from exc

    @contextmanager
    def _temporary_cookie_file(self, raw: bytes) -> Iterator[Path]:
        handle = tempfile.NamedTemporaryFile(prefix=".kira-google-web-", suffix=".txt", dir=self.root, delete=False)
        path = Path(handle.name)
        try:
            with handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            yield path
        finally:
            path.unlink(missing_ok=True)

    @contextmanager
    def _client(self) -> Iterator[tuple[object, object]]:
        self.refresh_session()
        raw, account_index = self._load_session()
        client_factory, payloads = self._dependency()
        with self._temporary_cookie_file(raw) as cookies_path:
            client = None
            try:
                client = client_factory(cookies_path, account_index=account_index)
                self._session_expired = False
                yield client, payloads
                self._save_cookies(getattr(getattr(client, "session", None), "cookies", []))
            except GooglePhotosWebError:
                raise
            except IndexError as exc:
                self._session_expired = True
                raise GooglePhotosWebError("Google Photos web session expired; click Sign in to Photos web") from exc
            except Exception as exc:
                raise GooglePhotosWebError(f"Google Photos web session failed: {exc}") from exc
            finally:
                self._close_client(client)

    @staticmethod
    def _close_client(client: object | None) -> None:
        session = getattr(client, "session", None)
        close = getattr(session, "close", None)
        if callable(close):
            close()

    def _list_albums(self, client: object, payloads: object) -> list[dict]:
        albums: list[dict] = []
        page_id = None
        seen_pages: set[str] = set()
        while True:
            try:
                response = client.send_api_request(payloads.GetAlbumsPage(page_id=page_id, page_size=100))
                page = response.data
            except Exception as exc:
                raise GooglePhotosWebError(f"Could not list Google Photos albums: {exc}") from exc
            for album in page.items:
                title = str(album.title or "").strip()
                if title and album.media_key:
                    albums.append(
                        {
                            "media_key": str(album.media_key),
                            "title": title,
                            "item_count": int(album.item_count or 0),
                            "shared": bool(album.is_shared),
                        }
                    )
            next_page = str(page.next_page_id or "")
            if not next_page or next_page in seen_pages:
                return albums
            seen_pages.add(next_page)
            page_id = next_page

    def _item_info(self, client: object, payloads: object, media_keys: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for start in range(0, len(media_keys), self.INFO_BATCH_SIZE):
            batch = media_keys[start : start + self.INFO_BATCH_SIZE]
            requests = [(key, payloads.GetItemInfo(key)) for key in batch]
            responses = client.send_api_request([payload for _, payload in requests])
            by_id = {response.response_id: response for response in responses}
            for key, payload in requests:
                data = getattr(by_id.get(payload.payload_id), "data", None)
                dedup_key = str(getattr(data, "dedup_key", "") or "")
                if not dedup_key:
                    raise GooglePhotosWebError(f"Google media ID is unavailable: {key}")
                result[key] = {"dedup_key": dedup_key}
        return result

    def _verify_album(
        self, client: object, payloads: object, media_keys: list[str], album_media_key: str
    ) -> list[dict]:
        remaining = list(media_keys)
        for attempt in range(self.VERIFY_ATTEMPTS):
            unconfirmed: list[str] = []
            for start in range(0, len(remaining), self.INFO_BATCH_SIZE):
                batch = remaining[start : start + self.INFO_BATCH_SIZE]
                requests = [(key, payloads.GetItemInfoExt(key)) for key in batch]
                responses = client.send_api_request([payload for _, payload in requests])
                by_id = {response.response_id: response for response in responses}
                for key, payload in requests:
                    data = getattr(by_id.get(payload.payload_id), "data", None)
                    if not any(
                        str(getattr(album, "media_key", "")) == album_media_key
                        for album in getattr(data, "albums", []) or []
                    ):
                        unconfirmed.append(key)
            if not unconfirmed:
                return [{"album_media_key": album_media_key} for _key in media_keys]
            remaining = unconfirmed
            if attempt < self.VERIFY_ATTEMPTS - 1:
                time.sleep(self.VERIFY_INTERVAL_SECONDS)
        raise GooglePhotosWebError(f"Google did not confirm album membership: {remaining[0]}")

    def _verify_archive(self, client: object, payloads: object, media_keys: list[str]) -> None:
        remaining = list(media_keys)
        for attempt in range(self.VERIFY_ATTEMPTS):
            unconfirmed: list[str] = []
            for start in range(0, len(remaining), self.INFO_BATCH_SIZE):
                batch = remaining[start : start + self.INFO_BATCH_SIZE]
                requests = [(key, payloads.GetItemInfo(key)) for key in batch]
                responses = client.send_api_request([payload for _, payload in requests])
                by_id = {response.response_id: response for response in responses}
                for key, payload in requests:
                    data = getattr(by_id.get(payload.payload_id), "data", None)
                    if not bool(getattr(data, "is_archived", False)):
                        unconfirmed.append(key)
            if not unconfirmed:
                return
            remaining = unconfirmed
            if attempt < self.VERIFY_ATTEMPTS - 1:
                time.sleep(self.VERIFY_INTERVAL_SECONDS)
        raise GooglePhotosWebError(f"Google did not confirm Archive: {remaining[0]}")
