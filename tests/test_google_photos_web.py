from __future__ import annotations

import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kira.google_photos_web import GooglePhotosWebError, GooglePhotosWebService
from kira.google_signin import GoogleSignInError


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
                                    media_key="album-2", title=self.album_title, item_count=2, is_shared=False
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
                data = SimpleNamespace(dedup_key=f"dedup:{payload.media_key}", is_archived=archived)
            elif payload.kind == "info_ext":
                album_visible = self.added_album
                if album_visible and type(self).membership_visibility_delay:
                    type(self).membership_visibility_delay -= 1
                    album_visible = False
                data = SimpleNamespace(
                    albums=[SimpleNamespace(media_key="album-1", title=self.album_title)] if album_visible else []
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
                    SimpleNamespace(hash=value, media_key=f"media:{value}", dedup_key=f"dedup:{value}")
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
            "# Netscape HTTP Cookie File\n.google.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-session\n", encoding="utf-8"
        )
        FakeClient.instances.clear()
        FakeClient.include_existing_album = True
        FakeClient.duplicate_existing_album = False
        FakeClient.existing_album_shared = False
        FakeClient.membership_visibility_delay = 0
        FakeClient.archive_visibility_delay = 0

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def test_browser_sign_in_verifies_and_encrypts_the_session(self) -> None:
        browser = SimpleNamespace(
            wait_until_closed=lambda _deadline: True,
            session=lambda: (
                [{"domain": ".google.com", "name": "SID", "value": "automatic-session", "session": True}],
                2,
            ),
        )
        with (
            patch("kira.google_photos_web.find_browser", return_value="chrome.exe"),
            patch("kira.google_photos_web.GoogleSignInBrowser") as factory,
        ):
            factory.return_value.__enter__.return_value = browser
            self.service.start_sign_in()
            self.service._signin_thread.join(timeout=3)
        self.assertEqual(self.service.sign_in_status(), {"status": "completed"})
        self.assertTrue(self.service.status()["connected"])
        self.assertEqual(self.service.status()["account_index"], 2)
        self.assertNotIn("automatic-session", self.service.session_path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.root.glob(".kira-google-web-*.txt")), [])

    def test_cookies_google_rotates_are_saved_back_into_the_session(self) -> None:
        from http.cookiejar import Cookie

        def cookie(domain: str, name: str, value: str) -> Cookie:
            return Cookie(0, name, value, None, False, domain, True, True, "/", True, True, None, False, None, None, {})

        self.service.import_session(self.cookies)
        before = self.service.session_path.read_text(encoding="utf-8")
        self.assertFalse(self.service._save_cookies([cookie(".google.com", "SID", "secret-session")]))
        self.assertEqual(self.service.session_path.read_text(encoding="utf-8"), before)
        rotated = [cookie(".google.com", "__Secure-1PSIDTS", "fresh"), cookie(".example.com", "SID", "foreign")]
        self.assertTrue(self.service._save_cookies(rotated))
        self.assertNotIn("fresh", self.service.session_path.read_text(encoding="utf-8"))
        raw, _ = self.service._load_session()
        self.assertIn(b"__Secure-1PSIDTS	fresh", raw)
        self.assertIn(b"SID	secret-session", raw)
        self.assertNotIn(b"foreign", raw)
        self.assertEqual(self.service.status()["account_index"], 0)
        self.assertFalse(self.service.refresh_session(force=True))  # an injected client never calls Google

    def test_disconnect_during_verification_cannot_restore_the_connection(self) -> None:
        verifying, release = threading.Event(), threading.Event()
        browser = SimpleNamespace(
            wait_until_closed=lambda _deadline: True,
            session=lambda: (
                [{"domain": ".google.com", "name": "SID", "value": "cancelled-session", "session": True}],
                0,
            ),
        )
        verify = self.service._verified_session

        def delayed_verification(*args):
            wrapper = verify(*args)
            verifying.set()
            release.wait(3)
            return wrapper

        with (
            patch("kira.google_photos_web.find_browser", return_value="chrome.exe"),
            patch("kira.google_photos_web.GoogleSignInBrowser") as factory,
            patch.object(self.service, "_verified_session", side_effect=delayed_verification),
        ):
            factory.return_value.__enter__.return_value = browser
            self.service.start_sign_in()
            try:
                self.assertTrue(verifying.wait(3))
                original_thread = self.service._signin_thread
                self.service.start_sign_in()
                self.assertIs(self.service._signin_thread, original_thread)
                self.service.disconnect()
            finally:
                release.set()
                self.service._signin_thread.join(timeout=3)
        self.assertEqual(self.service.sign_in_status()["status"], "cancelled")
        self.assertFalse(self.service.session_path.exists())

    def test_browser_failure_leaves_existing_connection_intact(self) -> None:
        self.service.import_session(self.cookies)
        stored = self.service.session_path.read_bytes()
        with (
            patch("kira.google_photos_web.find_browser", return_value="chrome.exe"),
            patch("kira.google_photos_web.GoogleSignInBrowser") as factory,
        ):
            factory.return_value.__enter__.side_effect = GoogleSignInError("Browser could not open.")
            self.service.start_sign_in()
            self.service._signin_thread.join(timeout=3)
        self.assertEqual(self.service.sign_in_status(), {"status": "failed", "error": "Browser could not open."})
        self.assertEqual(self.service.session_path.read_bytes(), stored)

    def test_sign_in_reports_a_missing_browser(self) -> None:
        with patch("kira.google_photos_web.find_browser", return_value=None):
            with self.assertRaisesRegex(GooglePhotosWebError, "Install Google Chrome or Microsoft Edge"):
                self.service.start_sign_in()

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
                        {"domain": ".example.com", "name": "unrelated", "path": "/", "value": "excluded-secret"},
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
        self.assertEqual(list(self.root.glob(".kira-google-web-*.txt")), [])

    def test_list_albums_uses_encrypted_session(self) -> None:
        self.service.import_session(self.cookies)
        albums = self.service.list_albums()

        self.assertEqual(albums, [{"media_key": "album-1", "title": "Trips", "item_count": 4, "shared": False}])

    def test_remote_hash_matching_is_content_based_and_batched(self) -> None:
        self.service.import_session(self.cookies)
        self.service.HASH_BATCH_SIZE = 2

        matches = self.service.find_remote_matches(["hash-a", "missing-b", "hash-c", "hash-a"])

        self.assertEqual([item["content_hash"] for item in matches], ["hash-a", "hash-c"])
        calls = [call for call in FakeClient.instances[-1].calls if call.kind == "remote_matches"]
        self.assertEqual([call.hashes for call in calls], [["hash-a", "missing-b"], ["hash-c"]])

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
        # A saved but rejected session no longer counts as connected until the next sign-in.
        self.assertEqual((self.service.status()["connected"], self.service.status()["expired"]), (False, True))
        self.service._client_factory = FakeClient
        self.service.import_session(self.cookies)
        self.assertEqual((self.service.status()["connected"], self.service.status()["expired"]), (True, False))

    def test_check_verifies_imported_cookie_json(self) -> None:
        self.service.import_session(self.cookies)
        self.service.check()

    def test_check_reports_missing_and_unreadable_cookie_json(self) -> None:
        with self.assertRaisesRegex(GooglePhotosWebError, "Sign in to Google Photos"):
            self.service.check()
        self.service.session_path.write_text("{broken", encoding="utf-8")
        self.assertFalse(self.service.status()["connected"])
        with self.assertRaisesRegex(GooglePhotosWebError, "unreadable"):
            self.service.check()

    def test_check_rejects_raw_cookie_export_saved_as_session(self) -> None:
        self.service.session_path.write_text(
            json.dumps(
                {
                    "url": "https://photos.google.com",
                    "cookies": [{"domain": ".google.com", "name": "SID", "value": "x"}],
                }
            ),
            encoding="utf-8",
        )
        self.assertFalse(self.service.status()["connected"])
        with self.assertRaisesRegex(GooglePhotosWebError, "cookie export"):
            self.service.check()


class PhotosPageParsingTests(unittest.TestCase):
    def test_page_data_is_read_without_lxml_and_signed_out_pages_raise(self) -> None:
        from gpwc import Client

        page = '<html><script nonce="x">var a;</script><script data-id="_gd" nonce="n">window.WIZ_global_data = {"oPEP7c":"me@example.com"};</script></html>'
        self.assertEqual(Client.parse_main_page(None, page), {"oPEP7c": "me@example.com"})
        with self.assertRaises(IndexError):  # Kira reports this as an expired session
            Client.parse_main_page(None, "<html><body>About Google Photos</body></html>")


class NameAndTimeMatchTests(unittest.TestCase):
    def test_matches_by_filename_near_capture_time_and_skips_ambiguous_or_missing(self) -> None:
        base = 1_783_159_260_000
        library = [
            ("k-exact", "IMG_0101.JPG", base + 300),
            ("k-dupe-far", "IMG_0101.JPG", base + 3 * 60 * 60 * 1000),
            ("k-other", "IMG_0200.JPG", base),
            ("k-twin-a", "IMG_0300.JPG", base + 5 * 60 * 1000),
            ("k-twin-b", "IMG_0300.JPG", base + 6 * 60 * 1000),
            ("k-shifted", "IMG_0400.HEIC", base - 7 * 60 * 60 * 1000),
        ]
        pages = [library[:3], library[3:]]

        class Client:
            def send_api_request(self, payload):
                if payload.kind == "page":
                    index = 0 if payload.page_id is None else 1
                    items = [
                        SimpleNamespace(media_key=key, dedup_key=f"d-{key}", timestamp=ms)
                        for key, _, ms in pages[index]
                    ]
                    return SimpleNamespace(
                        data=SimpleNamespace(
                            items=items,
                            next_page_id="p2" if index == 0 else None,
                            last_item_timestamp=items[-1].timestamp,
                        )
                    )
                names = {key: name for key, name, _ in library}
                return SimpleNamespace(
                    data=[SimpleNamespace(media_key=key, file_name=names[key]) for key in payload.media_keys]
                )

        payloads = SimpleNamespace(
            GetLibraryPageByTakenDate=lambda **values: SimpleNamespace(kind="page", **values),
            GetBatchMediaInfo=lambda keys: SimpleNamespace(kind="info", media_keys=keys),
        )

        @contextmanager
        def client():
            yield Client(), payloads

        service = GooglePhotosWebService(Path(tempfile.gettempdir()))
        with patch.object(service, "_client", client):
            found = service.find_by_name_and_time(
                [
                    {"name": "img_0101.jpg", "time_ms": base},
                    {"name": "IMG_0300.JPG", "time_ms": base},
                    {"name": "IMG_0400.HEIC", "time_ms": base},
                    {"name": "IMG_0999.JPG", "time_ms": base},
                ]
            )
        self.assertEqual(
            found,
            {
                0: {"media_key": "k-exact", "dedup_key": "d-k-exact"},
                2: {"media_key": "k-shifted", "dedup_key": "d-k-shifted"},
            },
        )


if __name__ == "__main__":
    unittest.main()
