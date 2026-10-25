"""Find other FlickShare devices (PCs and phones) on the LAN and send files to them.

Every FlickShare device runs the same receiver on port 8765 (`GET /info`, `POST /transfer/request`,
`POST /upload`). This module is Qt-free so it can be unit tested; see `devices_panel.py` for the UI.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import mimetypes
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

logger = logging.getLogger("flickshare.app")

DEFAULT_PORT = 8765
PROBE_TIMEOUT_S = 0.6
SCAN_WORKERS = 64
REQUEST_TIMEOUT_S = 130.0  # receivers hold the approval request open for up to 120 s
UPLOAD_TIMEOUT_S = 60.0
CHUNK_SIZE = 1024 * 1024
PROGRESS_INTERVAL_S = 0.05
PHONE_PLATFORMS = {"android", "ios"}


@dataclass(frozen=True)
class Peer:
    device_id: str
    name: str
    platform: str
    ip: str
    port: int
    protocol: int = 1
    requires_approval: bool = False

    @property
    def is_phone(self) -> bool:
        return self.platform in PHONE_PLATFORMS

    @property
    def address(self) -> str:
        return f"{self.ip}:{self.port}"


@dataclass(frozen=True)
class Identity:
    device_id: str
    name: str
    platform: str


def _text(value: Any, limit: int = 64) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable()).strip()[:limit]


def peer_from_info(ip: str, port: int, info: Any) -> Optional[Peer]:
    """Build a Peer from a `/info` response; v1 receivers (no protocol/device_id) are old desktops."""
    if not isinstance(info, dict) or info.get("status") != "ok":
        return None
    protocol = info.get("protocol", 1)
    if not isinstance(protocol, int) or isinstance(protocol, bool) or protocol < 1:
        protocol = 1
    return Peer(
        device_id=_text(info.get("device_id")) or f"ip:{ip}",
        name=_text(info.get("device_name")) or _text(info.get("hostname")) or f"Device ({ip})",
        platform=_text(info.get("platform"), 16).lower() or "windows",
        ip=ip,
        port=port,
        protocol=protocol,
        requires_approval=bool(info.get("requires_approval", False)),
    )


def parse_host(text: str, default_port: int = DEFAULT_PORT) -> Optional[Tuple[str, int]]:
    """Parse 'ip', 'ip:port' or 'http://ip:port/...' into (host, port)."""
    raw = text.strip()
    for prefix in ("http://", "https://"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
    raw = raw.split("/", 1)[0]
    host, _, port_text = raw.partition(":")
    host = host.strip()
    if not host or any(ch.isspace() for ch in host):
        return None
    port = default_port
    if port_text:
        try:
            port = int(port_text)
        except ValueError:
            return None
    if not 1 <= port <= 65535:
        return None
    return host, port


def subnet_hosts(local_ip: str) -> List[str]:
    """All other /24 addresses next to a private IPv4 address (empty for loopback/public/invalid)."""
    try:
        addr = ipaddress.ip_address(local_ip)
    except ValueError:
        return []
    if addr.version != 4 or addr.is_loopback or not addr.is_private:
        return []
    prefix = local_ip.rsplit(".", 1)[0]
    return [f"{prefix}.{n}" for n in range(1, 255) if f"{prefix}.{n}" != local_ip]


def _get_json(host: str, port: int, path: str, timeout: float) -> Any:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Accept": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        return json.loads(resp.read(64 * 1024))
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        conn.close()


def probe_peer(host: str, port: int = DEFAULT_PORT, timeout: float = PROBE_TIMEOUT_S) -> Optional[Peer]:
    return peer_from_info(host, port, _get_json(host, port, "/info", timeout))


def scan_network(
    local_ip: str,
    own_device_id: str,
    port: int = DEFAULT_PORT,
    extra_hosts: Iterable[Tuple[str, int]] = (),
    timeout: float = PROBE_TIMEOUT_S,
    probe: Callable[[str, int, float], Optional[Peer]] = probe_peer,
) -> List[Peer]:
    """Probe the local /24 plus `extra_hosts`; drop this device; phones first, then by name."""
    targets: List[Tuple[str, int]] = [(ip, port) for ip in subnet_hosts(local_ip)]
    for target in extra_hosts:
        if target not in targets:
            targets.append(target)
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(SCAN_WORKERS, len(targets))) as pool:
        results = list(pool.map(lambda t: probe(t[0], t[1], timeout), targets))
    peers: Dict[str, Peer] = {}
    for peer in results:
        if peer is None or peer.device_id == own_device_id:
            continue
        peers.setdefault(peer.device_id, peer)
    return sorted(peers.values(), key=lambda p: (not p.is_phone, p.name.lower(), p.ip))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


class SendError(Exception):
    pass


class SendCancelled(SendError):
    pass


@dataclass(frozen=True)
class OutgoingFile:
    path: Path
    name: str
    size: int
    mime: str

    @classmethod
    def from_path(cls, path: Path) -> "OutgoingFile":
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return cls(path=path, name=path.name, size=path.stat().st_size, mime=mime)


def _quote_filename(name: str) -> str:
    # Same escaping browsers use for multipart filenames.
    return name.replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A")


def multipart_layout(files: Sequence[OutgoingFile], boundary: str) -> Tuple[List[bytes], bytes, int]:
    """Per-file part headers, the closing delimiter, and the exact Content-Length of the body."""
    heads = [
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{_quote_filename(f.name)}"\r\n'
            f"Content-Type: {f.mime}\r\n\r\n"
        ).encode("utf-8")
        for f in files
    ]
    tail = f"--{boundary}--\r\n".encode("ascii")
    length = sum(len(h) + f.size + 2 for h, f in zip(heads, files)) + len(tail)
    return heads, tail, length


ProgressCallback = Callable[[int, int, str, float], None]  # sent, total, current file, elapsed s


class FileSender:
    """Handshake (`/transfer/request`) + streaming multipart upload to one peer. Blocking; run on a thread."""

    def __init__(
        self,
        peer: Peer,
        paths: Sequence[Path],
        identity: Identity,
        on_state: Optional[Callable[[str], None]] = None,
        on_progress: Optional[ProgressCallback] = None,
    ) -> None:
        self.peer = peer
        self.identity = identity
        self.files = [OutgoingFile.from_path(Path(p)) for p in paths]
        self.total_bytes = sum(f.size for f in self.files)
        self._on_state = on_state or (lambda _state: None)
        self._on_progress = on_progress or (lambda *_args: None)
        self._cancelled = threading.Event()
        self._conn_lock = threading.Lock()
        self._conn: Optional[http.client.HTTPConnection] = None

    def cancel(self) -> None:
        self._cancelled.set()
        with self._conn_lock:
            conn = self._conn
        if conn is not None:
            try:
                if conn.sock is not None:
                    conn.sock.shutdown(2)
            except OSError:
                pass

    def run(self) -> Dict[str, Any]:
        if not self.files:
            raise SendError("Nothing to send")
        token = ""
        if self.peer.protocol >= 2:
            self._on_state("waiting")
            token = self._request_token()
        self._check_cancelled()
        self._on_state("sending")
        return self._upload(token)

    # -- internals

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise SendCancelled("Cancelled")

    def _open(self, timeout: float) -> http.client.HTTPConnection:
        conn = http.client.HTTPConnection(self.peer.ip, self.peer.port, timeout=timeout)
        with self._conn_lock:
            self._conn = conn
        return conn

    def _close(self, conn: http.client.HTTPConnection) -> None:
        with self._conn_lock:
            self._conn = None
        conn.close()

    def _sender_headers(self) -> Dict[str, str]:
        return {
            "X-FlickShare-Client": "FlickShare-Desktop/2.0",
            "X-FlickShare-Sender-Id": self.identity.device_id,
            "X-FlickShare-Sender-Name": quote(self.identity.name, safe=""),
            "X-FlickShare-Sender-Platform": self.identity.platform,
        }

    @staticmethod
    def _detail(body: bytes) -> str:
        try:
            data = json.loads(body)
        except ValueError:
            return ""
        detail = data.get("detail") if isinstance(data, dict) else None
        return detail if isinstance(detail, str) else ""

    def _request_token(self) -> str:
        payload = json.dumps(
            {
                "sender_id": self.identity.device_id,
                "sender_name": self.identity.name,
                "sender_platform": self.identity.platform,
                "files": [{"name": f.name, "size": f.size, "mime": f.mime} for f in self.files],
                "total_bytes": self.total_bytes,
            }
        ).encode("utf-8")
        conn = self._open(REQUEST_TIMEOUT_S)
        try:
            headers = {"Content-Type": "application/json", **self._sender_headers()}
            conn.request("POST", "/transfer/request", body=payload, headers=headers)
            resp = conn.getresponse()
            body = resp.read(64 * 1024)
        except (OSError, http.client.HTTPException) as exc:
            self._check_cancelled()
            raise SendError(f"Couldn't reach {self.peer.name}: {exc}") from exc
        finally:
            self._close(conn)
        self._check_cancelled()
        if resp.status == 200:
            try:
                token = json.loads(body).get("token")
            except (ValueError, AttributeError):
                token = None
            if not isinstance(token, str) or not token:
                raise SendError(f"{self.peer.name} sent an invalid reply")
            return token
        if resp.status == 403:
            raise SendError(self._detail(body) or f"{self.peer.name} declined the files")
        if resp.status == 408:
            raise SendError(self._detail(body) or f"No response from {self.peer.name}")
        raise SendError(self._detail(body) or f"{self.peer.name} answered HTTP {resp.status}")

    def _upload(self, token: str) -> Dict[str, Any]:
        boundary = "----FlickShare" + secrets.token_hex(12)
        heads, tail, length = multipart_layout(self.files, boundary)
        conn = self._open(UPLOAD_TIMEOUT_S)
        start = time.monotonic()
        sent = 0
        last_emit = 0.0
        try:
            try:
                conn.putrequest("POST", "/upload", skip_accept_encoding=True)
                conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
                conn.putheader("Content-Length", str(length))
                if token:
                    conn.putheader("X-FlickShare-Token", token)
                for key, value in self._sender_headers().items():
                    conn.putheader(key, value)
                conn.endheaders()
                for head, item in zip(heads, self.files):
                    conn.send(head)
                    remaining = item.size
                    with open(item.path, "rb") as handle:
                        while remaining > 0:
                            self._check_cancelled()
                            chunk = handle.read(min(CHUNK_SIZE, remaining))
                            if not chunk:
                                raise SendError(f"{item.name} changed while it was being sent")
                            conn.send(chunk)
                            remaining -= len(chunk)
                            sent += len(chunk)
                            now = time.monotonic()
                            if now - last_emit >= PROGRESS_INTERVAL_S:
                                last_emit = now
                                self._on_progress(sent, self.total_bytes, item.name, now - start)
                    conn.send(b"\r\n")
                conn.send(tail)
            except OSError as exc:
                self._check_cancelled()
                # The receiver may have answered early (e.g. 403); prefer its explanation.
                try:
                    resp = conn.getresponse()
                    detail = self._detail(resp.read(64 * 1024))
                except (OSError, http.client.HTTPException):
                    detail = ""
                raise SendError(detail or f"Connection to {self.peer.name} lost: {exc}") from exc
            self._on_progress(sent, self.total_bytes, self.files[-1].name, time.monotonic() - start)
            try:
                resp = conn.getresponse()
                body = resp.read(1024 * 1024)
            except (OSError, http.client.HTTPException) as exc:
                self._check_cancelled()
                raise SendError(f"{self.peer.name} didn't confirm the files: {exc}") from exc
        finally:
            self._close(conn)
        if resp.status != 200:
            raise SendError(self._detail(body) or f"{self.peer.name} answered HTTP {resp.status}")
        try:
            result = json.loads(body)
        except ValueError:
            result = {}
        return result if isinstance(result, dict) else {}
