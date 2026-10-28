# Kira

Editing photos should not feel like moving house.

Right now, the workflow often looks like this:

```text
SD card → desktop → external drive → Google Drive → iPad → Lightroom
      → Google Drive → desktop → another folder → who knows
```

Every arrow makes another copy. Soon there are originals, selects, exports, "final" exports, and `final-final-2` exports spread across several devices, and nobody is quite sure which version is the real one — or whether it is safe to delete anything.

Kira cuts out the middle:

```text
Camera folder on your desktop → Kira → iPad (Lightroom) → Kira → organized camera folder
```

You plug the camera card or photo drive into your Windows desktop. In Kira, you pick the photos worth editing. Kira sends only those photos directly to your iPad over your home Wi-Fi. You edit in Lightroom, send the finished images back, and Kira puts everything in its proper place in the original folder.

Kira now keeps verified local checkpoints of files it sends, receives, and organizes. Lightroom is still where you edit. Keep a separate backup of Kira's working folder to protect those checkpoints against losing the drive. The wider image version-control hub is being built in stages; see the [implementation status](docs/preservation-implementation.md).

## What Kira does

- **Choose before you transfer.** Browse a fast grid of JPEG previews from the camera folder, mark keepers, move misses to Unselected, and put close alternatives into named groups for side-by-side comparison. If a photo has a matching RAW file, Kira keeps the pair together while you decide.
- **Send only the selection.** Pick JPEG (smaller, faster) or RAW (full editing range) for each job. Kira retains a verified local copy before sending it, including accompanying settings and project files.
- **Bring the edits home.** Upload finished exports back to Kira. The browser and desktop compare checksums, and Kira suggests originals by filename. On *Finish*, it reorganizes the source folder into `selected/` (with `raw/` and `pre-edit/` inside) plus `unselected_jpeg/`. ZIP returns keep their folder structure and every component, including projects, settings and videos.
- **Recover a saved file.** Open **Saved files** on the desktop to restore a copy, even after its transfer job has been removed. Checkpoints use extra space in Kira's working folder; identical contents share storage.
- **Optional: combine phone photos.** Connect Google Photos to pull chosen pictures and videos straight into a local collection folder, skipping exact duplicates, or upload local JPEGs/videos to Google Photos.

## Start Kira

1. Install [Python 3](https://www.python.org/downloads/windows/) if needed (select **Add Python to PATH**), and Pillow with `pip install pillow`.
2. Download Kira from [GitHub](https://github.com/QMjnh/kira) (**Code → Download ZIP**), extract it somewhere convenient, and open that folder.
3. Right-click inside the folder, choose **Open in Terminal**, and run:

```powershell
.\start-kira.ps1
```

To store Kira's working data on an attached hard drive instead:

```powershell
.\start-kira.ps1 -DataDir "E:\Kira"
```

Kira opens on the desktop and shows a QR code, the address for the iPad, and a six-digit pairing code as a manual fallback. Scan the QR code with the iPad's Camera app and Safari opens already paired.

If PowerShell says scripts are disabled, run this once in the same window, then start Kira again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## A session, end to end

1. Copy a shoot from the SD card to a folder on the desktop or an external drive.
2. Open Kira, browse to that folder, cull and compare until only keepers are checked.
3. Create the edit job and download it on the iPad; unpack the ZIP once in Files and import into Lightroom.
4. Edit. Export with original filenames via **Share → Export As**.
5. Upload the exports back to Kira, then click **Finish & organize source folder**.
6. Check the result and delete the job. The organized folders stay; only Kira's temporary files go.
7. Use **Saved files → Restore a copy** whenever you need a retained original or returned file. This is file-level recovery; complete editor project history and automatic version-family discovery are still being developed.

## Using Google Photos

Click **Connect Google Photos** on the dashboard once. This needs a free one-time setup in the [Google Cloud Console](https://console.cloud.google.com/) (enable the Photos Picker and Library APIs, create a Desktop-app OAuth client, save its JSON as `google-oauth-client.json` beside `server.py`). Details are in the technical guide.

After connecting you can import picked media into any local folder, upload selected photos and videos to Google Photos, and optionally have imports added to a matching album and archived automatically. Disconnecting never deletes anything already downloaded.

For album and Archive automation, open **Google Photos organizer** and click **Sign in to Google Photos**. Kira opens a separate Chrome or Edge window on the desktop. Sign in there (including any Google verification); Kira connects automatically and closes the window. No cookie export or browser extension is needed. The saved session is reused until Google requires another sign-in; use **Sign in again** if it expires. Cookie-file import remains available under **Import a session manually**.

## Keeping your photos private

Pairing is required: an unpaired device cannot use Kira just by finding the desktop on the network. But traffic between the desktop and iPad is not encrypted, so use Kira on a trusted home network or a private hotspot — not hotel, café, or school Wi-Fi. Keep the desktop awake until transfers finish. Tokens and sessions stay on your desktop.

---

For installation details, folder layouts, how matching works, API behavior guarantees, and the code layout, see [ARCHITECTURE.md](ARCHITECTURE.md).
