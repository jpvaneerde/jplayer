#!/usr/bin/env python3
"""Stand-in for cd-paranoia in tests: a 3-track disc, read at a chosen speed.

  fake_paranoia.py -d DEV -Q            table of contents (like cd-paranoia -Q)
  fake_paranoia.py ... -r N -           raw 16-bit stereo PCM of track N on stdout

FAKE_PARANOIA_SPEED sets the read speed as a multiple of real time (default 2x).
"""
import math
import os
import struct
import sys
import time

TRACKS = [750, 300, 300]  # sectors: 10 s, 4 s, 4 s
SECTOR = 2352


def toc() -> None:
    print("cdparanoia III release 10.2 (fake)\n")
    print("Table of contents (audio tracks only):")
    print("track        length               begin        copy pre ch")
    print("===========================================================")
    start = 0
    for i, length in enumerate(TRACKS, start=1):
        print(f"  {i}.    {length} [00:00.00]    {start} [00:00.00]    no   no  2")
        start += length
    print(f"TOTAL   {start} [00:00.00]    (audio only)")


def read(track: int) -> None:
    speed = float(os.environ.get("FAKE_PARANOIA_SPEED", "2"))
    total = TRACKS[track - 1] * SECTOR
    freq = 220 * track
    out = sys.stdout.buffer
    sent = 0
    began = time.monotonic()
    chunk_frames = 4410  # 0.1 s of audio
    frame = 0
    while sent < total:
        frames = min(chunk_frames, (total - sent) // 4)
        data = b"".join(
            struct.pack("<hh", v, v)
            for v in (int(3000 * math.sin(2 * math.pi * freq * (frame + i) / 44100)) for i in range(frames))
        )
        out.write(data)
        out.flush()
        sent += len(data)
        frame += frames
        # pace: `speed` seconds of audio per second
        ahead = frame / 44100 / speed - (time.monotonic() - began)
        if ahead > 0:
            time.sleep(ahead)


def main() -> int:
    args = sys.argv[1:]
    if "-Q" in args:
        toc()
        return 0
    if "-r" in args and args[-1] == "-":
        read(int(args[-2]))
        return 0
    print("fake_paranoia: unsupported arguments", args, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
