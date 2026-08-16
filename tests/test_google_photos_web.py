from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from google_photos_web import GooglePhotosWebError, GooglePhotosWebService


class FakePayload:
    counter = 0

    def __init__(self, kind: str, **values: object) -> None:
        type(self).counter += 1
        self.kind = kind
        self.payload_id = f"payload-{type(self).counter}"
        for key, value in values.items():
            setattr(self, key, value)


class FakePayloads:
    @staticmethod
    def GetAlbumsPage(page_id=None, page_size=100):
        return FakePayload("albums", page_id=page_id, page_size=page_size)

    @staticmethod
    def GetItemInfo(media_key):
        return FakePayload("info", media_key=media_key)

    @staticmethod
    def GetItemInfoExt(media_key):
        return FakePayload("info_ext", media_key=media_key)

    @staticmethod
    def AddItemsToExistingAlbum(media_keys, album_media_key):
        return FakePayload("add_existing", media_keys=media_keys, album_media_key=album_media_key)

    @staticmethod
    def AddItemsToExistingSharedAlbum(media_keys, album_media_key):
        return FakePayload("add_shared", media_keys=media_keys, album_media_key=album_media_key)

    @staticmethod
    def AddItemsToNewAlbum(media_keys, album_title):
        return FakePayload("add_new", media_keys=media_keys, album_title=album_title)

    @staticmethod
    def CreateAlbum(album_title):
        return FakePayload("create_album", album_title=album_title)

    @staticmethod
    def SetArchive(dedup_keys):
        return FakePayload("archive", dedup_keys=dedup_keys)

    @staticmethod
    def GetRemoteMatchesByHash(hashes):
        return FakePayload("remote_matches", hashes=hashes)


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeClient:
    instances = []
    include_existing_album = True
    duplicate_existing_album = False
    existing_album_shared = False
    membership_visibility_delay = 0
    archive_visibility_delay = 0

    def __init__(self, cookies_path, account_index=0) -> None:
        self.cookies = Path(cookies_path).read_text(encoding="utf-8")
        self.account_index = account_index
        self.global_data = {"oPEP7c": "photographer@example.com"}
        self.session = FakeSession()
        self.added_album = False
        self.album_title = "Trips"
        self.album_exists = type(self).include_existing_album
        self.archived = False
        self.calls = []
        self.request_batches = []
        type(self).instances.append(self)

    def send_api_request(self, payloads):
        many = isinstance(payloads, list)
        values = payloads if many else [payloads]
        self.request_batches.append([payload.kind for payload in values])
        responses = []
        for payload in values:
            self.calls.append(payload)
            if payload.kind == "albums":
                data = SimpleNamespace(
                    items=(
                        [
                            SimpleNamespace(
                                media_key="album-1",
                                title=self.album_title,
                                item_count=4,
                                is_shared=type(self).existing_album_shared,
                            )
                        ]
                        + (
                            [
                                SimpleNamespace(
                                    media_key="album-2",
                                    title=self.album_title,
                                    item_count=2,
                                    is_shared=False,
                                )
                            ]
                            if type(self).duplicate_existing_album
                            else []
                        )
                    )
                    if self.album_exists
                    else [],
                    next_page_id=None,
                )
            elif payload.kind == "info":
                archived = self.archived
                if archived and type(self).archive_visibility_delay:
                    type(self).archive_visibility_delay -= 1
                    archived = False
                data = SimpleNamespace(
                    dedup_key=f"dedup:{payload.media_key}",
                    is_archived=archived,
                )
            elif payload.kind == "info_ext":
                album_visible = self.added_album
                if album_visible and type(self).membership_visibility_delay:
                    type(self).membership_visibility_delay -= 1
                    album_visible = False
                data = SimpleNamespace(
                    albums=[SimpleNamespace(media_key="album-1", title=self.album_title)]
                    if album_visible
                    else []
                )
            elif payload.kind == "create_album":
                self.album_exists = True
                self.album_title = payload.album_title
                data = [["album-1"]]
            elif payload.kind in {"add_existing", "add_shared", "add_new"}:
                self.added_album = True
                if payload.kind == "add_new":
                    self.album_title = payload.album_title
                data = []
            elif payload.kind == "archive":
                self.archived = True
                data = []
            elif payload.kind == "remote_matches":
                data = [
                    SimpleNamespace(
                        hash=value,
                        media_key=f"media:{value}",
                        dedup_key=f"dedup:{value}",
                    )
                    for value in payload.hashes
                    if not value.startswith("missing")
                ]
            else:
                raise AssertionError(payload.kind)
            responses.append(SimpleNamespace(response_id=payload.payload_id, data=data))
        return responses if many else responses[0]


class GooglePhotosWebServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = GooglePhotosWebService(self.root, FakeClient, FakePayloads)
        self.cookies = self.root / "cookies.txt"
        self.cookies.write_text(
            "# Netscape HTTP Cookie File\n.google.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-session\n",
            encoding="utf-8",
        )
        FakeClient.instances.clear()
        FakeClient.include_existing_album = True
        FakeClient.duplicate_existing_album = False
        FakeClient.existing_album_shared = False
        FakeClient.membership_visibility_delay = 0
        FakeClient.archive_visibility_delay = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_session_import_is_verified_and_stored_without_plaintext_cookie(self) -> None:
        status = self.service.import_session(self.cookies, account_index=1)

        self.assertTrue(status["connected"])
        self.assertEqual(status["account"], "photographer@example.com")
        self.assertEqual(status["account_index"], 1)
        stored = self.service.session_path.read_text(encoding="utf-8")
        self.assertNotIn("secret-session", stored)
        self.assertNotIn(str(self.cookies), stored)
        self.assertTrue(FakeClient.instances[0].session.closed)
        self.assertEqual(list(self.root.glob(".kira-google-web-*.txt")), [])

    def test_json_cookie_export_is_normalized_without_persisting_plaintext(self) -> None:
        json_export = self.root / "photos.google.com.json"
        json_export.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "domain": ".google.com",
                            "expirationDate": 2_000_000_000,
                            "hostOnly": False,
                            "httpOnly": True,
                            "name": "SID",
                            "path": "/",
                            "secure": True,
                            "session": False,
                            "value": "json-secret-session",
                        },
                        {
                            "domain": ".example.com",
                            "name": "unrelated",
                            "path": "/",
                            "value": "excluded-secret",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        status = self.service.import_session(json_export)

        self.assertTrue(status["connected"])
        temporary_cookie_text = FakeClient.instances[0].cookies
        self.assertTrue(temporary_cookie_text.startswith("# Netscape HTTP Cookie File"))
        self.assertIn("#HttpOnly_.google.com\tTRUE\t/\tTRUE\t2000000000", temporary_cookie_text)
        self.assertNotIn("excluded-secret", temporary_cookie_text)
        stored = self.service.session_path.read_text(encoding="utf-8")
        self.assertNotIn("json-secret-session", stored)
        self.assertEqual(json.loads(stored)["source_format"], "json")
        self.assertEqual(list(self.root.glob(".kira-google-web-*.txt")), [])

    def test_list_albums_uses_encrypted_session(self) -> None:
        self.service.import_session(self.cookies)
        albums = self.service.list_albums()

        self.assertEqual(albums, [{"media_key": "album-1", "title": "Trips", "item_count": 4, "shared": False}])

    def test_web_calls_automatically_import_newest_cookie_json(self) -> None:
        app_root = self.root / "app"
        data_root = self.root / "data"
        app_root.mkdir()
        data_root.mkdir()
        service = GooglePhotosWebService(
            data_root,
            FakeClient,
            FakePayloads,
            cookie_export_root=app_root,
        )
        export = app_root / "photos.google.com_16-08-2026.json"
        export.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "domain": ".google.com",
                            "name": "SID",
                            "path": "/",
                            "secure": True,
                            "value": "newest-session",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        albums = service.list_albums()

        self.assertEqual(albums[0]["media_key"], "album-1")
        self.assertTrue(service.session_path.exists())
        self.assertIn("newest-session", FakeClient.instances[-1].cookies)

    def test_remote_hash_matching_is_content_based_and_batched(self) -> None:
        self.service.import_session(self.cookies)
        self.service.HASH_BATCH_SIZE = 2

        matches = self.service.find_remote_matches(["hash-a", "missing-b", "hash-c", "hash-a"])

        self.assertEqual([item["content_hash"] for item in matches], ["hash-a", "hash-c"])
        calls = [call for call in FakeClient.instances[-1].calls if call.kind == "remote_matches"]
        self.assertEqual([call.hashes for call in calls], [["hash-a", "missing-b"], ["hash-c"]])

    def test_visual_signature_matches_reencoded_image_content(self) -> None:
        first = self.root / "first.jpg"
        second = self.root / "renamed.jpg"
        image = Image.new("RGB", (128, 96))
        image.putdata(
            [
                ((x * 2) % 256, (y * 3) % 256, (x + y) % 256)
                for y in range(96)
                for x in range(128)
            ]
        )
        image.save(first, quality=96)
        image.save(second, quality=78)

        left = self.service._image_signature(first)
        right = self.service._image_signature(second)
        color, perceptual = self.service._signature_distance(left, right)

        self.assertEqual((left["width"], left["height"]), (128, 96))
        self.assertLessEqual(color, self.service.VISUAL_COLOR_THRESHOLD)
        self.assertLessEqual(perceptual, self.service.VISUAL_HASH_THRESHOLD)

    def test_organize_adds_to_album_then_archives_and_verifies(self) -> None:
        self.service.import_session(self.cookies)
        result = self.service.organize(["media-1", "media-2", "media-1"], "Trips", archive=True)

        self.assertEqual(result["album"], {"title": "Trips", "media_key": "album-1"})
        self.assertEqual([item["google_media_key"] for item in result["items"]], ["media-1", "media-2"])
        self.assertTrue(all(item["album_added"] and item["archived"] for item in result["items"]))
        calls = FakeClient.instances[-1].calls
        add_call = next(call for call in calls if call.kind == "add_existing")
        archive_call = next(call for call in calls if call.kind == "archive")
        self.assertEqual(add_call.media_keys, ["media-1", "media-2"])
        self.assertEqual(archive_call.dedup_keys, ["dedup:media-1", "dedup:media-2"])
        self.assertLess(
            next(index for index, call in enumerate(calls) if call.kind == "add_existing"),
            next(index for index, call in enumerate(calls) if call.kind == "archive"),
        )

    def test_organize_batches_metadata_reads_but_mutates_once(self) -> None:
        self.service.import_session(self.cookies)
        keys = [f"media-{index}" for index in range(205)]

        self.service.organize(keys, "Trips", archive=True)

        client = FakeClient.instances[-1]
        info_batches = [batch for batch in client.request_batches if batch[0] == "info"]
        extended_batches = [batch for batch in client.request_batches if batch[0] == "info_ext"]
        self.assertEqual([len(batch) for batch in info_batches], [100, 100, 5, 100, 100, 5])
        self.assertEqual([len(batch) for batch in extended_batches], [100, 100, 5])
        self.assertEqual(sum(call.kind == "add_existing" for call in client.calls), 1)
        self.assertEqual(sum(call.kind == "archive" for call in client.calls), 1)

    def test_organize_waits_for_google_read_visibility_without_repeating_mutations(self) -> None:
        self.service.import_session(self.cookies)
        self.service.VERIFY_INTERVAL_SECONDS = 0
        FakeClient.membership_visibility_delay = 1
        FakeClient.archive_visibility_delay = 1

        result = self.service.organize(["media-1"], "Trips", archive=True)

        client = FakeClient.instances[-1]
        self.assertEqual(result["items"][0]["status"], "complete")
        self.assertEqual(sum(call.kind == "add_existing" for call in client.calls), 1)
        self.assertEqual(sum(call.kind == "archive" for call in client.calls), 1)
        self.assertEqual(sum(call.kind == "info_ext" for call in client.calls), 2)

    def test_organize_creates_missing_album_with_first_add(self) -> None:
        self.service.import_session(self.cookies)
        FakeClient.include_existing_album = False
        result = self.service.organize(["media-1"], "New album", archive=False)

        self.assertEqual(result["album"], {"title": "New album", "media_key": "album-1"})
        self.assertFalse(result["items"][0]["archived"])
        calls = [call.kind for call in FakeClient.instances[-1].calls]
        self.assertIn("create_album", calls)
        self.assertIn("add_existing", calls)

    def test_duplicate_album_titles_use_existing_album(self) -> None:
        self.service.import_session(self.cookies)
        FakeClient.duplicate_existing_album = True

        album = self.service.ensure_album("Trips")

        self.assertEqual(album["media_key"], "album-1")

    def test_shared_existing_album_uses_shared_add_call(self) -> None:
        self.service.import_session(self.cookies)
        FakeClient.existing_album_shared = True

        self.service.organize(["media-1"], "Trips", archive=False)

        self.assertIn("add_shared", [call.kind for call in FakeClient.instances[-1].calls])

    def test_disconnect_removes_stored_session(self) -> None:
        self.service.import_session(self.cookies)
        status = self.service.disconnect()

        self.assertFalse(status["connected"])
        self.assertFalse(self.service.session_path.exists())

    def test_stored_session_metadata_contains_no_cookie_source_path(self) -> None:
        self.service.import_session(self.cookies)
        wrapper = json.loads(self.service.session_path.read_text(encoding="utf-8"))
        self.assertNotIn("cookies_path", wrapper)

    def test_expired_session_has_clear_error(self) -> None:
        self.service.import_session(self.cookies)

        class ExpiredClient:
            def __init__(self, *_args, **_kwargs):
                raise IndexError("missing signed-in page data")

        self.service._client_factory = ExpiredClient
        with self.assertRaisesRegex(GooglePhotosWebError, "session expired"):
            self.service.list_albums()


if __name__ == "__main__":
    unittest.main()
