"""Reusable custom widgets: cards, status dot, QR code, folder drop zone, transfer card."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import qrcode
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from flickshare_app import theme


def format_bytes(num_bytes: float) -> str:
    size = float(max(num_bytes, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def make_label(text: str = "", name: Optional[str] = None, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


class Card(QFrame):
    def __init__(self, title: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(10)
        if title:
            self.body.addWidget(make_label(title, "CardTitle"))


class StatusDot(QWidget):
    """Small colored dot that pulses while the receiver is live."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._color = QColor(theme.MUTED)
        self._pulse = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

    def set_state(self, color: str, pulse: bool) -> None:
        self._color = QColor(color)
        self._pulse = pulse
        if pulse:
            self._timer.start()
        else:
            self._timer.stop()
            self._phase = 0.0
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.05) % 1.0
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        if self._pulse:
            halo = QColor(self._color)
            halo.setAlphaF(0.45 * (1.0 - self._phase))
            p.setBrush(halo)
            r = 3.5 + 3.5 * self._phase
            p.drawEllipse(QRectF(7 - r, 7 - r, 2 * r, 2 * r))
        p.setBrush(self._color)
        p.drawEllipse(QRectF(3.5, 3.5, 7, 7))


class QrCodeWidget(QWidget):
    """Renders a QR code for arbitrary text using the `qrcode` matrix (no PIL round-trip)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._matrix: list[list[bool]] = []
        self._text = ""
        self.setFixedSize(136, 136)

    @property
    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        if text:
            qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
            qr.add_data(text)
            qr.make(fit=True)
            self._matrix = qr.get_matrix()
        else:
            self._matrix = []
        self.setToolTip(text)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FFFFFF") if self._matrix else QColor(theme.SURFACE_2))
        p.drawRoundedRect(rect, 12, 12)
        if not self._matrix:
            p.setPen(QColor(theme.MUTED))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Receiver\noffline")
            return
        n = len(self._matrix)
        cell = rect.width() / n
        p.setBrush(QColor("#0F1117"))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for y, row in enumerate(self._matrix):
            for x, on in enumerate(row):
                if on:
                    p.drawRect(QRectF(x * cell, y * cell, cell + 0.5, cell + 0.5))


class DropZone(QFrame):
    """Drop a folder to make it the save location; click to open the current save folder."""

    folder_dropped = Signal(str)
    clicked = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(96)
        self._hover = False
        self._reject = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(2)
        self.title = make_label("📂  Drop a folder here", "CardTitle")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle = make_label("to save throws there · click to open save folder", "Muted", wrap=True)
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        layout.addStretch(1)

    @staticmethod
    def _folder_from(event: QDragEnterEvent | QDropEvent) -> Optional[str]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.is_dir():
                    return str(path)
        return None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._folder_from(event):
            self._hover, self._reject = True, False
            event.acceptProposedAction()
        elif event.mimeData().hasUrls():
            self._hover, self._reject = True, True
            self.subtitle.setText("Only folders can be dropped here")
            event.ignore()
        self.update()

    def dragLeaveEvent(self, event) -> None:
        self._reset()

    def dropEvent(self, event: QDropEvent) -> None:
        folder = self._folder_from(event)
        self._reset()
        if folder:
            event.acceptProposedAction()
            self.folder_dropped.emit(folder)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()

    def _reset(self) -> None:
        self._hover = self._reject = False
        self.subtitle.setText("to save throws there · click to open save folder")
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = theme.DANGER if self._reject else (theme.ACCENT_2 if self._hover else theme.ACCENT)
        fill = QColor(color)
        fill.setAlphaF(0.12 if self._hover else 0.05)
        pen = QPen(QColor(color), 1.6, Qt.PenStyle.DashLine)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 14, 14)


class TransferCard(Card):
    """Shows the live incoming transfer (or an idle 'ready to catch' state)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Incoming", parent)
        self._active: dict[str, dict] = {}
        self._current: Optional[str] = None

        self.headline = make_label("Waiting for a throw…", "Headline")
        self.detail = make_label("Flick a file from your phone and it lands here.", "Muted", wrap=True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(10)
        self.bar.setTextVisible(False)
        self.bar.hide()

        row = QHBoxLayout()
        row.addWidget(self.headline, 1)
        self.extra = make_label("", "Pill")
        self.extra.hide()
        row.addWidget(self.extra)
        self.body.addLayout(row)
        self.body.addWidget(self.bar)
        self.body.addWidget(self.detail)

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._show_idle)

    def transfer_started(self, transfer_id: str, client: str, total_bytes: int) -> None:
        self._idle_timer.stop()
        self._active[transfer_id] = {
            "client": client,
            "total": total_bytes,
            "received": 0,
            "started": time.monotonic(),
            "elapsed": 0.0,
        }
        self._current = transfer_id
        self._render()

    def transfer_progress(self, transfer_id: str, received: int, total: int, elapsed: float) -> None:
        info = self._active.get(transfer_id)
        if info is None:
            return
        info["received"] = received
        info["total"] = total or info["total"]
        info["elapsed"] = elapsed
        if self._current is None:
            self._current = transfer_id
        if transfer_id == self._current:
            self._render()

    def transfer_finished(self, transfer_id: str, ok: bool, message: str) -> None:
        self._active.pop(transfer_id, None)
        if self._active:
            self._current = next(iter(self._active))
            self._render()
            return
        self._current = None
        self.bar.hide()
        self.extra.hide()
        self.headline.setText("Caught! ✓" if ok else "Throw dropped ✕")
        self.headline.setStyleSheet(f"color: {theme.SUCCESS if ok else theme.DANGER};")
        self.detail.setText(message)
        self._idle_timer.start(5000)

    def _render(self) -> None:
        info = self._active.get(self._current or "")
        if info is None:
            return
        self.headline.setStyleSheet("")
        received, total, elapsed = info["received"], info["total"], info["elapsed"]
        speed = received / elapsed if elapsed > 0 else 0.0
        self.headline.setText(f"Catching from {info['client']}")
        self.bar.show()
        if total > 0:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * min(received / total, 1.0)))
            pct = f"{100 * min(received / total, 1.0):.0f}% · "
            size = f"{format_bytes(received)} / {format_bytes(total)}"
        else:
            self.bar.setRange(0, 0)  # indeterminate
            pct = ""
            size = format_bytes(received)
        self.detail.setText(f"{pct}{size} · {format_bytes(speed)}/s")
        others = len(self._active) - 1
        self.extra.setText(f"+{others} more")
        self.extra.setVisible(others > 0)

    def _show_idle(self) -> None:
        self.headline.setStyleSheet("")
        self.headline.setText("Waiting for a throw…")
        self.detail.setText("Flick a file from your phone and it lands here.")
