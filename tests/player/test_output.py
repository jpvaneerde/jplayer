"""Sound card detection and DAC hand-over (fake /proc/asound)."""
from pathlib import Path

import httpx
import pytest

from jplay.player.config import load_config
from jplay.player.core import alsa
from jplay.player.core.context import AppContext
from jplay.player.core.events import EventHub
from jplay.player.core.manager import SourceManager
from jplay.player.core.mpv import MpvEngine
from jplay.player.core.output import Output
from jplay.player.core.store import StateStore
from jplay.player.core.volume import Volume
from jplay.player.sources.base import Source, SourceError

HAT = " 0 [sndrpihifiberry]: HifiberryDacpHD - snd_rpi_hifiberry_dacplushd\n                      snd_rpi_hifiberry_dacplushd\n"
HDMI = " 1 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0\n                      vc4-hdmi-0\n"
USB = " 2 [DAC            ]: USB-Audio - Topping DX3 Pro\n                      Topping DX3 Pro at usb-0000:01:00.0-1.3, high speed\n"
STREAM = """Topping DX3 Pro at usb-0000:01:00.0-1.3, high speed : USB Audio

Playback:
  Status: Stop
  Interface 1
    Altset 1
    Format: S32_LE
    Channels: 2
    Rates: 44100, 48000, 96000
"""


def make_proc(root: Path, cards: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "cards").write_text("".join(cards))
    for i in range(3):
        d = root / f"card{i}"
        if d.exists():
            for f in sorted(d.rglob("*"), reverse=True):
                f.unlink() if f.is_file() else f.rmdir()
            d.rmdir()
    for text in cards:
        idx = int(text.split("[")[0])
        d = root / f"card{idx}"
        (d / "pcm0p").mkdir(parents=True)
        if "USB-Audio" in text:
            (d / "usbid").write_text("152a:8750\n")
            (d / "stream0").write_text(STREAM)


def test_list_cards_and_kinds(tmp_path):
    make_proc(tmp_path, [HAT, HDMI, USB])
    cards = alsa.list_cards(tmp_path)
    assert [(c.id, c.kind) for c in cards] == [("sndrpihifiberry", "hat"), ("vc4hdmi0", "hdmi"), ("DAC", "usb")]
    usb = cards[2]
    assert usb.name == "Topping DX3 Pro" and usb.formats == ["S32_LE"]
    assert usb.device() == "hw:CARD=DAC,DEV=0"
    assert alsa.card_of("hw:CARD=DAC,DEV=0", cards) is usb
    assert alsa.card_of("hw:0,0", cards).id == "sndrpihifiberry"


def test_auto_prefers_usb_then_hat_never_hdmi(tmp_path):
    make_proc(tmp_path, [HAT, HDMI, USB])
    assert alsa.auto_pick(alsa.list_cards(tmp_path)).id == "DAC"
    make_proc(tmp_path, [HAT, HDMI])
    assert alsa.auto_pick(alsa.list_cards(tmp_path)).id == "sndrpihifiberry"
    make_proc(tmp_path, [HDMI])
    assert alsa.auto_pick(alsa.list_cards(tmp_path)) is None


def test_parse_volume_controls():
    text = """Simple mixer control 'Digital',0
  Capabilities: pvolume pswitch
Simple mixer control 'Clock',0
  Capabilities: enum
Simple mixer control 'PCM',0
  Capabilities: pvolume pvolume-joined
"""
    assert alsa.parse_volume_controls(text) == ["Digital", "PCM"]


class Recorder(Source):
    id = "rec"
    name = "Recorder"

    def __init__(self, ctx, log):
        super().__init__(ctx)
        self.log = log

    async def start(self):
        self.set_available(True)

    async def activate(self):
        self.log.append("activate")

    async def deactivate(self):
        self.log.append("deactivate")

    async def output_changed(self):
        self.log.append(f"output {self.ctx.output.device}")


@pytest.fixture
async def usb_box(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    make_proc(proc, [HDMI.replace(" 1 [", " 0 ["), USB.replace(" 2 [", " 1 [")])
    monkeypatch.setattr(alsa, "PROC", proc)
    cfg = load_config(None)
    ctx = AppContext(
        config=cfg, store=StateStore(tmp_path / "s.json"), hub=EventHub(),
        engine=MpvEngine("mpv", "null", [], tmp_path), volume=Volume("", ""),
        http=httpx.AsyncClient(), state_dir=tmp_path, runtime_dir=tmp_path,
    )
    ctx.output = Output(ctx)
    log: list[str] = []
    ctx.manager = SourceManager(ctx, [Recorder(ctx, log)])
    await ctx.manager.start()
    yield ctx, proc, log
    await ctx.manager.shutdown()
    await ctx.http.aclose()


async def test_usb_dac_switched_off_and_on(usb_box):
    ctx, proc, log = usb_box
    out = ctx.output
    assert out.present and out.device == "hw:CARD=DAC,DEV=0" and ctx.engine.device == out.device
    assert out.lossy_device == "plughw:CARD=DAC,DEV=0"
    await ctx.manager.activate("rec")

    make_proc(proc, [HDMI.replace(" 1 [", " 0 [")])  # DAC switched off
    assert await out.refresh()
    assert not out.present and ctx.manager.active_id is None and log[-1] == "deactivate"
    with pytest.raises(SourceError) as err:
        await ctx.manager.activate("rec")
    assert "DAC" in str(err.value) and err.value.status == 409
    assert ctx.manager.snapshot()["output"]["present"] is False

    make_proc(proc, [HDMI.replace(" 1 [", " 0 ["), USB.replace(" 2 [", " 1 [")])  # back on
    assert await out.refresh()
    assert out.present and log[-1] == "output hw:CARD=DAC,DEV=0"
    await ctx.manager.activate("rec")


async def test_select_fixed_device_is_saved(usb_box):
    ctx, proc, log = usb_box
    await ctx.output.select("hw:CARD=vc4hdmi0,DEV=0", mixer_control=None)
    assert ctx.output.device == "hw:CARD=vc4hdmi0,DEV=0" and ctx.engine.device == ctx.output.device
    assert ctx.store.get("output")["device"] == "hw:CARD=vc4hdmi0,DEV=0"
    assert log[-1] == "output hw:CARD=vc4hdmi0,DEV=0"
    with pytest.raises(SourceError):
        await ctx.output.select("hw:CARD=Nope,DEV=0")
    # a fresh Output reads the saved choice
    assert Output(ctx).setting == "hw:CARD=vc4hdmi0,DEV=0"
