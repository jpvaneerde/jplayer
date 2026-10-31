"""Panako (https://github.com/JorenSix/Panako), called through its command line.

Panako's own identifier of a file is the number in its name when the name is a number (else a hash of the file's
contents). The library hands Panako links named "<id>.flac", so every song has an identifier the library chose
and keeps (see library.py). Panako 2.1's delete doesn't work, so nothing is ever deleted from it: songs that
are gone are ignored by the library, and "rebuild" starts the index again from Panako's cache of prints.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

JAR = os.environ.get("PANAKO_JAR", "/opt/panako/panako.jar")
# lmdbjava (Panako's database) needs these on Java 17 and newer
JAVA = ["java", "--add-opens", "java.base/java.nio=ALL-UNNAMED", "--add-opens", "java.base/sun.nio.ch=ALL-UNNAMED"]


# Looser than Panako's defaults: a microphone in a room, a record a little off speed. Tested on real music with
# room-like clips (noise, highs cut, 0.5-2 % fast): more songs found, and still no false matches on music and
# speech that aren't in the collection. PANAKO_SETTINGS (environment) adds or overrides, e.g. "PANAKO_MIN_HITS_FILTERED=5".
MATCH_SETTINGS = ["PANAKO_MIN_HITS_UNFILTERED=5", "PANAKO_MIN_HITS_FILTERED=3", "PANAKO_MIN_SEC_WITH_MATCH=0.1",
                  "PANAKO_MIN_MATCH_DURATION=3"]


class PanakoError(Exception):
    pass


@dataclass
class Match:
    id: int
    score: int
    query_start: float          # seconds into the clip where the match starts
    match_start: float          # ... and where that is in the song
    time_factor: float          # 1.02: the clip plays 2 % faster than the file (vinyl, a turntable's speed)
    seconds_with_match: float   # 0..1: how much of the matched stretch really matched

    @property
    def offset(self) -> float:
        """Where in the song the clip started (seconds)."""
        return max(0.0, self.match_start - self.query_start * (self.time_factor or 1.0))


class Panako:
    def __init__(self, data_dir: str | Path, processors: int = 2, java_opts: list[str] | None = None):
        self.data_dir = Path(data_dir)
        self.processors = max(1, processors)
        self.java_opts = java_opts if java_opts is not None else os.environ.get("JAVA_OPTS", "").split()

    def _config(self) -> list[str]:
        return ["STRATEGY=PANAKO", f"PANAKO_LMDB_FOLDER={self.data_dir / 'panako_db'}",
                f"PANAKO_CACHE_FOLDER={self.data_dir / 'panako_cache'}", f"AVAILABLE_PROCESSORS={self.processors}",
                "CHECK_DUPLICATE_FILE_NAMES=FALSE", *MATCH_SETTINGS, *os.environ.get("PANAKO_SETTINGS", "").split()]

    def _run(self, args: list[str], timeout: float) -> str:
        cmd = JAVA + self.java_opts + ["-jar", JAR] + args + self._config()
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=self.data_dir)
        except subprocess.TimeoutExpired as e:
            raise PanakoError(f"Panako took longer than {timeout:.0f} s") from e
        except FileNotFoundError as e:
            raise PanakoError("Java isn't installed") from e
        if res.returncode != 0:
            err = [ln for ln in res.stderr.splitlines() if "Exception" in ln or "Error" in ln][-3:]
            raise PanakoError(f"Panako failed: {' '.join(err) or res.stderr[-300:]}")
        return res.stdout

    def store(self, paths: list[str], timeout: float = 6 * 3600) -> set[int]:
        """Fingerprint these files (links named <id>.<ext>). Returns the ids Panako has now (stored or already)."""
        if not paths:
            return set()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lst = self.data_dir / "store-list.txt"
        lst.write_text("\n".join(paths) + "\n")
        out = self._run(["store", str(lst)], timeout)
        done = set()
        for line in out.splitlines():
            # "3/5;12.flac;00:01:28;1.29 s;68.50" or "1/1;12.flac;Skipped: resource already stored;"
            m = re.match(r"\s*\d+/\d+;(\d+)\.\w+;(.*)$", line)
            if m and ("Skipped: resource already stored" in m.group(2) or re.match(r"\d\d:\d\d:\d\d;", m.group(2))):
                done.add(int(m.group(1)))
        return done

    def query(self, clip: str, timeout: float = 60) -> list[Match]:
        """Matches for a clip, best first (empty: nothing in the collection)."""
        out = self._run(["query", clip], timeout)
        matches = []
        for line in out.splitlines():
            cols = [c.strip() for c in line.split(";")]
            if len(cols) < 13 or not cols[0].isdigit() or cols[5] == "null":
                continue
            m = re.search(r"(\d+)\.\w+$", cols[5])
            try:
                matches.append(Match(
                    id=int(m.group(1)) if m else int(cols[6]), score=int(float(cols[9])),
                    query_start=float(cols[3]), match_start=float(cols[7]),
                    time_factor=float(cols[10].rstrip("% ").strip() or 1), seconds_with_match=float(cols[12] or 0)))
            except (ValueError, AttributeError):
                continue
        return sorted(matches, key=lambda x: -x.score)

    def clear(self) -> None:
        """Empty the index (the prints stay in the cache, so storing again is quick)."""
        import shutil
        shutil.rmtree(self.data_dir / "panako_db", ignore_errors=True)
