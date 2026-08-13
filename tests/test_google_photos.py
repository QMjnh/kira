from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google_photos import GooglePhotosService, PICKER_SCOPE, UPLOAD_SCOPE


class GooglePhotosServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_root = self.root / "data"
        self.app_root = self.root / "app"
        self.data_root.mkdir()
        self.app_root.mkdir()
        (self.app_root / "google-oauth-client.json").write_text(
            json.dumps({"installed": {"client_id": "client-id", "client_secret": "client-secret"}}),
            encoding="utf-8",
        )

    def service(self) -> GooglePhotosService:
        return GooglePhotosService(self.data_root, self.app_root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_oauth_start_uses_pkce_and_minimal_picker_upload_scopes(self) -> None:
        service = self.service()
        result = service.start_oauth("http://127.0.0.1:8787/api/google/oauth/callback")
        query = parse_qs(urlparse(result["authorization_url"]).query)
        self.assertEqual(query["client_id"], ["client-id"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(set(query["scope"][0].split()), {PICKER_SCOPE, UPLOAD_SCOPE})
        self.assertIn(query["state"][0], service.oauth_states)

    def test_token_storage_round_trip(self) -> None:
        service = self.service()
        token = {"access_token": "secret-access", "refresh_token": "secret-refresh", "expires_at": 12345}
        service._save_tokens(token)
        self.assertEqual(service._load_tokens(), token)
        self.assertNotIn("secret-refresh", service.token_path.read_text(encoding="utf-8"))

    def test_credentials_live_with_app_while_tokens_live_with_data(self) -> None:
        service = self.service()
        self.assertEqual(service.credentials_path, self.app_root / "google-oauth-client.json")
        self.assertEqual(service.token_path, self.data_root / "google-token.json")
        self.assertTrue(service.status()["configured"])

    def test_import_supports_photos_and_videos_and_keeps_changed_same_name(self) -> None:
        service = self.service()
        service._list_picked_items = lambda _session_id: [
            {"type": "PHOTO", "mediaFile": {"filename": "IMG_1.JPG", "baseUrl": "https://one"}},
            {"type": "PHOTO", "mediaFile": {"filename": "IMG_1.JPG", "baseUrl": "https://two"}},
            {"type": "VIDEO", "mediaFile": {"filename": "clip.mp4", "baseUrl": "https://video"}},
        ]
        service._access_token = lambda: "access-token"

        urls = []

        def download(url, _token, destination):
            urls.append(url)
            content = f"download-{len(urls)}".encode("utf-8")
            destination.write_bytes(content)
            return len(content), hashlib.sha256(content).hexdigest()

        service._download_file = download
        service._request_json = lambda *_args, **_kwargs: {}
        operation = service._new_operation("import", directory=service.inbox)
        service._run_import(operation["id"], "picker-session", service.inbox)
        finished = service.operation(operation["id"])

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["completed"], 3)
        self.assertEqual(finished["failed"], 0)
        self.assertEqual(finished["possible_edits"], 1)
        self.assertTrue((service.inbox / "IMG_1.JPG").exists())
        self.assertTrue((service.inbox / "IMG_1__google2.JPG").exists())
        self.assertTrue((service.inbox / "clip.mp4").exists())
        self.assertEqual(urls, ["https://one=d", "https://two=d", "https://video=dv"])

    def test_import_skips_byte_identical_media_even_when_name_differs(self) -> None:
        service = self.service()
        collection = self.root / "NYC"
        collection.mkdir()
        original = collection / "camera-name.JPG"
        original.write_bytes(b"same-image-bytes")
        service._list_picked_items = lambda _session_id: [
            {"type": "PHOTO", "mediaFile": {"filename": "phone-name.JPG", "baseUrl": "https://one"}}
        ]
        service._access_token = lambda: "access-token"

        def download(_url, _token, destination):
            content = b"same-image-bytes"
            destination.write_bytes(content)
            return len(content), hashlib.sha256(content).hexdigest()

        service._download_file = download
        service._request_json = lambda *_args, **_kwargs: {}
        operation = service._new_operation("import", directory=collection)
        service._run_import(operation["id"], "picker-session", collection)
        finished = service.operation(operation["id"])

        self.assertEqual(finished["duplicates"], 1)
        self.assertEqual(finished["completed"], 0)
        self.assertEqual(finished["files"][0]["classification"], "exact_duplicate")
        self.assertFalse((collection / "phone-name.JPG").exists())

    def test_upload_creates_google_media_item_after_sending_bytes(self) -> None:
        service = self.service()
        photo = self.root / "IMG_2.JPG"
        photo.write_bytes(b"jpeg")
        service._access_token = lambda: "access-token"
        service._upload_bytes = lambda path, token: f"upload:{path.name}:{token}"
        service._request_json = lambda *_args, **_kwargs: {
            "newMediaItemResults": [{"status": {}, "mediaItem": {"id": "google-media-id"}}]
        }
        operation = service._new_operation("upload", total=1)
        service._run_upload(operation["id"], [photo])
        finished = service.operation(operation["id"])

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["completed"], 1)
        self.assertEqual(finished["files"][0]["mediaItem"]["id"], "google-media-id")

    def test_upload_accepts_video(self) -> None:
        service = self.service()
        video = self.root / "clip.mp4"
        video.write_bytes(b"video")
        service._access_token = lambda: "access-token"
        service._upload_bytes = lambda path, token: f"upload:{path.name}:{token}"
        service._request_json = lambda *_args, **_kwargs: {"newMediaItemResults": [{"status": {}}]}
        operation = service._new_operation("upload", total=1)
        service._run_upload(operation["id"], [video])
        self.assertEqual(service.operation(operation["id"])["completed"], 1)


if __name__ == "__main__":
    unittest.main()
