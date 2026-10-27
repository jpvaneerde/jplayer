"""Bluetooth audio receiver (A2DP sink) through BlueZ + BlueALSA.

* BlueZ (over D-Bus) tells us when a phone starts streaming; Bluetooth then claims the DAC.
* bluealsa-aplay moves the audio from BlueALSA to the DAC and only runs while Bluetooth is
  the active source, so it never fights the other sources for the device.
* Track info and transport controls use AVRCP (org.bluez.MediaPlayer1).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from typing import Any

from ..core.models import Capabilities, PlayState
from .base import Source, SourceError

log = logging.getLogger(__name__)

BLUEZ = "org.bluez"
PROPS = "org.freedesktop.DBus.Properties"
OM = "org.freedesktop.DBus.ObjectManager"


def unwrap(value: Any) -> Any:
    """dbus-next Variants -> plain Python values."""
    if hasattr(value, "value") and hasattr(value, "signature"):
        return unwrap(value.value)
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    return value


def device_path_of(path: str) -> str:
    """/org/bluez/hci0/dev_AA_BB/player0 -> /org/bluez/hci0/dev_AA_BB"""
    parts = path.split("/")
    return "/".join(parts[:5]) if len(parts) > 5 else path


def explain_adapter_error(error: str, prop: str = "") -> str:
    """BlueZ's error -> what to do about it."""
    if "Blocked" in error or "rfkill" in error.lower():
        return ("Bluetooth is switched off on the Pi (blocked). On the Pi run: "
                "sudo rfkill unblock bluetooth && sudo systemctl restart bluetooth jplay")
    if "NotReady" in error or "Powered" in error or prop == "Powered":
        return ("The Pi's Bluetooth radio won't switch on. On the Pi run: sudo rfkill unblock bluetooth && "
                "sudo systemctl restart hciuart bluetooth jplay (or reboot the Pi)")
    return f"Bluetooth refused: {error}"


class BluetoothSource(Source):
    id = "bluetooth"
    name = "Bluetooth"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.alias = self.cfg.get("alias") or ctx.config["server"]["name"]
        self.aplay_binary = self.cfg.get("aplay_binary", "bluealsa-aplay")
        self.bus: Any = None
        self.adapter: str | None = None
        self.adapter_props: dict[str, Any] = {}
        self.devices: dict[str, dict[str, Any]] = {}
        self.transports: dict[str, dict[str, Any]] = {}
        self.players: dict[str, dict[str, Any]] = {}
        self._aplay: asyncio.subprocess.Process | None = None

    # ------------------------------------------------------------ setup
    async def start(self) -> None:
        try:
            from dbus_next import BusType, Message
            from dbus_next.aio import MessageBus
        except ImportError:
            self.set_available(False, "Install the 'dbus-next' Python package")
            return
        if shutil.which(self.aplay_binary) is None:
            self.set_available(False, f"'{self.aplay_binary}' is not installed (bluez-alsa-utils)")
            return
        try:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            await self.bus.call(
                Message(
                    destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus", member="AddMatch", signature="s",
                    body=["type='signal',sender='org.bluez'"],
                )
            )
            self.bus.add_message_handler(self._on_message)
            await self._load_objects()
        except Exception as exc:
            self.set_available(False, f"BlueZ not reachable: {exc}")
            return
        if not self.adapter:
            self.set_available(False, "No Bluetooth adapter found")
            return
        await self._set_adapter("Alias", "s", self.alias)
        await self._set_adapter("Powered", "b", True)
        await self._set_adapter("Pairable", "b", True)
        self.set_available(True)
        # A phone may already be streaming when we start.
        if any(t.get("State") in ("pending", "active") for t in self.transports.values()):
            asyncio.create_task(self._claim())

    async def shutdown(self) -> None:
        await self._stop_aplay()
        if self.bus:
            self.bus.disconnect()

    async def _call(self, path: str, interface: str, member: str, signature: str = "", body: list | None = None) -> Any:
        from dbus_next import Message, MessageType

        reply = await self.bus.call(
            Message(destination=BLUEZ, path=path, interface=interface, member=member,
                    signature=signature, body=body or [])
        )
        if reply.message_type == MessageType.ERROR:
            raise SourceError(f"Bluetooth: {reply.error_name}: {reply.body[0] if reply.body else ''}", 502)
        return unwrap(reply.body)

    async def _set_adapter(self, prop: str, sig: str, value: Any, strict: bool = False) -> None:
        """Change a property of the Pi's Bluetooth adapter. strict: let the error reach the web page."""
        from dbus_next import Variant

        try:
            await self._call(self.adapter, PROPS, "Set", "ssv", ["org.bluez.Adapter1", prop, Variant(sig, value)])
            self.adapter_props[prop] = value
        except SourceError as exc:
            log.warning("Could not set adapter %s: %s", prop, exc)
            if strict:
                raise SourceError(explain_adapter_error(str(exc), prop), 409) from exc

    async def _load_objects(self) -> None:
        (objects,) = await self._call("/", OM, "GetManagedObjects")
        for path, ifaces in objects.items():
            self._add_interfaces(path, ifaces)

    def _add_interfaces(self, path: str, ifaces: dict[str, dict[str, Any]]) -> None:
        if "org.bluez.Adapter1" in ifaces and self.adapter is None:
            self.adapter = path
            self.adapter_props = dict(ifaces["org.bluez.Adapter1"])
        if "org.bluez.Device1" in ifaces:
            self.devices[path] = dict(ifaces["org.bluez.Device1"])
        if "org.bluez.MediaTransport1" in ifaces:
            self.transports[path] = dict(ifaces["org.bluez.MediaTransport1"])
            self._transport_changed(path)
        if "org.bluez.MediaPlayer1" in ifaces:
            self.players[path] = dict(ifaces["org.bluez.MediaPlayer1"])
            self._player_changed(path)

    # ------------------------------------------------------------ D-Bus signals
    def _on_message(self, msg: Any) -> None:
        from dbus_next import MessageType

        if msg.message_type != MessageType.SIGNAL:
            return
        try:
            if msg.member == "PropertiesChanged" and msg.interface == PROPS:
                iface, changed, _invalid = msg.body
                changed = unwrap(changed)
                if iface == "org.bluez.Device1":
                    self.devices.setdefault(msg.path, {}).update(changed)
                    self.ctx.manager.notify(True)
                elif iface == "org.bluez.MediaTransport1":
                    self.transports.setdefault(msg.path, {}).update(changed)
                    self._transport_changed(msg.path)
                elif iface == "org.bluez.MediaPlayer1":
                    self.players.setdefault(msg.path, {}).update(changed)
                    self._player_changed(msg.path)
                elif iface == "org.bluez.Adapter1":
                    self.adapter_props.update(changed)
                    self.ctx.manager.notify(True)
            elif msg.member == "InterfacesAdded":
                path, ifaces = msg.body
                self._add_interfaces(path, unwrap(ifaces))
                self.ctx.manager.notify(True)
            elif msg.member == "InterfacesRemoved":
                path, ifaces = msg.body
                if "org.bluez.Device1" in ifaces:
                    self.devices.pop(path, None)
                if "org.bluez.MediaTransport1" in ifaces:
                    self.transports.pop(path, None)
                    self._transport_changed(path)
                    if not self.transports and self.active:
                        # The phone disconnected: stop the audio path, the player is idle again.
                        asyncio.get_running_loop().create_task(self._phone_left())
                if "org.bluez.MediaPlayer1" in ifaces:
                    self.players.pop(path, None)
                self.ctx.manager.notify(True)
        except Exception:
            log.exception("Bad BlueZ signal")

    def _transport_changed(self, path: str) -> None:
        state = self.transports.get(path, {}).get("State")
        if state in ("pending", "active"):
            if self.available:
                asyncio.get_running_loop().create_task(self._claim())
            if state == "active":
                self.update(state=PlayState.PLAYING)
        elif not any(t.get("State") in ("pending", "active") for t in self.transports.values()):
            if self.np.state == PlayState.PLAYING:
                self.update(state=PlayState.PAUSED)

    def _player_changed(self, path: str) -> None:
        p = self.players.get(path, {})
        track = p.get("Track") or {}
        status = p.get("Status", "")
        dev = self.devices.get(device_path_of(path), {})
        fields: dict[str, Any] = {
            "title": track.get("Title", ""),
            "artist": track.get("Artist", ""),
            "album": track.get("Album", ""),
            "duration": (track.get("Duration") or 0) / 1000 or None,
            "track_number": track.get("TrackNumber") or None,
            "extra": {"device": dev.get("Alias") or dev.get("Name", "")},
        }
        if "Position" in p:
            fields["position"] = round(p["Position"] / 1000, 1)
        if status == "playing":
            fields["state"] = PlayState.PLAYING
        elif status == "paused":
            fields["state"] = PlayState.PAUSED
        self.update(**fields)

    async def _phone_left(self) -> None:
        await self._stop_aplay()
        self.reset_now_playing()
        await self.ctx.manager.release(self.id)

    async def _claim(self) -> None:
        if not self.active:
            try:
                await self.ctx.manager.activate(self.id)
            except SourceError as exc:
                log.warning("Bluetooth could not take over: %s", exc)

    # ------------------------------------------------------------ audio path
    async def activate(self) -> None:
        await self._start_aplay()

    async def deactivate(self) -> None:
        player = self._player_path()
        if player:
            with contextlib.suppress(Exception):
                await self._call(player, "org.bluez.MediaPlayer1", "Pause")
        await self._stop_aplay()
        self.update(state=PlayState.STOPPED)

    async def _start_aplay(self) -> None:
        if self._aplay and self._aplay.returncode is None:
            return
        device = self.ctx.output.lossy_device if self.ctx.output else "null"
        log.info("Starting bluealsa-aplay -> %s", device)
        self._aplay = await asyncio.create_subprocess_exec(
            self.aplay_binary, "--profile-a2dp", f"--pcm={device}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )

    async def _stop_aplay(self) -> None:
        proc, self._aplay = self._aplay, None
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    # ------------------------------------------------------------ controls
    def _player_path(self) -> str | None:
        for path in self.players:
            dev = self.devices.get(device_path_of(path), {})
            if dev.get("Connected", True):
                return path
        return None

    def capabilities(self) -> Capabilities:
        has = self._player_path() is not None
        return Capabilities(play=has, pause=has, stop=has, next=has, previous=has, seek=False)

    async def _player(self, member: str) -> None:
        path = self._player_path()
        if not path:
            raise SourceError("No Bluetooth device is connected", 409)
        await self._call(path, "org.bluez.MediaPlayer1", member)

    async def play(self) -> None:
        await self._claim()
        await self._player("Play")

    async def pause(self) -> None:
        await self._player("Pause")

    async def stop(self) -> None:
        await self._player("Stop")

    async def next(self) -> None:
        await self._player("Next")

    async def previous(self) -> None:
        await self._player("Previous")

    # ------------------------------------------------------------ pairing / devices
    def device_list(self) -> list[dict[str, Any]]:
        out = []
        for path, d in self.devices.items():
            if not (d.get("Paired") or d.get("Connected")):
                continue
            out.append(
                {
                    "address": d.get("Address", ""),
                    "name": d.get("Alias") or d.get("Name") or d.get("Address", ""),
                    "connected": bool(d.get("Connected")),
                    "paired": bool(d.get("Paired")),
                    "icon": d.get("Icon", ""),
                    "path": path,
                }
            )
        return sorted(out, key=lambda x: (not x["connected"], x["name"].lower()))

    def details(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "discoverable": bool(self.adapter_props.get("Discoverable")),
            "devices": self.device_list(),
        }

    async def set_discoverable(self, on: bool) -> None:
        if not self.adapter:
            raise SourceError("No Bluetooth adapter", 409)
        seconds = int(self.cfg.get("discoverable_seconds", 180))
        if on:
            # Bluetooth may have been switched off since JPlay started (rfkill, a crash of bluetoothd...).
            await self._set_adapter("Powered", "b", True, strict=True)
            await self._set_adapter("DiscoverableTimeout", "u", seconds)
            await self._set_adapter("Pairable", "b", True, strict=True)
        await self._set_adapter("Discoverable", "b", on, strict=True)
        log.info("Bluetooth pairing %s", f"on for {seconds} s" if on else "off")
        self.ctx.manager.notify(True)

    def _path_for(self, address: str) -> str:
        for path, d in self.devices.items():
            if d.get("Address", "").lower() == address.lower():
                return path
        raise SourceError(f"Unknown device {address}", 404)

    async def disconnect(self, address: str) -> None:
        await self._call(self._path_for(address), "org.bluez.Device1", "Disconnect")

    async def connect(self, address: str) -> None:
        await self._call(self._path_for(address), "org.bluez.Device1", "Connect")

    async def forget(self, address: str) -> None:
        path = self._path_for(address)
        await self._call(self.adapter, "org.bluez.Adapter1", "RemoveDevice", "o", [path])
        self.devices.pop(path, None)
        self.ctx.manager.notify(True)
