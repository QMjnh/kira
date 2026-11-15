"""Browsing a card or folder outside the library: status, duplicates, shortcuts and clean-up."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from test_library_api import make_photo

from kira.api import APP_ROOT, KiraHTTPServer
from kira.app import KiraApp
from kira.main import STATIC_ROOT


class SourceBrowserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="kira-test-"))
        self.library_root = self.temp / "Library"
        self.card = self.temp / "card"
        self.app = KiraApp(self.temp / "data", APP_ROOT)
        self.server = KiraHTTPServer(("127.0.0.1", 0), self.app, STATIC_ROOT)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.token = self.request("GET", "/api/bootstrap")["token"]
        self.request("POST", "/api/library/root", {"path": str(self.library_root)})

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        for path in self.temp.rglob("*"):
            if path.is_file():
                path.chmod(0o666)
        shutil.rmtree(self.temp, ignore_errors=True)

    def request(self, method: str, path: str, body: object = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
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
        for _ in range(400):
            task = self.request("GET", f"/api/tasks/{task['id']}")
            if task["state"] != "running":
                return task
            time.sleep(0.05)
        self.fail("task did not finish")

    def listing(self, folder: Path) -> dict:
        return self.request("GET", f"/api/source?path={urllib.parse.quote(str(folder))}")

    def files(self, folder: Path) -> dict[str, dict]:
        return {item["name"]: item for item in self.listing(folder)["files"]}

    def test_folders_show_a_cover_and_counts_and_videos_play(self) -> None:
        dcim = self.card / "DCIM"
        make_photo(dcim / "100MSDCF" / "DSC00002.ARW", taken="2026:07:01 10:00:00", seed=1)
        make_photo(dcim / "100MSDCF" / "DSC00003.JPG", taken="2026:07:01 10:00:00", seed=2)
        (dcim / "100MSDCF" / "C0001.MP4").write_bytes(b"not really a video")
        (dcim / "100MSDCF" / "notes.txt").write_text("x")
        (dcim / "empty").mkdir()

        folders = {item["name"]: item for item in self.listing(dcim)["directories"]}
        self.assertEqual(folders["100MSDCF"]["media"], 3)
        self.assertEqual(folders["100MSDCF"]["cover"], str(dcim / "100MSDCF" / "DSC00003.JPG"))
        self.assertEqual((folders["empty"]["media"], folders["empty"]["cover"]), (0, None))
        # A folder of folders borrows its cover from the first one with media.
        card = {item["name"]: item for item in self.listing(self.card)["directories"]}
        self.assertEqual((card["DCIM"]["media"], card["DCIM"]["folders"]), (0, 2))
        self.assertEqual(card["DCIM"]["cover"], folders["100MSDCF"]["cover"])

        # All subfolders at once: every file below, labelled with the folder it sits in.
        deep = self.request("GET", f"/api/source?path={urllib.parse.quote(str(self.card))}&recursive=1")
        self.assertTrue(deep["recursive"])
        self.assertEqual(
            sorted((item["folder"], item["name"]) for item in deep["files"]),
            [(str(Path("DCIM/100MSDCF")), name) for name in ("C0001.MP4", "DSC00002.ARW", "DSC00003.JPG", "notes.txt")],
        )
        self.assertEqual(self.listing(self.card)["files"], [])

        video = urllib.request.Request(
            f"{self.base}/api/source/file?path={urllib.parse.quote(str(dcim / '100MSDCF' / 'C0001.MP4'))}",
            headers={"X-Kira-Token": self.token, "Range": "bytes=0-3"},
        )
        with urllib.request.urlopen(video, timeout=30) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"not ")
            self.assertTrue(response.headers["Content-Disposition"].startswith("inline"))

    def test_browse_scan_duplicates_links_and_clean_up(self) -> None:
        dcim = self.card / "DCIM"
        make_photo(dcim / "DSC00001.JPG", taken="2026:07:01 10:00:00", seed=1)
        make_photo(dcim / "DSC00002.JPG", taken="2026:07:01 10:05:00", seed=2)
        (dcim / "select").mkdir()
        shutil.copy2(dcim / "DSC00001.JPG", dcim / "select" / "DSC00001.JPG")
        make_photo(self.card / "extra" / "DSC00003.JPG", taken="2026:07:02 09:00:00", seed=3)
        done = self.wait(self.request("POST", "/api/import", {"source": str(dcim), "shoot_name": "NYC"}))
        self.assertEqual(done["state"], "done", done.get("error"))

        # Before a check nothing is known, and browsing never changes the card.
        before = self.files(dcim)
        self.assertFalse(before["DSC00002.JPG"]["checked"])
        self.assertEqual(self.listing(self.card)["library"], "same-drive")

        scan = self.wait(self.request("POST", "/api/source/scan", {"path": str(self.card)}))
        self.assertEqual(scan["state"], "done", scan.get("error"))
        self.assertEqual(scan["result"]["total"], 4)

        top = self.files(dcim)
        self.assertEqual(top["DSC00001.JPG"]["library"]["shoot_name"], "NYC")
        self.assertEqual(top["DSC00001.JPG"]["copies"], 1)
        self.assertIsNone(top["DSC00001.JPG"]["duplicate_of"])
        selected = self.files(dcim / "select")["DSC00001.JPG"]
        self.assertEqual(selected["duplicate_of"], str(dcim / "DSC00001.JPG"))
        extra = self.files(self.card / "extra")["DSC00003.JPG"]
        self.assertTrue(extra["checked"])
        self.assertIsNone(extra["library"])
        self.assertEqual(self.listing(self.card)["drive"]["copies"], 1)

        preview = self.request("GET", f"/api/source/preview?path={urllib.parse.quote(str(dcim / 'DSC00002.JPG'))}")
        self.assertEqual(preview[:2], b"\xff\xd8")

        # Shortcuts to the duplicates go into <folder>\_trash; the files themselves stay put.
        links = self.request("POST", "/api/source/links", {"path": str(self.card)})
        self.assertEqual((links["links"], links["groups"]), (1, 1))
        trash = self.card / "_trash"
        shortcut = next(trash.rglob("DSC00001.JPG*"))
        self.assertEqual(shortcut.parent, trash / "DCIM" / "select")
        self.assertNotIn(b"\r\r\n", (trash / "DUPLICATES.txt").read_bytes())
        note = (trash / "DUPLICATES.txt").read_text(encoding="utf-8")
        self.assertIn(f"KEEP  {dcim / 'DSC00001.JPG'}", note)
        self.assertIn(f"dup {dcim / 'select' / 'DSC00001.JPG'}", note)
        self.assertTrue((dcim / "select" / "DSC00001.JPG").exists())
        self.assertNotIn("_trash", {item["name"] for item in self.listing(self.card)["files"]})

        # On the library drive, files the library already holds can go to the library trash, with a reason.
        nothing = self.request("POST", "/api/source/trash-copies", {"path": str(self.card / "extra")})
        self.assertEqual(nothing["moved"], 0)
        moved = self.request("POST", "/api/source/trash-copies", {"path": str(dcim / "select")})
        self.assertEqual(moved["moved"], 1)
        self.assertFalse((dcim / "select" / "DSC00001.JPG").exists())
        self.assertTrue((dcim / "DSC00001.JPG").exists())
        self.assertTrue((self.card / "extra" / "DSC00003.JPG").exists())
        why = (Path(moved["trash_folder"]) / "WHY.txt").read_text(encoding="utf-8")
        self.assertIn(str(dcim / "select" / "DSC00001.JPG"), why)
        self.assertIn("Same bytes as 2026/NYC/DSC00001.JPG", why)
        items = self.request("GET", "/api/trash")["items"]
        self.assertTrue(any(item["reason"] and "Same bytes as" in item["reason"] for item in items))

        with self.assertRaises(urllib.error.HTTPError) as inside:
            self.request("POST", "/api/source/trash-copies", {"path": str(self.library_root / "2026" / "NYC")})
        self.assertEqual(inside.exception.code, 400)

        # Checking a folder that contains the library never hashes, and so never trashes, the library's files.
        outer = self.wait(self.request("POST", "/api/source/scan", {"path": str(self.temp)}))
        self.assertEqual(outer["state"], "done", outer.get("error"))
        swept = self.request("POST", "/api/source/trash-copies", {"path": str(self.temp)})
        self.assertEqual(swept["moved"], 2)  # the two card originals; the library keeps its own files
        self.assertFalse((dcim / "DSC00002.JPG").exists())
        self.assertTrue((self.library_root / "2026" / "NYC" / "DSC00001.JPG").exists())
        self.assertTrue((self.library_root / "2026" / "NYC" / "DSC00002.JPG").exists())


if __name__ == "__main__":
    unittest.main()
