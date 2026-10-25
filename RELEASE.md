# FlickShare Desktop v1.1.1

Share files between any of your devices: phone to PC, PC to phone, PC to PC, and phone to phone. Everything stays on your local Wi-Fi.

## Downloads

| File | What it is |
| :--- | :--- |
| `FlickShare.exe` | Portable single file. Just run it. |
| `FlickShare-windows.zip` | Folder version. Unzip and run `FlickShare.exe` inside. Starts faster. |

Windows 10/11, 64-bit. The exe isn't code-signed, so SmartScreen may warn: click **More info → Run anyway**.

## Fixed in 1.1.1

- The **incoming files** popup now closes after you click **Accept** or **Decline**, press Esc, or close it. Before, it stayed open even though your answer was sent.

## New in 1.1.0

- **Send to nearby devices**: a new card lists the phones and PCs running FlickShare on your Wi-Fi. Drag files onto one, or pick a device and click **Send files…**. Progress, speed, and Cancel are shown while it sends.
- **Accept or decline**: the first time a phone or PC sends you files, FlickShare asks before saving anything. Tick **Always accept files from this device** to skip the question next time. Manage this in **Settings → Incoming files from other devices**.
- **Add a device by IP** for devices the scan can't reach (other subnets, some guest networks).
- Incoming transfers and the history show the **sender's device name** instead of an IP address.

> **Update the FlickShare Android app too.** Phones need the new app version to send to this PC (older versions get *"Upload rejected"*) and to receive files. Phones receive while the app is open and save to **Downloads**.

## Also included (from 1.0)

- **Receive from phone**: files, photos, videos, and links from the FlickShare Android app. The PC is found automatically, with no pairing.
- **Browser upload**: scan the QR code with any phone camera to send files without the app.
- **Saved and ready to paste**: files go to your Desktop (or a folder you choose) and are copied to the clipboard.
- **Liquid glass catch animation** when the window is hidden. It comes in from the side opposite to your throw.
- **Firewall helper**: FlickShare checks Windows Firewall when it starts and can allow itself with one admin prompt.
- **Tray app**: keeps receiving in the background, with optional *Start with Windows* and start minimized.

## Getting started

1. Run `FlickShare.exe`.
2. If asked **"Allow FlickShare?"**, click **Allow** and approve the admin prompt.
3. Open the FlickShare app on your phone (same Wi-Fi) and flick a file, or drag a file from this PC onto your phone in **Send to nearby devices**.