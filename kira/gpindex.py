"""What Google Photos holds that the library does not, found without downloading anything.

A scan reads the Google Photos library's metadata through the web client: each item's key,
dedup key, capture time, size and file name. Each item is then matched to the library:

* ``library``: the library holds the same bytes. Kira already linked the item while checking,
  or a library file of the same byte size hashes to it (Google confirms hashes it can't be
  told from the dedup key alone).
* ``similar``: the library has the same capture under the same name within two seconds, but
  other bytes: a picker download Google stripped of location, a JPEG made from a HEIC, an edit.
* ``missing``: nothing in the library corresponds.

Only library files whose size equals some Google item's size are ever read, so a scan
costs a few hashes rather than a pass over the whole library. Thumbnails are Google's own
small previews, fetched with the signed-in session only when the page shows them and cached in
Kira's working folder; originals are never downloaded here.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import uuid
from http import HTTPStatus
from pathlib import Path

from .errors import KiraError
from .gphotos import PhotosSync
from .library import Library, _capture_seconds
from .media import match_key, utc_now

SAME_SHOT_SECONDS = 2
STATUSES = ("library", "similar", "missing")
LINKABLE_ROLES = {"original", "edit", "copy", "live"}


def dedup_key_of(google_hash: str) -> str:
    """Google's dedup key for a file whose standard base64 SHA-1 is ``google_hash``."""
    return base64.urlsafe_b64encode(base64.b64decode(google_hash)).decode("ascii").rstrip("=")


class GooglePhotosIndex:
    def __init__(self, library: Library, photos: PhotosSync) -> None:
        self.library = library
        self.photos = photos
        self.web = photos.web
        self.state = library.state
        self.thumbnails_root = library.data_root / "gphotos-thumbnails"
        self.thumbnail_slots = threading.Semaphore(6)

    # -- Scan ---------------------------------------------------------------------------

    def scan(self, task=None) -> dict:
        cancelled = task.cancelled if task else None
        say = (lambda done, total, text: task.progress(done, total, text)) if task else (lambda *_: None)

        say(0, 0, "Reading the Google Photos library")
        items = self.web.library_items(lambda count: say(0, 0, f"Read {count} items"), cancelled)
        if cancelled and cancelled.is_set():
            return {"cancelled": True}
        self._store(items)

        rows = self.state.execute("SELECT media_key FROM gp_items WHERE file_name IS NULL")
        unnamed = [row["media_key"] for row in rows]
        if unnamed:
            details = self.web.media_details(
                unnamed, lambda done, total: say(done, total, "Reading file names and sizes"), cancelled
            )
            self.state.executemany(
                "UPDATE gp_items SET file_name=?, size=?, original_quality=? WHERE media_key=?",
                [(d["file_name"], d["size"], int(d["original_quality"]), key) for key, d in details.items()],
            )
        if cancelled and cancelled.is_set():
            return {"cancelled": True}

        counts = self.match(task)
        self.state.set("gp_index_scanned_at", utc_now())
        return {"items": len(items), **counts}

    def _store(self, items: list[dict]) -> None:
        """Keep cached names and sizes for known items; forget items no longer in Google Photos."""
        now = f"{utc_now()} {uuid.uuid4().hex[:8]}"  # unique per scan, even two in one second
        self.state.executemany(
            """INSERT INTO gp_items (media_key, dedup_key, taken_ms, local_time, thumbnail_url, width, height,
                                     video, archived, owned, seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(media_key) DO UPDATE SET dedup_key=excluded.dedup_key, taken_ms=excluded.taken_ms,
                   local_time=excluded.local_time, thumbnail_url=excluded.thumbnail_url, width=excluded.width,
                   height=excluded.height, video=excluded.video, archived=excluded.archived,
                   owned=excluded.owned, seen_at=excluded.seen_at""",
            [
                (
                    item["media_key"],
                    item["dedup_key"],
                    item["taken_ms"],
                    item["local_time"],
                    item["thumbnail_url"],
                    item["width"],
                    item["height"],
                    int(item["video"]),
                    int(item["archived"]),
                    int(item["owned"]),
                    now,
                )
                for item in items
            ],
        )
        self.state.execute("DELETE FROM gp_items WHERE seen_at<>?", (now,))

    # -- Matching -----------------------------------------------------------------------

    def _where(self, shoot: dict, photo: dict, record: dict, how: str) -> dict:
        return {
            "shoot_id": shoot["id"],
            "shoot_name": shoot["name"],
            "photo_id": photo["id"],
            "file": record["name"],
            "how": how,
        }

    def match(self, task=None) -> dict:
        """Match every scanned item against the library as it is now. Reads only same-size files."""
        items = [dict(row) for row in self.state.execute("SELECT * FROM gp_items")]
        by_sha: dict[str, tuple] = {}
        by_size: dict[int, list[tuple]] = {}
        by_name: dict[str, list[tuple]] = {}
        with self.library.lock:
            for shoot, photo, record in self.library.all_files():
                if record["role"] not in LINKABLE_ROLES:
                    continue
                entry = (shoot, photo, record)
                by_sha.setdefault(record["sha256"], entry)
                by_size.setdefault(int(record.get("size") or 0), []).append(entry)
                for name in {record["name"], record.get("original_name") or record["name"]}:
                    by_name.setdefault(match_key(name), []).append(entry)

        found: dict[str, dict] = {}

        # 1. Items Kira already linked to a library file while checking what Google Photos holds.
        for row in self.state.execute("SELECT sha256, media_key FROM gphotos WHERE state='present'"):
            if row["media_key"] and row["sha256"] in by_sha:
                found[row["media_key"]] = self._where(*by_sha[row["sha256"]], "exact")

        # 2. Exact bytes: hash only library files whose size equals an unmatched item's size.
        open_items = [item for item in items if item["media_key"] not in found and item.get("size")]
        candidates = [(item, entry) for item in open_items for entry in by_size.get(item["size"], [])]
        unconfirmed: dict[str, list[tuple]] = {}
        for index, (item, entry) in enumerate(candidates, start=1):
            if task and task.cancelled.is_set():
                break
            if task:
                task.progress(index, len(candidates), f"Comparing {entry[2]['name']}")
            if item["media_key"] in found:
                continue
            shoot, photo, record = entry
            try:
                google_hash = self.photos._google_hash(self.library.file_path(shoot, photo, record), record["sha256"])
            except OSError:
                continue
            if dedup_key_of(google_hash) == item["dedup_key"]:
                found[item["media_key"]] = self._where(shoot, photo, record, "exact")
            else:
                unconfirmed.setdefault(google_hash, []).append((item, entry))
        # The dedup key may not be a plain SHA-1 for every item; let Google say which hashes it holds.
        if unconfirmed:
            for google_hash, hit in self.web.lookup_hashes(list(unconfirmed)).items():
                for item, entry in unconfirmed[google_hash]:
                    if hit["media_key"] == item["media_key"] and item["media_key"] not in found:
                        found[item["media_key"]] = self._where(*entry, "exact")

        # 3. Same capture under the same name, other bytes.
        similar: dict[str, dict] = {}
        for item in items:
            if item["media_key"] in found or not item.get("file_name") or not item.get("local_time"):
                continue
            taken = _capture_seconds(item["local_time"])
            for entry in by_name.get(match_key(item["file_name"]), []):
                captured = _capture_seconds(entry[1]["capture"].get("time") or "")
                if taken is not None and captured is not None and abs(taken - captured) <= SAME_SHOT_SECONDS:
                    similar[item["media_key"]] = self._where(*entry, "same_shot")
                    break

        updates = []
        counts = dict.fromkeys(STATUSES, 0)
        for item in items:
            key = item["media_key"]
            where = found.get(key) or similar.get(key)
            status = "library" if key in found else "similar" if key in similar else "missing"
            counts[status] += 1
            updates.append((status, json.dumps(where) if where else None, key))
        self.state.executemany("UPDATE gp_items SET status=?, match=? WHERE media_key=?", updates)
        return counts

    # -- Listing ------------------------------------------------------------------------

    @staticmethod
    def _month(row: dict) -> str:
        time = row.get("local_time") or ""
        return f"{time[:4]}-{time[5:7]}" if len(time) >= 7 else "undated"

    def listing(self, month: str = "", status: str = "") -> dict:
        """Month summaries, plus the items of one month (or of every month) with an optional status."""
        rows = [dict(row) for row in self.state.execute("SELECT * FROM gp_items ORDER BY taken_ms DESC")]
        months: dict[str, dict] = {}
        for row in rows:
            summary = months.setdefault(
                self._month(row), {"month": self._month(row), "items": 0, "cover": None, **dict.fromkeys(STATUSES, 0)}
            )
            summary["items"] += 1
            summary[row["status"]] = summary.get(row["status"], 0) + 1
            if summary["cover"] is None and row["thumbnail_url"]:
                summary["cover"] = row["media_key"]
        wanted = [
            row
            for row in rows
            if (month in ("", "all") or self._month(row) == month) and (not status or row["status"] == status)
        ]
        return {
            "scanned_at": self.state.get("gp_index_scanned_at") or None,
            "web": self.web.status(),
            "totals": {"items": len(rows), **{key: sum(m[key] for m in months.values()) for key in STATUSES}},
            "months": list(months.values()),
            "items": [self._view(row) for row in wanted] if month else [],
        }

    def thumbnail(self, media_key: str, requested_size: int) -> Path:
        """A cached copy of Google's thumbnail for one scanned item, fetched on first view."""
        size = 1600 if requested_size > 480 else 400
        # Only keys of scanned items reach the file name.
        rows = self.state.execute("SELECT thumbnail_url FROM gp_items WHERE media_key=?", (media_key,))
        if not rows or not rows[0]["thumbnail_url"]:
            raise KiraError(HTTPStatus.NOT_FOUND, "No thumbnail for this item")
        destination = self.thumbnails_root / f"{media_key}-{size}.jpg"
        if destination.exists():
            return destination
        with self.thumbnail_slots:
            if destination.exists():
                return destination
            data = self.web.thumbnail(rows[0]["thumbnail_url"], size)
            self.thumbnails_root.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f"{destination.stem}-{threading.get_ident()}.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, destination)
        return destination

    @staticmethod
    def _view(row: dict) -> dict:
        return {
            "media_key": row["media_key"],
            "file_name": row["file_name"] or "",
            "local_time": row["local_time"] or "",
            "size": row["size"] or 0,
            "width": row["width"],
            "height": row["height"],
            "video": bool(row["video"]),
            "archived": bool(row["archived"]),
            "owned": bool(row["owned"]),
            "original_quality": bool(row["original_quality"]),
            "thumbnail": bool(row["thumbnail_url"]),
            "status": row["status"],
            "match": json.loads(row["match"]) if row["match"] else None,
        }
