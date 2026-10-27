"""Which DAC we play to, and whether it is there right now.

The setting is "auto", "null" or an ALSA device such as "hw:CARD=DAC,DEV=0". It comes
from the config and can be changed from the web page (saved in the state file).
A USB DAC that is switched off simply disappears from ALSA; we notice within a few
seconds, refuse to start playback, and pick it up again when it comes back.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from . import alsa

if TYPE_CHECKING:
    from .context import AppContext

log = logging.getLogger(__name__)

STORE_KEY = "output"
POLL_SECONDS = 3.0


class Output:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        audio = ctx.config["audio"]
        saved = ctx.store.get(STORE_KEY) or {}
        self.setting: str = saved.get("device") or audio.get("device") or "auto"
        # Hardware volume control per card; the config value applies to its own card.
        self.mixers: dict[str, str] = dict(saved.get("mixers") or {})
        # Names of DACs we have seen, so a switched-off USB DAC still shows its name.
        self.names: dict[str, str] = dict(saved.get("names") or {})
        self._config_mixer = (audio.get("mixer_card", ""), audio.get("mixer_control", ""))
        self.cards: list[alsa.Card] = []
        self.card: alsa.Card | None = None
        self.device: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ resolution
    def _resolve(self) -> tuple[str | None, alsa.Card | None]:
        if self.setting == "null":
            return "null", None
        if self.setting == "auto":
            card = alsa.auto_pick(self.cards)
            return (card.device(), card) if card else (None, None)
        return self.setting, alsa.card_of(self.setting, self.cards)

    @property
    def present(self) -> bool:
        if self.device == "null":
            return True
        if self.device is None:
            return False
        # Named devices we can't map to a card ("default", a custom PCM) are trusted.
        return self.card is not None or (alsa.card_of(self.device, []) is None and "hw:" not in self.device)

    @property
    def lossy_device(self) -> str:
        """For Spotify and Bluetooth (already lossy): 'plughw' converts formats the DAC
        doesn't take natively, e.g. a USB DAC that only accepts 32-bit."""
        dev = self.device or "null"
        return "plug" + dev if dev.startswith("hw:") else dev

    @property
    def mixer_control(self) -> str:
        if not self.card:
            return ""
        if self.card.id in self.mixers:
            return self.mixers[self.card.id]
        cfg_card, cfg_control = self._config_mixer
        return cfg_control if cfg_control and cfg_card in ("", self.card.id) else ""

    def describe(self) -> str:
        if self.device == "null":
            return "Silent output (testing)"
        if self.card:
            return self.card.name
        if self.setting == "auto":
            return "No DAC found"
        return self.names.get(self.device or "", self.device or "No DAC")

    def info(self) -> dict[str, Any]:
        return {
            "setting": self.setting,
            "device": self.device,
            "name": self.describe(),
            "kind": self.card.kind if self.card else None,
            "present": self.present,
            "mixer_control": self.mixer_control,
        }

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.refresh(apply=False)
        self._apply_to_players()
        log.info("Audio output: %s (%s)", self.describe(), self.device)
        self._task = asyncio.create_task(self._watch(), name="output-watch")

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                await self.refresh()
            except Exception:
                log.exception("Checking the sound cards failed")

    async def refresh(self, apply: bool = True) -> bool:
        """Re-read the cards. Returns True if the output changed."""
        async with self._lock:
            cards = await asyncio.to_thread(alsa.list_cards)
            was_present, old_device = self.present, self.device
            self.cards = cards
            self.device, self.card = self._resolve()
            if (was_present, old_device) == (self.present, self.device):
                return False
            if self.present and not was_present:
                log.info("DAC is back: %s", self.describe())
            elif was_present and not self.present:
                log.warning("DAC disappeared: %s", self.setting)
        if apply:
            await self._switch(device_changed=old_device != self.device, lost=was_present and not self.present)
        return True

    async def _switch(self, device_changed: bool, lost: bool) -> None:
        manager = self.ctx.manager
        if device_changed or lost:
            # Whatever plays must let go of the old (or vanished) device.
            await manager.deactivate_all()
        self._apply_to_players()
        if device_changed and self.device is not None:
            for source in manager.sources.values():
                with contextlib.suppress(Exception):
                    await source.output_changed()
        manager.volume = await self.ctx.volume.get()
        manager.notify(True)

    def _apply_to_players(self) -> None:
        self.ctx.engine.device = self.device or "null"
        self.ctx.volume.card = self.card.id if self.card else ""
        self.ctx.volume.control = self.mixer_control if self.present else ""

    # ------------------------------------------------------------ user choice
    async def choices(self) -> dict[str, Any]:
        cards = await asyncio.to_thread(alsa.list_cards)
        options = []
        for c in cards:
            options.append({**c.to_dict(), "volume_controls": await alsa.volume_controls(c.id)})
        auto = alsa.auto_pick(cards)
        return {
            "current": self.info(),
            "auto_choice": auto.name if auto else None,
            "cards": options,
        }

    def _save(self) -> None:
        self.ctx.store.set(STORE_KEY, {"device": self.setting, "mixers": self.mixers, "names": self.names})

    async def select(self, setting: str, mixer_control: str | None = None) -> dict[str, Any]:
        from ..sources.base import SourceError

        setting = setting.strip()
        if setting not in ("auto", "null"):
            cards = await asyncio.to_thread(alsa.list_cards)
            card = alsa.card_of(setting, cards)
            if card is None:
                raise SourceError(f"No sound card matches '{setting}'. Is the DAC switched on?", 404)
            if mixer_control:
                controls = await alsa.volume_controls(card.id)
                if mixer_control not in controls:
                    raise SourceError(f"'{mixer_control}' is not a volume control on {card.name}")
            self.names[setting] = card.name
            if mixer_control is not None:
                if mixer_control:
                    self.mixers[card.id] = mixer_control
                else:
                    self.mixers[card.id] = ""  # explicitly fixed volume
        old_device = self.device
        self.setting = setting
        self._save()
        async with self._lock:
            self.cards = await asyncio.to_thread(alsa.list_cards)
            self.device, self.card = self._resolve()
        if setting == "auto" and mixer_control is not None and self.card:
            self.mixers[self.card.id] = mixer_control
            self._save()
        await self._switch(device_changed=old_device != self.device, lost=False)
        log.info("Audio output set to %s (%s)", self.describe(), self.device)
        return self.info()
