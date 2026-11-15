# Kira

Editing photos should not feel like moving house.

Right now, the workflow often looks like this:

```text
SD card → desktop → external drive → Google Drive → iPad → Lightroom
      → Google Drive → desktop → another folder → who knows
```

Every arrow makes another copy. Soon there are originals, selects, exports, "final" exports, and `final-final-2` exports spread across several devices, and nobody is quite sure which version is the real one — or whether it is safe to delete anything.

Kira replaces that with **one library** and lets files arrive by any route:

```text
camera card ──┐                                        ┌─ Google Drive: exact backup of camera files and edits
phone (sync) ─┼─> Kira library (one folder per shoot) ─┤
iPad edits ───┘     originals + edits side by side     └─ Google Photos: phone photos + your Finals
```

You never tell Kira what a file is. It works out whether each file is new, already known, another original (RAW + JPEG), an edit, or a lower-quality copy, and files it next to the photo it belongs to. Anything it isn't sure about waits in **Review**; nothing is thrown away.

## What a shoot folder looks like

```text
G:\Kira Library\2026\2026-07 NYC\
  DSC01234.ARW            original (read-only)
  DSC01234.JPG            original (read-only)
  DSC01234_edit1.jpg      first edit that came back
  DSC01234_edit2.jpg      second edit
  DSC01234.ARW.xmp        sidecar, exact name kept
  _rejects\               culled photos, never deleted
  _copies\                recompressed duplicates (WhatsApp, Instagram, ...)
  _kira.json              which version is the Final, readable without Kira
```

Versions are never overwritten or renamed. One version per photo is the **Final**: the newest edit unless you pin another.

## A shoot, end to end

1. **Import.** Plug in the card, open **Import**, pick the card folder and name the shoot. Kira copies only new files and verifies every byte. Phone photos that synced to Google Photos can be added with **Pick from Google Photos**; once they're safely in Kira, they go into the shoot's album in Google Photos and are archived, so the picker doesn't offer them again (this step needs **Sign in to Photos web**).
2. **Cull.** Select misses and click **Reject**.
3. **Send to iPad.** Select keepers and click **Send to iPad**. The "Get from Kira" Shortcut pulls them into Lightroom (setup steps are on the iPad page).
4. **Edit and send back.** In Lightroom: select the edits, then **Share → Share to… → Send to Kira**. Each edit lands next to its original as the next version. Edits that Photoshop or other desktop apps save into the shoot folder are picked up with **Rescan folder**; files dropped onto a shoot are sorted the same way.
5. **Back up and share.** On **Cloud**, **Back up now** copies everything to Google Drive and **Upload Finals** puts each Final in a per-shoot Google Photos album.
6. **Wipe the card** once **Is it safe to wipe?** says every file is in the library and backed up.

## Google Drive and Google Photos

- **Drive** gets exact copies of camera originals, edits, sidecars and project files. Kira never deletes or overwrites anything there: a changed file is added beside the old one, and a file deleted on the library drive stays on Drive and can be put back with **Restore missing files**.
- **Phone photos** keep Google Photos as their cloud home. They skip Drive only after **Check phone photos** confirms Google Photos holds the exact file.
- **Clean up** hides copies that duplicate a Final (other edits, phone originals you edited) by moving them to Google Photos' Archive. It runs only when you click **Hide**, shows the list first, and never deletes anything.

Setup: create a Desktop-app OAuth client in the [Google Cloud Console](https://console.cloud.google.com/) with the Drive, Photos Library and Photos Picker APIs enabled, and save its JSON as `google-oauth-client.json` beside `server.py`. Checking, clean-up and filing picked photos also need **Sign in to Photos web**, which uses Google Photos' unofficial web interface; if that ever breaks, uploads and backup keep working.

## Start Kira

1. Install [Python 3](https://www.python.org/downloads/windows/) if needed (select **Add Python to PATH**), and Pillow with `pip install pillow`.
2. Download Kira from [GitHub](https://github.com/QMjnh/kira) (**Code → Download ZIP**), extract it somewhere convenient, and open that folder.
3. Right-click inside the folder, choose **Open in Terminal**, and run:

```powershell
.\start-kira.ps1
```

To keep Kira's working data (catalog cache, thumbnails, settings) somewhere else:

```powershell
.\start-kira.ps1 -DataDir "E:\Kira"
```

On first start Kira asks where the photo library should live, for example `G:\Kira Library`. The iPad page shows a QR code and a six-digit code for pairing the iPad.

If PowerShell says scripts are disabled, run this once in the same window, then start Kira again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

## Keeping your photos private

Pairing is required: an unpaired device cannot use Kira just by finding the desktop on the network. But traffic between the desktop and iPad is not encrypted, so use Kira on a trusted home network or a private hotspot — not hotel, café, or school Wi-Fi. Keep the desktop awake until transfers finish. Tokens and sessions stay on your desktop.

---

For installation details, folder layouts, how matching works, API behavior guarantees, and the code layout, see [ARCHITECTURE.md](ARCHITECTURE.md).
