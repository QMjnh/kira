# Kira architecture

Kira is a small, local-first photo-transfer service. It runs on a Windows laptop, serves a browser UI to the laptop and a paired iPad, and moves selected JPEG or RAW files between the laptop's source drive and Lightroom on the iPad. This document is for people working on the code; the [README](README.md) covers usage.

## Run locally

Kira needs Python 3 **and Pillow** (thumbnails, previews, and pixel-identity matching are core behavior). The remaining dependencies are vendored in `vendor/`, including the pinned `gpwc` web client (see `vendor/gpwc-PINNED.txt`).

```powershell
git clone git@github.com:QMjnh/kira.git
cd kira
.\start-kira.ps1
```

`start-kira.ps1` starts `server.py` on port 8787 and stores working data in `kira-data/` by default (`-DataDir` overrides this). Tests:

```powershell
python -m unittest discover -s tests -v
```

## Code map

| Path | Responsibility |
| --- | --- |
| `server.py` | Entry-point shim: runs `kira.main.main()`, re-exports public names. The only Python file outside `kira/`. |
| `kira/main.py` | CLI arguments and server startup. |
| `kira/api.py` | HTTP layer: declarative route table, auth, request handlers, file serving with range support. |
| `kira/store.py` | Job manifests, resumable uploads, transfer bundles, source-folder organization, thumbnail cache. |
| `kira/media.py` | Folder scanning, photo grouping, culling moves, media identity (pixel or byte hashing). |
| `kira/google_photos.py` | Google Picker/Library OAuth client: imports, uploads, operations, folder matching. |
| `kira/google_photos_web.py` | Album/Archive automation through the vendored `gpwc` web client. |
| `kira/errors.py` | Shared `KiraError` type. |
| `web/` | Static browser UI served by the Python server. |
| `vendor/` | Bundled dependencies used when Kira starts. |
| `tests/` | Unit and workflow tests over a real loopback HTTP server. |

The server uses `ThreadingHTTPServer`. It discovers the adapter owning the default route, avoiding Hyper-V/WSL addresses where possible. Authed routes live in one table (`AUTHED_ROUTES`) mapping `(pattern, methods) -> handler`; `{name}` segments become handler parameters. Laptop-only routes are marked `requires_local` and rejected for non-loopback clients.

## Photo workflow and data model

Kira works against an existing camera/source directory. Creating an edit job records paths and metadata for selected sources; it never copies them into Kira's data directory.

- The culling view presents JPEG-backed groups. A JPEG and RAW sharing a stem form a pair: the JPEG previews, the RAW remains the transfer source.
- Checked groups move reversibly into `select/`, `unselect/`, or `compare_groups/<name>/` below the source root. Moves reject name collisions by renaming to `__variantN`, delete byte/pixel-identical duplicates at the destination, and roll back on OS errors.
- Media identity: photos are hashed by decoded pixels (EXIF-transposed, 256px strips); everything else by file bytes. Results are memoized with `functools.lru_cache` keyed on resolved path + size + mtime + suffix.
- Grid thumbnails (480 px) and compare previews (1600 px) are cached under `<DataDir>\thumbnails\`, generated two at a time behind a semaphore.

For an edit job the user chooses `jpeg` or `raw`. On first bundle download, each selected source is read once — simultaneously written into an uncompressed ZIP and SHA-256-hashed. The ZIP is reused until any source signature (path/size/mtime) changes.

Returned JPEG, TIFF, DNG, and JXL files match originals by filename stem (edit-suffix aware). Uploads are resumable in 16 MiB chunks; the server validates offsets and final size before completion.

## Job files

Manifests, partial uploads, and ZIP caches live under `<DataDir>\jobs\<job-id>\` (`manifest.json`, `uploads/`, `.cache/originals.zip`). Writes use atomic JSON updates (temp file + fsync + replace). Manifest writes happen under the store lock; multi-gigabyte ZIP I/O happens outside it, guarded by a separate bundle lock that job deletion also respects.

Finishing a job reorganizes the source directory in place (layout above in the README), extracts returned ZIPs directly into `selected/`, updates stored source references, and leaves `KIRA-ORGANIZED-<job-id>.json`. Deleting a job removes only Kira's cache.

## Pairing and network security

The laptop dashboard is restricted to loopback requests. The QR contains a one-time pairing secret; successful pairing returns the persistent device token, which iPad API requests supply via `X-Kira-Token` (or token query parameter). The six-digit code is an alternate pairing factor shown on the Dell.

This controls access; it is not transport encryption. Kira serves plain HTTP on trusted private networks only.

## Google Photos modules

`kira/google_photos.py` owns OAuth (DPAPI-encrypted tokens beside the data dir), picker sessions, background import/upload operations persisted as JSON records, and content-hash matching back to web media keys (SHA-1 remote match first, then visual-signature matching for re-encoded photos).

`kira/google_photos_web.py` wraps `gpwc` for album assignment and Archive only. Cookie sessions are imported explicitly, normalized to Netscape format, DPAPI-encrypted at rest, decrypted into a temporary cookie file per client session, and deleted after each use.

## API behavior worth preserving

- Large file downloads support HTTP range requests.
- Uploads are resumable; offsets and sizes are validated server-side.
- File and manifest writes use atomic JSON updates where applicable.
- Source folders are reorganized only when **Finish & organize** is explicitly requested; the operation is idempotent once complete.
- Culling moves are reversible and never overwrite silently.
- Cross-folder moves reject destination-name collisions without partial moves.
- Folder management (`/api/local/mkdir`, `/rename`, `/delete`, `/reveal`) is laptop-only. Delete always sends folders to the Windows Recycle Bin — there is no permanent-delete path anywhere in Kira.
