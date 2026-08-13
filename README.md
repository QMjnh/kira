# Kira

Editing photos should not feel like moving house.

Right now, the workflow often looks like this:

```text
SD card → laptop → external drive → tabbing between folders to choose the shots
        → Google Drive → unzip on the iPad → Lightroom
        → Google Drive → laptop → another folder → who knows
```

Every arrow makes another copy. Soon there are originals, selects, exports, “final” exports, and `final-final-2` exports spread across several devices. Nobody is quite sure which version is the real one, or whether it is safe to delete anything.

Kira cuts out the middle.

```text
Camera folder on your laptop → Kira → iPad → Kira → organized camera folder
```

You plug the camera card or photo drive into your Windows laptop. In Kira, you pick the photos worth editing. Kira sends only those photos directly to your iPad over your home Wi-Fi. You edit in Lightroom, send the finished images back, and Kira puts everything in its proper place.

No uploading an entire shoot to Google Drive just to get it onto the iPad. No downloading it again. No wondering which of six copies is the one to keep.

Kira is not an editor, a backup service, or a photo library. Lightroom is still where you edit. Your backup is still your backup. Kira just keeps the trip between your laptop and iPad from turning into a mess.

## Keep the photos where they are

Kira runs on your laptop. Your source photos stay on the laptop or the drive connected to it; Kira does not upload them to a cloud service or save them in somebody else's account. It creates only the temporary files needed to move the selected photos to and from the iPad, then lets you remove those when the job is finished.

## Choose before you transfer

Kira helps you decide what is worth editing before you send anything to the iPad. It shows a fast grid of JPEG previews, so you can review a shoot without opening every large original. Mark the keepers, move the obvious misses to **Unselected**, and put close alternatives into named groups for side-by-side comparison. If a photo has a matching RAW file, Kira keeps that pair together while you choose.

When you are happy with the selection, choose whether the iPad should receive smaller JPEGs or the original RAW files. Only that selection makes the trip.

## Lightroom on iPad

Kira is designed around Lightroom on iPad. The iPad version has a free option, so it can be a practical way to edit without paying for Lightroom desktop. Kira handles getting selected photos to the iPad and the exports back; Lightroom does the editing.

## What a session looks like

1. Copy a shoot from the SD card to a folder on your laptop or external drive.
2. Open Kira and choose the photos you want to work on.
3. Scan Kira’s QR code with the iPad and download those photos.
4. Edit them in Lightroom.
5. Upload the finished exports back to Kira.
6. Tell Kira to finish. It organizes the originals, rejects, and finished images in the source folder.

That’s it: one source folder, one editing trip, and a clear place for the finished work.

## Download and start Kira

On the Windows laptop, install [Python 3](https://www.python.org/downloads/windows/) if it is not already installed. During installation, select **Add Python to PATH**.

Then download Kira from [GitHub](https://github.com/QMjnh/kira): choose **Code**, then **Download ZIP**, and extract the ZIP somewhere convenient, such as your Documents folder. Open the extracted `Kira` folder, right-click inside it, choose **Open in Terminal**, and run:

```powershell
.\start-kira.ps1
```

Windows may ask whether to allow Kira through its firewall. Allow it on **Private networks** only. Kira will open on the laptop and show a QR code. Scan it with the iPad’s Camera app to connect.

If PowerShell says scripts are disabled, run this once in the same Terminal window, then run the start command again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### For developers

If you use Git, clone Kira instead of downloading the ZIP:

```powershell
git clone git@github.com:QMjnh/kira.git
cd kira
.\start-kira.ps1
```

The same Python requirement applies. Run the test suite with `python -m unittest discover -s tests -v`.

## A note about privacy

Pairing is required: scanning the QR code gives the iPad a secret device token, and an unpaired device cannot use Kira just by finding the laptop on the network.

However, the current version does **not** encrypt photo traffic in transit. Use Kira only on a trusted home network or a private hotspot—not hotel, café, school, or other public Wi-Fi. Keep the laptop awake until the transfer is done.

For implementation details, setup options, folder layout, and tests, see the [technical README](README_TECHNICAL.md).
