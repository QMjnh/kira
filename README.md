# Kira

New to Kira? Start with the [plain-language guide](README_FOR_HUMANS.md). This file remains the detailed technical reference.

Kira is a transfer-first photo workflow MVP for moving selected RAW/JPEG files directly between a Windows laptop and an iPad on the same local network. It does not require Google Drive or another cloud-storage service.

## What works

- Create named edit jobs on the Dell.
- Browse Windows drives and folders directly in Kira.
- Jump to any parent folder from clickable path breadcrumbs.
- Cull only JPEG-backed photo groups; a same-name RAW remains available as a transfer source.
- Compare every checked JPEG preview with synchronized zoom and pan.
- Reversibly move paired photos into `select`, `unselect`, or named `compare_groups` folders without deleting them.
- Choose JPEG (smaller/faster) or RAW (full editing range) for each edit job.
- Reference selected source files in place instead of copying them into the Kira data folder.
- Pair an iPad by scanning a local QR code; no password or code entry required.
- Download individual files with HTTP range support or one ZIP bundle.
- Upload Lightroom exports back to the Dell in resumable chunks.
- Verify every completed transfer with SHA-256.
- Match a returned JPEG/TIFF/DNG to its original by filename stem.
- Store a job manifest recording originals, returned versions, sizes, hashes, and match state.
- Finish a job by reorganizing the existing camera folder in place and extracting returned Lightroom ZIPs.
- Delete the Kira job afterward without deleting the organized source folders.
- Handle large libraries efficiently with lazy thumbnails, single-pass ZIP hashing, ZIP reuse, and 16 MB upload chunks.
- Combine chosen Google photos and videos with a local collection folder, skip exact duplicates, preserve possible edits, and upload selected JPEGs/videos.

## Start Kira on Windows

Open PowerShell in this folder and run:

```powershell
.\start-kira.ps1
```

To store Kira jobs on an attached hard drive, specify its folder:

```powershell
.\start-kira.ps1 -DataDir "E:\Kira"
```

Kira opens the Dell dashboard and displays:

- The address to open in Safari on the iPad.
- A six-digit pairing code.
- The small data folder used for manifests, resumable uploads, and temporary ZIPs.

If Windows asks whether Python may communicate through Windows Defender Firewall, allow it on **Private networks**. Do not enable it on public networks.

## Google Photos MVP

Kira uses Google's current Photos Picker API for imports and Photos Library API for uploads. Google no longer lets third-party apps browse an entire existing Photos library directly, so imports use Google's picker and include only the photos and videos you explicitly choose.

One-time setup:

1. Create or select a project in the [Google Cloud Console](https://console.cloud.google.com/).
2. Enable **Google Photos Picker API** and **Photos Library API**.
3. Configure the OAuth consent screen. While testing, add your Google account as a test user.
4. Create an OAuth client with application type **Desktop app** and download its JSON file.
5. Rename it to `google-oauth-client.json` and put it directly beside `server.py` in the Kira application folder, for example `D:\Kira\google-oauth-client.json`. It stays there even if `-DataDir` changes.
6. Restart Kira and click **Connect Google Photos** on the Dell dashboard.

To combine camera and phone media for one theme, first use **Browse folders** and open the local collection folder (for example `D:\Photos\NYC`). Then click **Add Google media here**, choose photos or videos in Google's window, and click **Done**. Kira downloads new media directly into that open folder. If no local folder is open, it uses:

```text
<DataDir>\google-photos-inbox\
```

The resulting folder opens in Kira's normal workspace. Photos show thumbnails; videos show selectable video tiles. Google download links are temporary, so Kira starts downloading as soon as the picker finishes.

Before keeping a download, Kira compares it with supported media already anywhere under the collection folder:

- A matching SHA-256 hash is a confirmed byte-for-byte duplicate and is skipped, even if its filename differs.
- The same filename with different bytes is kept with a `__google2`-style name and reported as a possible edit or changed version.
- A related base filename with different bytes is kept and reported as a related variant.
- A new filename and new content is added normally.

The import summary shows each category. “Possible edit” is intentionally a conservative label: Kira can prove identical bytes, but a changed file with the same name may be an edit, metadata change, or recompression.

To upload, open a local folder in Kira, select photo/video tiles, and click **Upload media to Google Photos**. Kira uploads selected JPEGs and supported videos. Selected videos can also be included in an iPad edit job in their original format; the JPEG/RAW selector applies only to photos. Uploads are additive: Kira does not delete or modify existing Google Photos items.

Google tokens stay on the Dell. On Windows they are encrypted for the current Windows user with DPAPI. **Disconnect** revokes Google access and removes Kira's token while keeping already-imported local files.

## iPad and Lightroom workflow

1. Connect the Dell and iPad to the same private Wi-Fi network.
2. Keep the Dell awake with Kira running.
3. Open the Camera app on the iPad and scan Kira's QR code. Safari opens already paired. The displayed address and six-digit code remain available as a fallback.
4. On the Dell, choose **Browse folders**, open the camera folder, select photos, and compare any number at once.
   - **Move to Unselected** moves checked JPEG/RAW pairs out of consideration without deleting them.
   - **Move to Selected** moves checked pairs into the final culling folder.
   - Enter a group name and choose **Group to compare** to create as many comparison groups as needed.
   - Use **Select all** or **Deselect all** for bulk checking. Folder chips open the inbox, select/unselect folders, or a comparison group. From `unselect`, use **Restore to inbox** to bring a photo back. Empty comparison-group folders are removed automatically.
5. Choose **JPEG** or **RAW**, then create the edit job. Kira records references to the selected files without duplicating them. On the iPad, open the job and download the edit-sources ZIP.
6. In the Files app, tap the ZIP once to unpack it.
7. Import the unpacked photos into Lightroom.
8. When editing is complete, select the photos in Lightroom and choose **Share -> Export As**.
9. Use the original filename and save the exports to Files.
10. Return to Kira in Safari, choose those exports, and upload them.
11. On the Dell, click **Finish & organize source folder**. Kira moves the existing camera files into the structure below and copies or extracts the returned edits.
12. After checking the result, click **Delete job** to remove Kira's transfer cache. The organized source folders remain.

The chosen camera/source folder becomes:

```text
selected/
selected/raw/
selected/pre-edit/
unselected_jpeg/
```

Edited JPEGs live directly inside `selected/`. RAW files go into `selected/raw/`, and selected camera JPEGs are retained in `selected/pre-edit/`. Keep the camera/source drive connected while the job is active. Kira reads the chosen RAW or JPEG files directly from it when the iPad ZIP is requested. Completed individual Lightroom uploads go directly into `selected/`; an uploaded Lightroom ZIP is staged there, extracted, and removed. A `KIRA-ORGANIZED-<job-id>.json` report remains in the source folder even after the job is deleted.

The culling workspace shows JPEG-backed photo groups only. RAW-only files are not displayed in preview or compare. Kira generates and caches 480 px grid thumbnails and 1600 px compare previews instead of sending full camera JPEGs to the browser.

Creating a job only records file paths and sizes, so hundreds of large photos become ready quickly. When the edit-sources ZIP is first requested, Kira reads every selected source once, simultaneously hashing and writing it into an uncompressed ZIP. Later downloads reuse that verified ZIP unless a source file changes.

Kira keeps only job metadata, upload-resume state, and temporary ZIPs at:

```text
<DataDir>\jobs\<job-id>\
```

The corresponding transfer manifest is:

```text
<DataDir>\jobs\<job-id>\manifest.json
```

## Security scope

This MVP uses HTTP on the private local network. Scanning the QR exchanges its session pairing secret for a long device token, but traffic is not encrypted. Use Kira only on a trusted home network or private hotspot. A production version should add a native iPad app and certificate-pinned encrypted transport.

Kira advertises the Dell adapter that owns the default internet route, which avoids Hyper-V/WSL virtual-adapter addresses. If the displayed address is correct but the iPad still cannot open it, the Wi-Fi network may isolate wireless devices from one another. Connect both devices to a private phone hotspot or home router and restart Kira.

## Tests

```powershell
python -m unittest discover -s tests -v
```
