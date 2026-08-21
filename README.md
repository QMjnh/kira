# Kira

Kira is a transfer-first photo workflow app: it moves the photos worth editing from your Windows laptop to your iPad and back, over your home Wi-Fi, and puts everything in its proper place afterward. No cloud storage in the middle.

```text
Camera folder on your laptop → Kira → iPad (Lightroom) → Kira → organized camera folder
```

Kira is not an editor, a backup service, or a photo library. Lightroom is still where you edit; your backup is still your backup. Kira keeps the trip between laptop and iPad from turning into a mess of six near-identical copies.

## What works

- Create named edit jobs on the Dell.
- Browse Windows drives and folders directly in Kira, with clickable path breadcrumbs.
- Cull only JPEG-backed photo groups; a same-name RAW remains available as a transfer source.
- Compare every checked JPEG preview with synchronized zoom and pan.
- Reversibly move paired photos into `select`, `unselect`, or named `compare_groups` folders without deleting anything.
- Choose JPEG (smaller/faster) or RAW (full editing range) for each edit job.
- Reference selected source files in place instead of copying them into the Kira data folder.
- Pair an iPad by scanning a local QR code; the displayed six-digit code remains as a manual fallback.
- Download individual files with HTTP range support or one ZIP bundle.
- Upload Lightroom exports back to the Dell in resumable chunks.
- Verify every completed transfer with SHA-256.
- Match a returned JPEG/TIFF/DNG to its original by filename stem.
- Store a job manifest recording originals, returned versions, sizes, hashes, and match state.
- Finish a job by reorganizing the existing camera folder in place and extracting returned Lightroom ZIPs.
- Delete the Kira job afterward without deleting the organized source folders.
- Combine chosen Google photos and videos with a local collection folder, skip exact duplicates, preserve possible edits, and upload selected JPEGs/videos.

## Start Kira on Windows

Install [Python 3](https://www.python.org/downloads/windows/) with **Add Python to PATH**, then:

1. Open PowerShell in this folder.
2. Run:

```powershell
.\start-kira.ps1
```

To store Kira jobs on an attached hard drive instead:

```powershell
.\start-kira.ps1 -DataDir "E:\Kira"
```

Kira opens the Dell dashboard and displays:

- The address to open in Safari on the iPad.
- A six-digit pairing code.
- The small data folder used for manifests, resumable uploads, and temporary ZIPs.

If Windows asks whether Python may communicate through Windows Defender Firewall, allow it on **Private networks** only. If PowerShell blocks scripts, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` once in the same window.

## Google Photos

Kira uses Google's current **Photos Picker API** for imports and the **Photos Library API** for uploads. Google no longer lets third-party apps browse an entire existing library, so imports include only media you explicitly pick.

One-time setup:

1. Create or select a project in the [Google Cloud Console](https://console.cloud.google.com/).
2. Enable **Google Photos Picker API** and **Photos Library API**.
3. Configure the OAuth consent screen; while testing, add your Google account as a test user.
4. Create an OAuth client of type **Desktop app** and download its JSON file.
5. Rename it to `google-oauth-client.json` and put it directly beside `server.py` (for example `D:\Kira\google-oauth-client.json`). It stays there even if `-DataDir` changes.
6. Restart Kira and click **Connect Google Photos** on the Dell dashboard.

### Import

Enter a destination folder in the Google Photos panel (default `<DataDir>\google-photos-inbox`; any absolute local path such as `D:\Photos\NYC` also works and is created when needed), then click **Add Google media** and choose photos or videos in Google's window. The resulting folder opens in Kira's normal workspace; download starts immediately because Google's links are temporary.

Before keeping a download, Kira compares it with supported media already under the destination folder:

- Matching SHA-256 → confirmed duplicate, skipped even if renamed.
- Same filename, different bytes → kept as `__google2`-style *possible edit*.
- Related base filename, different bytes → kept as a related variant.
- New filename and content → added normally.

The **Create the folder-named album and archive** option (checked by default) creates/reuses an album named after the destination folder and archives the selected Google media after downloading. It requires the separate Google Photos web session described below; uncheck it to download without that step.

### Upload

Open a local folder in Kira, select photo/video tiles, and click **Upload media to Google Photos**. Selected videos can also be included in an iPad edit job in their original format; the JPEG/RAW selector applies only to photos.

### Match a folder into an album

To find copies of a local folder that already exist in Google Photos, add them to an album, and archive them without opening Google Photos:

1. Use **Browse folders**, open the local folder, and click **Use this folder**.
2. Export either a browser cookie JSON file or Netscape-format `cookies.txt` from a private browser session signed into Google Photos. Close that private window immediately afterward.
3. In **Match this folder in Google Photos**, enter the path to the JSON/`cookies.txt` export and the account index (`0` unless the export holds multiple accounts), then click **Import session**.
4. Enter the album name, leave **Archive matched items after adding** checked, and click **Match folder → album + archive**.

Matching first uses the SHA-1 of actual file bytes via Google's remote-match endpoint; re-encoded photos are then shortlisted by dimensions/metadata and accepted only when decoded visual content matches. Filenames alone never count.

The organizer is deliberately limited to album assignment and Archive — no trash, no permanent delete. It uses the pinned MIT-licensed [`google_photos_web_client`](https://github.com/xob0t/google_photos_web_client), which calls undocumented endpoints that can change or stop working at any time. The imported web session is encrypted with Windows DPAPI and the plaintext copy removed after each request.

**Disconnect** revokes OAuth access; **Remove web session** deletes the album/Archive session. Both keep already-imported local files. Tokens and sessions stay on the Dell.

## iPad and Lightroom workflow

1. Connect the Dell and iPad to the same private Wi-Fi network and keep the Dell awake.
2. Scan Kira's QR code with the iPad Camera app; Safari opens already paired.
3. On the Dell, choose **Browse folders**, open the camera folder, select photos, and compare any number at once:
   - **Move to Unselected / Move to Selected** move checked JPEG+RAW pairs reversibly.
   - Comparison groups hold close alternatives side by side; identical pixels are de-duplicated automatically, changed same-name photos are preserved as variants.
   - From `unselect`, **Restore to inbox** brings photos back.
4. Choose **JPEG** or **RAW** and create the edit job. On the iPad, download the edit-sources ZIP, unpack it in Files, and import into Lightroom.
5. When editing is done: select the photos in Lightroom, **Share → Export As**, keep original filenames, save to Files.
6. Return to Kira in Safari, choose those exports, and upload them.
7. Click **Finish & organize source folder**, check the result, then **Delete job**.

The chosen camera/source folder becomes:

```text
selected/            returned finished JPEGs
selected/raw/        selected RAW originals
selected/pre-edit/   selected camera JPEGs
unselected_jpeg/     unselected camera JPEGs
```

A `KIRA-ORGANIZED-<job-id>.json` report remains in the source folder even after the job is deleted. Kira itself keeps only manifests, upload-resume state, and temporary ZIPs under `<DataDir>\jobs\<job-id>\`.

## Security scope

Pairing is required: scanning the QR exchanges a one-time secret for a long-lived device token, and unpaired devices cannot use Kira. However, traffic is plain **HTTP**: use Kira only on a trusted home network or private hotspot — never public Wi-Fi. SHA-256 checksums verify integrity, not confidentiality. A production version would add certificate-pinned TLS.

Kira advertises the adapter owning the default internet route, avoiding Hyper-V/WSL virtual addresses. If wireless isolation prevents connecting both devices, use a private phone hotspot or home router.

## Learn more

For the code layout, data model, and API behavior guarantees, see [ARCHITECTURE.md](ARCHITECTURE.md).
