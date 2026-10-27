from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from kira.google_signin import GoogleSignInBrowser, GoogleSignInError


class GoogleSignInBrowserTests(unittest.TestCase):
    def test_opens_its_own_profile_on_loopback_and_cleans_it_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process = Mock()
            process.poll.return_value = None
            connection = Mock()
            connection.recv.return_value = json.dumps({"id": 1, "result": {}})

            def launch(arguments, **_kwargs):
                profile = Path(next(value.split("=", 1)[1] for value in arguments if value.startswith("--user-data-dir=")))
                (profile / "DevToolsActivePort").write_text("12345\n/devtools/browser/test\n", encoding="utf-8")
                return process

            with patch("kira.google_signin.find_browser", return_value="chrome.exe"), \
                 patch("kira.google_signin.subprocess.Popen", side_effect=launch) as launcher, \
                 patch("kira.google_signin.websocket.create_connection", return_value=connection) as connect:
                with GoogleSignInBrowser(Path(directory), Event()) as browser:
                    profile = Path(browser.profile.name)
                    self.assertTrue(profile.is_dir())
                self.assertFalse(profile.exists())
            arguments = launcher.call_args.args[0]
            self.assertIn("--remote-debugging-address=127.0.0.1", arguments)
            self.assertIn("--remote-debugging-port=0", arguments)
            self.assertNotIn("--enable-automation", arguments)
            self.assertTrue(connect.call_args.kwargs["suppress_origin"])
            self.assertTrue(connect.call_args.args[0].startswith("ws://127.0.0.1:"))
            process.wait.assert_called()

    def test_collects_only_google_cookies_and_detects_selected_account(self) -> None:
        browser = GoogleSignInBrowser(Path("."), Event())
        browser.command = Mock(side_effect=[
            {"targetInfos": [{"type": "page", "url": "https://photos.google.com/u/2/"}]},
            {"cookies": [
                {"domain": ".google.com", "name": "SID", "value": "test-session", "expires": 2_000_000_000},
                {"domain": ".example.com", "name": "unrelated", "value": "private"},
                {"domain": "notgoogle.com", "name": "unrelated", "value": "private"},
            ]},
        ])
        cookies, account = browser.session()
        self.assertEqual(account, 2)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0]["expirationDate"], 2_000_000_000)

    def test_does_not_accept_a_similar_hostname_or_signed_out_page(self) -> None:
        browser = GoogleSignInBrowser(Path("."), Event())
        browser.command = Mock(return_value={"targetInfos": [
            {"type": "page", "url": "https://photos.google.com.example.com/"},
            {"type": "page", "url": "https://accounts.google.com/"},
        ]})
        self.assertIsNone(browser.session())
        browser.command.assert_called_once_with("Target.getTargets")
        browser.command = Mock(side_effect=[
            {"targetInfos": [{"type": "page", "url": "https://photos.google.com/"}]},
            {"cookies": [{"domain": ".google.com", "name": "CONSENT", "value": "test"}]},
        ])
        self.assertIsNone(browser.session())

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
