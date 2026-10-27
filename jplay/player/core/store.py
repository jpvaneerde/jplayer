"""Tiny persistent key/value store (JSON file, atomic writes)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        try:
            self._data = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("Could not read state file %s: %s (starting empty)", path, exc)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            value = self._data.get(key, default)
            return json.loads(json.dumps(value))  # defensive copy

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
