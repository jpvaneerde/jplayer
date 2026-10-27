"""Announce a JPlay service on the network (mDNS/Bonjour), so Home Assistant and the other units find it by itself.

  _jplay._tcp          a player (port 8080)
  _jplay-display._tcp  a JPlaying Now display (port 5000)
  _jplay-hub._tcp      the hub (port 8090)


Raspberry Pi OS runs Avahi; this asks it over D-Bus to publish the service. No root and no extra packages are
needed (dbus-next is already there for Bluetooth). Without Avahi or D-Bus, nothing is announced and JPlay
carries on normally.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

SERVICE_TYPE = "_jplay._tcp"
DISPLAY_TYPE = "_jplay-display._tcp"
HUB_TYPE = "_jplay-hub._tcp"
AVAHI = "org.freedesktop.Avahi"


class Announcer:
    def __init__(self, name: str, port: int, txt: dict[str, str], service_type: str = SERVICE_TYPE):
        self.name, self.port, self.txt, self.type = name, port, txt, service_type
        self.bus: Any = None

    async def start(self) -> None:
        try:
            from dbus_next import BusType, Message, MessageType
            from dbus_next.aio import MessageBus
        except ImportError:
            log.info("Not announcing on the network: the 'dbus-next' package is missing")
            return
        try:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

            async def call(path: str, interface: str, member: str, signature: str = "", body: list | None = None):
                reply = await self.bus.call(Message(destination=AVAHI, path=path, interface=interface, member=member,
                                                    signature=signature, body=body or []))
                if reply.message_type == MessageType.ERROR:
                    raise RuntimeError(f"{reply.error_name}: {reply.body[0] if reply.body else ''}")
                return reply.body

            (group,) = await call("/", "org.freedesktop.Avahi.Server", "EntryGroupNew")
            txt = [f"{k}={v}".encode() for k, v in self.txt.items()]
            # interface -1 and protocol -1 = all networks, IPv4 and IPv6; empty domain/host = Avahi's defaults
            await call(group, "org.freedesktop.Avahi.EntryGroup", "AddService", "iiussssqaay",
                       [-1, -1, 0, self.name, self.type, "", "", self.port, txt])
            await call(group, "org.freedesktop.Avahi.EntryGroup", "Commit")
            log.info("Announced on the network as '%s' (%s, port %s)", self.name, self.type, self.port)
        except Exception as exc:  # no Avahi, no D-Bus, name clash...
            log.info("Not announcing on the network: %s", exc)
            await self.shutdown()

    async def shutdown(self) -> None:
        # Closing the D-Bus connection makes Avahi withdraw the announcement.
        if self.bus is not None:
            try:
                self.bus.disconnect()
            except Exception:
                pass
            self.bus = None
