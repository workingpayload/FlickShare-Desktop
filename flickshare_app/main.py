"""FlickShare desktop app entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import List, Optional

from flickshare_app import APP_NAME, APP_VERSION

logger = logging.getLogger("flickshare.app")

SINGLE_INSTANCE_KEY = f"{APP_NAME}-desktop-{os.environ.get('USERNAME', os.environ.get('USER', 'user'))}"


def _ensure_std_streams() -> None:
    # Windowed (pythonw / PyInstaller --windowed) builds have no console streams.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _configure_logging() -> None:
    from flickshare_app.settings import app_data_dir

    handler = RotatingFileHandler(
        app_data_dir() / "flickshare.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stderr))
    root.setLevel(logging.INFO)


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} desktop receiver app")
    parser.add_argument("--minimized", action="store_true", help="Start hidden in the system tray")
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: Optional[List[str]] = None) -> None:
    _ensure_std_streams()
    args = parse_args(argv if argv is not None else sys.argv[1:])
    _configure_logging()

    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setApplicationVersion(APP_VERSION)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)

    # Single instance: if already running, ask the existing window to show itself and exit.
    probe = QLocalSocket()
    probe.connectToServer(SINGLE_INSTANCE_KEY)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        logger.info("Another instance is running; handed off and exiting.")
        return

    from flickshare_app import autostart, theme
    from flickshare_app.history import HistoryStore
    from flickshare_app.main_window import MainWindow
    from flickshare_app.receiver import ReceiverController
    from flickshare_app.settings import AppSettings

    app.setStyleSheet(theme.STYLESHEET)

    settings = AppSettings.load()
    if autostart.is_supported():
        enabled = autostart.is_enabled()
        if enabled:
            # Keep the registry command pointing at the current executable location.
            autostart.set_enabled(True)
        settings.autostart = enabled

    history = HistoryStore()
    history.load()
    controller = ReceiverController()
    window = MainWindow(settings, history, controller)

    instance_server = QLocalServer()
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    if instance_server.listen(SINGLE_INSTANCE_KEY):
        def _on_new_connection() -> None:
            conn = instance_server.nextPendingConnection()
            if conn is not None:
                conn.readyRead.connect(conn.readAll)
                conn.disconnected.connect(conn.deleteLater)
            window.show_window()

        instance_server.newConnection.connect(_on_new_connection)
    else:
        logger.warning("Single-instance server unavailable: %s", instance_server.errorString())

    window.start_receiver()

    start_hidden = (args.minimized or settings.start_minimized) and window.tray is not None
    if not start_hidden:
        window.show()

    logger.info("%s %s started (minimized=%s)", APP_NAME, APP_VERSION, start_hidden)
    exit_code = app.exec()
    controller.shutdown()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
