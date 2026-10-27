"""Shared playback engine: one mpv process driven over its JSON IPC socket.

Radio, Plex and CD all play through this engine. Only one source owns it at a time.
When the owner releases it, mpv quits so the ALSA device is free for Spotify or Bluetooth.
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

OBSERVED = (
    "pause",
    "time-pos",
    "duration",
    "playlist-pos",
    "playlist-count",
    "metadata",
    "idle-active",
    "media-title",
    "audio-params",
)


class EngineListener(Protocol):
    async def on_mpv_property(self, name: str, value: Any) -> None: ...

    async def on_mpv_event(self, event: dict[str, Any]) -> None: ...


class MpvError(RuntimeError):
    pass


class MpvEngine:
    def __init__(self, binary: str, device: str, extra_args: list[str], runtime_dir: Path):
        self.binary = binary
        self.device = device
        self.extra_args = list(extra_args)
        self.socket_path = runtime_dir / "mpv.sock"
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()
        self._stopping = False
        self.listener: EngineListener | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and self._writer is not None

    def _args(self) -> list[str]:
        args = [
            self.binary,
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            "--config=no",
            "--load-scripts=no",
            "--ytdl=no",
            "--input-default-bindings=no",
            f"--input-ipc-server={self.socket_path}",
            "--gapless-audio=yes",
            "--prefetch-playlist=yes",  # open the next track early, so track changes are seamless
            "--keep-open=no",
            "--audio-display=no",
            "--replaygain=no",
            "--volume=100",
            "--volume-max=100",
            "--audio-pitch-correction=no",
            "--audio-buffer=0.5",
            "--cache=yes",
            "--demuxer-max-bytes=16MiB",
            "--demuxer-readahead-secs=20",
            "--network-timeout=15",
            "--user-agent=JPlay/0.1",
            "--msg-level=all=warn",
        ]
        if self.device == "null":
            args.append("--ao=null")
        else:
            args += ["--ao=alsa", f"--audio-device=alsa/{self.device}"]
        return args + self.extra_args

    async def acquire(self, listener: EngineListener) -> None:
        """Start mpv if needed and make `listener` the owner."""
        async with self._lock:
            if not self.running:
                await self._start()
            self.listener = listener

    async def release(self, listener: EngineListener | None = None) -> None:
        """Quit mpv so the audio device is free. Ignored if `listener` is not the owner."""
        async with self._lock:
            if listener is not None and self.listener is not listener:
                return
            self.listener = None
            await self._stop()

    async def _start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        self._stopping = False
        log.info("Starting mpv on %s", self.device)
        self._proc = await asyncio.create_subprocess_exec(
            *self._args(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL
        )
        for _ in range(100):  # up to 5 s
            if self.socket_path.exists():
                try:
                    self._reader, self._writer = await asyncio.open_unix_connection(
                        str(self.socket_path), limit=4 * 1024 * 1024
                    )
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    pass
            if self._proc.returncode is not None:
                raise MpvError(f"mpv exited with code {self._proc.returncode}")
            await asyncio.sleep(0.05)
        else:
            await self._kill()
            raise MpvError("mpv IPC socket did not appear")
        self._read_task = asyncio.create_task(self._read_loop(), name="mpv-reader")
        for i, prop in enumerate(OBSERVED, start=1):
            await self._send(["observe_property", i, prop])

    async def _stop(self) -> None:
        if self._proc is None:
            return
        self._stopping = True
        if self.running:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._send(["quit"]), 2)
        try:
            await asyncio.wait_for(self._proc.wait(), 3)
        except asyncio.TimeoutError:
            await self._kill()
        await self._cleanup()

    async def _kill(self) -> None:
        if self._proc and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            with contextlib.suppress(Exception):
                await self._proc.wait()

    async def _cleanup(self) -> None:
        if self._read_task:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
        if self._writer:
            self._writer.close()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MpvError("mpv stopped"))
        self._pending.clear()
        self._proc = self._reader = self._writer = self._read_task = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.socket_path)

    # ------------------------------------------------------------------ IPC
    async def _send(self, command: list[Any], timeout: float = 5.0) -> Any:
        if self._writer is None:
            raise MpvError("mpv is not running")
        req_id = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        line = json.dumps({"command": command, "request_id": req_id}) + "\n"
        self._writer.write(line.encode())
        await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(req_id, None)

    async def command(self, *args: Any, timeout: float = 5.0) -> Any:
        return await self._send(list(args), timeout)

    async def set_property(self, name: str, value: Any) -> None:
        await self._send(["set_property", name, value])

    async def get_property(self, name: str) -> Any:
        return await self._send(["get_property", name])

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "request_id" in msg and "event" not in msg:
                    fut = self._pending.get(msg["request_id"])
                    if fut and not fut.done():
                        if msg.get("error") == "success":
                            fut.set_result(msg.get("data"))
                        else:
                            fut.set_exception(MpvError(msg.get("error", "unknown error")))
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            log.exception("mpv reader failed")
        if self._stopping:
            return
        # mpv went away on its own (crash, killed). Clean up and tell the owner.
        log.warning("mpv exited unexpectedly")
        self._read_task = None  # this task is finishing; _cleanup must not await it
        asyncio.create_task(self._after_crash())

    async def _after_crash(self) -> None:
        async with self._lock:
            listener = self.listener
            self.listener = None
            if self._proc is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._proc.wait(), 2)
            await self._kill()
            await self._cleanup()
        if listener is not None:
            await listener.on_mpv_event({"event": "engine-exit"})

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        listener = self.listener
        if listener is None:
            return
        try:
            if msg.get("event") == "property-change":
                await listener.on_mpv_property(msg.get("name", ""), msg.get("data"))
            elif "event" in msg:
                await listener.on_mpv_event(msg)
        except Exception:
            log.exception("Error handling mpv message %s", msg.get("event"))

    async def shutdown(self) -> None:
        await self.release()
