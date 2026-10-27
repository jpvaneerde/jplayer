"""Hardware volume through ALSA's amixer (e.g. the DAC2 HD's "DAC" control).

Applies to every source, because it sits in the DAC, after all of them.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil

log = logging.getLogger(__name__)

_PERCENT = re.compile(r"\[(\d{1,3})%\]")


class Volume:
    def __init__(self, card: str, control: str):
        self.card = card
        self.control = control

    @property
    def available(self) -> bool:
        return bool(self.control) and shutil.which("amixer") is not None

    async def _amixer(self, *args: str) -> str:
        cmd = ["amixer"]
        if self.card:
            cmd += ["-c", self.card]
        cmd += ["-M", *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(err.decode(errors="replace").strip() or "amixer failed")
        return out.decode(errors="replace")

    async def get(self) -> int | None:
        if not self.available:
            return None
        try:
            out = await self._amixer("sget", self.control)
        except RuntimeError as exc:
            log.warning("Volume read failed: %s", exc)
            return None
        values = [int(v) for v in _PERCENT.findall(out)]
        return round(sum(values) / len(values)) if values else None

    async def set(self, percent: int) -> int | None:
        if not self.available:
            return None
        percent = max(0, min(100, int(percent)))
        await self._amixer("sset", self.control, f"{percent}%")
        return await self.get()
