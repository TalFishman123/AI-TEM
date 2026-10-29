"""Where everything is written, and what happens when the NAS is not there.

Layout under ``<log_root>/<subdir>/``::

    2026/2026-10/2026-10-15/
        state.jsonl            one JSON object per line: start, change, heartbeat, frame, status events
        frames/
            2026-10-15_16-20-01.123.png   one screen-camera frame, the state embedded as PNG text
        frames_dm/
            2026-10-15_17-02-11.456.png   one Orius frame copied from DM's live View (photoemission)

Lines are only ever appended; a day is one folder, so old days can be archived or deleted as whole
folders. When the NAS cannot be written, the same layout is written to ``spool_dir`` on the local
disk, and moved to the NAS (appending the JSONL) as soon as it can be written again.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
import shutil
import time

from . import png

log = logging.getLogger("utem_logger")


def day_rel(t: datetime.datetime) -> pathlib.Path:
    return pathlib.Path(t.strftime("%Y"), t.strftime("%Y-%m"), t.strftime("%Y-%m-%d"))


class Store:
    def __init__(self, log_root, subdir, spool_dir, retry_s=60, png_level=1):
        self.primary = pathlib.Path(log_root) / subdir
        self.spool = pathlib.Path(spool_dir)
        self.retry_s, self.png_level = retry_s, png_level
        self.on_nas = True
        self.next_retry = 0.0
        self.events = []          # status changes for the logger to record (nas_unavailable, ...)

    # ---- where to write
    def _root(self):
        if not self.on_nas and time.monotonic() >= self.next_retry:
            self.next_retry = time.monotonic() + self.retry_s
            if self._flush_spool():
                self.on_nas = True
                log.info("NAS back: spool moved to %s", self.primary)
                self.events.append(("nas_available", {"path": str(self.primary)}))
        return self.primary if self.on_nas else self.spool

    def _fail(self, e):
        if self.on_nas:
            log.error("cannot write to %s (%s): writing to %s until it is back", self.primary, e, self.spool)
            self.events.append(("nas_unavailable", {"path": str(self.primary), "error": str(e)[:200],
                                                    "spool": str(self.spool)}))
        self.on_nas = False
        self.next_retry = time.monotonic() + self.retry_s

    def _write(self, fn):
        """fn(root) -> result; on the NAS first, the spool if that fails."""
        root = self._root()
        if root == self.primary:
            try:
                return fn(root)
            except OSError as e:
                self._fail(e)
        return fn(self.spool)       # a failing local disk raises: the logger records the error

    # ---- writers
    def append(self, t: datetime.datetime, record: dict):
        line = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"

        def w(root):
            d = root / day_rel(t)
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "state.jsonl", "a", encoding="utf-8", newline="\n") as f:
                f.write(line)
        self._write(w)

    def save_frame(self, t: datetime.datetime, img, text: dict, significant_bits=None, folder="frames"):
        """-> (path relative to the day folder, bytes)."""
        rel = pathlib.Path(folder, t.strftime("%Y-%m-%d_%H-%M-%S.") + f"{t.microsecond // 1000:03d}.png")

        def w(root):
            d = root / day_rel(t) / folder
            d.mkdir(parents=True, exist_ok=True)
            return png.save(root / day_rel(t) / rel, img, text, self.png_level, significant_bits)
        return rel.as_posix(), self._write(w)

    def free_gb(self):
        try:
            return shutil.disk_usage(self.primary if self.on_nas else self.spool).free / 1e9
        except OSError:
            return None

    def check(self):
        """Create the root once at start-up, so a wrong path shows up at once."""
        try:
            self.primary.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._fail(e)
            return
        if self.spool.exists() and any(p.is_file() for p in self.spool.rglob("*")):
            self.on_nas, self.next_retry = False, 0.0     # left from an earlier outage: move it now

    # ---- spool -> NAS
    def _flush_spool(self) -> bool:
        if not self.spool.exists():
            try:
                self.primary.mkdir(parents=True, exist_ok=True)
                return True
            except OSError:
                return False
        try:
            self.primary.mkdir(parents=True, exist_ok=True)
            for src in sorted(p for p in self.spool.rglob("*") if p.is_file()):
                dst = self.primary / src.relative_to(self.spool)
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.suffix == ".jsonl":
                    with open(src, "rb") as fi, open(dst, "ab") as fo:
                        shutil.copyfileobj(fi, fo)
                elif src.suffix == ".part":
                    src.unlink()
                    continue
                else:
                    tmp = dst.with_name(dst.name + ".part")
                    shutil.copyfile(src, tmp)
                    os.replace(tmp, dst)
                src.unlink()
            for d in sorted((p for p in self.spool.rglob("*") if p.is_dir()), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
            return True
        except OSError as e:
            log.info("NAS still unavailable: %s", e)
            return False
