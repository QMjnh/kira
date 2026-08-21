from __future__ import annotations

import base64
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from kira.google_photos import GooglePhotosService, PICKER_SCOPE, UPLOAD_SCOPE, _google_remote_hash


def _download_result(content: bytes) -> tuple[int, str, str]:
    """Mirror GooglePhotosService._download_file's (size, sha256, google-hash)."""
    return (
        len(content),
        hashlib.sha256(content).hexdigest(),
        base64.b64encode(hashlib.sha1(content).digest()).decode("ascii"),
    )


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

    def test_import_destination_defaults_to_inbox_and_accepts_external_absolute_path(self) -> None:
        service = self.service()
        self.assertEqual(service.resolve_import_destination(""), service.inbox)
        self.assertEqual(service.resolve_import_destination("NYC"), (service.inbox / "NYC").resolve())

        external = self.root / "outside-collection"
        self.assertEqual(service.resolve_import_destination(str(external)), external.resolve())
        self.assertTrue(external.is_dir())

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
            return _download_result(content)

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
            return _download_result(content)

        service._download_file = download
        service._request_json = lambda *_args, **_kwargs: {}
        operation = service._new_operation("import", directory=collection)
        service._run_import(operation["id"], "picker-session", collection)
        finished = service.operation(operation["id"])

        self.assertEqual(finished["duplicates"], 1)
        self.assertEqual(finished["completed"], 0)
        self.assertEqual(finished["files"][0]["classification"], "exact_duplicate")
        self.assertFalse((collection / "phone-name.JPG").exists())

    def test_zip_import_extracts_batch_and_removes_temporary_files(self) -> None:
        service = self.service()
        service._list_picked_items = lambda _session_id: [
            {
                "id": f"google-{index}",
                "createTime": f"2026-08-{index + 1:02d}T12:00:00Z",
                "type": "PHOTO",
                "mediaFile": {
                    "filename": f"IMG_{index}.JPG",
                    "baseUrl": f"https://photo-{index}",
                },
            }
            for index in range(3)
        ]
        service._access_token = lambda: "access-token"

        def download(url, _token, destination):
            content = f"bytes:{url}".encode("utf-8")
            destination.write_bytes(content)
            return _download_result(content)

        service._download_file = download
        service._request_json = lambda *_args, **_kwargs: {}
        operation = service._new_operation("import", directory=service.inbox)
        service._run_import(
            operation["id"],
            "picker-session",
            service.inbox,
            "zip",
            25,
        )
        finished = service.operation(operation["id"])

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["download_mode"], "zip")
        self.assertEqual(finished["completed"], 3)
        self.assertEqual(
            {path.name for path in service.inbox.iterdir()},
            {"IMG_0.JPG", "IMG_1.JPG", "IMG_2.JPG"},
        )
        self.assertEqual(
            {record["google_media_id"] for record in finished["files"]},
            {"google-0", "google-1", "google-2"},
        )

    def test_automatic_import_uses_files_below_custom_threshold(self) -> None:
        service = self.service()
        service._list_picked_items = lambda _session_id: [
            {"id": "one", "type": "PHOTO", "mediaFile": {"filename": "one.jpg", "baseUrl": "https://one"}}
        ]
        service._access_token = lambda: "access-token"

        def download(_url, _token, destination):
            destination.write_bytes(b"one")
            return _download_result(b"one")

        service._download_file = download
        service._request_json = lambda *_args, **_kwargs: {}
        operation = service._new_operation("import", directory=service.inbox)
        service._run_import(
            operation["id"],
            "picker-session",
            service.inbox,
            "automatic",
            10,
        )

        finished = service.operation(operation["id"])
        self.assertEqual(finished["download_mode"], "files")

    def test_automatic_album_resolves_picker_ids_to_web_media_keys_by_content(self) -> None:
        service = self.service()
        content = b"picker-original-bytes"
        expected_hash = base64.b64encode(hashlib.sha1(content).digest()).decode("ascii")
        service._list_picked_items = lambda _session_id: [
            {
                "id": "picker-only-AFc71b",
                "type": "PHOTO",
                "mediaFile": {"filename": "selected.jpg", "baseUrl": "https://selected"},
            }
        ]
        service._access_token = lambda: "access-token"

        def download(_url, _token, destination):
            destination.write_bytes(content)
            return _download_result(content)

        requested_hashes = []
        organized_keys = []

        def find_remote_matches(hashes):
            requested_hashes.extend(hashes)
            return [
                {
                    "content_hash": expected_hash,
                    "media_key": "web-media-key",
                    "dedup_key": "web-dedup-key",
                }
            ]

        def organize(media_keys, album_title, archive):
            organized_keys.extend(media_keys)
            return {
                "album": {"title": album_title, "media_key": "album-key"},
                "archived": archive,
                "items": [{"google_media_key": media_keys[0], "status": "complete"}],
            }

        service._download_file = download
        service._request_json = lambda *_args, **_kwargs: {}
        service.web = SimpleNamespace(
            find_remote_matches=find_remote_matches,
            organize=organize,
        )
        operation = service._new_operation("import", directory=service.inbox)
        service._run_import(
            operation["id"],
            "picker-session",
            service.inbox,
            album_title="Imported trip",
            archive=True,
        )
        finished = service.operation(operation["id"])

        self.assertEqual(requested_hashes, [expected_hash])
        self.assertEqual(organized_keys, ["web-media-key"])
        self.assertNotIn("picker-only-AFc71b", organized_keys)
        self.assertEqual(finished["organize_status"], "complete")
        self.assertEqual(finished["organize_matched"], 1)
        self.assertEqual(finished["organized"], 1)
        self.assertTrue(finished["archived"])

    def test_automatic_album_resolves_an_exact_duplicate_before_removing_download(self) -> None:
        service = self.service()
        content = b"already-downloaded-original"
        existing = service.inbox / "existing.jpg"
        existing.write_bytes(content)
        expected_hash = base64.b64encode(hashlib.sha1(content).digest()).decode("ascii")
        service._list_picked_items = lambda _session_id: [
            {
                "id": "picker-duplicate",
                "type": "PHOTO",
                "mediaFile": {"filename": "selected.jpg", "baseUrl": "https://selected"},
            }
        ]
        service._access_token = lambda: "access-token"

        def download(_url, _token, destination):
            destination.write_bytes(content)
            return _download_result(content)

        organized_keys = []
        service._download_file = download
        service._request_json = lambda *_args, **_kwargs: {}
        service.web = SimpleNamespace(
            find_remote_matches=lambda hashes: [
                {
                    "content_hash": hashes[0],
                    "media_key": "web-duplicate-key",
                    "dedup_key": "dedup-key",
                }
            ],
            organize=lambda media_keys, album_title, archive: (
                organized_keys.extend(media_keys)
                or {
                    "album": {"title": album_title, "media_key": "album-key"},
                    "archived": archive,
                    "items": [{"google_media_key": media_keys[0], "status": "complete"}],
                }
            ),
        )
        operation = service._new_operation("import", directory=service.inbox)
        service._run_import(
            operation["id"],
            "picker-session",
            service.inbox,
            album_title="Duplicates",
            archive=True,
        )
        finished = service.operation(operation["id"])

        self.assertEqual(finished["duplicates"], 1)
        self.assertEqual(finished["files"][0]["google_content_hash"], expected_hash)
        self.assertEqual(organized_keys, ["web-duplicate-key"])
        self.assertEqual(finished["organize_status"], "complete")

    def test_automatic_album_organizes_matches_and_reports_unmatched_downloads(self) -> None:
        service = self.service()
        contents = {"one.jpg": b"one", "two.jpg": b"two"}
        service._list_picked_items = lambda _session_id: [
            {
                "id": f"picker-{name}",
                "type": "PHOTO",
                "mediaFile": {"filename": name, "baseUrl": f"https://{name}"},
            }
            for name in contents
        ]
        service._access_token = lambda: "access-token"

        def download(url, _token, destination):
            name = url.removeprefix("https://").removesuffix("=d")
            content = contents[name]
            destination.write_bytes(content)
            return _download_result(content)

        first_hash = base64.b64encode(hashlib.sha1(contents["one.jpg"]).digest()).decode("ascii")
        organize_calls = []

        def organize(media_keys, album_title, archive):
            organize_calls.append(list(media_keys))
            return {
                "album": {"title": album_title, "media_key": "album-key"},
                "archived": archive,
                "items": [{"google_media_key": media_keys[0], "status": "complete"}],
            }

        service._download_file = download
        service._request_json = lambda *_args, **_kwargs: {}
        service.web = SimpleNamespace(
            find_remote_matches=lambda _hashes: [
                {"content_hash": first_hash, "media_key": "web-one", "dedup_key": "dedup-one"}
            ],
            find_visual_matches=lambda _paths: ([], []),
            organize=organize,
        )
        operation = service._new_operation("import", directory=service.inbox)
        service._run_import(
            operation["id"],
            "picker-session",
            service.inbox,
            album_title="Incomplete",
            archive=True,
        )
        finished = service.operation(operation["id"])

        self.assertEqual(organize_calls, [["web-one"]])
        self.assertEqual(finished["status"], "complete_with_errors")
        self.assertEqual(finished["organize_status"], "partial")
        self.assertEqual(finished["organize_matched"], 1)
        self.assertEqual(finished["organize_unmatched"], 1)
        self.assertEqual(finished["organized"], 1)

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

    def test_organize_operation_persists_album_and_archive_results(self) -> None:
        service = self.service()
        service.web = SimpleNamespace(
            organize=lambda media_keys, album_title, archive: {
                "album": {"title": album_title, "media_key": "album-1"},
                "archived": archive,
                "items": [
                    {
                        "google_media_key": media_key,
                        "google_dedup_key": f"dedup:{media_key}",
                        "album_added": True,
                        "archived": archive,
                        "status": "complete",
                    }
                    for media_key in media_keys
                ],
            }
        )
        operation = service._new_operation("organize", total=2)
        service._run_organize(operation["id"], ["media-1", "media-2"], "Trips", True)
        finished = service.operation(operation["id"])

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["completed"], 2)
        self.assertEqual(finished["album"], {"title": "Trips", "media_key": "album-1"})
        self.assertTrue(finished["archived"])
        self.assertEqual(finished["files"][0]["google_dedup_key"], "dedup:media-1")

    def test_match_folder_uses_file_content_hashes_then_organizes_matches(self) -> None:
        service = self.service()
        first = self.root / "first.jpg"
        second = self.root / "renamed.jpg"
        different = self.root / "different.jpg"
        first.write_bytes(b"same-content")
        second.write_bytes(b"same-content")
        different.write_bytes(b"different-content")
        expected_hash = _google_remote_hash(first)
        requested_hashes = []

        def find_remote_matches(hashes):
            requested_hashes.extend(hashes)
            return [
                {
                    "content_hash": expected_hash,
                    "media_key": "google-media-1",
                    "dedup_key": "google-dedup-1",
                }
            ]

        def organize(media_keys, album_title, archive, resolved_album=None):
            return {
                "album": {"title": album_title, "media_key": "album-1"},
                "archived": archive,
                "items": [
                    {
                        "google_media_key": media_keys[0],
                        "google_dedup_key": "google-dedup-1",
                        "album_added": True,
                        "archived": archive,
                        "status": "complete",
                    }
                ],
            }

        service.web = SimpleNamespace(
            find_remote_matches=find_remote_matches,
            find_visual_matches=lambda _paths: ([], []),
            ensure_album=lambda album_title: {
                "title": album_title,
                "media_key": "album-1",
                "shared": False,
            },
            organize=organize,
        )
        operation = service._new_operation("match_folder", total=3)
        service._run_match_folder(operation["id"], [first, second, different], "Local match", True)
        finished = service.operation(operation["id"])

        self.assertEqual(finished["status"], "complete")
        self.assertEqual(len(requested_hashes), 2)
        self.assertEqual(finished["matched"], 1)
        self.assertEqual(finished["matched_local_files"], 2)
        self.assertEqual(finished["unmatched"], 1)
        self.assertEqual(finished["files"][0]["local_files"], [str(first), str(second)])
        self.assertTrue(finished["archived"])

    def test_match_folder_logs_local_failure_and_batches_valid_matches(self) -> None:
        service = self.service()
        rejected = self.root / "rejected.jpg"
        successful = self.root / "successful.jpg"
        unreadable = self.root / "missing.jpg"
        rejected.write_bytes(b"rejected-content")
        successful.write_bytes(b"successful-content")
        rejected_hash = _google_remote_hash(rejected)
        successful_hash = _google_remote_hash(successful)
        organize_calls = []

        def organize(media_keys, album_title, archive, resolved_album=None):
            organize_calls.append(list(media_keys))
            return {
                "album": {"title": album_title, "media_key": "album-1"},
                "archived": archive,
                "items": [
                    {
                        "google_media_key": media_key,
                        "google_dedup_key": f"dedup:{media_key}",
                        "album_added": True,
                        "archived": archive,
                        "status": "complete",
                    }
                    for media_key in media_keys
                ],
            }

        service.web = SimpleNamespace(
            find_remote_matches=lambda _hashes: [
                {
                    "content_hash": rejected_hash,
                    "media_key": "google-rejected",
                    "dedup_key": "dedup-rejected",
                },
                {
                    "content_hash": successful_hash,
                    "media_key": "google-successful",
                    "dedup_key": "dedup-successful",
                },
            ],
            find_visual_matches=lambda _paths: ([], []),
            ensure_album=lambda album_title: {
                "title": album_title,
                "media_key": "album-1",
                "shared": False,
            },
            organize=organize,
        )
        operation = service._new_operation("match_folder", total=3)
        terminal = io.StringIO()
        with redirect_stdout(terminal):
            service._run_match_folder(
                operation["id"],
                [unreadable, rejected, successful],
                "Resilient album",
                True,
            )
        finished = service.operation(operation["id"])

        self.assertEqual(finished["status"], "complete_with_errors")
        self.assertEqual(finished["failed"], 1)
        self.assertEqual(finished["organized"], 2)
        self.assertEqual(organize_calls, [["google-rejected", "google-successful"]])
        self.assertIn("missing.jpg", terminal.getvalue())
        self.assertEqual(
            [item["stage"] for item in finished["files"] if item["status"] == "failed"],
            ["local_hash"],
        )


if __name__ == "__main__":
    unittest.main()
