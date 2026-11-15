"""The iPad round trip over local Wi-Fi, driven by two iOS Shortcuts.

* **Send to iPad** queues originals in the outbox. The "Get from Kira" Shortcut
  lists the outbox, downloads each file and hands it to Lightroom.
* **Send to Kira** (Lightroom > Share > Share to...) uploads finished exports.
  Kira files each one as an edit of its original. Matching only has to consider
  the photos that were sent to the iPad, so it is close to certain.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

from .errors import KiraError
from .library import Library, format_group
from .media import utc_now

HINT_DAYS = 60
INBOX_SHOOT = "iPad inbox"


class Outbox:
    def __init__(self, library: Library) -> None:
        self.library = library
        self.state = library.state

    def _pick(self, photo: dict, prefer: str) -> dict | None:
        """The file to edit: the RAW when there is one (unless JPEG is asked for), else the still original."""
        originals = [r for r in photo["files"] if r["role"] == "original" and not r.get("missing")]
        by_group = {format_group(r["name"]): r for r in originals}
        order = ("raw", "still") if prefer == "raw" else ("still", "raw")
        for group in order:
            if group in by_group:
                return by_group[group]
        final = self.library.effective_final(photo)
        return self.library.file_record(photo, final) if final else None

    def send(self, shoot_id: str, photo_ids: list[str], prefer: str = "raw") -> dict:
        queued, skipped = 0, []
        with self.library.lock:
            shoot = self.library.shoot(shoot_id)
            waiting = {
                (row["photo_id"], row["file_name"])
                for row in self.state.execute("SELECT photo_id, file_name FROM outbox WHERE state='waiting'")
            }
            for photo_id in photo_ids:
                photo = shoot["photos"].get(str(photo_id))
                if photo is None:
                    continue
                if photo.get("kind") == "video":
                    skipped.append(photo["stem"])
                    continue
                record = self._pick(photo, prefer)
                if record is None:
                    skipped.append(photo["stem"])
                    continue
                if (photo["id"], record["name"]) in waiting:
                    continue
                self.state.execute(
                    "INSERT INTO outbox (id, shoot_id, photo_id, file_name, state, queued_at) VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex[:12], shoot["id"], photo["id"], record["name"], "waiting", utc_now()),
                )
                queued += 1
        return {"queued": queued, "skipped": skipped, "waiting": len(self.items("waiting"))}

    def items(self, state: str | None = None) -> list[dict]:
        sql = "SELECT * FROM outbox"
        params: tuple = ()
        if state:
            sql += " WHERE state=?"
            params = (state,)
        result = []
        for row in self.state.execute(sql + " ORDER BY queued_at", params):
            item = dict(row)
            shoot = self.library.shoots.get(item["shoot_id"])
            photo = shoot["photos"].get(item["photo_id"]) if shoot else None
            if photo is None:
                continue
            item["shoot_name"] = shoot["name"]
            item["stem"] = photo["stem"]
            try:
                item["size"] = self.library.file_record(photo, item["file_name"]).get("size", 0)
            except KiraError:
                continue
            item["returned"] = any(
                r["role"] == "edit" and r.get("source") == "ipad" and r.get("added_at", "") >= item["queued_at"]
                for r in photo["files"]
            )
            result.append(item)
        return result

    def file(self, item_id: str) -> tuple[Path, str]:
        rows = self.state.execute("SELECT * FROM outbox WHERE id=?", (item_id,))
        if not rows:
            raise KiraError(HTTPStatus.NOT_FOUND, "Outbox item not found")
        row = rows[0]
        shoot, photo = self.library.photo(row["shoot_id"], row["photo_id"])
        record = self.library.file_record(photo, row["file_name"])
        path = self.library.file_path(shoot, photo, record)
        if not path.is_file():
            raise KiraError(HTTPStatus.SERVICE_UNAVAILABLE, "The library drive is not connected")
        return path, record["name"]

    def mark_sent(self, item_id: str) -> None:
        self.state.execute(
            "UPDATE outbox SET state='sent', sent_at=? WHERE id=? AND state='waiting'", (utc_now(), item_id)
        )

    def remove(self, item_id: str) -> None:
        self.state.execute("DELETE FROM outbox WHERE id=?", (item_id,))

    def clear_sent(self) -> dict:
        rows = self.state.execute("SELECT COUNT(*) AS n FROM outbox WHERE state='sent'")
        self.state.execute("DELETE FROM outbox WHERE state='sent'")
        return {"cleared": rows[0]["n"]}

    def _hints(self) -> tuple[set[str], str | None]:
        """Photos recently sent to the iPad, and the shoot most recently sent from."""
        since = (datetime.now(UTC) - timedelta(days=HINT_DAYS)).replace(microsecond=0).isoformat()
        rows = self.state.execute(
            "SELECT photo_id, shoot_id FROM outbox WHERE queued_at>=? ORDER BY queued_at", (since,)
        )
        hints = {row["photo_id"] for row in rows}
        shoot_id = rows[-1]["shoot_id"] if rows else None
        return hints, shoot_id if shoot_id in self.library.shoots else None

    def receive(self, staged: Path, name: str) -> dict:
        """File one upload from the iPad. ``staged`` must already sit on the library drive."""
        hints, shoot_id = self._hints()
        if shoot_id is None:
            shoot = self.library.find_shoot(INBOX_SHOOT) or self.library.create_shoot(INBOX_SHOOT)
            shoot_id = shoot["id"]
        result = self.library.ingest_file(
            staged, shoot_id, source_kind="ipad", original_name=name, hints=hints or None, consume=True
        )
        self.library.flush()
        return result
