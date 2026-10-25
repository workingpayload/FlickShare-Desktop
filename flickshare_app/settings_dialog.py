"""Settings dialog for save folder, port, and behaviour toggles."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from flickshare_app import autostart
from flickshare_app.settings import MAX_PORT, MIN_PORT, AppSettings, default_save_dir
from flickshare_app.widgets import make_label


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FlickShare Settings")
        self.setMinimumWidth(500)
        self._original = settings

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(14)
        root.addWidget(make_label("Settings", "Title"))

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)

        self.save_dir = QLineEdit(settings.save_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        reset = QPushButton("Desktop")
        reset.setToolTip("Reset to your Desktop folder")
        reset.clicked.connect(lambda: self.save_dir.setText(str(default_save_dir())))
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.save_dir, 1)
        folder_row.addWidget(browse)
        folder_row.addWidget(reset)
        form.addRow("Save folder", folder_row)

        self.port = QSpinBox()
        self.port.setRange(MIN_PORT, MAX_PORT)
        self.port.setValue(settings.port)
        self.port.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.port.setFixedWidth(110)
        form.addRow("Port", self.port)
        root.addLayout(form)

        root.addWidget(make_label("Receiving", "CardTitle"))
        self.mdns = QCheckBox("Broadcast on the network (mDNS) so phones find this PC automatically")
        self.mdns.setChecked(settings.enable_mdns)
        self.clipboard = QCheckBox("Copy caught files to clipboard (image, or file path)")
        self.clipboard.setChecked(settings.enable_clipboard)
        self.notifications = QCheckBox("Show a notification when a file is caught")
        self.notifications.setChecked(settings.enable_notifications)
        self.glass = QCheckBox("Play the liquid glass animation when a throw arrives while this window is hidden")
        self.glass.setChecked(settings.glass_overlay)
        for box in (self.mdns, self.clipboard, self.notifications, self.glass):
            root.addWidget(box)

        root.addWidget(make_label("Incoming files from other devices", "CardTitle"))
        self.require_approval = QCheckBox("Ask me before accepting files from a new phone or PC (recommended)")
        self.require_approval.setChecked(settings.require_approval)
        root.addWidget(self.require_approval)
        self._forget_trusted = False
        trusted_row = QHBoxLayout()
        self.trusted_label = make_label("", "Muted", wrap=True)
        trusted_row.addWidget(self.trusted_label, 1)
        self.forget_btn = QPushButton("Forget all")
        self.forget_btn.setToolTip("Ask again next time any of these devices sends files")
        self.forget_btn.clicked.connect(self._forget_all)
        trusted_row.addWidget(self.forget_btn)
        root.addLayout(trusted_row)
        self._update_trusted_label()

        root.addWidget(make_label("App", "CardTitle"))
        self.tray = QCheckBox("Keep running in the system tray when the window is closed")
        self.tray.setChecked(settings.minimize_to_tray)
        self.start_min = QCheckBox("Start minimized to the tray")
        self.start_min.setChecked(settings.start_minimized)
        self.autostart = QCheckBox("Start FlickShare when I sign in to Windows")
        self.autostart.setChecked(autostart.is_enabled() if autostart.is_supported() else False)
        self.autostart.setEnabled(autostart.is_supported())
        for box in (self.tray, self.start_min, self.autostart):
            root.addWidget(box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("Primary")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addSpacing(6)
        root.addWidget(buttons)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose save folder", self.save_dir.text())
        if folder:
            self.save_dir.setText(str(Path(folder)))

    def _update_trusted_label(self) -> None:
        names = [] if self._forget_trusted else sorted(self._original.trusted_devices.values(), key=str.lower)
        if names:
            shown = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
            self.trusted_label.setText(f"Always accepted: {shown}")
        else:
            self.trusted_label.setText("No trusted devices yet.")
        self.forget_btn.setEnabled(bool(names))

    def _forget_all(self) -> None:
        self._forget_trusted = True
        self._update_trusted_label()

    def _accept(self) -> None:
        folder = Path(self.save_dir.text().strip()).expanduser()
        if not self.save_dir.text().strip():
            QMessageBox.warning(self, "FlickShare", "Please choose a save folder.")
            return
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "FlickShare", f"Cannot use that folder:\n{exc}")
            return
        self.save_dir.setText(str(folder))
        self.accept()

    def result_settings(self, base: Optional[AppSettings] = None) -> AppSettings:
        """Dialog values applied on top of `base` (the app's current settings; defaults to the opening ones)."""
        base = base or self._original
        return replace(
            base,
            save_dir=self.save_dir.text().strip(),
            port=self.port.value(),
            enable_mdns=self.mdns.isChecked(),
            enable_clipboard=self.clipboard.isChecked(),
            enable_notifications=self.notifications.isChecked(),
            glass_overlay=self.glass.isChecked(),
            minimize_to_tray=self.tray.isChecked(),
            start_minimized=self.start_min.isChecked(),
            autostart=self.autostart.isChecked(),
            require_approval=self.require_approval.isChecked(),
            trusted_devices={} if self._forget_trusted else dict(base.trusted_devices),
        )
