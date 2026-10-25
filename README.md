# FlickShare Desktop

[![Build Windows app](https://github.com/workingpayload/FlickShare-Desktop/actions/workflows/build-windows.yml/badge.svg)](https://github.com/workingpayload/FlickShare-Desktop/actions/workflows/build-windows.yml)
[![Latest release](https://img.shields.io/github/v/release/workingpayload/FlickShare-Desktop)](https://github.com/workingpayload/FlickShare-Desktop/releases/latest)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)

The Windows app for **FlickShare**. Flick files, photos, videos, and links from your Android phone and they land on your PC over local Wi-Fi. Send files back the other way too: from this PC to phones and to other PCs running FlickShare. No cloud, no account, no cable.

---

## Download

Grab the latest build from **[Releases](https://github.com/workingpayload/FlickShare-Desktop/releases/latest)**:

| File | What it is |
| :--- | :--- |
| `FlickShare.exe` | Portable single file. Just run it. |
| `FlickShare-windows.zip` | Folder version. Unzip anywhere and run `FlickShare.exe` inside. Starts faster. |

> Windows SmartScreen may warn because the exe isn't code-signed. Click **More info → Run anyway**.

On first start FlickShare checks Windows Firewall. If phones would be blocked, it asks **"Allow FlickShare?"**. Click **Allow** and approve one admin (UAC) prompt. You don't need to open ports by hand.

## How it works

1. Run FlickShare on your PC. It advertises itself on the network over mDNS (`_flickshare._tcp.local.`).
2. Open the FlickShare Android app on the same Wi-Fi. It finds the PC automatically.
3. Send a file:
   - flick the file card up off the screen, or
   - hold the card and flick your wrist toward the PC, or
   - with **Hands-free Flick** on, just flick the phone, or
   - with **Tap to Share** on, tap the phone twice against the PC.
4. The file is saved to your Desktop (or a folder you choose), copied to the clipboard ready for `Ctrl+V`, and you get a notification.

No app on the phone? Scan the QR code in the window with any phone camera. It opens an upload page in the browser.

### Sending from this PC (to phones and other PCs)

Every FlickShare device (this app, and the Android app while it's open) runs the same receiver on port 8765. The **Send to nearby devices** card scans your Wi-Fi every 20 seconds and lists the phones and PCs it finds.

- Drag files from Explorer onto a device in the list, or
- select a device and click **Send files…** (double-click or right-click a device works too).

The other device asks *"Accept files from <this PC>?"* the first time. Tick **Always accept files from this device** to skip the question next time. Can't see a device (different subnet, guest Wi-Fi)? Click **＋** and type its IP address. Phones only receive while the FlickShare app is open; they save to **Downloads**.

When another phone or PC sends to *this* PC, you get the same Accept / Decline popup (it declines by itself after 2 minutes). Turn the question off, or forget trusted devices, in **Settings → Incoming files from other devices**. The browser upload page asks too.

> Both apps must be updated together: with approvals on, an older FlickShare Android app (no handshake) gets *"Upload rejected"*.

## Features

- **Status panel**: live/paused, LAN `IP:port` (one-click copy), PC name, mDNS state, Pause/Start and Restart. The receiver restarts by itself when your network address changes.
- **Live transfer card**: progress, bytes, and speed for each incoming file, with the sender's device name.
- **Send to nearby devices**: finds other FlickShare phones and PCs on your Wi-Fi; drag files onto one (or use *Send files…*) to send. Shows *waiting for accept*, progress, speed, and Cancel.
- **Accept / Decline popup** for files from devices you haven't trusted yet, with *Always accept files from this device*.
- **History**: every received file. Double-click to open, *Show in folder*, *Copy path*, or remove (files stay on disk).
- **QR code** for browser uploads from any phone.
- **Folder drop zone**: drop a folder on the window to make it the save location.
- **Liquid glass catch animation**: while the window is hidden, a glass drop rises from the screen edge, shows progress, then shows a thumbnail and the file name when the file lands. It comes in from the side opposite to your throw. You can turn it off in Settings.
- **Firewall helper**: one-click fix if Windows Firewall blocks phones, including Block rules left behind by a cancelled Windows prompt and Wi-Fi marked *Public*.
- **Tray app**: closing the window keeps it receiving in the background. Only one copy runs at a time.
- **Settings**: save folder, port, mDNS, clipboard copy, notifications, animation, ask before accepting files (and forget trusted devices), minimize to tray, start minimized, *Start with Windows*.

Settings, history, and logs are stored in `%APPDATA%\FlickShare\` (`settings.json`, `history.json`, `flickshare.log`).

## Run from source

Needs Python **3.10+**.

```powershell
git clone https://github.com/workingpayload/FlickShare-Desktop.git
cd FlickShare-Desktop
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python flickshare_desktop.py           # add --minimized to start in the tray
```

### Headless receiver (no GUI)

`server.py` is the same receiver without the window. It also works on Linux and macOS.

```powershell
python server.py --port 8765 --output-dir ~/Desktop
python server.py --port 8765 --output-dir ./incoming --no-mdns --no-clipboard --no-notify
```

| Flag | Environment variable | Default | Description |
| :--- | :--- | :--- | :--- |
| `--host` | | `0.0.0.0` | Interface to bind. |
| `--port` | `FLICKSHARE_PORT` | `8765` | TCP port for uploads. |
| `--output-dir` | `FLICKSHARE_OUTPUT_DIR` | `~/Desktop` | Where received files are saved. |
| `--no-mdns` | `FLICKSHARE_NO_MDNS=1` | off | Don't advertise over mDNS. |
| `--no-clipboard` | `FLICKSHARE_NO_CLIPBOARD=1` | off | Don't copy received files to the clipboard. |
| `--no-notify` | `FLICKSHARE_NO_NOTIFY=1` | off | Don't show notifications. |

The headless receiver doesn't fix the firewall for you. Allow it by hand (PowerShell as Administrator):

```powershell
New-NetFirewallRule -DisplayName "FlickShare HTTP (TCP 8765)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 -Profile Private
New-NetFirewallRule -DisplayName "FlickShare mDNS (UDP 5353)" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 5353 -Profile Private
```

On Linux, `xclip` and `libnotify-bin` are needed for the clipboard and notifications.

## API

| Endpoint | Description |
| :--- | :--- |
| `GET /` | Mobile-friendly browser upload page (what the QR code opens). |
| `GET /health`, `GET /info` | Status plus hostname, IP, port, and save folder; protocol v2 adds `protocol`, `device_id`, `device_name`, `platform`, and `requires_approval`. |
| `POST /transfer/request` | Handshake before an upload. JSON `{sender_id, sender_name, sender_platform, files:[{name,size,mime}], total_bytes}`. Returns `200 {"token": …}` at once for trusted senders (or when approvals are off); otherwise waits up to 120 s for the user: `403` declined, `408` (with `Retry-After`) no answer. |
| `POST /upload` | `multipart/form-data` with one `file` part or several `files` parts. Streamed to disk in 1 MB chunks. When the receiver requires approval, send the one-time token as `X-FlickShare-Token` (single use, 10 minutes, same IP), otherwise `403`. |

```bash
curl -X POST http://192.168.1.42:8765/upload -F "files=@screenshot.png" -F "files=@notes.txt"
```

The command-line `server.py` doesn't ask (approvals are off), so the `curl` above works as is. Optional sender headers `X-FlickShare-Sender-Id`, `X-FlickShare-Sender-Platform`, and `X-FlickShare-Sender-Name` (percent-encoded UTF-8) put a friendly name in the transfer card and history.

Optional header `X-FlickShare-Direction: <deg>` (−90…90; negative means thrown to the left) sets which side the glass animation comes in from.

## Tests

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest -q
```

## Build the exe

```powershell
.\build_exe.ps1             # dist\FlickShare\FlickShare.exe (folder build, fast startup)
.\build_exe.ps1 -OneFile    # dist\FlickShare.exe (single portable file)
```

Options: `-SkipInstall` skips `pip install`, and `-Python <path>` picks the interpreter.

## Releases

[`.github/workflows/build-windows.yml`](.github/workflows/build-windows.yml) runs the tests and builds both exes on every push and pull request.

- **Push to `main`**: publishes release `v<APP_VERSION>` (from [`flickshare_app/__init__.py`](flickshare_app/__init__.py)). The release notes come from [`RELEASE.md`](RELEASE.md). Pushing again with the same version replaces that release's files and notes.
- **New version**: bump `APP_VERSION`, update `RELEASE.md`, and push. Or push a tag (`git tag v1.1.0; git push origin v1.1.0`), and the tag's version is stamped into the app.
- **Pull requests and manual runs**: the exes are attached to the workflow run as an artifact.
