# Kira

Editing photos should not feel like moving house.

Right now, the workflow often looks like this:

```text
SD card → laptop → external drive → Google Drive → iPad → Lightroom
      → Google Drive → laptop → another folder → who knows
```

Every arrow makes another copy. Soon there are originals, selects, exports, "final" exports, and `final-final-2` exports spread across several devices, and nobody is quite sure which version is the real one — or whether it is safe to delete anything.

Kira cuts out the middle:

```text
Camera folder on your laptop → Kira → iPad (Lightroom) → Kira → organized camera folder
```

You plug the camera card or photo drive into your Windows laptop. In Kira, you pick the photos worth editing. Kira sends only those photos directly to your iPad over your home Wi-Fi. You edit in Lightroom, send the finished images back, and Kira puts everything in its proper place in the original folder.

Kira is not an editor, a backup service, or a photo library. Lightroom is still where you edit. Your backup is still your backup. Kira just keeps the trip between your laptop and iPad from turning into a mess.

## What Kira does

- **Choose before you transfer.** Browse a fast grid of JPEG previews from the camera folder, mark keepers, move misses to Unselected, and put close alternatives into named groups for side-by-side comparison. If a photo has a matching RAW file, Kira keeps the pair together while you decide.
- **Send only the selection.** Pick JPEG (smaller, faster) or RAW (full editing range) for each job. Only that selection makes the trip — nothing is copied twice.
- **Bring the edits home.** Upload finished Lightroom exports back to Kira. It matches each returned file to its original by name, verifies every transfer, and on *Finish* reorganizes the source folder into `selected/` (with `raw/` and `pre-edit/` inside) plus `unselected_jpeg/`. Nothing gets deleted without you asking.
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

Kira opens on the laptop and shows a QR code, the address for the iPad, and a six-digit pairing code as a manual fallback. Scan the QR code with the iPad's Camera app and Safari opens already paired.

If PowerShell says scripts are disabled, run this once in the same window, then start Kira again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## A session, end to end

1. Copy a shoot from the SD card to a folder on the laptop or an external drive.
2. Open Kira, browse to that folder, cull and compare until only keepers are checked.
3. Create the edit job and download it on the iPad; unpack the ZIP once in Files and import into Lightroom.
4. Edit. Export with original filenames via **Share → Export As**.
5. Upload the exports back to Kira, then click **Finish & organize source folder**.
6. Check the result and delete the job. The organized folders stay; only Kira's temporary files go.

## Using Google Photos

Click **Connect Google Photos** on the dashboard once. This needs a free one-time setup in the [Google Cloud Console](https://console.cloud.google.com/) (enable the Photos Picker and Library APIs, create a Desktop-app OAuth client, save its JSON as `google-oauth-client.json` beside `server.py`). Details are in the technical guide.

After connecting you can import picked media into any local folder, upload selected photos and videos to Google Photos, and optionally have imports added to a matching album and archived automatically. Disconnecting never deletes anything already downloaded.

## Keeping your photos private

Pairing is required: an unpaired device cannot use Kira just by finding the laptop on the network. But traffic between the laptop and iPad is not encrypted, so use Kira on a trusted home network or a private hotspot — not hotel, café, or school Wi-Fi. Keep the laptop awake until transfers finish. Tokens and sessions stay on your laptop.

---

For installation details, folder layouts, how matching works, API behavior guarantees, and the code layout, see [ARCHITECTURE.md](ARCHITECTURE.md).
