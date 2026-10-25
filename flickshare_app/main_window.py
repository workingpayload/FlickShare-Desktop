"""FlickShare main window: receiver status, QR connect card, folder drop zone, transfers, history, tray."""

from __future__ import annotations

import datetime as dt
import logging
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional, Set

from PySide6.QtCore import QEvent, QFileInfo, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileIconProvider,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import server
from flickshare_app import APP_NAME, autostart, firewall, theme
from flickshare_app.approval_dialog import ApprovalDialog
from flickshare_app.devices_panel import DevicesCard
from flickshare_app.glass_overlay import LiquidGlassOverlay
from flickshare_app.history import HistoryStore, ReceivedFile
from flickshare_app.icons import app_icon
from flickshare_app.receiver import ReceiverController, ReceiverStatus
from flickshare_app.settings import AppSettings
from flickshare_app.settings_dialog import SettingsDialog
from flickshare_app.widgets import (
    Card,
    DropZone,
    QrCodeWidget,
    StatusDot,
    TransferCard,
    format_bytes,
    make_label,
)

logger = logging.getLogger("flickshare.app")

ITEM_ROLE = Qt.ItemDataRole.UserRole + 1
NETWORK_CHECK_MS = 10_000


def reveal_in_folder(path: Path) -> None:
    if sys.platform == "win32":
        if path.exists():
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent if path.is_file() else path)))


def format_when(timestamp: float) -> str:
    when = dt.datetime.fromtimestamp(timestamp)
    if when.date() == dt.date.today():
        return when.strftime("%H:%M")
    return when.strftime("%d %b %H:%M")


def sender_label(payload: Dict[str, Any], default: str = "?") -> str:
    """Friendly sender name from a receiver event (falls back to the client IP)."""
    return str(payload.get("sender_name") or payload.get("client") or default)


class MainWindow(QMainWindow):
    def __init__(
        self,
        settings: AppSettings,
        history: HistoryStore,
        controller: ReceiverController,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.history = history
        self.controller = controller
        self._quitting = False
        self._active_transfers: Set[str] = set()
        self._last_caught: Optional[Path] = None
        self._approvals: Dict[str, ApprovalDialog] = {}
        self._icon_provider = QFileIconProvider()
        self._icon_on = app_icon(True)
        self._icon_off = app_icon(False)
        self.glass = LiquidGlassOverlay()
        self.firewall = firewall.FirewallHelper(self)
        self.firewall.checked.connect(self._on_firewall_checked)
        self.firewall.fixed.connect(self._on_firewall_fixed)
        self._firewall_status: Optional[firewall.FirewallStatus] = None
        self._firewall_auto_prompted = False
        self._firewall_check_port: Optional[int] = None

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(self._icon_on)
        self.resize(1040, 780)
        self.setMinimumSize(900, 720)

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 18, 22, 20)
        outer.setSpacing(16)
        outer.addLayout(self._build_header())

        columns = QHBoxLayout()
        columns.setSpacing(16)
        outer.addLayout(columns, 1)

        left = QVBoxLayout()
        left.setSpacing(16)
        left.addWidget(self._build_status_card())
        left.addWidget(self._build_connect_card())
        self.drop_zone = DropZone()
        self.drop_zone.folder_dropped.connect(self._on_folder_dropped)
        self.drop_zone.clicked.connect(self.open_save_folder)
        left.addWidget(self.drop_zone, 1)
        left_host = QWidget()
        left_host.setLayout(left)
        left_host.setFixedWidth(340)
        columns.addWidget(left_host)

        right = QVBoxLayout()
        right.setSpacing(16)
        self.devices_card = DevicesCard(settings.manual_peers)
        self.devices_card.notify.connect(self._notify)
        self.devices_card.manual_peers_changed.connect(self._on_manual_peers_changed)
        right.addWidget(self.devices_card)
        self.transfer_card = TransferCard()
        right.addWidget(self.transfer_card)
        right.addWidget(self._build_history_card(), 1)
        columns.addLayout(right, 1)

        self._build_tray()

        controller.status_changed.connect(self._on_status_changed)
        controller.receiver_event.connect(self._on_receiver_event)

        self._network_timer = QTimer(self)
        self._network_timer.setInterval(NETWORK_CHECK_MS)
        self._network_timer.timeout.connect(self._check_network_change)
        self._network_timer.start()

        self._reload_history()
        self._on_status_changed(controller.status)

    # ------------------------------------------------------------------ layout

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(self._icon_on.pixmap(QSize(44, 44)))
        row.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(make_label(APP_NAME, "Title"))
        titles.addWidget(make_label("Send and catch files between your phones and PCs", "Muted"))
        row.addLayout(titles, 1)
        settings_btn = QPushButton("⚙  Settings")
        settings_btn.clicked.connect(self.open_settings)
        row.addWidget(settings_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _build_status_card(self) -> Card:
        card = Card()
        top = QHBoxLayout()
        top.setSpacing(8)
        self.status_dot = StatusDot()
        top.addWidget(self.status_dot)
        self.status_label = make_label("Starting…", "CardTitle")
        top.addWidget(self.status_label, 1)
        self.mdns_pill = make_label("", "Pill")
        top.addWidget(self.mdns_pill)
        card.body.addLayout(top)

        endpoint_row = QHBoxLayout()
        self.endpoint_label = make_label("—", "Endpoint")
        self.endpoint_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        endpoint_row.addWidget(self.endpoint_label, 1)
        self.copy_btn = QToolButton()
        self.copy_btn.setText("⧉")
        self.copy_btn.setToolTip("Copy IP:port")
        self.copy_btn.clicked.connect(self._copy_endpoint)
        endpoint_row.addWidget(self.copy_btn)
        card.body.addLayout(endpoint_row)

        self.host_label = make_label("", "Muted")
        card.body.addWidget(self.host_label)
        self.folder_label = make_label("", "Muted")
        card.body.addWidget(self.folder_label)

        buttons = QHBoxLayout()
        self.toggle_btn = QPushButton("Start receiving")
        self.toggle_btn.setObjectName("Primary")
        self.toggle_btn.clicked.connect(self.toggle_receiver)
        buttons.addWidget(self.toggle_btn, 1)
        self.restart_btn = QPushButton("Restart")
        self.restart_btn.setToolTip("Restart the receiver (re-detects your network address)")
        self.restart_btn.clicked.connect(self.restart_receiver)
        buttons.addWidget(self.restart_btn)
        card.body.addLayout(buttons)

        self.firewall_btn = QPushButton("⚠  Windows Firewall is blocking phones — Fix")
        self.firewall_btn.setToolTip("Allow FlickShare through Windows Firewall (asks for admin permission once)")
        self.firewall_btn.clicked.connect(lambda: self._prompt_firewall(self._firewall_status))
        self.firewall_btn.setVisible(False)
        card.body.addWidget(self.firewall_btn)
        return card

    def _build_connect_card(self) -> Card:
        card = Card("Connect a phone")
        row = QHBoxLayout()
        row.setSpacing(14)
        self.qr = QrCodeWidget()
        row.addWidget(self.qr, 0, Qt.AlignmentFlag.AlignTop)
        hint = make_label(
            "The FlickShare app finds this PC automatically.\n\n"
            "No app? Scan with a phone camera to throw from the browser.",
            "Muted",
            wrap=True,
        )
        row.addWidget(hint, 1, Qt.AlignmentFlag.AlignVCenter)
        card.body.addLayout(row)
        return card

    def _build_history_card(self) -> Card:
        card = Card()
        header = QHBoxLayout()
        header.addWidget(make_label("Received files", "CardTitle"))
        self.count_pill = make_label("0", "Pill")
        header.addWidget(self.count_pill)
        header.addStretch(1)
        clear_btn = QPushButton("Clear list")
        clear_btn.setToolTip("Clear the list (files stay on disk)")
        clear_btn.clicked.connect(self._clear_history)
        header.addWidget(clear_btn)
        card.body.addLayout(header)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Name", "Size", "From", "When"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(20, 20))
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._history_menu)
        self.tree.itemDoubleClicked.connect(lambda item, _col: self._open_item(item))
        self.tree.itemSelectionChanged.connect(self._update_history_buttons)
        head = self.tree.header()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        card.body.addWidget(self.tree, 1)

        self.empty_label = make_label(
            "Nothing caught yet. Throws will show up here.", "Muted"
        )
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card.body.addWidget(self.empty_label)

        actions = QHBoxLayout()
        self.open_btn = QPushButton("Open")
        self.open_btn.clicked.connect(lambda: self._for_selected(self._open_item))
        self.reveal_btn = QPushButton("Show in folder")
        self.reveal_btn.clicked.connect(lambda: self._for_selected(self._reveal_item))
        self.copy_path_btn = QPushButton("Copy path")
        self.copy_path_btn.clicked.connect(self._copy_selected_paths)
        for btn in (self.open_btn, self.reveal_btn, self.copy_path_btn):
            actions.addWidget(btn)
        actions.addStretch(1)
        card.body.addLayout(actions)
        self._update_history_buttons()
        return card

    def _build_tray(self) -> None:
        self.tray: Optional[QSystemTrayIcon] = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self._icon_on, self)
        menu = QMenu()
        show_action = QAction("Open FlickShare", menu)
        show_action.triggered.connect(self.show_window)
        menu.addAction(show_action)
        folder_action = QAction("Open save folder", menu)
        folder_action.triggered.connect(self.open_save_folder)
        menu.addAction(folder_action)
        menu.addSeparator()
        self.tray_toggle = QAction("Pause receiving", menu)
        self.tray_toggle.triggered.connect(self.toggle_receiver)
        menu.addAction(self.tray_toggle)
        menu.addSeparator()
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(quit_action)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(self._on_tray_message_clicked)
        self.tray.setToolTip(APP_NAME)
        self.tray.show()

    # ------------------------------------------------------------------ receiver control

    def start_receiver(self) -> None:
        s = self.settings
        ok = self.controller.start(
            port=s.port,
            save_dir=s.save_dir,
            enable_mdns=s.enable_mdns,
            enable_clipboard=s.enable_clipboard,
            require_approval=s.require_approval,
            trusted_devices=s.trusted_devices.keys(),
        )
        if not ok and self.controller.status.error:
            self._notify("Receiver not started", self.controller.status.error)
        if ok:
            self.check_firewall()

    # ------------------------------------------------------------------ firewall

    def check_firewall(self) -> None:
        if firewall.is_supported() and self.firewall.start_check(self.settings.port):
            self._firewall_check_port = self.settings.port

    def _on_firewall_checked(self, status: firewall.FirewallStatus) -> None:
        if self._firewall_check_port != self.settings.port:
            self.check_firewall()  # port changed while checking; result is stale
            return
        self._firewall_status = status
        self._update_firewall_button()
        if status.needs_fix and not self.settings.firewall_dont_ask and not self._firewall_auto_prompted:
            self._firewall_auto_prompted = True
            self._prompt_firewall(status)

    def _update_firewall_button(self) -> None:
        status = self._firewall_status
        show = bool(status and status.needs_fix and self.controller.is_running)
        self.firewall_btn.setVisible(show)
        self.firewall_btn.setEnabled(not self.firewall.busy)

    def _prompt_firewall(self, status: Optional[firewall.FirewallStatus]) -> None:
        if status is None or self.firewall.busy:
            return
        port = self.settings.port
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("Windows Firewall is blocking phones from sending files to this PC.")
        info = (
            f"Allow {APP_NAME} on port {port}? Windows will ask for admin permission once, "
            "then you're set."
        )
        if status.has_block_rule:
            info += "\n\nAn earlier Windows prompt was cancelled, so FlickShare is blocked; this removes that block."
        private_box: Optional[QCheckBox] = None
        if status.public_network:
            info += (
                "\n\nYour Wi-Fi is set to Public. Mark it Private if this is your home or another "
                "trusted network. Leave it unticked to allow FlickShare on public networks too."
            )
            private_box = QCheckBox("This is my home / trusted Wi-Fi — mark it as Private")
            private_box.setChecked(True)
            box.setCheckBox(private_box)
        box.setInformativeText(info)
        allow_btn = box.addButton("Allow", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        never_btn = box.addButton("Don't ask again", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(allow_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked is never_btn:
            self.settings = replace(self.settings, firewall_dont_ask=True)
            self._save_settings()
            return
        if clicked is not allow_btn:
            return
        make_private = bool(private_box and private_box.isChecked())
        allow_public = bool(status.public_network and not make_private)
        if self.firewall.start_fix(port, allow_public, make_private, int(self.winId())):
            self._update_firewall_button()

    def _on_firewall_fixed(self, error: str) -> None:
        self._update_firewall_button()
        if not error:
            if self.settings.firewall_dont_ask:
                self.settings = replace(self.settings, firewall_dont_ask=False)
                self._save_settings()
            self._notify("Firewall updated", "Phones on your Wi-Fi can now send files to this PC.")
            self.check_firewall()
        elif error.startswith("Admin permission was declined"):
            self._notify("Firewall not changed", "Admin permission was declined. Use the Fix button to try again.")
        else:
            QMessageBox.warning(self, APP_NAME, f"Could not update Windows Firewall:\n{error}")

    def stop_receiver(self) -> None:
        self._dismiss_approvals()
        self.controller.stop()
        self._active_transfers.clear()

    def restart_receiver(self) -> None:
        self.stop_receiver()
        self.start_receiver()

    def toggle_receiver(self) -> None:
        if self.controller.is_running:
            self.stop_receiver()
        else:
            self.start_receiver()

    def _check_network_change(self) -> None:
        if not self.controller.is_running or self._active_transfers:
            return
        current_ip = server.get_local_ip()
        if current_ip and current_ip != self.controller.status.ip:
            logger.info("Network address changed %s -> %s; restarting receiver", self.controller.status.ip, current_ip)
            self.restart_receiver()

    def _on_status_changed(self, status: ReceiverStatus) -> None:
        running = status.running
        if running:
            self.status_dot.set_state(theme.SUCCESS, pulse=True)
            self.status_label.setText("Ready to catch")
            endpoint = f"{status.ip}:{status.port}"
            self.endpoint_label.setText(endpoint)
            self.host_label.setText(f"PC name: {status.hostname}")
            self.mdns_pill.setText("Discoverable" if status.mdns else "mDNS off")
            self.qr.set_text(f"http://{endpoint}/")
        elif status.error:
            self.status_dot.set_state(theme.DANGER, pulse=False)
            self.status_label.setText(status.error)
            self.endpoint_label.setText("—")
            self.host_label.setText("Change the port in Settings, or press Start to retry.")
            self.mdns_pill.setText("Offline")
            self.qr.set_text("")
        else:
            self.status_dot.set_state(theme.MUTED, pulse=False)
            self.status_label.setText("Paused")
            self.endpoint_label.setText("—")
            self.host_label.setText("Phones can't send files while paused.")
            self.mdns_pill.setText("Offline")
            self.qr.set_text("")
        self.mdns_pill.setVisible(True)
        self.copy_btn.setEnabled(running)
        self.restart_btn.setEnabled(running)
        self.toggle_btn.setText("Pause receiving" if running else "Start receiving")
        self.toggle_btn.setObjectName("" if running else "Primary")
        self.toggle_btn.style().unpolish(self.toggle_btn)
        self.toggle_btn.style().polish(self.toggle_btn)
        self._update_folder_label()
        self._update_firewall_button()

        if self.tray is not None:
            self.tray.setIcon(self._icon_on if running else self._icon_off)
            self.tray_toggle.setText("Pause receiving" if running else "Start receiving")
            tip = f"{APP_NAME} — ready on {status.ip}:{status.port}" if running else f"{APP_NAME} — paused"
            self.tray.setToolTip(tip)
        self.setWindowIcon(self._icon_on if running else self._icon_off)

    def _update_folder_label(self) -> None:
        full = str(self.settings.save_path)
        metrics = self.folder_label.fontMetrics()
        elided = metrics.elidedText(full, Qt.TextElideMode.ElideMiddle, 220)
        self.folder_label.setText(f"Saving to: {elided}")
        self.folder_label.setToolTip(full)

    # ------------------------------------------------------------------ receiver events

    def _on_receiver_event(self, event: str, payload: Dict[str, Any]) -> None:
        transfer_id = str(payload.get("transfer_id") or "")
        if event == "transfer_request":
            self._show_approval(payload)
        elif event == "transfer_request_closed":
            dialog = self._approvals.pop(str(payload.get("request_id") or ""), None)
            if dialog is not None:
                dialog.dismiss()
                if payload.get("reason") == "timeout":
                    self._notify("Request expired", f"{dialog.sender_name} stopped waiting for an answer.")
        elif event == "transfer_started":
            self._active_transfers.add(transfer_id)
            client = sender_label(payload)
            total = int(payload.get("total_bytes", 0))
            self.transfer_card.transfer_started(transfer_id, client, total)
            if self._glass_wanted():
                direction = payload.get("direction")
                self.glass.transfer_started(
                    transfer_id, client, total, float(direction) if isinstance(direction, (int, float)) else None
                )
        elif event == "transfer_progress":
            received = int(payload.get("received_bytes", 0))
            total = int(payload.get("total_bytes", 0))
            self.transfer_card.transfer_progress(transfer_id, received, total, float(payload.get("elapsed_s", 0.0)))
            self.glass.transfer_progress(transfer_id, received, total)
        elif event == "transfer_completed":
            self._active_transfers.discard(transfer_id)
            self._on_transfer_completed(transfer_id, payload)
        elif event == "transfer_failed":
            self._active_transfers.discard(transfer_id)
            error = str(payload.get("error", "unknown error"))
            sender = sender_label(payload)
            self.transfer_card.transfer_finished(transfer_id, False, f"From {sender}: {error}")
            self.glass.transfer_finished(transfer_id, False, "Throw didn't land", error, f"from {sender}")
        elif event == "server_stopped":
            self._active_transfers.clear()
            self._dismiss_approvals()
            self.glass.dismiss()
            error = str(payload.get("error") or "Receiver stopped")
            self.controller.mark_stopped(error)
            self._notify("Receiver stopped", error)

    def _on_transfer_completed(self, transfer_id: str, payload: Dict[str, Any]) -> None:
        files = payload.get("files") or []
        client = sender_label(payload, "unknown")
        speed = float(payload.get("speed_mbps", 0.0))
        for entry in files:
            self.history.add(
                ReceivedFile(
                    filename=str(entry.get("filename", "file")),
                    saved_path=str(entry.get("saved_path", "")),
                    size_bytes=int(entry.get("size_bytes", 0)),
                    client=client,
                    speed_mbps=float(entry.get("speed_mbps", speed)),
                )
            )
        self._reload_history()

        if not files:
            self.glass.transfer_finished(transfer_id, False, "Nothing landed", "The throw had no files", f"from {client}")
            return
        first = files[0]
        self._last_caught = Path(str(first.get("saved_path", "")))
        total = format_bytes(int(payload.get("total_bytes", 0)))
        label = first.get("filename", "file") if len(files) == 1 else f"{len(files)} files"
        clip = " · copied to clipboard" if payload.get("clipboard_copied") else ""
        self.transfer_card.transfer_finished(transfer_id, True, f"{label} ({total}) from {client}{clip}")
        title = "File caught!" if len(files) == 1 else f"{len(files)} files caught!"
        shown_on_glass = self.glass.transfer_finished(
            transfer_id, True, title, label, f"{total} · from {client}{clip}", self._last_caught
        )
        # The glass card already shows the catch; a toast on top of it would just be noise.
        if self.settings.enable_notifications and not shown_on_glass:
            self._notify(title, f"{label} · {total}{clip}")

    def _glass_wanted(self) -> bool:
        window_open = self.isVisible() and not self.isMinimized()
        return self.settings.glass_overlay and not window_open

    # ------------------------------------------------------------------ approvals

    def _show_approval(self, payload: Dict[str, Any]) -> None:
        request_id = str(payload.get("request_id") or "")
        if not request_id or request_id in self._approvals:
            return
        dialog = ApprovalDialog(payload)
        dialog.setWindowIcon(self._icon_on)
        dialog.decided.connect(self._on_approval_decided)
        self._approvals[request_id] = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        QApplication.alert(dialog)
        if not (self.isVisible() and not self.isMinimized()):
            count = int(payload.get("file_count") or 0)
            self._notify(
                f"{dialog.sender_name} wants to send files",
                f"{count} file{'s' if count != 1 else ''} · accept or decline in the popup",
            )

    def _on_approval_decided(self, request_id: str, accepted: bool, trust: bool) -> None:
        dialog = self._approvals.pop(request_id, None)
        if not server.resolve_transfer_request(request_id, accepted, trust):
            if accepted:
                self._notify("Too late", "That device stopped waiting. Ask it to send again.")
            return
        if accepted and trust and dialog is not None and dialog.sender_id:
            trusted = dict(self.settings.trusted_devices)
            trusted[dialog.sender_id] = dialog.sender_name
            self.settings = replace(self.settings, trusted_devices=trusted)
            self._save_settings()
            self.controller.set_approval(self.settings.require_approval, trusted.keys())

    def _dismiss_approvals(self) -> None:
        dialogs = list(self._approvals.values())
        self._approvals.clear()
        for dialog in dialogs:
            dialog.dismiss()

    def _on_manual_peers_changed(self, peers: list) -> None:
        self.settings = replace(self.settings, manual_peers=list(peers))
        self._save_settings()

    # ------------------------------------------------------------------ history

    def _reload_history(self) -> None:
        self.tree.clear()
        for record in self.history.items:
            item = QTreeWidgetItem(
                [
                    record.filename,
                    format_bytes(record.size_bytes),
                    record.client,
                    format_when(record.received_at),
                ]
            )
            item.setData(0, ITEM_ROLE, record)
            item.setIcon(0, self._icon_provider.icon(QFileInfo(record.saved_path)))
            item.setToolTip(0, record.saved_path)
            if not record.exists:
                font = item.font(0)
                font.setItalic(True)
                for col in range(4):
                    item.setFont(col, font)
                    item.setForeground(col, Qt.GlobalColor.gray)
                item.setToolTip(0, f"Moved or deleted: {record.saved_path}")
            self.tree.addTopLevelItem(item)
        count = len(self.history.items)
        self.count_pill.setText(str(count))
        self.empty_label.setVisible(count == 0)
        self.tree.setVisible(count > 0)
        self._update_history_buttons()

    def _selected_records(self) -> list[ReceivedFile]:
        return [item.data(0, ITEM_ROLE) for item in self.tree.selectedItems()]

    def _for_selected(self, fn) -> None:
        for item in self.tree.selectedItems()[:10]:
            fn(item)

    def _update_history_buttons(self) -> None:
        has = bool(self.tree.selectedItems())
        for btn in (self.open_btn, self.reveal_btn, self.copy_path_btn):
            btn.setEnabled(has)

    def _open_item(self, item: QTreeWidgetItem) -> None:
        record: ReceivedFile = item.data(0, ITEM_ROLE)
        if not record.exists:
            QMessageBox.information(self, APP_NAME, f"This file was moved or deleted:\n{record.saved_path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(record.saved_path))

    def _reveal_item(self, item: QTreeWidgetItem) -> None:
        record: ReceivedFile = item.data(0, ITEM_ROLE)
        reveal_in_folder(Path(record.saved_path))

    def _copy_selected_paths(self) -> None:
        paths = [r.saved_path for r in self._selected_records()]
        if paths:
            QGuiApplication.clipboard().setText("\n".join(paths))

    def _history_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)
        menu = QMenu(self)
        menu.addAction("Open", lambda: self._for_selected(self._open_item))
        menu.addAction("Show in folder", lambda: self._for_selected(self._reveal_item))
        menu.addAction("Copy path", self._copy_selected_paths)
        menu.addSeparator()
        menu.addAction("Remove from list", self._remove_selected)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _remove_selected(self) -> None:
        for record in self._selected_records():
            self.history.remove(record)
        self._reload_history()

    def _clear_history(self) -> None:
        if not self.history.items:
            return
        answer = QMessageBox.question(
            self, APP_NAME, "Clear the received files list?\nThe files themselves are not deleted."
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.history.clear()
            self._reload_history()

    # ------------------------------------------------------------------ settings & folder

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return
        self.apply_settings(dialog.result_settings(self.settings))

    def apply_settings(self, new: AppSettings) -> None:
        old = self.settings
        if new.autostart != autostart.is_enabled() and autostart.is_supported():
            if not autostart.set_enabled(new.autostart):
                QMessageBox.warning(self, APP_NAME, "Could not update the Windows startup setting.")
                new = replace(new, autostart=autostart.is_enabled())
        self.settings = new
        self._save_settings()

        server.config.save_dir = new.save_path
        self.controller.set_clipboard_enabled(new.enable_clipboard)
        self.controller.set_approval(new.require_approval, new.trusted_devices.keys())
        if not new.glass_overlay:
            self.glass.dismiss()
        needs_restart = new.port != old.port or new.enable_mdns != old.enable_mdns
        if needs_restart and (self.controller.is_running or self.controller.status.error):
            self.restart_receiver()
        self._update_folder_label()

    def _on_folder_dropped(self, folder: str) -> None:
        self.apply_settings(replace(self.settings, save_dir=folder))
        self._notify("Save folder changed", f"Throws will now land in {folder}")

    def open_save_folder(self) -> None:
        path = self.settings.save_path
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _save_settings(self) -> None:
        try:
            self.settings.save()
        except OSError as exc:
            logger.warning("Failed to save settings: %s", exc)

    def _copy_endpoint(self) -> None:
        QGuiApplication.clipboard().setText(self.endpoint_label.text())
        self.copy_btn.setText("✓")
        QTimer.singleShot(1200, lambda: self.copy_btn.setText("⧉"))

    # ------------------------------------------------------------------ tray & window

    def _notify(self, title: str, message: str) -> None:
        if self.tray is not None and self.tray.supportsMessages():
            self.tray.showMessage(title, message, self._icon_on, 4000)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self.show_window()

    def _on_tray_message_clicked(self) -> None:
        if self._last_caught is not None and self._last_caught.exists():
            reveal_in_folder(self._last_caught)
        else:
            self.show_window()

    def show_window(self) -> None:
        self.glass.dismiss()
        self._reload_history()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.WindowStateChange and self.isVisible() and not self.isMinimized():
            self.glass.dismiss()
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._quitting and self.settings.minimize_to_tray and self.tray is not None:
            event.ignore()
            self.hide()
            if not self.settings.tray_hint_shown:
                self._notify(APP_NAME, "Still catching files in the background. Right-click the tray icon to quit.")
                self.settings.tray_hint_shown = True
                self._save_settings()
            return
        event.accept()
        self.quit_app()

    def quit_app(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._network_timer.stop()
        self._dismiss_approvals()
        self.devices_card.cancel_send()
        self.glass.close()
        self.controller.shutdown()
        self.history.save()
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()
