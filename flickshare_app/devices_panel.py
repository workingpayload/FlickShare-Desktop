"""'Nearby devices' card: discover other FlickShare PCs/phones and send files to them."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, List, Optional, Tuple

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QToolButton,
    QWidget,
)

import server
from flickshare_app import APP_NAME, theme
from flickshare_app.peers import (
    DEFAULT_PORT,
    FileSender,
    Identity,
    Peer,
    SendCancelled,
    SendError,
    parse_host,
    probe_peer,
    scan_network,
)
from flickshare_app.widgets import Card, format_bytes, make_label

logger = logging.getLogger("flickshare.app")

PEER_ROLE = Qt.ItemDataRole.UserRole + 2
RESCAN_MS = 20_000
PLATFORM_LABELS = {"android": "Android", "ios": "iPhone", "windows": "Windows", "macos": "Mac", "linux": "Linux"}


def platform_icon(platform: str) -> str:
    return "📱" if platform in ("android", "ios") else ("🌐" if platform == "web" else "💻")


def platform_label(platform: str) -> str:
    return PLATFORM_LABELS.get(platform, platform.capitalize() or "Device")


def local_identity() -> Identity:
    return Identity(server.config.device_id, server.config.device_name, server.platform_name())


def dropped_paths(event: QDragEnterEvent | QDragMoveEvent | QDropEvent) -> Tuple[List[Path], int]:
    """Local files in a drag (folders are counted separately, they can't be sent)."""
    files: List[Path] = []
    folders = 0
    mime = event.mimeData()
    if mime.hasUrls():
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.is_file():
                files.append(path)
            elif path.is_dir():
                folders += 1
    return files, folders


class PeerScanner(QObject):
    """Runs LAN scans / single-host probes on worker threads; results arrive on the Qt thread."""

    scan_finished = Signal(list)  # List[Peer]
    probe_finished = Signal(str, int, object)  # host, port, Optional[Peer]

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._scanning = False

    @property
    def scanning(self) -> bool:
        return self._scanning

    def scan(self, extra_hosts: List[Tuple[str, int]]) -> bool:
        if self._scanning:
            return False
        self._scanning = True
        threading.Thread(target=self._scan, args=(list(extra_hosts),), name="flickshare-scan", daemon=True).start()
        return True

    def _scan(self, extra_hosts: List[Tuple[str, int]]) -> None:
        peers: List[Peer] = []
        try:
            peers = scan_network(server.get_local_ip(), server.config.device_id, DEFAULT_PORT, extra_hosts)
        except Exception:
            logger.exception("Device scan failed")
        finally:
            self._scanning = False
        self.scan_finished.emit(peers)

    def probe(self, host: str, port: int) -> None:
        def run() -> None:
            peer = probe_peer(host, port, timeout=2.0)
            if peer is not None and peer.device_id == server.config.device_id:
                peer = None
            self.probe_finished.emit(host, port, peer)

        threading.Thread(target=run, name="flickshare-probe", daemon=True).start()


class SendJob(QObject):
    """Runs one FileSender on a worker thread."""

    state_changed = Signal(str)  # "waiting" | "sending"
    progress = Signal(object, object, str, float)  # sent, total (objects: may exceed 2 GB), name, elapsed
    finished = Signal(bool, str)  # ok, message

    def __init__(self, peer: Peer, paths: List[Path], parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.peer = peer
        self._sender = FileSender(
            peer,
            paths,
            local_identity(),
            on_state=self.state_changed.emit,
            on_progress=lambda sent, total, name, elapsed: self.progress.emit(sent, total, name, elapsed),
        )

    def start(self) -> None:
        threading.Thread(target=self._run, name="flickshare-send", daemon=True).start()

    def cancel(self) -> None:
        self._sender.cancel()

    def _run(self) -> None:
        files = self._sender.files
        try:
            self._sender.run()
        except SendCancelled:
            self.finished.emit(False, "Cancelled")
            return
        except SendError as exc:
            self.finished.emit(False, str(exc))
            return
        except Exception as exc:  # never leave the UI stuck in "sending"
            logger.exception("Send to %s failed", self.peer.address)
            self.finished.emit(False, f"Send failed: {exc}")
            return
        label = files[0].name if len(files) == 1 else f"{len(files)} files"
        self.finished.emit(True, f"{label} ({format_bytes(self._sender.total_bytes)}) sent to {self.peer.name}")


class DeviceList(QListWidget):
    """Device rows; dropping files onto a row sends them to that device."""

    files_dropped = Signal(object, list, int)  # Peer, List[Path], skipped folder count

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Devices")
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setDropIndicatorShown(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)

    def _peer_at(self, pos: QPoint) -> Optional[Peer]:
        item = self.itemAt(pos)
        return item.data(PEER_ROLE) if item is not None else None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt override
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802 - Qt override
        item = self.itemAt(event.position().toPoint())
        if item is not None and event.mimeData().hasUrls():
            self.setCurrentItem(item)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt override
        peer = self._peer_at(event.position().toPoint())
        if peer is None:
            event.ignore()
            return
        files, folders = dropped_paths(event)
        event.acceptProposedAction()
        self.files_dropped.emit(peer, files, folders)


class DevicesCard(Card):
    """Lists FlickShare devices on the network; drag files onto one, or use 'Send files…'."""

    notify = Signal(str, str)
    manual_peers_changed = Signal(list)

    def __init__(self, manual_peers: List[str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent=parent)
        self._manual_peers = list(manual_peers)
        self._peers: List[Peer] = []
        self._job: Optional[SendJob] = None
        self.scanner = PeerScanner(self)
        self.scanner.scan_finished.connect(self._on_scan_finished)
        self.scanner.probe_finished.connect(self._on_probe_finished)

        header = QHBoxLayout()
        header.addWidget(make_label("Send to nearby devices", "CardTitle"))
        self.count_pill = make_label("0", "Pill")
        header.addWidget(self.count_pill)
        header.addStretch(1)
        self.scan_label = make_label("", "Muted")
        header.addWidget(self.scan_label)
        self.refresh_btn = QToolButton()
        self.refresh_btn.setText("⟳")
        self.refresh_btn.setToolTip("Look for devices again")
        self.refresh_btn.clicked.connect(self.scan)
        header.addWidget(self.refresh_btn)
        self.add_btn = QToolButton()
        self.add_btn.setText("＋")
        self.add_btn.setToolTip("Add a device by IP address")
        self.add_btn.clicked.connect(self._add_by_ip)
        header.addWidget(self.add_btn)
        self.body.addLayout(header)

        self.list = DeviceList()
        self.list.setFixedHeight(150)
        self.list.files_dropped.connect(self._on_files_dropped)
        self.list.itemSelectionChanged.connect(self._update_buttons)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._device_menu)
        self.list.itemDoubleClicked.connect(lambda item: self.pick_files_for(item.data(PEER_ROLE)))
        self.body.addWidget(self.list)
        self.empty_label = make_label(
            "Looking for phones and PCs… Open FlickShare on another device on this Wi-Fi.", "Muted", wrap=True
        )
        self.body.addWidget(self.empty_label)

        status_row = QHBoxLayout()
        self.status_label = make_label("Drag files onto a device, or pick files to send.", "Muted", wrap=True)
        status_row.addWidget(self.status_label, 1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_send)
        self.cancel_btn.hide()
        status_row.addWidget(self.cancel_btn)
        self.send_btn = QPushButton("Send files…")
        self.send_btn.setObjectName("Primary")
        self.send_btn.clicked.connect(self._send_clicked)
        status_row.addWidget(self.send_btn)
        self.body.addLayout(status_row)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(8)
        self.bar.setTextVisible(False)
        self.bar.hide()
        self.body.addWidget(self.bar)

        self._timer = QTimer(self)
        self._timer.setInterval(RESCAN_MS)
        self._timer.timeout.connect(self._auto_scan)
        self._timer.start()
        self._render_peers()

    # ------------------------------------------------------------------ discovery

    def _extra_hosts(self) -> List[Tuple[str, int]]:
        return [hp for hp in (parse_host(p) for p in self._manual_peers) if hp is not None]

    def _auto_scan(self) -> None:
        window = self.window()
        if window.isVisible() and not window.isMinimized():
            self.scan()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.scan()

    def scan(self) -> None:
        if self.scanner.scan(self._extra_hosts()):
            self.scan_label.setText("Scanning…")
            self.refresh_btn.setEnabled(False)

    def _on_scan_finished(self, peers: List[Peer]) -> None:
        self.scan_label.setText("")
        self.refresh_btn.setEnabled(True)
        self._peers = peers
        self._render_peers()

    def _render_peers(self) -> None:
        selected = self._selected_peer()
        self.list.clear()
        for peer in self._peers:
            text = f"{platform_icon(peer.platform)}  {peer.name}\n      {platform_label(peer.platform)} · {peer.ip}"
            if peer.protocol < 2:
                text += " · older FlickShare"
            item = QListWidgetItem(text)
            item.setData(PEER_ROLE, peer)
            item.setToolTip(f"Drop files here to send them to {peer.name} ({peer.address})")
            self.list.addItem(item)
            if selected is not None and selected.device_id == peer.device_id:
                item.setSelected(True)
        count = len(self._peers)
        self.count_pill.setText(str(count))
        self.list.setVisible(count > 0)
        self.empty_label.setVisible(count == 0)
        self._update_buttons()

    def _add_by_ip(self) -> None:
        text, ok = QInputDialog.getText(
            self, APP_NAME, "IP address of the other device (optionally ip:port):", text=""
        )
        if not ok or not text.strip():
            return
        target = parse_host(text)
        if target is None:
            QMessageBox.warning(self, APP_NAME, f"'{text}' isn't a valid address.")
            return
        self.scan_label.setText(f"Checking {target[0]}…")
        self.scanner.probe(*target)

    def _on_probe_finished(self, host: str, port: int, peer: Optional[Peer]) -> None:
        self.scan_label.setText("")
        if peer is None:
            QMessageBox.information(
                self,
                APP_NAME,
                f"No FlickShare device answered at {host}:{port}.\n\n"
                "Make sure FlickShare is open on that device and both are on the same network.",
            )
            return
        entry = f"{host}:{port}"
        if entry not in self._manual_peers:
            self._manual_peers.append(entry)
            self.manual_peers_changed.emit(list(self._manual_peers))
        self._peers = [p for p in self._peers if p.device_id != peer.device_id] + [peer]
        self._render_peers()

    def _device_menu(self, pos: QPoint) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        peer: Peer = item.data(PEER_ROLE)
        menu = QMenu(self)
        menu.addAction("Send files…", lambda: self.pick_files_for(peer))
        if peer.address in self._manual_peers:
            menu.addAction("Forget this address", lambda: self._forget_manual(peer))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _forget_manual(self, peer: Peer) -> None:
        self._manual_peers = [p for p in self._manual_peers if p != peer.address]
        self.manual_peers_changed.emit(list(self._manual_peers))
        self.scan()

    # ------------------------------------------------------------------ sending

    def _selected_peer(self) -> Optional[Peer]:
        items = self.list.selectedItems()
        return items[0].data(PEER_ROLE) if items else None

    def _update_buttons(self) -> None:
        busy = self._job is not None
        self.send_btn.setEnabled(not busy and bool(self._peers))

    def _send_clicked(self) -> None:
        peer = self._selected_peer()
        if peer is None and len(self._peers) == 1:
            peer = self._peers[0]
        if peer is not None:
            self.pick_files_for(peer)
            return
        menu = QMenu(self)
        for candidate in self._peers:
            menu.addAction(
                f"{platform_icon(candidate.platform)}  {candidate.name}",
                lambda p=candidate: self.pick_files_for(p),
            )
        menu.exec(self.send_btn.mapToGlobal(QPoint(0, self.send_btn.height())))

    def pick_files_for(self, peer: Optional[Peer]) -> None:
        if peer is None or self._job is not None:
            return
        names, _ = QFileDialog.getOpenFileNames(self, f"Send files to {peer.name}")
        if names:
            self.send_to(peer, [Path(n) for n in names])

    def _on_files_dropped(self, peer: Peer, files: List[Path], folders: int) -> None:
        if folders and not files:
            self.notify.emit("Folders can't be sent", "Drop the files themselves (or zip the folder first).")
            return
        self.send_to(peer, files)

    def send_to(self, peer: Peer, paths: List[Path]) -> None:
        if self._job is not None:
            QMessageBox.information(self, APP_NAME, "Wait for the current send to finish (or cancel it).")
            return
        if not paths:
            return
        try:
            job = SendJob(peer, paths, self)
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"Can't read that file:\n{exc}")
            return
        self._job = job
        job.state_changed.connect(self._on_send_state)
        job.progress.connect(self._on_send_progress)
        job.finished.connect(self._on_send_finished)
        self.status_label.setStyleSheet("")
        self.status_label.setText(f"Connecting to {peer.name}…")
        self.bar.setRange(0, 0)
        self.bar.show()
        self.cancel_btn.show()
        self._update_buttons()
        job.start()

    def cancel_send(self) -> None:
        if self._job is not None:
            self._job.cancel()

    def _on_send_state(self, state: str) -> None:
        if self._job is None:
            return
        name = self._job.peer.name
        if state == "waiting":
            self.status_label.setText(f"Waiting for {name} to accept…")
            self.bar.setRange(0, 0)
        elif state == "sending":
            self.status_label.setText(f"Sending to {name}…")
            self.bar.setRange(0, 1000)
            self.bar.setValue(0)

    def _on_send_progress(self, sent: Any, total: Any, current: str, elapsed: float) -> None:
        if self._job is None:
            return
        sent, total = int(sent), int(total)
        fraction = min(sent / total, 1.0) if total > 0 else 1.0
        self.bar.setRange(0, 1000)
        self.bar.setValue(int(1000 * fraction))
        speed = sent / elapsed if elapsed > 0 else 0.0
        self.status_label.setText(
            f"Sending to {self._job.peer.name} · {100 * fraction:.0f}% · "
            f"{format_bytes(sent)} / {format_bytes(total)} · {format_bytes(speed)}/s"
        )

    def _on_send_finished(self, ok: bool, message: str) -> None:
        if self._job is not None:
            self._job.deleteLater()
        self._job = None
        self.bar.hide()
        self.cancel_btn.hide()
        self.status_label.setText(("Sent ✓  " if ok else "Not sent ✕  ") + message)
        self.status_label.setStyleSheet(f"color: {theme.SUCCESS if ok else theme.DANGER};")
        self._update_buttons()
        window = self.window()
        if not window.isVisible() or window.isMinimized():
            self.notify.emit("Files sent" if ok else "Files not sent", message)
        QTimer.singleShot(8000, self._reset_status)

    def _reset_status(self) -> None:
        if self._job is None:
            self.status_label.setStyleSheet("")
            self.status_label.setText("Drag files onto a device, or pick files to send.")
