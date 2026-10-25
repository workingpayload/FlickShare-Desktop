"""Popup asking whether to accept files another device wants to send."""

from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from flickshare_app import APP_NAME
from flickshare_app.devices_panel import platform_icon, platform_label
from flickshare_app.widgets import format_bytes, make_label

MAX_LISTED_FILES = 5


class ApprovalDialog(QDialog):
    """Non-modal, stays on top; emits `decided` exactly once (closing = decline)."""

    decided = Signal(str, bool, bool)  # request_id, accepted, trust

    def __init__(self, request: Dict[str, Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.request_id = str(request.get("request_id", ""))
        self.sender_id = str(request.get("sender_id", ""))
        self.sender_name = str(request.get("sender_name") or "Unknown device")
        self._done = False
        self._remaining = max(1, int(float(request.get("timeout_s", 120))))

        self.setWindowTitle(f"{APP_NAME} – incoming files")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMinimumWidth(400)

        platform = str(request.get("sender_platform") or "")
        client = str(request.get("client") or "")
        count = int(request.get("file_count") or 0)
        total = int(request.get("total_bytes") or 0)
        files = [f for f in request.get("files") or [] if isinstance(f, dict)]

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(10)

        head = QHBoxLayout()
        icon = QLabel(platform_icon(platform))
        icon.setStyleSheet("font-size: 28pt;")
        head.addWidget(icon)
        who = QVBoxLayout()
        who.addWidget(make_label(self.sender_name, "Headline"))
        details = platform_label(platform) + (f" · {client}" if client else "")
        who.addWidget(make_label(details, "Muted"))
        head.addLayout(who, 1)
        root.addLayout(head)

        summary = f"wants to send you {count} file{'s' if count != 1 else ''}"
        if total:
            summary += f" ({format_bytes(total)})"
        root.addWidget(make_label(summary, wrap=True))

        if files:
            names = [str(f.get("name") or "file") for f in files[:MAX_LISTED_FILES]]
            lines = "\n".join(f"•  {name}" for name in names)
            if count > len(names):
                lines += f"\n…and {count - len(names)} more"
            listing = make_label(lines, "Muted", wrap=True)
            listing.setTextFormat(Qt.TextFormat.PlainText)
            root.addWidget(listing)

        self.trust_box = QCheckBox("Always accept files from this device")
        root.addWidget(self.trust_box)

        self.countdown = make_label("", "Muted")
        root.addWidget(self.countdown)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        decline = QPushButton("Decline")
        decline.setObjectName("Danger")
        decline.clicked.connect(lambda: self._finish(False))
        buttons.addWidget(decline)
        accept = QPushButton("Accept")
        accept.setObjectName("Primary")
        accept.setDefault(True)
        accept.clicked.connect(lambda: self._finish(True))
        buttons.addWidget(accept)
        root.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._update_countdown()

    def _update_countdown(self) -> None:
        self.countdown.setText(f"Declines automatically in {self._remaining} s")

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._finish(False)
        else:
            self._update_countdown()

    def _decide(self, accepted: bool) -> None:
        if self._done:
            return
        self._done = True
        self._timer.stop()
        self.decided.emit(self.request_id, accepted, accepted and self.trust_box.isChecked())

    def _finish(self, accepted: bool) -> None:
        self._decide(accepted)
        self.close()

    def dismiss(self) -> None:
        """Close without deciding (the sender gave up or the request timed out server-side)."""
        self._done = True
        self._timer.stop()
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._decide(False)
        # QDialog.closeEvent calls reject() and ignores the close if the dialog is still visible.
        super().closeEvent(event)

    def reject(self) -> None:  # Esc key, and QDialog.closeEvent
        self._decide(False)
        super().reject()
