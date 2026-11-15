from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from kira.google_signin import GoogleSignInBrowser, GoogleSignInError


class GoogleSignInBrowserTests(unittest.TestCase):
    def test_signs_in_without_debugging_then_reads_cookies_headless_on_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            visible, reader = Mock(), Mock()
            visible.poll.return_value = 0
            reader.poll.return_value = None
            connection = Mock()
            connection.recv.side_effect = lambda: json.dumps(
                {
                    "id": browser.request_id,
                    "result": {"cookies": [{"domain": ".google.com", "name": "SID", "value": "test"}]},
                }
            )
            launches = []

            def launch(arguments, **_kwargs):
                launches.append(arguments)
                if len(launches) == 1:
                    return visible
                profile = Path(
                    next(value.split("=", 1)[1] for value in arguments if value.startswith("--user-data-dir="))
                )
                (profile / "DevToolsActivePort").write_text("12345\n/devtools/browser/test\n", encoding="utf-8")
                return reader

            with (
                patch("kira.google_signin.find_browser", return_value="chrome.exe"),
                patch("kira.google_signin.subprocess.Popen", side_effect=launch),
                patch("kira.google_signin.websocket.create_connection", return_value=connection) as connect,
            ):
                with GoogleSignInBrowser(Path(directory), Event()) as browser:
                    profile = Path(browser.profile.name)
                    self.assertTrue(browser.wait_until_closed(time.monotonic() + 5))
                    cookies, account = browser.session()
                self.assertFalse(profile.exists())
            self.assertEqual((account, cookies[0]["name"]), (0, "SID"))
            self.assertFalse(any("remote-debugging" in value for value in launches[0]))
            self.assertNotIn("--headless=new", launches[0])
            self.assertIn("--remote-debugging-address=127.0.0.1", launches[1])
            self.assertIn("--headless=new", launches[1])
            self.assertFalse(any("--enable-automation" in value for launch in launches for value in launch))
            self.assertTrue(connect.call_args.kwargs["suppress_origin"])
            self.assertTrue(connect.call_args.args[0].startswith("ws://127.0.0.1:"))

    def test_collects_only_google_cookies(self) -> None:
        cookies, account = GoogleSignInBrowser.google_session(
            [
                {"domain": ".google.com", "name": "SID", "value": "test-session", "expires": 2_000_000_000},
                {"domain": ".example.com", "name": "unrelated", "value": "private"},
                {"domain": "notgoogle.com", "name": "unrelated", "value": "private"},
            ]
        )
        self.assertEqual(account, 0)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["expirationDate"], 2_000_000_000)

    def test_signed_out_profile_has_no_session(self) -> None:
        self.assertIsNone(GoogleSignInBrowser.google_session([{"domain": ".google.com", "name": "CONSENT"}]))
        self.assertIsNone(GoogleSignInBrowser.google_session([{"domain": "google.com.example.com", "name": "SID"}]))

    def test_protocol_ignores_events_and_does_not_expose_error_details(self) -> None:
        browser = GoogleSignInBrowser(Path("."), Event())
        browser.socket = Mock()
        browser.socket.recv.side_effect = [
            json.dumps({"method": "Target.targetCreated"}),
            json.dumps({"id": 1, "result": {"cookies": []}}),
            json.dumps({"id": 2, "error": {"message": "private browser details"}}),
        ]
        self.assertEqual(browser.command("Storage.getCookies"), {"cookies": []})
        with self.assertRaises(GoogleSignInError) as error:
            browser.command("Storage.getCookies")
        self.assertNotIn("private browser details", str(error.exception))


if __name__ == "__main__":
    unittest.main()
