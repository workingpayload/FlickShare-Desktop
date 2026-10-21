"""'Start with Windows' support via the per-user HKCU Run registry key."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from flickshare_app import APP_NAME

logger = logging.getLogger("flickshare.app")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_supported() -> bool:
    return sys.platform == "win32"


def launch_command() -> str:
    """Command line that starts the app minimized to the tray."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    if pythonw.exists():
        interpreter = pythonw
    launcher = Path(__file__).resolve().parent.parent / "flickshare_desktop.py"
    return f'"{interpreter}" "{launcher}" --minimized'


def is_enabled() -> bool:
    if not is_supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    """Enable/disable autostart. Returns True on success."""
    if not is_supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        logger.warning("Failed to update autostart registry entry: %s", exc)
        return False
