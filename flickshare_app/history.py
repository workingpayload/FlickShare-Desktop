"""Persistent list of received files shown in the desktop app."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import List, Optional

from flickshare_app.settings import app_data_dir

logger = logging.getLogger("flickshare.app")

MAX_HISTORY = 500


@dataclass(eq=False)
class ReceivedFile:
    filename: str
    saved_path: str
    size_bytes: int
    client: str = "unknown"
    speed_mbps: float = 0.0
    received_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.received_at:
            self.received_at = time.time()

    @property
    def exists(self) -> bool:
        return Path(self.saved_path).exists()


class HistoryStore:
    """Newest-first list of ReceivedFile entries, persisted to history.json."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (app_data_dir() / "history.json")
        self.items: List[ReceivedFile] = []

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable history file %s: %s", self.path, exc)
            return
        known = {f.name for f in fields(ReceivedFile)}
        items: List[ReceivedFile] = []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict):
                continue
            try:
                items.append(ReceivedFile(**{k: v for k, v in entry.items() if k in known}))
            except TypeError:
                continue
        self.items = items[:MAX_HISTORY]

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps([asdict(i) for i in self.items], indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("Failed to save history: %s", exc)

    def add(self, item: ReceivedFile) -> None:
        self.items.insert(0, item)
        del self.items[MAX_HISTORY:]
        self.save()

    def remove(self, item: ReceivedFile) -> None:
        if item in self.items:
            self.items.remove(item)
            self.save()

    def clear(self) -> None:
        self.items.clear()
        self.save()
