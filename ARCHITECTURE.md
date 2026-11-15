# Kira architecture

Kira is a local-first photo library. It runs on the desktop, keeps one library folder of shoots with originals and edits side by side, serves a browser UI to the desktop and a paired iPad, and syncs to Google Drive and Google Photos. This document is for people working on the code; the [README](README.md) covers usage.

## Run locally

Kira needs Python 3 **and Pillow** (thumbnails, previews, and pixel-identity matching are core behavior). The remaining dependencies are vendored in `vendor/`, including the pinned `gpwc` web client and `websocket-client` (see their `*-PINNED.txt` files). Browser sign-in uses installed Chrome or Edge.

```powershell
git clone git@github.com:QMjnh/kira.git
cd kira
.\start-kira.ps1
```

`start-kira.ps1` starts `server.py` on port 8787 and stores working data (catalog cache, thumbnails, SQLite state, settings) in `kira-data/` by default; the library folder itself is chosen in the UI (`-DataDir` overrides this). Tests:

```powershell
python -m unittest discover -s tests -v
```

Style is enforced with [Ruff](https://docs.astral.sh/ruff/) (configured in `pyproject.toml`): run `ruff check .` and `ruff format .` before committing.

## Code map

| Path | Responsibility |
| --- | --- |
| `server.py` | Entry-point shim: runs `kira.main.main()`. |
| `kira/main.py` | CLI arguments, writer lease, server startup. |
| `kira/app.py` | `KiraApp`: settings, pairing secrets, and the services below. |
| `kira/api.py` | HTTP layer: route table, auth, handlers, raw-body uploads, range-capable file serving. |
| `kira/library.py` | The library: shoot folders, `_kira.json` manifests, ingest and classification, review queue, rescans, Finals, rejects, trash, previews. |
| `kira/metadata.py` | Capture metadata (JPEG, TIFF-based RAW, HEIC, QuickTime) without decoding pixels; RAW embedded previews. |
| `kira/visual.py` | Visual signatures and distances used to tell edits from copies. |
| `kira/statedb.py` | SQLite for rebuildable state: import receipts, review items, outbox, trash log, Drive and Photos replica records. |
| `kira/ipad.py` | iPad outbox and receiving edits. |
| `kira/drive.py` | Never-overwriting Drive mirror, move detection, restore. |
| `kira/gphotos.py` | Google Photos: presence check, Final uploads, clean-up plan and apply, picker import (then album + Archive, matched by hash or filename and capture time). |
| `kira/gpindex.py` | What Google Photos holds that the library doesn't: scans metadata only (no downloads), matches by Kira's own links, then exact bytes (hashing only same-size library files, with Google confirming hashes), then same name and capture time. |
| `kira/sources.py` | Browsing a card or folder outside the library: hashes, duplicates across the drive, shortcuts to duplicates, moving library copies to trash. |
| `kira/google_auth.py` | One OAuth consent (Drive `drive.file`, Photos append-only, app-created read, picker), DPAPI-encrypted tokens. |
| `kira/google_photos_web.py`, `kira/google_signin.py` | Unofficial web client (library listing, file details, hash lookup, Archive) and its browser sign-in. |
| `kira/tasks.py` | Background tasks with progress. |
| `kira/media.py` | Shared file helpers (hashing, atomic writes, folder browsing, extension tables). |
| `web/` | Static browser UI. |

## How a file is classified

Every route (card import, browser drop, iPad upload, Google picker, rescan) calls `Library.ingest_file`:

1. **Same bytes** (SHA-256) as a library file: recorded as a duplicate, no new copy.
2. **Same capture** (time, camera model, sub-seconds when present): an editor's file becomes the next `_editN`; an unedited file with the same name is another original (RAW + JPEG), a higher-resolution original (the old one is demoted), or a recompressed copy in `_copies`.
3. **Several shots in that second:** compared visually against only those shots; ambiguous results go to Review.
4. **No capture metadata:** matched by name and appearance within the shoot (or within the photos sent to the iPad, for iPad uploads); otherwise Review or a new photo.

Originals, edits and copies are set read-only. A rescan keeps an overwritten tracked file as a new edit. Sidecars and project files keep their exact names; their previous state goes to the library trash. Manifests are the source of truth; a cache under the data dir lets you browse while the library drive is unplugged.

## Sync rules

- **Drive** mirrors library-relative paths under a `Kira Library` folder. Uploads are verified against Drive's `sha256Checksum`. A changed file renames the old Drive file with a timestamp and uploads the new one beside it. A path change whose bytes Drive already has becomes a move. Local deletions are never mirrored. Phone originals are skipped only when `gphotos` records them as present.
- **Google Photos uploads** go through the official API into one album per shoot, only for Finals that Photos doesn't already hold (by upload record or hash lookup). Phone originals are never re-uploaded; RAW and video Finals are reported as skipped.
- **Clean-up** looks up candidate files by base64 SHA-1 through the web client and Archives only the items the user ticks. It never trashes.

## iPad round trip

`POST /api/ipad/send` queues originals (RAW when available). `GET /api/ipad/outbox?format=urls` returns plain download links for a Shortcut; downloading an item marks it sent. `POST /api/ipad/receive?name=` takes the raw file body (optional `X-Kira-Sha256`), detects the type when the name has no extension, and files the upload as an edit, using the photos sent in the last 60 days as hints.

## Pairing and network security

The desktop dashboard is restricted to loopback requests. The QR contains a one-time pairing secret; successful pairing returns the persistent device token, which iPad API requests supply via `X-Kira-Token` (or token query parameter). The six-digit code is an alternate pairing factor shown on the desktop.

This controls access; it is not transport encryption. Kira serves plain HTTP on trusted private networks only.

## Google Photos web client

### Photos web sign-in

`POST /api/google/web-signin` starts a background sign-in; `GET` returns its state and `DELETE` cancels it. All three routes require authentication and a loopback client. Kira launches installed Chrome or Edge with a fresh temporary profile and an automatically allocated, loopback-only DevTools port. It never opens the everyday browser profile or disables browser security. The user handles Google's login, MFA, and consent directly. Kira detects the Photos account, collects only Google-domain cookies through the browser protocol, and verifies them with `gpwc` before replacing the saved session. The browser and its temporary profile are closed and removed afterward. Cancellation or disconnect prevents a late verification from saving credentials. A failed sign-in preserves any existing connection. Sign-in expires after ten minutes, and server shutdown cancels it.

The saved encrypted session is reused across Kira restarts. This does not silently renew a revoked Google login: the user clicks **Sign in again** when Google requires fresh authentication. The OAuth consent for Drive, uploads and the picker is separate because it grants different permissions.

The approach follows the browser-login pattern demonstrated by [gphotohandler](https://github.com/hensenx/gphotohandler), using our own implementation and the small Apache-2.0 `websocket-client` library instead of adding Playwright and a bundled browser at runtime. Google's [Library API changes](https://developers.google.com/photos/support/updates) restrict official access to app-created content, so OAuth alone cannot replace the existing organizer. Reading the user's default Chrome cookie database is unsuitable on Windows because of [App-Bound Encryption](https://security.googleblog.com/2024/07/improving-security-of-chrome-cookies-on.html); Chrome also [requires a separate profile for remote debugging](https://developer.chrome.com/blog/remote-debugging-port). The web organizer still uses an unofficial API and can require maintenance when Google changes its site.

## API behavior worth preserving

- Large file downloads support HTTP range requests.
- Uploads are streamed to staging on the library drive, verified, then moved into place; nothing is overwritten.
- Long work (imports, backup, Photos sync) runs as background tasks; `GET /api/tasks/{id}` reports progress.
- Desktop-only routes are rejected for non-loopback clients; the iPad and Shortcuts use only the outbox, receive, shoot viewing and review routes.
- Emptying the library trash needs an explicit `confirm: true`.
