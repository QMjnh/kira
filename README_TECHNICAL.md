# Kira technical guide

The [main README](README.md) explains Kira for photographers. This guide is for people working on the code.

Kira is a small, local-first photo-transfer service. It runs on a Windows laptop, serves a browser UI to the laptop and a paired iPad, and moves selected JPEG or RAW files between the laptop’s source drive and Lightroom on the iPad.

## Run locally

Kira needs Python 3. Its required QR-code package is vendored in `vendor/`; Pillow is optional and enables generated thumbnails and compare previews.

```powershell
git clone git@github.com:QMjnh/kira.git
cd kira
.\start-kira.ps1
```

`start-kira.ps1` starts `server.py` on port 8787 and stores Kira’s working data in `kira-data/` by default. To put that working data on another drive:

```powershell
.\start-kira.ps1 -DataDir "E:\Kira"
```

The server opens the laptop dashboard at `http://127.0.0.1:<port>` and shows the local-network address and QR code for the iPad. When Windows Firewall prompts, permit Python on **Private networks** only.

Run tests with:

```powershell
python -m unittest discover -s tests -v
```

## Code map

| Path | Responsibility |
| --- | --- |
| `server.py` | HTTP server, file scanning, culling operations, job manifests, ZIP creation, uploads, organization, pairing, and API routes. |
| `web/` | Browser UI served by the Python server. |
| `vendor/` | Bundled Python dependencies used when Kira starts. |
| `tests/` | Unit and workflow tests. |
| `start-kira.ps1` | Windows entry point and default data-directory configuration. |

The server uses `ThreadingHTTPServer`. It discovers the laptop address that owns the default route, avoiding addresses from Hyper-V and WSL adapters where possible.

## Photo workflow and data model

Kira works against an existing camera/source directory. Creating an edit job records paths and metadata for the selected source files; it does not first copy the whole selection into Kira’s data directory.

The culling view presents JPEG-backed photo groups. A JPEG and RAW file with the same stem are treated as a pair: the JPEG provides the preview, while the paired RAW remains available as the transfer source. RAW-only files are not rendered in the culling or comparison UI. Two folders can stay open side by side in the laptop library; each pane keeps its own scan and selection state. Selected photo groups can be moved between panes, with all matching RAW, JPEG, video, and other supported files moved together.

Checked groups can be moved, reversibly, into these folders below the source root:

```text
select/
unselect/
compare_groups/<name>/
```

Kira creates 480 px thumbnails for the grid and 1600 px previews for comparison. Both are cached in the Kira data directory rather than being full-resolution browser downloads.

For an edit job, the user chooses `jpeg` or `raw`. On the first request for the edit-source ZIP, Kira reads each selected source once, writes an uncompressed ZIP, and calculates a SHA-256 checksum at the same time. The ZIP is reused until the source-file signature changes.

Returned JPEG, TIFF, DNG, and JXL files are matched to originals by filename stem. Uploads are resumable and use 16 MiB chunks. On completion, Kira hashes the returned file with SHA-256 and saves its metadata in the job manifest.

## Job files and organization

Kira stores manifests, partial uploads, and ZIP caches under:

```text
<DataDir>\jobs\<job-id>\
```

The manifest is `manifest.json` in that directory. It records source paths, sizes, checksums, returned files, and match state.

Finishing a job reorganizes the source directory in place:

```text
selected/            returned finished JPEGs
selected/raw/        selected RAW originals
selected/pre-edit/   selected camera JPEGs
unselected_jpeg/     unselected camera JPEGs
```

Individual returned files go into `selected/`. A returned ZIP is staged there, extracted, then removed. Kira leaves `KIRA-ORGANIZED-<job-id>.json` in the source directory as a record of the operation. Deleting a job removes its Kira cache and metadata only; it does not remove the organized source folders.

## Pairing and network security

The laptop dashboard is restricted to loopback requests. The iPad is paired using the QR code, which contains a one-time pairing secret. Successful pairing returns a persistent random device token. API requests from the iPad must provide that token in `X-Kira-Token` (or, where required, the token query parameter).

This controls access; it is not transport encryption. Kira currently serves **HTTP**, not HTTPS. SHA-256 checksums verify that files did not change during handling, but do not encrypt files. Run Kira only on a trusted home network or private hotspot. Do not use public Wi-Fi.

Moving to encrypted transport requires HTTPS/TLS plus a certificate the iPad trusts. A future native client should use certificate-pinned TLS.

## API behavior worth preserving

- Large file downloads support HTTP range requests.
- Uploads are resumable; the server validates offsets and final size before accepting completion.
- File and job writes use atomic JSON updates where applicable.
- Source folders are reorganized only when **Finish & organize source folder** is explicitly requested.
- Culling moves are reversible and reject destination-name collisions rather than overwriting photos.
- Cross-folder moves use `POST /api/local/move`, reject destination-name collisions, and return fresh scans for both the source and destination folders.
