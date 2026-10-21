"""Persistent user settings and per-user application data paths."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from flickshare_app import APP_NAME

logger = logging.getLogger("flickshare.app")

MIN_PORT = 1024
MAX_PORT = 65535


def app_data_dir() -> Path:
    """Return (and create) the per-user data directory, overridable via FLICKSHARE_APP_DIR."""
    override = os.environ.get("FLICKSHARE_APP_DIR")
    if override:
        base = Path(override)
    elif os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"]) / APP_NAME
    else:
        base = Path.home() / f".{APP_NAME.lower()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def default_save_dir() -> Path:
    return Path.home() / "Desktop"


@dataclass
class AppSettings:
    save_dir: str = ""
    port: int = 8765
    enable_mdns: bool = True
    enable_clipboard: bool = True
    enable_notifications: bool = True
    glass_overlay: bool = True
    minimize_to_tray: bool = True
    start_minimized: bool = False
    autostart: bool = False
    tray_hint_shown: bool = False
    firewall_dont_ask: bool = False

    def __post_init__(self) -> None:
        if not self.save_dir:
            self.save_dir = str(default_save_dir())
        try:
            self.port = int(self.port)
        except (TypeError, ValueError):
            self.port = 8765
        if not MIN_PORT <= self.port <= MAX_PORT:
            self.port = 8765

    @property
    def save_path(self) -> Path:
        return Path(self.save_dir).expanduser()

    @classmethod
    def settings_file(cls) -> Path:
        return app_data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "AppSettings":
        path = cls.settings_file()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable settings file %s: %s", path, exc)
            return cls()
        if not isinstance(raw, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        path = self.settings_file()
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)
