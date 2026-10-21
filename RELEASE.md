# FlickShare Desktop v1.0.0

First release of the FlickShare Windows receiver. Flick files from your Android phone straight onto your PC over local Wi-Fi.

## Downloads

| File | What it is |
| :--- | :--- |
| `FlickShare.exe` | Portable single file. Just run it. |
| `FlickShare-windows.zip` | Folder version. Unzip and run `FlickShare.exe` inside. Starts faster. |

Windows 10/11, 64-bit. The exe isn't code-signed, so SmartScreen may warn: click **More info → Run anyway**.

## What's in it

- **Receive from phone**: files, photos, videos, and links from the FlickShare Android app. The PC is found automatically over mDNS, with no pairing.
- **Browser upload**: scan the QR code with any phone camera to send files without the app.
- **Saved and ready to paste**: files go to your Desktop (or a folder you choose) and are copied to the clipboard.
- **Live transfer progress** and a **history** of received files (open, show in folder, copy path).
- **Liquid glass catch animation** when the window is hidden. It comes in from the side opposite to your throw.
- **Firewall helper**: FlickShare checks Windows Firewall when it starts and can allow itself with one admin prompt. It also fixes leftover Block rules and Wi-Fi marked *Public*. No manual port setup.
- **Tray app**: keeps receiving in the background, with optional *Start with Windows* and start minimized.
- **Settings**: save folder, port, mDNS, clipboard, notifications, animation.

## Getting started

1. Run `FlickShare.exe`.
2. If asked **"Allow FlickShare?"**, click **Allow** and approve the admin prompt.
3. Open the FlickShare app on your phone (same Wi-Fi) and flick a file.
