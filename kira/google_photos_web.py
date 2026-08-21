from __future__ import annotations

import base64
import concurrent.futures
import importlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterator

from .google_photos import GooglePhotosError, _name_key, _windows_protect, _windows_unprotect

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None
    ImageOps = None


class GooglePhotosWebError(GooglePhotosError):
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
    VISUAL_SIZE = 32
    VISUAL_COLOR_THRESHOLD = 8.0
    VISUAL_HASH_THRESHOLD = 12

    def __init__(
        self,
        data_root: Path,
        client_factory: object | None = None,
        payloads_module: object | None = None,
    ) -> None:
        self.root = data_root.resolve()
        self.session_path = self.root / "google-web-session.json"
        self._client_factory = client_factory
        self._payloads_module = payloads_module

    def status(self) -> dict:
        metadata = self._session_metadata()
        try:
            self._dependency()
            available = True
        except GooglePhotosWebError:
            available = False
        return {
            "available": available,
            "connected": self.session_path.exists(),
            "account": metadata.get("account"),
            "account_index": metadata.get("account_index", 0),
            "session_path": str(self.session_path),
        }

    def import_session(self, cookies_path: Path, account_index: int = 0) -> dict:
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

        account = ""
        client_factory, _ = self._dependency()
        with self._temporary_cookie_file(normalized) as temporary:
            client = None
            try:
                client = client_factory(temporary, account_index=account_index)
                account = str(getattr(client, "global_data", {}).get("oPEP7c", "")).strip()
            except IndexError as exc:
                raise GooglePhotosWebError(
                    "Google Photos cookie export is expired or not signed in"
                ) from exc
            except Exception as exc:
                raise GooglePhotosWebError(f"Google Photos web session could not be verified: {exc}") from exc
            finally:
                self._close_client(client)

        protected = _windows_protect(normalized) if os.name == "nt" else normalized
        wrapper = {
            "version": 1,
            "encrypted": os.name == "nt",
            "account": account or None,
            "account_index": account_index,
            "data": base64.b64encode(protected).decode("ascii"),
        }
        self._write_session(wrapper)
        return self.status()

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
            lines.append(
                "\t".join(
                    (stored_domain, include_subdomains, path, secure, str(expiration), name, value)
                )
            )
            google_entries += 1
        if not google_entries:
            raise GooglePhotosWebError("Cookie JSON export contains no Google cookies")
        return ("\n".join(lines) + "\n").encode("utf-8")

    def disconnect(self) -> dict:
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
                        matches[(content_hash, media_key)] = {
                            "content_hash": content_hash,
                            "media_key": media_key,
                        }
        return list(matches.values())

    def find_visual_matches(self, paths: list[Path]) -> tuple[list[dict], list[dict]]:
        """Match photos by decoded visual content, independent of names and encoding."""
        if Image is None or ImageOps is None:
            raise GooglePhotosWebError("Pillow is required for visual photo matching")

        errors: list[dict] = []
        local_groups: dict[tuple, dict] = {}
        for path in paths:
            try:
                signature = self._image_signature(path)
                key = (signature["width"], signature["height"], signature["pixels"])
                group = local_groups.setdefault(
                    key,
                    {
                        "signature": signature,
                        "paths": [],
                        "name_keys": set(),
                    },
                )
                group["paths"].append(str(path))
                group["name_keys"].add(_name_key(path.name))
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

        groups_by_dimensions: dict[tuple[int, int], list[dict]] = {}
        for group in local_groups.values():
            signature = group["signature"]
            dimensions = (signature["width"], signature["height"])
            groups_by_dimensions.setdefault(dimensions, []).append(group)

        matches: dict[str, dict] = {}
        with self._client() as (client, payloads):
            remote_candidates: list[tuple[dict, list[dict]]] = []
            for remote in self._list_library(client, payloads):
                if remote["video"] or remote["partial"] or not remote["owned"]:
                    continue
                dimensions = (remote["width"], remote["height"])
                candidates = groups_by_dimensions.get(dimensions)
                if not candidates:
                    candidates = groups_by_dimensions.get((dimensions[1], dimensions[0]))
                if not candidates:
                    continue
                remote_candidates.append((remote, candidates))

            remote_candidates = self._resolve_visual_downloads(
                client, payloads, remote_candidates
            )

            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
                futures = {
                    executor.submit(self._remote_image_signature, client, remote): (
                        remote,
                        candidates,
                    )
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
            return self._image_signature(response.content)
        except GooglePhotosWebError:
            raise
        except Exception as exc:
            raise GooglePhotosWebError(
                f"original download failed ({type(exc).__name__})"
            ) from exc

    def _resolve_visual_downloads(
        self,
        client: object,
        payloads: object,
        candidates: list[tuple[dict, list[dict]]],
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
                remote_name = _name_key(str(getattr(metadata, "file_name", "") or ""))
                remote_capture = self._remote_capture_time(
                    getattr(metadata, "timestamp", None),
                    getattr(metadata, "timezone_offset", None),
                )
                compatible = [
                    group
                    for group in groups
                    if remote_name in group["name_keys"]
                    or (
                        remote_capture
                        and remote_capture == group["signature"].get("capture_time")
                    )
                ]
                if compatible:
                    narrowed[media_key] = (remote, compatible)

        narrowed_keys = list(narrowed)
        resolved: list[tuple[dict, list[dict]]] = []
        for start in range(0, len(narrowed_keys), 100):
            requests = [
                (key, payloads.GetItemInfo(key))
                for key in narrowed_keys[start : start + 100]
            ]
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
            captured = datetime.fromtimestamp(seconds, timezone.utc) + timedelta(seconds=offset)
            return captured.strftime("%Y:%m:%d %H:%M:%S")
        except (OSError, OverflowError, TypeError, ValueError):
            return None

    def _record_visual_match(
        self, matches: dict[str, dict], remote: dict, candidates: list[dict], signature: dict
    ) -> None:
        ranked = sorted(
            (
                (
                    self._signature_distance(signature, candidate["signature"]),
                    candidate,
                )
                for candidate in candidates
            ),
            key=lambda value: (value[0][0], value[0][1]),
        )
        (color_distance, hash_distance), best = ranked[0]
        if (
            color_distance > self.VISUAL_COLOR_THRESHOLD
            or hash_distance > self.VISUAL_HASH_THRESHOLD
        ):
            return
        if len(ranked) > 1:
            next_color, next_hash = ranked[1][0]
            if next_color - color_distance < 1.0 and next_hash - hash_distance < 3:
                return
        matches[remote["media_key"]] = {
            "media_key": remote["media_key"],
            "local_files": list(best["paths"]),
            "visual_color_distance": round(color_distance, 3),
            "visual_hash_distance": hash_distance,
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
                        timestamp=timestamp,
                        page_id=page_id,
                        source="both",
                        page_size=500,
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

    @classmethod
    def _image_signature(cls, source: Path | bytes) -> dict:
        stream = BytesIO(source) if isinstance(source, bytes) else source
        with Image.open(stream) as opened:
            width, height = opened.size
            try:
                orientation = int(opened.getexif().get(274, 1))
            except (AttributeError, TypeError, ValueError):
                orientation = 1
            if orientation in {5, 6, 7, 8}:
                width, height = height, width
            capture_time = None
            try:
                capture_time = str(opened.getexif().get(36867) or "").strip() or None
            except (AttributeError, TypeError, ValueError):
                pass
            opened.draft("RGB", (256, 256))
            image = ImageOps.exif_transpose(opened)
            rgb = image.convert("RGB").resize(
                (cls.VISUAL_SIZE, cls.VISUAL_SIZE), Image.Resampling.LANCZOS
            )
            grayscale = image.convert("L").resize(
                (17, 16), Image.Resampling.LANCZOS
            )
            grayscale_bytes = grayscale.tobytes()
            difference_hash = 0
            for row in range(16):
                offset = row * 17
                for column in range(16):
                    difference_hash = (difference_hash << 1) | (
                        grayscale_bytes[offset + column]
                        > grayscale_bytes[offset + column + 1]
                    )
            return {
                "width": width,
                "height": height,
                "pixels": rgb.tobytes(),
                "difference_hash": difference_hash,
                "capture_time": capture_time,
            }

    @staticmethod
    def _signature_distance(left: dict, right: dict) -> tuple[float, int]:
        color = sum(
            abs(first - second) for first, second in zip(left["pixels"], right["pixels"])
        ) / len(left["pixels"])
        perceptual = (left["difference_hash"] ^ right["difference_hash"]).bit_count()
        return color, perceptual

    def ensure_album(self, album_title: str) -> dict:
        """Resolve one exact album once, creating it when it does not exist."""
        title = self._validated_album_title(album_title)
        with self._client() as (client, payloads):
            return self._ensure_album(client, payloads, title)

    def organize(
        self,
        media_keys: list[str],
        album_title: str,
        archive: bool = False,
        resolved_album: dict | None = None,
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
        self,
        client: object,
        payloads: object,
        keys: list[str],
        title: str,
        archive: bool,
        album: dict,
    ) -> dict:
        if not str(album.get("media_key", "")).strip():
            raise GooglePhotosWebError("Google Photos album ID is missing")
        if str(album.get("title", "")).strip().casefold() != title.casefold():
            raise GooglePhotosWebError("Resolved Google Photos album does not match the requested name")
        info = self._item_info(client, payloads, keys)

        try:
            if album.get("shared"):
                client.send_api_request(
                    payloads.AddItemsToExistingSharedAlbum(keys, album["media_key"])
                )
            else:
                client.send_api_request(payloads.AddItemsToExistingAlbum(keys, album["media_key"]))
        except Exception as exc:
            raise GooglePhotosWebError(f"Could not add media to the Google Photos album: {exc}") from exc

        verified_items = self._verify_album(client, payloads, keys, album["media_key"])
        if archive:
            try:
                client.send_api_request(payloads.SetArchive([info[key]["dedup_key"] for key in keys]))
            except Exception as exc:
                raise GooglePhotosWebError(
                    f"Media was added to the album, but Archive failed: {exc}"
                ) from exc
            self._verify_archive(client, payloads, keys)

        return {
            "album": {
                "title": title,
                "media_key": album["media_key"],
            },
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
            album
            for album in self._list_albums(client, payloads)
            if album["title"].casefold() == title.casefold()
        ]
        if exact:
            return exact[0]

        try:
            client.send_api_request(payloads.CreateAlbum(title))
        except Exception as exc:
            raise GooglePhotosWebError(f"Could not create the Google Photos album: {exc}") from exc

        for attempt in range(5):
            exact = [
                album
                for album in self._list_albums(client, payloads)
                if album["title"].casefold() == title.casefold()
            ]
            if exact:
                return exact[0]
            if attempt < 4:
                time.sleep(0.25 * (2**attempt))
        raise GooglePhotosWebError(
            "Google created the album but did not return its ID yet; wait a moment and retry"
        )

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
            return {
                "account": wrapper.get("account"),
                "account_index": int(wrapper.get("account_index", 0)),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _write_session(self, wrapper: dict) -> None:
        temp = self.session_path.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps(wrapper), encoding="utf-8")
            os.replace(temp, self.session_path)
            if os.name != "nt":
                try:
                    os.chmod(self.session_path, 0o600)
                except OSError:
                    pass
        except OSError as exc:
            raise GooglePhotosWebError(f"Could not store Google Photos web session: {exc}") from exc
        finally:
            temp.unlink(missing_ok=True)

    def _load_session(self) -> tuple[bytes, int]:
        if not self.session_path.exists():
            raise GooglePhotosWebError("Import a Google Photos cookies.txt file first")
        try:
            wrapper = json.loads(self.session_path.read_text(encoding="utf-8"))
            raw = base64.b64decode(wrapper["data"])
            if wrapper.get("encrypted"):
                if os.name != "nt":
                    raise GooglePhotosWebError("Google web session belongs to another computer")
                raw = _windows_unprotect(raw)
            return raw, int(wrapper.get("account_index", 0))
        except GooglePhotosWebError:
            raise
        except Exception as exc:
            raise GooglePhotosWebError("Stored Google Photos web session is unreadable; import it again") from exc

    @contextmanager
    def _temporary_cookie_file(self, raw: bytes) -> Iterator[Path]:
        handle = tempfile.NamedTemporaryFile(
            prefix=".kira-google-web-",
            suffix=".txt",
            dir=self.root,
            delete=False,
        )
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
        raw, account_index = self._load_session()
        client_factory, payloads = self._dependency()
        with self._temporary_cookie_file(raw) as cookies_path:
            client = None
            try:
                client = client_factory(cookies_path, account_index=account_index)
                yield client, payloads
            except GooglePhotosWebError:
                raise
            except IndexError as exc:
                raise GooglePhotosWebError(
                    "Google Photos web session expired; import a fresh cookie export"
                ) from exc
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
        self,
        client: object,
        payloads: object,
        media_keys: list[str],
        album_media_key: str,
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

    def _verify_archive(
        self, client: object, payloads: object, media_keys: list[str]
    ) -> None:
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
