from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from server import build_server


class KiraTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.server = build_server("127.0.0.1", 0, Path(self.temp.name))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.token = self.server.store.token

    def tearDown(self) -> None:
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        request_headers = {"X-Kira-Token": self.token}
        request_headers.update(headers or {})
        self.connection.request(method, path, body=body, headers=request_headers)
        response = self.connection.getresponse()
        data = response.read()
        return response, data

    def json_request(self, method: str, path: str, payload: dict):
        response, data = self.request(
            method,
            path,
            json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json"},
        )
        return response, json.loads(data)

    def create_job(self) -> str:
        response, payload = self.json_request("POST", "/api/jobs", {"name": "Transfer Test"})
        self.assertEqual(response.status, 201)
        return payload["id"]

    def test_dashboard_and_local_bootstrap_are_served(self) -> None:
        self.connection.request("GET", "/")
        response = self.connection.getresponse()
        html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("<title>Kira</title>", html)
        self.assertIn('id="job-pair-qr"', html)
        self.assertIn('id="job-pair-code"', html)
        self.assertIn('id="mark-unselect"', html)
        self.assertIn('id="group-selected"', html)
        self.assertIn('id="compare-group-select"', html)
        self.assertIn('id="select-all"', html)
        self.assertIn('id="clear-picked"', html)
        self.assertIn('id="google-connect"', html)
        self.assertIn('id="google-download-mode"', html)
        self.assertIn('id="google-zip-threshold"', html)
        self.assertIn('id="google-download-settings-toggle"', html)
        self.assertNotIn('id="google-finish"', html)
        self.assertIn('id="google-destination-folder"', html)
        self.assertIn('id="google-import-archive"', html)
        self.assertIn('id="google-pick"', html)
        self.assertIn('id="google-upload-selected"', html)
        self.assertIn('id="library-pane-left"', html)
        self.assertIn('id="library-pane-right"', html)
        self.assertIn('id="left-folder-path"', html)
        self.assertIn('id="right-folder-path"', html)
        self.assertIn('>Open folder</button>', html)
        self.assertIn('>Compare a folder</button>', html)
        self.assertIn('id="close-folder-browser"', html)
        self.assertIn('id="move-selected-left"', html)
        self.assertIn('id="move-selected-right"', html)

        self.connection.request("GET", "/app.js")
        response = self.connection.getresponse()
        script = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("CHUNK_SIZE", script)
        self.assertNotIn("slice(0, 4)", script)

        self.connection.request("GET", "/api/bootstrap")
        response = self.connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["token"], self.token)
        self.assertRegex(payload["pair_code"], r"^\d{6}$")
        self.assertIn("?pair=", payload["pair_url"])

        self.connection.request("GET", "/api/pair-qr.svg")
        response = self.connection.getresponse()
        qr_svg = response.read()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/svg+xml; charset=utf-8")
        self.assertIn(b"<svg", qr_svg)

    def test_google_photos_routes_use_local_server_side_service(self) -> None:
        source = Path(self.temp.name) / "google-upload-source"
        source.mkdir()
        (source / "IMG_9000.JPG").write_bytes(b"jpeg-for-google")
        (source / "clip.mp4").write_bytes(b"video-for-google")
        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        assets = json.loads(raw)["assets"]
        photo_asset = next(asset for asset in assets if asset["jpeg_files"])
        video_asset = next(asset for asset in assets if asset["video_files"])

        class FakeGooglePhotos:
            def __init__(self):
                self.uploaded = []
                self.finished = None
                self.import_destination = None
                self.web_session = None
                self.organized = None
                self.matched_folder = None

            def status(self):
                return {
                    "configured": True,
                    "connected": True,
                    "inbox": "fake-inbox",
                    "organizer": {"available": True, "connected": True},
                }

            def start_oauth(self, redirect_uri):
                return {"authorization_url": f"https://accounts.example/auth?redirect={redirect_uri}"}

            def finish_oauth(self, code, state):
                self.finished = (code, state)

            def create_picker_session(self, max_items):
                return {"id": "picker-1", "pickerUri": "https://photos.example/pick", "max": max_items}

            def picker_session(self, session_id):
                return {"id": session_id, "mediaItemsSet": True}

            def resolve_import_destination(self, folder):
                return Path(folder).resolve() if folder else None

            def start_import(self, session_id, destination=None, download_mode=None, zip_threshold=None, album_title=None, archive=None):
                self.import_destination = destination
                return {
                    "id": "import-1",
                    "kind": "import",
                    "session_id": session_id,
                    "download_mode": download_mode,
                    "zip_threshold": zip_threshold,
                }

            def start_upload(self, paths):
                self.uploaded = paths
                return {"id": "upload-1", "kind": "upload"}

            def operation(self, operation_id):
                return {
                    "id": operation_id,
                    "kind": "organize" if operation_id == "organize-1" else "upload",
                    "status": "complete",
                    "completed": 1,
                    "duplicates": 0,
                    "failed": 0,
                }

            def disconnect(self):
                return {"connected": False}

            def import_web_session(self, cookies_path, account_index):
                self.web_session = (cookies_path, account_index)
                return {"available": True, "connected": True, "account_index": account_index}

            def disconnect_web_session(self):
                return {"available": True, "connected": False}

            def albums(self):
                return {"albums": [{"media_key": "album-1", "title": "Trips"}]}

            def start_organize(self, media_ids, album_title, archive):
                self.organized = (media_ids, album_title, archive)
                return {"id": "organize-1", "kind": "organize", "status": "starting"}

            def start_match_folder(self, paths, album_title, archive):
                self.matched_folder = (paths, album_title, archive)
                return {"id": "match-1", "kind": "match_folder", "status": "complete"}

        fake = FakeGooglePhotos()
        self.server.google_photos = fake

        response, status = self.request("GET", "/api/google/status")
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(status)["connected"])

        response, oauth = self.json_request("POST", "/api/google/oauth/start", {})
        self.assertEqual(response.status, 200)
        self.assertIn(f"redirect=http://127.0.0.1:{self.server.server_address[1]}", oauth["authorization_url"])

        self.connection.request("GET", "/?code=oauth-code&state=oauth-state")
        callback = self.connection.getresponse()
        callback_html = callback.read().decode("utf-8")
        self.assertEqual(callback.status, 200)
        self.assertIn("Google Photos connected", callback_html)
        self.assertEqual(fake.finished, ("oauth-code", "oauth-state"))

        response, picker = self.json_request("POST", "/api/google/picker/sessions", {"max_items": 25})
        self.assertEqual(response.status, 201)
        self.assertEqual(picker["max"], 25)

        response, session = self.request("GET", "/api/google/picker/sessions/picker-1")
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(session)["mediaItemsSet"])

        response, imported = self.json_request(
            "POST",
            "/api/google/imports",
            {
                "session_id": "picker-1",
                "destination_folder": str(source),
                "download_mode": "zip",
                "zip_threshold": 40,
                "archive": False,
            },
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(imported["id"], "import-1")
        self.assertEqual(imported["download_mode"], "zip")
        self.assertEqual(fake.import_destination, source.resolve())

        response, uploaded = self.json_request(
            "POST",
            "/api/google/uploads",
            {"source_directory": str(source), "asset_ids": [photo_asset["id"], video_asset["id"]]},
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(uploaded["id"], "upload-1")
        self.assertEqual(set(fake.uploaded), {source / "IMG_9000.JPG", source / "clip.mp4"})

        response, web_session = self.json_request(
            "POST",
            "/api/google/web-session",
            {"cookies_path": str(source / "cookies.txt"), "account_index": 2},
        )
        self.assertEqual(response.status, 200)
        self.assertTrue(web_session["connected"])
        self.assertEqual(fake.web_session, (source / "cookies.txt", 2))

        response, invalid_account = self.json_request(
            "POST",
            "/api/google/web-session",
            {"cookies_path": str(source / "cookies.txt"), "account_index": None},
        )
        self.assertEqual(response.status, 400)
        self.assertIn("account_index", invalid_account["error"])

        response, albums = self.request("GET", "/api/google/albums")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(albums)["albums"][0]["title"], "Trips")

        response, matched = self.json_request(
            "POST",
            "/api/google/match-folder",
            {"source_directory": str(source), "album_title": "Trips", "archive": True},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(matched["id"], "match-1")
        self.assertEqual(matched["status"], "complete")
        self.assertEqual(fake.matched_folder[1:], ("Trips", True))
        self.assertEqual(set(fake.matched_folder[0]), {source / "IMG_9000.JPG", source / "clip.mp4"})

        response, organized = self.json_request(
            "POST",
            "/api/google/organize",
            {"media_ids": ["media-1"], "album_title": "Trips", "archive": True},
        )
        self.assertEqual(response.status, 202)
        self.assertEqual(organized["id"], "organize-1")
        self.assertEqual(fake.organized, (["media-1"], "Trips", True))

        response, invalid = self.json_request(
            "POST",
            "/api/google/organize",
            {"media_ids": ["media-1"], "album_title": "Trips", "archive": "false"},
        )
        self.assertEqual(response.status, 400)
        self.assertIn("boolean", invalid["error"])

        response, operation = self.request("GET", "/api/google/operations/upload-1")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(operation)["status"], "complete")

    def test_google_import_name_variants_stay_in_one_asset_group(self) -> None:
        source = Path(self.temp.name) / "mixed-collection"
        source.mkdir()
        (source / "IMG_1000.JPG").write_bytes(b"local")
        (source / "IMG_1000__google2.JPG").write_bytes(b"google-edit")
        (source / "IMG_1000-final.JPG").write_bytes(b"final-edit")
        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        scan = json.loads(raw)

        self.assertEqual(response.status, 200)
        self.assertEqual(len(scan["assets"]), 1)
        self.assertEqual(len(scan["assets"][0]["jpeg_files"]), 3)

    def upload(self, job_id: str, kind: str, filename: str, content: bytes, last_modified: int = 1):
        response, started = self.json_request(
            "POST",
            f"/api/jobs/{job_id}/uploads/start",
            {"kind": kind, "filename": filename, "size": len(content), "last_modified": last_modified},
        )
        self.assertEqual(response.status, 200)
        upload_id = started["upload_id"]
        split = max(1, len(content) // 2)
        offset = started["offset"]
        for chunk in (content[offset:split], content[max(offset, split):]):
            if not chunk:
                continue
            response, raw = self.request(
                "PUT",
                f"/api/jobs/{job_id}/uploads/{upload_id}/chunk?offset={offset}",
                chunk,
                {"Content-Type": "application/octet-stream"},
            )
            self.assertEqual(response.status, 200, raw)
            offset = json.loads(raw)["offset"]
        response, completed = self.json_request(
            "POST", f"/api/jobs/{job_id}/uploads/{upload_id}/complete", {}
        )
        self.assertEqual(response.status, 200)
        return completed

    def test_pairing_rejects_wrong_code_and_accepts_current_code(self) -> None:
        self.connection.request(
            "POST",
            "/api/pair",
            body=b'{"code":"000000"}',
            headers={"Content-Type": "application/json"},
        )
        wrong = self.connection.getresponse()
        wrong.read()
        self.assertEqual(wrong.status, 401)

        self.connection.request(
            "POST",
            "/api/pair",
            body=json.dumps({"code": self.server.store.pair_code}),
            headers={"Content-Type": "application/json"},
        )
        correct = self.connection.getresponse()
        payload = json.loads(correct.read())
        self.assertEqual(correct.status, 200)
        self.assertEqual(payload["token"], self.token)

        self.connection.request(
            "POST",
            "/api/pair",
            body=json.dumps({"secret": self.server.store.pair_secret}),
            headers={"Content-Type": "application/json"},
        )
        scanned = self.connection.getresponse()
        payload = json.loads(scanned.read())
        self.assertEqual(scanned.status, 200)
        self.assertEqual(payload["token"], self.token)

    def test_source_round_trip_range_and_bundle(self) -> None:
        source = Path(self.temp.name) / "range-source"
        source.mkdir()
        content = (b"raw-photo-bytes-" * 1000) + b"end"
        (source / "IMG_4218.CR3").write_bytes(content)

        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        self.assertEqual(response.status, 200)
        asset = json.loads(raw)["assets"][0]
        response, job = self.json_request(
            "POST",
            "/api/jobs/from-selection",
            {
                "name": "Range Test",
                "source_directory": str(source),
                "selected_ids": [asset["id"]],
                "source_format": "raw",
            },
        )
        self.assertEqual(response.status, 201)
        job_id = job["id"]
        record = self.server.store.get_job(job_id)["files"][0]

        response, downloaded = self.request(
            "GET",
            f"/api/jobs/{job_id}/files/{record['id']}/download",
            headers={"Range": "bytes=10-99"},
        )
        self.assertEqual(response.status, 206)
        self.assertEqual(downloaded, content[10:100])

        response, bundle = self.request("GET", f"/api/jobs/{job_id}/bundle.zip")
        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            self.assertEqual(archive.namelist(), ["IMG_4218.CR3"])
            self.assertEqual(archive.read("IMG_4218.CR3"), content)

    def test_return_matches_original_by_filename_stem(self) -> None:
        source = Path(self.temp.name) / "match-source"
        source.mkdir()
        (source / "IMG_4218.CR3").write_bytes(b"raw-data")

        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        self.assertEqual(response.status, 200)
        asset = json.loads(raw)["assets"][0]
        response, job = self.json_request(
            "POST",
            "/api/jobs/from-selection",
            {
                "name": "Match Test",
                "source_directory": str(source),
                "selected_ids": [asset["id"]],
                "source_format": "raw",
            },
        )
        self.assertEqual(response.status, 201)
        job_id = job["id"]
        original = self.server.store.get_job(job_id)["files"][0]

        returned = self.upload(job_id, "returns", "IMG_4218.jpg", b"edited-jpeg", last_modified=2)
        self.assertEqual(returned["match_status"], "matched")
        self.assertEqual(returned["matched_file_id"], original["id"])

        response, body = self.request("GET", f"/api/jobs/{job_id}")
        self.assertEqual(response.status, 200)
        manifest = json.loads(body)
        self.assertEqual(manifest["returns"][0]["sha256"], returned["sha256"])

    def test_resume_reports_existing_offset(self) -> None:
        job_id = self.create_job()
        content = b"0123456789"
        _, started = self.json_request(
            "POST",
            f"/api/jobs/{job_id}/uploads/start",
            {"kind": "returns", "filename": "resume.nef", "size": len(content), "last_modified": 9},
        )
        upload_id = started["upload_id"]
        response, _ = self.request(
            "PUT",
            f"/api/jobs/{job_id}/uploads/{upload_id}/chunk?offset=0",
            content[:4],
            {"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status, 200)
        _, resumed = self.json_request(
            "POST",
            f"/api/jobs/{job_id}/uploads/start",
            {"kind": "returns", "filename": "resume.nef", "size": len(content), "last_modified": 9},
        )
        self.assertEqual(resumed["offset"], 4)

    def test_stale_chunk_is_rejected_without_corrupting_next_request(self) -> None:
        job_id = self.create_job()
        _, started = self.json_request(
            "POST",
            f"/api/jobs/{job_id}/uploads/start",
            {"kind": "returns", "filename": "conflict.cr3", "size": 8, "last_modified": 10},
        )
        upload_id = started["upload_id"]
        response, _ = self.request(
            "PUT",
            f"/api/jobs/{job_id}/uploads/{upload_id}/chunk?offset=0",
            b"1234",
            {"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status, 200)

        response, body = self.request(
            "PUT",
            f"/api/jobs/{job_id}/uploads/{upload_id}/chunk?offset=0",
            b"xx",
            {"Content-Type": "application/octet-stream"},
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(json.loads(body)["expected_offset"], 4)

        response, resumed = self.json_request(
            "POST",
            f"/api/jobs/{job_id}/uploads/start",
            {"kind": "returns", "filename": "conflict.cr3", "size": 8, "last_modified": 10},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(resumed["offset"], 4)

    def test_source_folder_organization_zip_extraction_and_job_deletion(self) -> None:
        source = Path(self.temp.name) / "camera-folder"
        source.mkdir()
        (source / "IMG_0001.CR3").write_bytes(b"selected-raw")
        (source / "IMG_0001.JPG").write_bytes(b"selected-camera-jpeg")
        (source / "IMG_0002.CR3").write_bytes(b"unselected-raw")
        (source / "IMG_0002.JPG").write_bytes(b"unselected-camera-jpeg")

        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        self.assertEqual(response.status, 200)
        scan = json.loads(raw)
        self.assertEqual(len(scan["assets"]), 2)
        first = scan["assets"][0]
        self.assertEqual(first["stem"], "IMG_0001")
        self.assertEqual(len(first["raw_files"]), 1)
        self.assertEqual(len(first["jpeg_files"]), 1)

        response, job = self.json_request(
            "POST",
            "/api/jobs/from-selection",
            {
                "name": "Organize Test",
                "source_directory": str(source),
                "selected_ids": [first["id"]],
                "source_format": "raw",
            },
        )
        self.assertEqual(response.status, 201)
        job_id = job["id"]
        manifest = self.server.store.get_job(job_id)
        self.assertEqual([item["filename"] for item in manifest["files"]], ["IMG_0001.CR3"])
        self.assertEqual(manifest["edit_source_format"], "raw")
        self.assertTrue(manifest["files"][0]["referenced"])
        self.assertEqual(Path(manifest["files"][0]["source_path"]), source / "IMG_0001.CR3")
        self.assertIsNone(manifest["files"][0]["sha256"])

        lightroom_zip = BytesIO()
        with zipfile.ZipFile(lightroom_zip, "w") as archive:
            archive.writestr("Lightroom exports/IMG_0001.jpg", b"lightroom-edit")
            archive.writestr("Lightroom exports/notes.txt", b"not a photo")
        self.upload(job_id, "returns", "Lightroom-exports.zip", lightroom_zip.getvalue(), last_modified=20)

        response, result = self.json_request("POST", f"/api/jobs/{job_id}/organize", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(result["status"], "complete")
        self.assertEqual((source / "selected" / "raw" / "IMG_0001.CR3").read_bytes(), b"selected-raw")
        self.assertEqual((source / "selected" / "raw" / "IMG_0002.CR3").read_bytes(), b"unselected-raw")
        self.assertEqual(
            (source / "selected" / "pre-edit" / "IMG_0001.JPG").read_bytes(),
            b"selected-camera-jpeg",
        )
        self.assertEqual((source / "unselected_jpeg" / "IMG_0002.JPG").read_bytes(), b"unselected-camera-jpeg")
        self.assertEqual(
            (source / "selected" / "IMG_0001.jpg").read_bytes(),
            b"lightroom-edit",
        )
        report = source / f"KIRA-ORGANIZED-{job_id}.json"
        self.assertTrue(report.exists())

        # Repeating the action must not create duplicate files.
        response, repeated = self.json_request("POST", f"/api/jobs/{job_id}/organize", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(repeated["report_path"], str(report))
        self.assertFalse((source / "selected" / "IMG_0001__v2.jpg").exists())

        job_folder = self.server.store._job_dir(job_id)
        response, deleted_raw = self.request("DELETE", f"/api/jobs/{job_id}")
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(deleted_raw)["deleted"])
        self.assertFalse(job_folder.exists())
        self.assertTrue((source / "selected" / "raw" / "IMG_0001.CR3").exists())
        self.assertTrue((source / "selected" / "IMG_0001.jpg").exists())
        self.assertTrue(report.exists())

    def test_local_move_transfers_a_photo_group_between_open_folders(self) -> None:
        source = Path(self.temp.name) / "source"
        destination = Path(self.temp.name) / "destination"
        source.mkdir()
        destination.mkdir()
        (source / "IMG_0007.CR3").write_bytes(b"raw")
        (source / "IMG_0007.JPG").write_bytes(b"preview")
        (source / "IMG_0007.MP4").write_bytes(b"video")

        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        self.assertEqual(response.status, 200)
        asset = json.loads(raw)["assets"][0]
        response, moved = self.json_request(
            "POST",
            "/api/local/move",
            {
                "source_directory": str(source),
                "destination_directory": str(destination),
                "asset_ids": [asset["id"]],
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(moved["moved_assets"], 1)
        self.assertEqual(len(moved["source"]["assets"]), 0)
        self.assertEqual(moved["destination"]["assets"][0]["stem"], "IMG_0007")
        self.assertEqual((destination / "IMG_0007.CR3").read_bytes(), b"raw")
        self.assertEqual((destination / "IMG_0007.JPG").read_bytes(), b"preview")
        self.assertEqual((destination / "IMG_0007.MP4").read_bytes(), b"video")

    def test_local_move_rejects_destination_collisions_without_partial_move(self) -> None:
        source = Path(self.temp.name) / "source-collision"
        destination = Path(self.temp.name) / "destination-collision"
        source.mkdir()
        destination.mkdir()
        (source / "IMG_0008.JPG").write_bytes(b"source")
        (destination / "IMG_0008.JPG").write_bytes(b"destination")

        _, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        asset = json.loads(raw)["assets"][0]
        response, body = self.json_request(
            "POST",
            "/api/local/move",
            {
                "source_directory": str(source),
                "destination_directory": str(destination),
                "asset_ids": [asset["id"]],
            },
        )
        self.assertEqual(response.status, 409)
        self.assertIn("already exists", body["error"])
        self.assertEqual((source / "IMG_0008.JPG").read_bytes(), b"source")
        self.assertEqual((destination / "IMG_0008.JPG").read_bytes(), b"destination")

    def test_jpeg_edit_source_is_referenced_and_bundled_without_copy(self) -> None:
        source = Path(self.temp.name) / "jpeg-source"
        source.mkdir()
        (source / "DSC_0100.ARW").write_bytes(b"raw-original")
        (source / "DSC_0100.JPG").write_bytes(b"camera-jpeg")

        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        self.assertEqual(response.status, 200)
        asset = json.loads(raw)["assets"][0]
        response, job = self.json_request(
            "POST",
            "/api/jobs/from-selection",
            {
                "name": "JPEG source",
                "source_directory": str(source),
                "selected_ids": [asset["id"]],
                "source_format": "jpeg",
            },
        )
        self.assertEqual(response.status, 201)
        manifest = self.server.store.get_job(job["id"])
        self.assertEqual(manifest["files"][0]["filename"], "DSC_0100.JPG")
        self.assertEqual(manifest["files"][0]["source_path"], str(source / "DSC_0100.JPG"))
        self.assertIsNone(manifest["files"][0]["sha256"])

        response, bundle = self.request("GET", f"/api/jobs/{job['id']}/bundle.zip")
        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            self.assertEqual(archive.namelist(), ["DSC_0100.JPG"])
            self.assertEqual(archive.read("DSC_0100.JPG"), b"camera-jpeg")
        verified = self.server.store.get_job(job["id"])
        self.assertEqual(
            verified["files"][0]["sha256"],
            __import__("hashlib").sha256(b"camera-jpeg").hexdigest(),
        )

    def test_video_only_selection_is_bundled_for_ipad(self) -> None:
        source = Path(self.temp.name) / "video-source"
        source.mkdir()
        (source / "NYC_0001.MOV").write_bytes(b"original-video")

        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        self.assertEqual(response.status, 200)
        asset = json.loads(raw)["assets"][0]
        self.assertEqual(asset["video_files"][0]["filename"], "NYC_0001.MOV")
        response, job = self.json_request(
            "POST",
            "/api/jobs/from-selection",
            {
                "name": "NYC video",
                "source_directory": str(source),
                "selected_ids": [asset["id"]],
                "source_format": "jpeg",
            },
        )
        self.assertEqual(response.status, 201)
        response, bundle = self.request("GET", f"/api/jobs/{job['id']}/bundle.zip")
        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(BytesIO(bundle)) as archive:
            self.assertEqual(archive.namelist(), ["NYC_0001.MOV"])
            self.assertEqual(archive.read("NYC_0001.MOV"), b"original-video")

    def test_returned_individual_edit_is_stored_in_source_and_survives_job_deletion(self) -> None:
        source = Path(self.temp.name) / "individual-return"
        source.mkdir()
        (source / "IMG_1000.JPG").write_bytes(b"pre-edit")
        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        asset = json.loads(raw)["assets"][0]
        response, job = self.json_request(
            "POST",
            "/api/jobs/from-selection",
            {
                "name": "Individual return",
                "source_directory": str(source),
                "selected_ids": [asset["id"]],
                "source_format": "jpeg",
            },
        )
        self.assertEqual(response.status, 201)
        returned = self.upload(job["id"], "returns", "IMG_1000.jpg", b"edited", last_modified=21)
        edited = source / "selected" / returned["filename"]
        self.assertEqual(Path(returned["storage_path"]), edited)
        self.assertEqual(edited.read_bytes(), b"edited")

        response, result = self.json_request("POST", f"/api/jobs/{job['id']}/organize", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(result["status"], "complete")
        self.assertFalse((source / "selected" / "IMG_1000__v2.jpg").exists())

        response, _ = self.request("DELETE", f"/api/jobs/{job['id']}")
        self.assertEqual(response.status, 200)
        self.assertEqual(edited.read_bytes(), b"edited")

    def test_preview_endpoint_returns_cached_thumbnail(self) -> None:
        source = Path(self.temp.name) / "large-preview.JPG"
        image = Image.new("RGB", (3000, 2000), color=(43, 91, 137))
        image.save(source, format="JPEG", quality=96)

        response, thumbnail = self.request(
            "GET",
            f"/api/local/preview?path={quote(str(source))}&size=480",
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
        self.assertIn("max-age=3600", response.getheader("Cache-Control"))
        with Image.open(BytesIO(thumbnail)) as preview:
            self.assertLessEqual(max(preview.size), 480)

        cached = list(self.server.store.thumbnails_root.glob("*.jpg"))
        self.assertEqual(len(cached), 1)

    def test_clickable_breadcrumbs_and_reversible_culling_folders(self) -> None:
        source = Path(self.temp.name) / "trip" / "inbox"
        source.mkdir(parents=True)
        for stem in ("BURST_1", "BURST_2"):
            (source / f"{stem}.ARW").write_bytes(f"raw-{stem}".encode())
            (source / f"{stem}.JPG").write_bytes(f"jpeg-{stem}".encode())

        response, raw = self.request("GET", f"/api/local/browse?path={quote(str(source))}")
        self.assertEqual(response.status, 200)
        browse = json.loads(raw)
        names = [crumb["name"] for crumb in browse["breadcrumbs"]]
        self.assertIn("trip", names)
        self.assertIn("inbox", names)
        self.assertEqual(browse["breadcrumbs"][-1]["path"], str(source))

        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        scan = json.loads(raw)
        ids = [asset["id"] for asset in scan["assets"]]
        response, grouped = self.json_request(
            "POST",
            "/api/local/cull",
            {
                "source_directory": str(source),
                "asset_ids": ids,
                "action": "group",
                "group_name": "Bridge burst",
            },
        )
        self.assertEqual(response.status, 200)
        group = source / "compare_groups" / "Bridge burst"
        self.assertEqual(grouped["destination"], str(group))
        self.assertEqual(grouped["destination_group_name"], "Bridge burst")
        bridge_folder = next(folder for folder in grouped["culling"]["folders"] if folder["role"] == "group")
        self.assertEqual(bridge_folder["group_name"], "Bridge burst")
        self.assertTrue((group / "BURST_1.ARW").exists())
        self.assertTrue((group / "BURST_1.JPG").exists())

        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(group))}")
        group_scan = json.loads(raw)
        first, second = group_scan["assets"]
        response, selected = self.json_request(
            "POST",
            "/api/local/cull",
            {
                "source_directory": str(group),
                "asset_ids": [first["id"]],
                "action": "select",
            },
        )
        self.assertEqual(response.status, 200)
        selected_stem = first["stem"]
        self.assertTrue((source / "select" / f"{selected_stem}.ARW").exists())
        self.assertTrue((source / "select" / f"{selected_stem}.JPG").exists())

        response, eliminated = self.json_request(
            "POST",
            "/api/local/cull",
            {
                "source_directory": str(group),
                "asset_ids": [second["id"]],
                "action": "unselect",
            },
        )
        self.assertEqual(response.status, 200)
        eliminated_stem = second["stem"]
        unselect = source / "unselect"
        self.assertTrue(eliminated["group_deleted"])
        self.assertEqual(eliminated["directory"], str(source))
        self.assertFalse(group.exists())
        self.assertTrue((unselect / f"{eliminated_stem}.ARW").exists())
        self.assertTrue((unselect / f"{eliminated_stem}.JPG").exists())

        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(unselect))}")
        eliminated_asset = json.loads(raw)["assets"][0]
        response, restored = self.json_request(
            "POST",
            "/api/local/cull",
            {
                "source_directory": str(unselect),
                "asset_ids": [eliminated_asset["id"]],
                "action": "restore",
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(restored["destination"], str(source))
        self.assertTrue((source / f"{eliminated_stem}.ARW").exists())
        self.assertTrue((source / f"{eliminated_stem}.JPG").exists())

    def test_culling_deletes_source_when_pixel_identical_photo_already_exists(self) -> None:
        source = Path(self.temp.name) / "visual-duplicates"
        group = source / "compare_groups" / "baker"
        group.mkdir(parents=True)
        image = Image.new("RGB", (24, 18), (45, 120, 80))
        inbox_exif = Image.Exif()
        inbox_exif[0x010E] = "inbox metadata"
        group_exif = Image.Exif()
        group_exif[0x010E] = "group metadata"
        inbox_photo = source / "IMG_100.JPG"
        grouped_photo = group / "renamed-copy.JPG"
        image.save(inbox_photo, format="JPEG", quality=96, exif=inbox_exif)
        image.save(grouped_photo, format="JPEG", quality=96, exif=group_exif)
        self.assertNotEqual(inbox_photo.read_bytes(), grouped_photo.read_bytes())

        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        asset = json.loads(raw)["assets"][0]
        response, result = self.json_request(
            "POST",
            "/api/local/cull",
            {
                "source_directory": str(source),
                "asset_ids": [asset["id"]],
                "action": "group",
                "group_name": "baker",
            },
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(result["moved_assets"], 0)
        self.assertEqual(result["duplicate_assets"], 1)
        self.assertEqual(len(result["duplicate_files"]), 1)
        self.assertEqual(result["deleted_duplicate_files"], 1)
        self.assertFalse(inbox_photo.exists())
        self.assertTrue(grouped_photo.exists())

    def test_culling_preserves_edited_same_name_photo_as_a_variant(self) -> None:
        source = Path(self.temp.name) / "same-name-edits"
        group = source / "compare_groups" / "baker"
        group.mkdir(parents=True)
        inbox_photo = source / "IMG_200.JPG"
        grouped_photo = group / "IMG_200.JPG"
        Image.new("RGB", (24, 18), (180, 40, 40)).save(inbox_photo, format="JPEG", quality=96)
        Image.new("RGB", (24, 18), (40, 40, 180)).save(grouped_photo, format="JPEG", quality=96)

        response, raw = self.request("GET", f"/api/local/scan?path={quote(str(source))}")
        asset = json.loads(raw)["assets"][0]
        response, result = self.json_request(
            "POST",
            "/api/local/cull",
            {
                "source_directory": str(source),
                "asset_ids": [asset["id"]],
                "action": "group",
                "group_name": "baker",
            },
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(result["moved_assets"], 1)
        self.assertEqual(result["duplicate_assets"], 0)
        self.assertEqual(result["renamed_files"][0]["destination"], "IMG_200__variant2.JPG")
        self.assertFalse(inbox_photo.exists())
        self.assertTrue(grouped_photo.exists())
        self.assertTrue((group / "IMG_200__variant2.JPG").exists())


if __name__ == "__main__":
    unittest.main()
