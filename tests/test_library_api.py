"""End-to-end checks of the library workflow over a real loopback HTTP server."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import av
from PIL import Image, ImageDraw

from kira.api import APP_ROOT, KiraHTTPServer
from kira.app import KiraApp
from kira.gphotos import google_hash_of
from kira.main import STATIC_ROOT


def make_photo(
    path: Path,
    *,
    taken: str,
    make: str = "SONY",
    model: str = "ILCE-7M4",
    software: str = "",
    seed: int = 0,
    size: tuple[int, int] = (640, 480),
    tint: int = 0,
) -> Path:
    image = Image.new("RGB", size, (40 + seed * 30, 90, 140))
    draw = ImageDraw.Draw(image)
    for index in range(6):
        offset = (seed * 37 + index * 53) % (size[0] - 120)
        draw.rectangle((offset, index * 70, offset + 110, index * 70 + 50), fill=(220, 200 - index * 20, 60))
    if tint:
        image = Image.eval(image, lambda value: min(255, value + tint))
    exif = Image.Exif()
    exif[271] = make
    exif[272] = model
    if software:
        exif[305] = software
    exif.get_ifd(0x8769)[36867] = taken
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG", quality=92, exif=exif.tobytes())
    return path


class FakeWeb:
    """Stands in for the Google Photos web client: holds a set of exact files."""

    def __init__(self) -> None:
        self.held: dict[str, dict] = {}
        self.archived: list[str] = []
        self.by_name: dict[tuple[str, int], dict] = {}
        self.albums: dict[str, list[str]] = {}

    def hold(self, path: Path) -> None:
        value = google_hash_of(path)
        self.held[value] = {"media_key": f"m-{len(self.held)}", "dedup_key": f"d-{len(self.held)}"}

    def lookup_hashes(self, hashes: list[str]) -> dict[str, dict]:
        return {value: self.held[value] for value in hashes if value in self.held}

    def archive(self, items: list[dict]) -> list[str]:
        self.archived += [item["media_key"] for item in items]
        return [item["media_key"] for item in items]

    def find_by_name_and_time(self, wanted: list[dict]) -> dict[int, dict]:
        return {
            index: self.by_name[(entry["name"], entry["time_ms"])]
            for index, entry in enumerate(wanted)
            if (entry["name"], entry["time_ms"]) in self.by_name
        }

    def organize(self, media_keys: list[str], album_title: str, archive: bool = False) -> dict:
        self.albums.setdefault(album_title, []).extend(media_keys)
        if archive:
            self.archived += media_keys
        return {}

    def status(self) -> dict:
        return {"connected": True}

    def close(self) -> None:
        pass


class LibraryWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="kira-test-"))
        self.data = self.temp / "data"
        self.library_root = self.temp / "Library"
        self.card = self.temp / "card" / "DCIM"
        self.app = KiraApp(self.data, APP_ROOT)
        self.server = KiraHTTPServer(("127.0.0.1", 0), self.app, STATIC_ROOT)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.token = self.request("GET", "/api/bootstrap")["token"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        for path in self.temp.rglob("*"):
            if path.is_file():
                path.chmod(0o666)
        shutil.rmtree(self.temp, ignore_errors=True)

    def request(self, method: str, path: str, body: object = None, *, raw: bytes | None = None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if getattr(self, "token", None):
            request.add_header("X-Kira-Token", self.token)
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
            if response.headers.get("Content-Type", "").startswith("application/json"):
                return json.loads(payload)
            return payload

    def wait(self, task: dict) -> dict:
        for _ in range(200):
            task = self.request("GET", f"/api/tasks/{task['id']}")
            if task["state"] != "running":
                return task
            time.sleep(0.05)
        self.fail("task did not finish")

    def import_card(self, shoot_name: str = "2026-07 NYC") -> dict:
        task = self.request("POST", "/api/import", {"source": str(self.card), "shoot_name": shoot_name})
        done = self.wait(task)
        self.assertEqual(done["state"], "done", done.get("error"))
        return done["result"]

    def test_full_round_trip(self) -> None:
        overview = self.request("GET", "/api/library")
        self.assertFalse(overview["configured"])
        self.request("POST", "/api/library/root", {"path": str(self.library_root)})

        make_photo(self.card / "DSC00001.JPG", taken="2026:07:01 10:00:00", seed=1)
        make_photo(self.card / "DSC00002.JPG", taken="2026:07:01 10:05:00", seed=2)
        shutil.copy(self.card / "DSC00001.JPG", self.card / "DSC00001 copy.JPG")

        report = self.import_card()
        self.assertEqual(report["counts"].get("new"), 2)
        self.assertEqual(report["counts"].get("duplicate"), 1)
        shoot_id = report["shoot_id"]
        again = self.import_card()
        self.assertEqual(again["counts"], {"duplicate": 3})

        shoot = self.request("GET", f"/api/shoots/{shoot_id}")
        self.assertEqual(len(shoot["photos"]), 2)
        first = next(photo for photo in shoot["photos"] if photo["stem"] == "DSC00001")
        self.assertTrue((self.library_root / "2026" / "2026-07 NYC" / "DSC00001.JPG").exists())
        self.assertTrue((self.library_root / "2026" / "2026-07 NYC" / "_kira.json").exists())

        # The card is in the library but nothing is backed up yet, so it isn't safe to wipe.
        check = self.request("POST", "/api/import/check", {"source": str(self.card)})
        self.assertEqual(check["imported"], 3)
        self.assertFalse(check["safe_to_wipe"])

        # Send to the iPad; the Shortcut gets a plain list of links and downloads each.
        sent = self.request("POST", "/api/ipad/send", {"shoot_id": shoot_id, "photo_ids": [first["id"]]})
        self.assertEqual(sent["queued"], 1)
        urls = self.request("GET", "/api/ipad/outbox?format=urls").decode().split()
        self.assertEqual(len(urls), 1)
        path = "/api/" + urls[0].split("/api/", 1)[1]
        original = self.request("GET", path)
        self.assertEqual(original, (self.card / "DSC00001.JPG").read_bytes())
        self.assertEqual(self.request("GET", "/api/ipad/outbox?format=urls"), b"")

        # Lightroom exports an edit: same capture, new pixels, renamed, sent without an extension.
        edit = make_photo(
            self.temp / "export" / "IMG_7001.jpg",
            taken="2026:07:01 10:00:00",
            software="Adobe Lightroom 9.0 (iOS)",
            seed=1,
            tint=40,
            size=(1200, 900),
        )
        result = self.request("POST", "/api/ipad/receive?name=IMG_7001", raw=edit.read_bytes())
        self.assertEqual(result["role"], "edit")
        self.assertEqual(result["photo_id"], first["id"])
        self.assertEqual(result["file"], "DSC00001_edit1.jpg")
        # The same export sent twice is recognised.
        self.assertEqual(
            self.request("POST", "/api/ipad/receive?name=x.jpg", raw=edit.read_bytes())["status"], "duplicate"
        )
        # A damaged transfer is refused.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(
                "POST", "/api/ipad/receive?name=y.jpg", raw=b"\xff\xd8\xffjunk", headers={"X-Kira-Sha256": "0" * 64}
            )
        self.assertEqual(caught.exception.code, 409)

        shoot = self.request("GET", f"/api/shoots/{shoot_id}")
        first = next(photo for photo in shoot["photos"] if photo["id"] == first["id"])
        self.assertEqual(first["final"], "DSC00001_edit1.jpg")
        outbox = self.request("GET", "/api/ipad/outbox")["items"]
        self.assertTrue(outbox[0]["returned"])

        # A second edit becomes edit2 and the newest edit is the Final until one is pinned.
        edit2 = make_photo(
            self.temp / "export" / "IMG_7002.jpg", taken="2026:07:01 10:00:00", software="Lightroom", seed=1, tint=80
        )
        self.assertEqual(
            self.request("POST", "/api/ipad/receive?name=IMG_7002.jpg", raw=edit2.read_bytes())["file"],
            "DSC00001_edit2.jpg",
        )
        pinned = self.request(
            "POST", f"/api/shoots/{shoot_id}/final", {"photo_id": first["id"], "name": "DSC00001_edit1.jpg"}
        )
        self.assertEqual(pinned["final"], "DSC00001_edit1.jpg")

        # Rejecting moves files into _rejects; nothing is deleted.
        second = next(photo for photo in shoot["photos"] if photo["stem"] == "DSC00002")
        self.request("POST", f"/api/shoots/{shoot_id}/reject", {"photo_ids": [second["id"]], "rejected": True})
        self.assertTrue((self.library_root / "2026" / "2026-07 NYC" / "_rejects" / "DSC00002.JPG").exists())

        # Drive backup would carry every original, edit and reject.
        wanted = set(self.app.drive.wanted())
        self.assertIn("2026/2026-07 NYC/DSC00001_edit2.jpg", wanted)
        self.assertIn("2026/2026-07 NYC/_rejects/DSC00002.JPG", wanted)

        # Only the pinned Final is waiting to go to Google Photos.
        todo, _ = self.app.photos.pending_finals()
        self.assertEqual([record["name"] for _, _, record in todo], ["DSC00001_edit1.jpg"])

        # Clean-up offers to hide the other edit if Google Photos holds it, and only on request.
        fake = FakeWeb()
        self.app.photos.web = fake
        folder = self.library_root / "2026" / "2026-07 NYC"
        fake.hold(folder / "DSC00001_edit2.jpg")
        fake.hold(folder / "DSC00001_edit1.jpg")
        plan = self.app.photos.cleanup_plan()
        self.assertEqual([item["name"] for item in plan["items"]], ["DSC00001_edit2.jpg"])
        self.assertEqual(fake.archived, [])
        self.app.photos.cleanup_apply([plan["items"][0]["media_key"]])
        self.assertEqual(fake.archived, [plan["items"][0]["media_key"]])
        self.assertEqual(self.app.photos.cleanup_plan()["items"], [])

    def test_phone_photos_rely_on_google_photos_only_after_a_check(self) -> None:
        self.request("POST", "/api/library/root", {"path": str(self.library_root)})
        make_photo(self.card / "IMG_0001.JPG", taken="2026:07:02 09:00:00", make="Apple", model="iPhone 15 Pro", seed=3)
        shoot_id = self.import_card("Phone")["shoot_id"]
        relative = "2026/Phone/IMG_0001.JPG"
        self.assertIn(relative, self.app.drive.wanted())

        fake = FakeWeb()
        fake.hold(self.library_root / relative)
        self.app.photos.web = fake
        result = self.app.photos.check()
        self.assertEqual(result["present"], 1)
        self.assertNotIn(relative, self.app.drive.wanted())

        # A phone original is never uploaded again as a Final.
        shoot = self.request("GET", f"/api/shoots/{shoot_id}")
        photo_id = shoot["photos"][0]["id"]
        self.request("POST", f"/api/shoots/{shoot_id}/final", {"photo_id": photo_id, "name": "IMG_0001.JPG"})
        self.assertEqual(self.app.photos.pending_finals()[0], [])

    def test_drive_backup_moves_instead_of_reuploading_and_never_overwrites(self) -> None:
        self.request("POST", "/api/library/root", {"path": str(self.library_root)})
        make_photo(self.card / "DSC00010.JPG", taken="2026:07:03 08:00:00", seed=4)
        (self.card / "DSC00010.xmp").write_text("<x:xmpmeta>v1</x:xmpmeta>")
        shoot_id = self.import_card("Drive")["shoot_id"]
        drive = self.app.drive
        calls: list[tuple] = []
        drive._verify_root = lambda: None
        drive._folder = lambda relative: f"folder:{relative}"
        drive._sync_manifest = lambda relative, path: None
        drive._upload = lambda path, name, parent, digest: calls.append(("upload", parent, name)) or f"id-{len(calls)}"
        drive._move = lambda file_id, old, new: calls.append(("move", old, new))
        drive._rename_old = lambda file_id, name: calls.append(("rename", name))

        report = drive.backup()
        self.assertEqual(report["uploaded"], 2)
        self.assertEqual(drive.status()["pending"], 0)

        calls.clear()
        photo_id = self.request("GET", f"/api/shoots/{shoot_id}")["photos"][0]["id"]
        self.request("POST", f"/api/shoots/{shoot_id}/reject", {"photo_ids": [photo_id], "rejected": True})
        report = drive.backup()
        self.assertEqual(report, {**report, "uploaded": 0, "moved": 2})
        self.assertIn(("move", "2026/Drive/DSC00010.JPG", "2026/Drive/_rejects/DSC00010.JPG"), calls)

        # An editor changes the sidecar: the old Drive copy is renamed and the new one uploaded beside it.
        calls.clear()
        sidecar = self.library_root / "2026" / "Drive" / "_rejects" / "DSC00010.xmp"
        sidecar.chmod(0o666)
        sidecar.write_text("<x:xmpmeta>v2</x:xmpmeta>")
        time.sleep(2.1)
        self.request("POST", f"/api/shoots/{shoot_id}/rescan")
        drive.backup()
        self.assertEqual(calls[0], ("rename", "2026/Drive/_rejects/DSC00010.xmp"))
        self.assertEqual(calls[1][0], "upload")

    def test_picker_import_files_photos_into_the_shoot_album_and_archives_them(self) -> None:
        self.request("POST", "/api/library/root", {"path": str(self.library_root)})
        source = self.temp / "google"
        exact = make_photo(source / "IMG_0100.JPG", taken="2026:07:04 10:00:00", make="Apple", model="iPhone", seed=1)
        stripped = make_photo(
            source / "IMG_0101.JPG", taken="2026:07:04 10:01:00", make="Apple", model="iPhone", seed=2
        )
        lost = make_photo(source / "IMG_0102.JPG", taken="2026:07:04 10:02:00", make="Apple", model="iPhone", seed=5)
        picked = [
            {
                "id": "p0",
                "createTime": "2026-07-04T10:00:00Z",
                "mediaFile": {"filename": exact.name, "baseUrl": str(exact)},
            },
            {
                "id": "p1",
                "createTime": "2026-07-04T10:01:00.250Z",
                "mediaFile": {"filename": stripped.name, "baseUrl": str(stripped)},
            },
            {
                "id": "p2",
                "createTime": "2026-07-04T10:02:00Z",
                "mediaFile": {"filename": lost.name, "baseUrl": str(lost)},
            },
        ]
        shoot_id = self.request("POST", "/api/shoots", {"name": "Picked"})["id"]
        fake = FakeWeb()
        fake.hold(exact)
        fake.by_name[(stripped.name, 1783159260250)] = {"media_key": "m-name", "dedup_key": "d-name"}
        photos = self.app.photos
        photos.web = fake
        photos.auth.access_token = lambda scope: "token"

        def fake_request(url, method="GET", **_kwargs):
            return {} if method == "DELETE" else {"mediaItems": picked}

        def fake_download(url, token, destination):
            shutil.copyfile(url.rsplit("=", 1)[0], destination)

        with patch("kira.gphotos.request_json", fake_request), patch("kira.gphotos._download", fake_download):
            report = photos.picker_import("session", shoot_id)

        self.assertEqual(report["counts"], {"new": 3})
        self.assertEqual(report["google"]["album"], "Picked")
        self.assertEqual(report["google"]["archived"], 2)
        self.assertEqual(report["google"]["not_found"], [lost.name])
        self.assertEqual(sorted(fake.albums["Picked"]), ["m-0", "m-name"])
        self.assertEqual(sorted(fake.archived), ["m-0", "m-name"])

    def test_deleting_a_shoot_moves_its_folder_to_the_trash(self) -> None:
        self.request("POST", "/api/library/root", {"path": str(self.library_root)})
        make_photo(self.card / "DSC00020.JPG", taken="2026:07:05 08:00:00", seed=6)
        shoot_id = self.import_card("Delete me")["shoot_id"]
        folder = self.library_root / "2026" / "Delete me"
        self.assertTrue((folder / "DSC00020.JPG").exists())

        result = self.request("POST", f"/api/shoots/{shoot_id}/trash")
        self.assertEqual(result["trashed"], 2)  # the photo and the manifest
        self.assertFalse(folder.exists())
        self.assertTrue((Path(result["trash_folder"]) / "DSC00020.JPG").exists())
        self.assertNotIn(shoot_id, [shoot["id"] for shoot in self.request("GET", "/api/library")["shoots"]])
        self.assertEqual(len(self.request("GET", "/api/trash")["items"]), 2)

        # The same photo can be imported again, and a reopened library does not resurrect the shoot.
        self.assertEqual(self.import_card("Again")["counts"].get("new"), 1)
        self.app.library.open(self.library_root)
        self.assertEqual([shoot["name"] for shoot in self.app.library.shoots.values()], ["Again"])

        self.request("POST", "/api/trash/empty", {"confirm": True})
        self.assertFalse(Path(result["trash_folder"]).exists())

    def test_videos_show_a_frame_and_play_inline(self) -> None:
        self.request("POST", "/api/library/root", {"path": str(self.library_root)})
        self.card.mkdir(parents=True)
        with av.open(str(self.card / "C0001.MP4"), "w") as container:
            stream = container.add_stream("mpeg4", rate=10)
            stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
            for _ in range(5):
                frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), (200, 30, 30)))
                container.mux(stream.encode(frame))
            container.mux(stream.encode())
        shoot_id = self.import_card()["shoot_id"]

        video = self.request("GET", f"/api/shoots/{shoot_id}")["photos"][0]
        self.assertEqual((video["kind"], video["cover"]), ("video", "C0001.MP4"))
        base = f"/api/shoots/{shoot_id}/photos/{video['id']}/C0001.MP4"
        preview = Image.open(BytesIO(self.request("GET", f"{base}/preview?size=480")))
        self.assertEqual(preview.size, (64, 48))
        self.assertGreater(preview.getpixel((32, 24))[0], 150)
        inline = self.request("GET", f"{base}?inline=1", headers={"Range": "bytes=0-9"})
        self.assertEqual(inline, (self.card / "C0001.MP4").read_bytes()[:10])

    def test_wrong_token_is_rejected(self) -> None:
        self.request("POST", "/api/library/root", {"path": str(self.library_root)})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.token, token = "wrong", self.token
            try:
                self.request("GET", "/api/library")
            finally:
                self.token = token
        self.assertEqual(caught.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
