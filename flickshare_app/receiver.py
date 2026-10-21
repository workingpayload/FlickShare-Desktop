"""Runs the FastAPI receiver (server.py) on a background thread and bridges events to Qt."""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import uvicorn
from PySide6.QtCore import QObject, Signal

import server

logger = logging.getLogger("flickshare.app")


@dataclass
class ReceiverStatus:
    running: bool = False
    ip: str = ""
    hostname: str = ""
    port: int = 0
    mdns: bool = False
    error: str = ""


def bind_listen_socket(host: str, port: int) -> socket.socket:
    """Bind the TCP listening socket up-front so port conflicts surface as a clean OSError."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.set_inheritable(True)
    except OSError:
        sock.close()
        raise
    return sock


class ReceiverController(QObject):
    """Owns the uvicorn server thread. All signals are delivered on the Qt main thread."""

    status_changed = Signal(object)  # ReceiverStatus
    receiver_event = Signal(str, dict)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.status = ReceiverStatus()
        server.add_event_listener(self._forward_event)

    def _forward_event(self, event: str, payload: Dict[str, Any]) -> None:
        # Called from the server thread; Qt queues cross-thread signal emission.
        self.receiver_event.emit(event, payload)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        port: int,
        save_dir: str,
        enable_mdns: bool,
        enable_clipboard: bool,
        host: str = "0.0.0.0",
    ) -> bool:
        with self._lock:
            if self.is_running:
                return True

            cfg = server.config
            cfg.host = host
            cfg.port = port
            cfg.save_dir = Path(save_dir).expanduser()
            cfg.enable_mdns = enable_mdns
            cfg.enable_clipboard = enable_clipboard
            # The GUI shows its own tray notifications instead of spawning PowerShell toasts.
            cfg.enable_notifications = False
            server.app.state.config = cfg

            try:
                cfg.save_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._set_status(ReceiverStatus(error=f"Cannot use save folder: {exc}"))
                return False

            try:
                sock = bind_listen_socket(host, port)
            except OSError as exc:
                logger.warning("Cannot bind port %d: %s", port, exc)
                self._set_status(
                    ReceiverStatus(port=port, error=f"Port {port} is unavailable (in use?)")
                )
                return False

            uv_config = uvicorn.Config(
                server.app,
                host=host,
                port=port,
                log_config=None,
                access_log=False,
                lifespan="on",
            )
            self._server = uvicorn.Server(uv_config)
            self._thread = threading.Thread(
                target=self._run, args=(self._server, sock), name="flickshare-receiver", daemon=True
            )
            self._thread.start()

        self._set_status(
            ReceiverStatus(
                running=True,
                ip=server.get_local_ip(),
                hostname=server.get_clean_hostname(),
                port=port,
                mdns=enable_mdns,
            )
        )
        return True

    def _run(self, uv_server: uvicorn.Server, sock: socket.socket) -> None:
        error = ""
        try:
            asyncio.run(uv_server.serve(sockets=[sock]))
        except SystemExit:
            error = "Receiver failed to start (see log)"
        except Exception as exc:
            logger.exception("Receiver crashed")
            error = f"Receiver crashed: {exc}"
        finally:
            try:
                sock.close()
            except OSError:
                pass
        if error or not uv_server.should_exit:
            # Unexpected stop: report it on the Qt thread.
            self.receiver_event.emit("server_stopped", {"error": error or "Receiver stopped"})

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            uv_server, thread = self._server, self._thread
            if uv_server is not None:
                uv_server.should_exit = True
            if thread is not None:
                thread.join(timeout)
                if thread.is_alive():
                    uv_server.force_exit = True
                    thread.join(2.0)
            self._server = None
            self._thread = None
        self._set_status(ReceiverStatus(port=server.config.port))

    def set_clipboard_enabled(self, enabled: bool) -> None:
        server.config.enable_clipboard = enabled

    def mark_stopped(self, error: str) -> None:
        with self._lock:
            self._server = None
            self._thread = None
        self._set_status(ReceiverStatus(port=server.config.port, error=error))

    def _set_status(self, status: ReceiverStatus) -> None:
        self.status = status
        self.status_changed.emit(status)

    def shutdown(self) -> None:
        self.stop()
        server.remove_event_listener(self._forward_event)
