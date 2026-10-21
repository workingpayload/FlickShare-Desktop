"""
FlickShare Desktop Receiver Server.

High-speed local Wi-Fi file receiver with:
- Zeroconf / mDNS auto-discovery (_flickshare._tcp.local.)
- Streaming multipart uploads in 1 MB chunks directly to ~/Desktop
- Automatic filename sanitization and collision auto-renaming (file_1.png, file_2.png)
- Smart clipboard integration (raw bitmap copy for images, file path copy for other files)
- Native OS toast notifications on catch
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import math
import os
import platform
import re
import socket
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pyperclip
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from PIL import Image
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

# ---------------------------------------------------------------------------
# Constants & Logging Configuration
# ---------------------------------------------------------------------------

SERVICE_TYPE = "_flickshare._tcp.local."
DEFAULT_PORT = 8765
CHUNK_SIZE = 1024 * 1024  # 1 MB streaming buffer
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("flickshare.receiver")


# ---------------------------------------------------------------------------
# Receiver Event Hooks (consumed by the desktop GUI)
# ---------------------------------------------------------------------------

EventListener = Callable[[str, Dict[str, Any]], None]
_event_listeners: List[EventListener] = []
PROGRESS_EMIT_INTERVAL_S = 0.05


def add_event_listener(listener: EventListener) -> None:
    """Register a callback invoked as `listener(event_name, payload)` from the server thread."""
    if listener not in _event_listeners:
        _event_listeners.append(listener)


def remove_event_listener(listener: EventListener) -> None:
    if listener in _event_listeners:
        _event_listeners.remove(listener)


def emit_event(event: str, **payload: Any) -> None:
    """Dispatch a receiver event to all listeners. Listener failures never break uploads."""
    for listener in list(_event_listeners):
        try:
            listener(event, payload)
        except Exception as exc:
            logger.debug("Event listener failed for %s: %s", event, exc)


# ---------------------------------------------------------------------------
# Receiver Configuration
# ---------------------------------------------------------------------------


@dataclass
class ReceiverConfig:
    host: str = "0.0.0.0"
    port: int = field(
        default_factory=lambda: int(os.environ.get("FLICKSHARE_PORT", str(DEFAULT_PORT)))
    )
    save_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("FLICKSHARE_OUTPUT_DIR", str(Path.home() / "Desktop"))
        )
    )
    enable_mdns: bool = field(
        default_factory=lambda: os.environ.get("FLICKSHARE_NO_MDNS", "0") != "1"
    )
    enable_clipboard: bool = field(
        default_factory=lambda: os.environ.get("FLICKSHARE_NO_CLIPBOARD", "0") != "1"
    )
    enable_notifications: bool = field(
        default_factory=lambda: os.environ.get("FLICKSHARE_NO_NOTIFY", "0") != "1"
    )


config = ReceiverConfig()


# ---------------------------------------------------------------------------
# Network & mDNS Auto-Discovery
# ---------------------------------------------------------------------------


def get_local_ip() -> str:
    """Detect the active LAN IPv4 address via UDP socket route probing."""
    probe_targets = [("8.8.8.8", 80), ("10.255.255.255", 1)]
    for target_host, target_port in probe_targets:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1.0)
                sock.connect((target_host, target_port))
                ip = sock.getsockname()[0]
                if ip and not ip.startswith("127."):
                    return ip
        except OSError:
            continue

    try:
        hostname_ip = socket.gethostbyname(socket.gethostname())
        if hostname_ip:
            return hostname_ip
    except OSError:
        pass

    return "127.0.0.1"


def get_clean_hostname() -> str:
    """Return a clean, mDNS-safe machine hostname."""
    raw = socket.gethostname() or "Desktop"
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", raw).strip("-")
    return cleaned or "Desktop"


class MDNSAdvertiser:
    """Manages Zeroconf mDNS registration for _flickshare._tcp.local."""

    def __init__(self, port: int, ip_address: Optional[str] = None) -> None:
        self.port = port
        self.ip_address = ip_address or get_local_ip()
        self.hostname = get_clean_hostname()
        self.aiozc: Optional[AsyncZeroconf] = None
        self.service_info: Optional[ServiceInfo] = None

    def build_service_info(self) -> ServiceInfo:
        service_name = f"FlickShare-{self.hostname}.{SERVICE_TYPE}"
        server_fqdn = f"{self.hostname}.local."
        properties = {
            b"version": b"1.0",
            b"hostname": self.hostname.encode("utf-8"),
            b"platform": platform.system().lower().encode("utf-8"),
            b"endpoint": b"/upload",
        }
        return ServiceInfo(
            type_=SERVICE_TYPE,
            name=service_name,
            addresses=[socket.inet_aton(self.ip_address)],
            port=self.port,
            properties=properties,
            server=server_fqdn,
        )

    async def start(self) -> bool:
        try:
            self.service_info = self.build_service_info()
            self.aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
            await self.aiozc.async_register_service(
                self.service_info, allow_name_change=True
            )
            logger.info(
                "mDNS registered: %s -> %s:%d",
                self.service_info.name,
                self.ip_address,
                self.port,
            )
            return True
        except Exception as exc:
            logger.warning("Failed to register mDNS service: %s", exc)
            await self.stop()
            return False

    async def stop(self) -> None:
        if self.aiozc is not None:
            try:
                if self.service_info is not None:
                    await self.aiozc.async_unregister_service(self.service_info)
            except Exception as exc:
                logger.debug("Error unregistering mDNS service: %s", exc)
            finally:
                try:
                    await self.aiozc.async_close()
                except Exception:
                    pass
                self.aiozc = None
                self.service_info = None
                logger.info("mDNS service unregistered.")


# ---------------------------------------------------------------------------
# Filename Sanitization & Collision Auto-Rename
# ---------------------------------------------------------------------------

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def sanitize_filename(filename: Optional[str]) -> str:
    """
    Sanitize an untrusted filename for safe cross-platform saving.
    Strips directory traversal components, control characters, and Windows-illegal chars.
    """
    if not filename:
        return "flicked_file"

    # Normalize slashes so Windows & POSIX path segments are stripped consistently
    normalized = filename.replace("\\", "/").strip()
    base_name = Path(normalized).name

    # Replace illegal characters with underscore
    cleaned = _INVALID_FILENAME_CHARS.sub("_", base_name)
    # Strip leading/trailing dots and whitespace (Windows disallows trailing dots/spaces)
    cleaned = cleaned.strip(" .")

    if not cleaned or cleaned in {".", ".."}:
        return "flicked_file"

    # Guard against Windows reserved device names (CON, NUL, COM1, etc.)
    stem_upper = Path(cleaned).stem.upper()
    if stem_upper in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    # Cap filename length to 200 chars while preserving extension
    if len(cleaned) > 200:
        p = Path(cleaned)
        ext = p.suffix[:30]
        stem = p.stem[: 200 - len(ext)]
        cleaned = f"{stem}{ext}"

    return cleaned or "flicked_file"


def resolve_unique_path(directory: Path, filename: str) -> Path:
    """
    Return a non-colliding Path inside `directory`.
    If `file.png` exists, tries `file_1.png`, `file_2.png`, etc.
    """
    safe_name = sanitize_filename(filename)
    candidate = directory / safe_name
    if not candidate.exists():
        return candidate

    parsed = Path(safe_name)
    stem = parsed.stem
    suffix = parsed.suffix

    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def format_bytes(num_bytes: int) -> str:
    """Format byte count into human-readable string."""
    size = float(max(num_bytes, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.2f} TB"


# ---------------------------------------------------------------------------
# Post-Receive Actions: Clipboard & Toast Notifications
# ---------------------------------------------------------------------------


def is_image_file(file_path: Path) -> bool:
    """Return True if the file extension indicates a supported bitmap image."""
    return file_path.suffix.lower() in IMAGE_EXTENSIONS


def copy_text_to_clipboard(text: str) -> bool:
    """Copy plain text (such as a file path) to the system clipboard."""
    try:
        pyperclip.copy(text)
        return True
    except Exception as exc:
        logger.debug("pyperclip.copy failed (%s); trying native OS command", exc)

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["clip"],
                input=text.encode("utf-16le"),
                check=True,
                timeout=5,
                creationflags= getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        elif sys.platform == "darwin":
            subprocess.run(
                ["pbcopy"],
                input=text.encode("utf-8"),
                check=True,
                timeout=5,
            )
            return True
        else:
            subprocess.run(
                ["xclip", "-selection", "clipboard"],
                input=text.encode("utf-8"),
                check=True,
                timeout=5,
            )
            return True
    except Exception as fallback_exc:
        logger.warning("Unable to copy text to clipboard: %s", fallback_exc)
        return False


def copy_image_to_clipboard(file_path: Path) -> bool:
    """
    Copy image bitmap data directly to the OS clipboard so the user can Ctrl+V paste the image.
    Falls back to copying the file path text if headless or unavailable.
    """
    resolved = file_path.resolve()
    temp_converted_png: Optional[Path] = None

    try:
        # Validate image with Pillow; convert WebP to PNG on Windows because
        # System.Drawing.Image.FromFile does not natively decode WebP.
        with Image.open(resolved) as img:
            img.load()
            if sys.platform == "win32" and resolved.suffix.lower() == ".webp":
                temp_converted_png = resolved.with_name(
                    f".{resolved.stem}_{uuid.uuid4().hex[:6]}_clip.png"
                )
                img.convert("RGBA").save(temp_converted_png, format="PNG")

        target_img_path = temp_converted_png or resolved

        if sys.platform == "win32":
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms, System.Drawing; "
                "$img = [System.Drawing.Image]::FromFile($env:FLICK_IMG_PATH); "
                "try { [System.Windows.Forms.Clipboard]::SetImage($img) } "
                "finally { $img.Dispose() }"
            )
            env = {**os.environ, "FLICK_IMG_PATH": str(target_img_path)}
            subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
                env=env,
                check=True,
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True

        elif sys.platform == "darwin":
            escaped = str(target_img_path).replace('"', '\\"')
            osa_cmd = f'set the clipboard to (read (POSIX file "{escaped}") as TIFF picture)'
            subprocess.run(
                ["osascript", "-e", osa_cmd],
                check=True,
                timeout=8,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            return True

        else:
            # Linux X11 clipboard via xclip
            subprocess.run(
                [
                    "xclip",
                    "-selection",
                    "clipboard",
                    "-t",
                    "image/png",
                    "-i",
                    str(target_img_path),
                ],
                check=True,
                timeout=8,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            return True

    except Exception as exc:
        logger.warning(
            "Image bitmap clipboard copy failed (%s); falling back to path copy.", exc
        )
        return copy_text_to_clipboard(str(resolved))
    finally:
        if temp_converted_png is not None and temp_converted_png.exists():
            try:
                temp_converted_png.unlink()
            except OSError:
                pass


def copy_to_clipboard(file_path: Path) -> bool:
    """
    Smart clipboard handler:
    - If `file_path` is an image (.png, .jpg, .jpeg, .bmp, .webp, .gif), copies image bitmap.
    - Otherwise, copies the resolved file path string to the clipboard.
    """
    if is_image_file(file_path):
        return copy_image_to_clipboard(file_path)
    return copy_text_to_clipboard(str(file_path.resolve()))


def copy_files_to_clipboard(file_paths: List[Path]) -> bool:
    """Copy a single file (image bitmap or path) or multiple file paths to clipboard."""
    if not file_paths:
        return False
    if len(file_paths) == 1:
        return copy_to_clipboard(file_paths[0])
    joined_paths = "\n".join(str(p.resolve()) for p in file_paths)
    return copy_text_to_clipboard(joined_paths)


def send_catch_notification(
    filename: str,
    size_bytes: int,
    speed_mbps: float,
    file_count: int = 1,
) -> bool:
    """
    Trigger a native OS toast notification confirming the file catch.
    Tries `plyer.notification` first, then falls back to platform-native commands.
    Never raises an exception.
    """
    size_str = format_bytes(size_bytes)
    title = (
        "FlickShare — File Caught!"
        if file_count == 1
        else f"FlickShare — {file_count} Files Caught!"
    )
    message = f"{filename} ({size_str} @ {speed_mbps:.2f} MB/s)"

    # 1. Attempt plyer notification
    try:
        from plyer import notification as plyer_notification

        if plyer_notification is not None and hasattr(plyer_notification, "notify"):
            plyer_notification.notify(
                title=title,
                message=message,
                app_name="FlickShare",
                timeout=4,
            )
            return True
    except Exception as exc:
        logger.debug("plyer notification failed (%s); using OS fallback", exc)

    # 2. Native OS fallback
    try:
        if sys.platform == "win32":
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms, System.Drawing; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                "$n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info; "
                "$n.BalloonTipTitle = $env:FLICK_NOTIFY_TITLE; "
                "$n.BalloonTipText = $env:FLICK_NOTIFY_MSG; "
                "$n.Visible = $true; "
                "$n.ShowBalloonTip(3000); "
                "Start-Sleep -Milliseconds 350; "
                "$n.Dispose()"
            )
            env = {
                **os.environ,
                "FLICK_NOTIFY_TITLE": title,
                "FLICK_NOTIFY_MSG": message,
            }
            subprocess.Popen(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True

        elif sys.platform == "darwin":
            safe_title = title.replace('"', '\\"')
            safe_msg = message.replace('"', '\\"')
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    f'display notification "{safe_msg}" with title "{safe_title}"',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True

        else:
            subprocess.Popen(
                ["notify-send", title, message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    except Exception as fallback_exc:
        logger.debug("Native notification fallback failed: %s", fallback_exc)
        return False


# ---------------------------------------------------------------------------
# FastAPI Lifespan & Application Setup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: ReceiverConfig = getattr(app.state, "config", config)
    cfg.save_dir.mkdir(parents=True, exist_ok=True)
    local_ip = get_local_ip()
    hostname = get_clean_hostname()

    advertiser: Optional[MDNSAdvertiser] = None
    if cfg.enable_mdns:
        advertiser = MDNSAdvertiser(port=cfg.port, ip_address=local_ip)
        await advertiser.start()
    else:
        logger.info("mDNS advertising is disabled (--no-mdns / FLICKSHARE_NO_MDNS=1).")

    logger.info("=" * 60)
    logger.info("FlickShare Desktop Receiver Ready")
    logger.info("  Hostname   : %s", hostname)
    logger.info("  LAN Endpoint: http://%s:%d/upload", local_ip, cfg.port)
    logger.info("  Save Folder: %s", cfg.save_dir.resolve())
    logger.info("  mDNS Type  : %s (enabled=%s)", SERVICE_TYPE, cfg.enable_mdns)
    logger.info("=" * 60)

    try:
        yield
    finally:
        if advertiser is not None:
            await advertiser.stop()


app = FastAPI(
    title="FlickShare Desktop Receiver",
    version="1.0.0",
    description="Zero-config LAN file receiver with mDNS discovery and streaming disk I/O.",
    lifespan=lifespan,
)
app.state.config = config

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def parse_throw_direction(raw: bytes) -> Optional[float]:
    """
    Parse the `X-FlickShare-Direction` header: the angle (degrees) the throw arrives from,
    0 = straight on from below the screen, negative = from the left, positive = from the right.
    Values are clamped to [-90, 90]; anything unparsable is ignored.
    """
    try:
        value = float(raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return max(-90.0, min(90.0, value))


class TransferProgressMiddleware:
    """
    Pure ASGI middleware that reports raw network progress of `POST /upload` bodies.

    Starlette parses multipart bodies before the endpoint runs, so byte-level progress
    must be measured at the ASGI `receive` channel.
    """

    def __init__(self, asgi_app: Any) -> None:
        self.app = asgi_app

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/upload"
        ):
            await self.app(scope, receive, send)
            return

        transfer_id = uuid.uuid4().hex[:12]
        scope["flickshare.transfer_id"] = transfer_id
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"

        total_bytes = 0
        direction: Optional[float] = None
        direction_source = ""
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    total_bytes = max(int(value), 0)
                except ValueError:
                    total_bytes = 0
            elif name == b"x-flickshare-direction":
                direction = parse_throw_direction(value)
            elif name == b"x-flickshare-direction-source":
                direction_source = value.decode("latin-1", "replace")[:16]
        if direction is not None:
            logger.info("Throw from %s aimed %+.0f deg (%s)", client_ip, direction, direction_source or "unknown")

        start = time.perf_counter()
        received = 0
        last_emit = 0.0
        response_status: Dict[str, int] = {}

        emit_event(
            "transfer_started",
            transfer_id=transfer_id,
            client=client_ip,
            total_bytes=total_bytes,
            direction=direction,
            direction_source=direction_source if direction is not None else "",
        )

        async def tracked_receive() -> Dict[str, Any]:
            nonlocal received, last_emit
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                now = time.perf_counter()
                finished = not message.get("more_body", False)
                if finished or now - last_emit >= PROGRESS_EMIT_INTERVAL_S:
                    last_emit = now
                    emit_event(
                        "transfer_progress",
                        transfer_id=transfer_id,
                        client=client_ip,
                        received_bytes=received,
                        total_bytes=total_bytes,
                        elapsed_s=now - start,
                    )
            return message

        async def tracked_send(message: Dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                response_status["status"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, tracked_receive, tracked_send)
        except Exception as exc:
            emit_event(
                "transfer_failed",
                transfer_id=transfer_id,
                client=client_ip,
                error=str(exc) or exc.__class__.__name__,
            )
            raise

        status = response_status.get("status", 500)
        if status >= 400:
            emit_event(
                "transfer_failed",
                transfer_id=transfer_id,
                client=client_ip,
                error=f"HTTP {status}",
            )


app.add_middleware(TransferProgressMiddleware)


def _get_active_config(request: Request) -> ReceiverConfig:
    return getattr(request.app.state, "config", config)


_BROWSER_UPLOAD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FlickShare &middot; __HOST__</title>
<style>
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1117;color:#e7e9ee;
display:flex;min-height:100vh;align-items:center;justify-content:center}
main{width:min(92vw,420px);text-align:center}
h1{font-size:1.4rem;margin:0 0 .25rem}p{color:#9aa3b2;margin:.25rem 0 1.5rem}
label{display:block;padding:2.5rem 1rem;border:2px dashed #7c5cff;border-radius:18px;background:#171a23;cursor:pointer;font-weight:600}
input{display:none}progress{width:100%;height:10px;margin-top:1.25rem;accent-color:#22d3ee}
#s{margin-top:.75rem;min-height:1.2em;color:#9aa3b2}
</style></head><body><main>
<h1>Throw to __HOST__</h1><p>Files land in the PC's FlickShare folder.</p>
<label for="f">Tap to pick files</label><input id="f" type="file" multiple>
<progress id="p" max="1" value="0" hidden></progress><div id="s"></div>
<script>
const f=document.getElementById('f'),p=document.getElementById('p'),s=document.getElementById('s');
f.onchange=()=>{if(!f.files.length)return;const d=new FormData();for(const x of f.files)d.append('files',x);
const r=new XMLHttpRequest();r.open('POST','/upload');p.hidden=false;p.value=0;s.textContent='Throwing\\u2026';
r.upload.onprogress=e=>{if(e.lengthComputable)p.value=e.loaded/e.total};
r.onload=()=>{s.textContent=r.status==200?'Caught! \\u2713':'Failed ('+r.status+')';f.value=''};
r.onerror=()=>{s.textContent='Connection lost';f.value=''};r.send(d)};
</script></main></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def browser_upload_page() -> HTMLResponse:
    """Minimal browser upload page (opened by scanning the desktop app's QR code)."""
    return HTMLResponse(_BROWSER_UPLOAD_PAGE.replace("__HOST__", html.escape(get_clean_hostname())))


@app.get("/health")
@app.get("/info")
async def health_info(request: Request) -> Dict[str, Any]:
    """Return receiver status, discovery metadata, and target output directory."""
    cfg = _get_active_config(request)
    return {
        "status": "ok",
        "service": SERVICE_TYPE,
        "version": "1.0",
        "hostname": get_clean_hostname(),
        "ip": get_local_ip(),
        "port": cfg.port,
        "save_dir": str(cfg.save_dir.resolve()),
    }


async def _stream_upload_to_disk(upload: UploadFile, save_dir: Path) -> Dict[str, Any]:
    """Stream a single UploadFile in 1 MB chunks to a .part file, then atomically rename."""
    start_ns = time.perf_counter_ns()
    save_dir.mkdir(parents=True, exist_ok=True)

    original_name = upload.filename or "flicked_file"
    dest_path = resolve_unique_path(save_dir, original_name)
    temp_path = save_dir / f".{dest_path.name}.{uuid.uuid4().hex[:8]}.part"

    total_bytes = 0
    try:
        with open(temp_path, "wb") as out_file:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                out_file.write(chunk)
                total_bytes += len(chunk)
            out_file.flush()

        # Re-verify collision just before atomic move in case of concurrent uploads
        if dest_path.exists():
            dest_path = resolve_unique_path(save_dir, original_name)
        os.replace(temp_path, dest_path)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise
    finally:
        await upload.close()

    elapsed_ms = max((time.perf_counter_ns() - start_ns) / 1_000_000.0, 0.01)
    speed_mbps = (total_bytes / (1024 * 1024)) / (elapsed_ms / 1000.0)

    return {
        "filename": dest_path.name,
        "saved_path": str(dest_path.resolve()),
        "size_bytes": total_bytes,
        "duration_ms": round(elapsed_ms, 2),
        "speed_mbps": round(speed_mbps, 2),
        "content_type": upload.content_type or "application/octet-stream",
        "_path_obj": dest_path,
    }


@app.post("/upload")
async def upload_endpoint(
    request: Request,
    files: Optional[List[UploadFile]] = File(default=None),
    file: Optional[UploadFile] = File(default=None),
) -> Dict[str, Any]:
    """
    Accept single or multiple files streamed over multipart/form-data.
    Supports field names `file` and/or `files`.
    """
    cfg = _get_active_config(request)

    incoming: List[UploadFile] = []
    if files:
        incoming.extend(files)
    if file is not None:
        incoming.append(file)

    if not incoming:
        raise HTTPException(
            status_code=400,
            detail="No file uploaded. Provide 'file' or 'files' in multipart form-data.",
        )

    batch_start_ns = time.perf_counter_ns()
    saved_entries: List[Dict[str, Any]] = []
    saved_paths: List[Path] = []

    for item in incoming:
        entry = await _stream_upload_to_disk(item, cfg.save_dir)
        saved_paths.append(entry.pop("_path_obj"))
        saved_entries.append(entry)
        logger.info(
            "Caught '%s' (%s) in %.1f ms [%.2f MB/s] -> %s",
            entry["filename"],
            format_bytes(entry["size_bytes"]),
            entry["duration_ms"],
            entry["speed_mbps"],
            entry["saved_path"],
        )

    total_bytes = sum(e["size_bytes"] for e in saved_entries)
    total_duration_ms = max(
        (time.perf_counter_ns() - batch_start_ns) / 1_000_000.0, 0.01
    )
    overall_speed_mbps = round(
        (total_bytes / (1024 * 1024)) / (total_duration_ms / 1000.0), 2
    )

    clipboard_copied = False
    if cfg.enable_clipboard and saved_paths:
        clipboard_copied = await asyncio.to_thread(copy_files_to_clipboard, saved_paths)

    notification_sent = False
    if cfg.enable_notifications and saved_entries:
        primary_label = (
            saved_entries[0]["filename"]
            if len(saved_entries) == 1
            else f"{saved_entries[0]['filename']} (+{len(saved_entries) - 1} more)"
        )
        notification_sent = await asyncio.to_thread(
            send_catch_notification,
            primary_label,
            total_bytes,
            overall_speed_mbps,
            len(saved_entries),
        )

    primary = saved_entries[0]
    client = request.client
    emit_event(
        "transfer_completed",
        transfer_id=request.scope.get("flickshare.transfer_id"),
        client=client.host if client else "unknown",
        files=[dict(entry) for entry in saved_entries],
        total_bytes=total_bytes,
        duration_ms=round(total_duration_ms, 2),
        speed_mbps=overall_speed_mbps,
        clipboard_copied=clipboard_copied,
    )
    return {
        "status": "ok",
        "filename": primary["filename"],
        "saved_path": primary["saved_path"],
        "size_bytes": total_bytes,
        "duration_ms": round(total_duration_ms, 2),
        "speed_mbps": overall_speed_mbps,
        "clipboard_copied": clipboard_copied,
        "notification_sent": notification_sent,
        "files": saved_entries,
    }


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="FlickShare Desktop Receiver — Catch files flicked from your phone."
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host interface to bind (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FLICKSHARE_PORT", str(DEFAULT_PORT))),
        help=f"TCP port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(
            os.environ.get("FLICKSHARE_OUTPUT_DIR", str(Path.home() / "Desktop"))
        ),
        help="Directory where incoming files are saved (default: ~/Desktop)",
    )
    parser.add_argument(
        "--no-mdns",
        action="store_true",
        help="Disable Zeroconf mDNS service broadcasting",
    )
    parser.add_argument(
        "--no-clipboard",
        action="store_true",
        help="Disable automatic clipboard copy after receiving a file",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Disable OS toast notifications on file receive",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    config.host = args.host
    config.port = args.port
    config.save_dir = args.output_dir.expanduser()
    if args.no_mdns:
        config.enable_mdns = False
    if args.no_clipboard:
        config.enable_clipboard = False
    if args.no_notify:
        config.enable_notifications = False

    app.state.config = config
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
