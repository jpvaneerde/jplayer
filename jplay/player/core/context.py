"""Everything a source needs, in one place."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from ..config import Config
from .events import EventHub
from .mpv import MpvEngine
from .store import StateStore
from .volume import Volume

if TYPE_CHECKING:
    from .manager import SourceManager
    from .output import Output


@dataclass
class AppContext:
    config: Config
    store: StateStore
    hub: EventHub
    engine: MpvEngine
    volume: Volume
    http: httpx.AsyncClient
    state_dir: Path
    runtime_dir: Path
    manager: "SourceManager" = None  # type: ignore[assignment]  # set right after creation
    output: "Output" = None  # type: ignore[assignment]  # set right after creation
    hook_token: str = ""  # shared secret for local hooks (librespot events)
