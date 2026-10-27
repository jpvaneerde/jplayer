"""Find the ALSA sound cards: DAC HATs, USB DACs, HDMI, the Pi's headphone jack.

Reads /proc/asound directly, so it is cheap enough to poll for USB DACs being switched
on and off.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# JPLAY_ASOUND_PROC lets tests and UI work point this at a fake tree.
PROC = Path(os.environ.get("JPLAY_ASOUND_PROC", "/proc/asound"))

_CARD = re.compile(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s+(\S+)\s+-\s+(.*)$")
_CARD_ID = re.compile(r"CARD=([^,]+)")
_CARD_NUM = re.compile(r"^(?:plug)?hw:(\d+)")


# Friendlier names for common Raspberry Pi audio boards (by ALSA driver name).
KNOWN_DRIVERS = {
    "HifiberryDacpHD": "HiFiBerry DAC2 HD",
    "HifiberryDacp": "HiFiBerry DAC+ / DAC2 Pro",
    "HifiberryDac": "HiFiBerry DAC+ Light",
    "IQaudIODAC": "Raspberry Pi DAC+ (IQaudio)",
    "snd_rpi_hifiberry_dac": "HiFiBerry DAC",
}


def friendly_name(cid: str, driver: str, name: str) -> str:
    if driver in KNOWN_DRIVERS:
        return KNOWN_DRIVERS[driver]
    m = re.fullmatch(r"vc4hdmi(\d)", cid)
    if m:
        return f"HDMI {int(m.group(1)) + 1}"
    if cid == "Headphones":
        return "Pi headphone jack"
    if name.startswith("snd_rpi_"):
        return name[8:].replace("_", " ").title()
    return name


@dataclass
class Card:
    index: int
    id: str  # stable name, e.g. "sndrpihifiberry" or "DAC" (USB)
    driver: str
    name: str
    usb: bool
    playback: list[int] = field(default_factory=list)  # playback PCM device numbers
    formats: list[str] = field(default_factory=list)  # USB only: what the DAC accepts

    @property
    def kind(self) -> str:
        if self.usb:
            return "usb"
        low = f"{self.id} {self.driver} {self.name}".lower()
        if "hdmi" in low:
            return "hdmi"
        if "headphones" in low or "bcm2835" in low:
            return "analog"
        return "hat"  # I2S DAC boards: HiFiBerry, IQaudio/Raspberry Pi DAC, Allo ...

    def device(self, pcm: int | None = None) -> str:
        pcm = self.playback[0] if pcm is None and self.playback else (pcm or 0)
        return f"hw:CARD={self.id},DEV={pcm}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind
        d["device"] = self.device()
        return d


def list_cards(proc: Path | None = None) -> list[Card]:
    """All sound cards that can play audio."""
    proc = proc or PROC
    try:
        text = (proc / "cards").read_text()
    except OSError:
        return []
    cards = []
    for line in text.splitlines():
        m = _CARD.match(line)
        if not m:
            continue
        index, cid, driver, name = int(m.group(1)), m.group(2), m.group(3), m.group(4).strip()
        cdir = proc / f"card{index}"
        playback = sorted(
            int(p.name[3:-1]) for p in cdir.glob("pcm*p") if p.name[3:-1].isdigit()
        ) if cdir.exists() else [0]
        if cdir.exists() and not playback:
            continue  # capture-only card (a microphone, say)
        usb = driver == "USB-Audio" or (cdir / "usbid").exists()
        formats: list[str] = []
        if usb:
            try:
                stream = (cdir / "stream0").read_text()
                formats = list(dict.fromkeys(re.findall(r"Format:\s*(\S+)", stream.split("Capture:")[0])))
            except OSError:
                pass
        cards.append(Card(index, cid, driver, friendly_name(cid, driver, name), usb, playback or [0], formats))
    return cards


def card_of(device: str, cards: list[Card]) -> Card | None:
    """Which card an ALSA device string ('hw:CARD=DAC,DEV=0', 'hw:1,0') points at."""
    m = _CARD_ID.search(device)
    if m:
        return next((c for c in cards if c.id == m.group(1)), None)
    m = _CARD_NUM.match(device)
    if m:
        return next((c for c in cards if c.index == int(m.group(1))), None)
    return None


AUTO_ORDER = {"usb": 0, "hat": 1}


def auto_pick(cards: list[Card]) -> Card | None:
    """Best guess for a hi-fi box: a USB DAC if one is switched on, else a DAC HAT.

    HDMI and the Pi's own headphone jack are never picked automatically, so a USB DAC
    that is switched off doesn't make the music come out of the TV instead.
    """
    candidates = [c for c in cards if c.kind in AUTO_ORDER]
    if not candidates:
        return None
    return sorted(candidates, key=lambda c: (AUTO_ORDER[c.kind], c.index))[0]


async def volume_controls(card_id: str) -> list[str]:
    """Hardware volume controls on a card (amixer controls with playback volume)."""
    if shutil.which("amixer") is None:
        return []
    proc = await asyncio.create_subprocess_exec(
        "amixer", "-c", card_id, "scontents",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return parse_volume_controls(out.decode(errors="replace"))


def parse_volume_controls(text: str) -> list[str]:
    controls = []
    current = None
    for line in text.splitlines():
        m = re.match(r"Simple mixer control '(.+)',(\d+)", line)
        if m:
            current = m.group(1)
            continue
        if current and "Capabilities:" in line and re.search(r"\b(pvolume|volume)\b", line):
            controls.append(current)
            current = None
    return controls
