"""The loop: poll the state every configured interval, record what changed, and grab a frame on every change:
from the screen camera while the beam is on, not blanked and the screen is down, from the Orius live
View in DM while the beam is not blanked, the screen is up and DM's View is running (config "frames").
Also one frame when a source's conditions start to hold, and one when a View becomes live.

Per poll (default 30 s): one PyJEM read (26 fast getters plus up to 5 rotating getters), a comparison, and nothing else unless
something changed. A frame costs one camera exposure and one PNG compression (~0.15 s of one core for a
full 1440 x 1080 16-bit colour frame at png_level 1). Changes between observations make one record and
one frame.

Record types in state.jsonl (every line has "t", local time with UTC offset, and "event"):

    start        logger started: full "state", "config" (what is logged and how), "version"
    change       "changed": {key: [old, new]} and the full "state" after the change
    heartbeat    full "state" every heartbeat_s without a change, plus "poll_ms", "frames_today", "bytes_today"
    frame        "source" (screen / dm_view), "file" (relative to the day folder), "state_t" (the line
                 whose state it shows), camera "meta"
    status       "what": pyjem_unavailable / pyjem_available / camera_error / nas_unavailable / ... with details
    stop         logger stopped
"""
from __future__ import annotations

import datetime
import json
import logging
import signal
import statistics
import time

from . import __version__
from .camera import BrioCamera, DMViewCamera, MockCamera, NotLive, ThorlabsCamera
from .pyjem_source import Unavailable, timed_read
from .store import Store

log = logging.getLogger("utem_logger")


def now():
    return datetime.datetime.now().astimezone()


def iso(t):
    return t.isoformat(timespec="milliseconds")


def differs(a, b, tol):
    if a is None or b is None:       # unknown this poll: not a change
        return False
    if tol and isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return any(differs(x, y, tol) for x, y in zip(a, b))
    if tol and isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(tol, dict):    # relative with a floor: {"rel": 0.05, "min": 1.0}
            limit = max(tol.get("rel", 0.0) * max(abs(a), abs(b)), tol.get("min", 0.0))
            return abs(a - b) > limit
        return abs(a - b) > tol
    return a != b


class FrameSource:
    """A camera plus the bookkeeping of when it owes a frame."""

    def __init__(self, name, camera, cfg, folder, when):
        self.name, self.camera, self.cfg, self.folder, self.when = name, camera, cfg, folder, when
        self.pending = False
        self.last_frame = -1e9
        self.next_try = 0.0
        self.failed = False
        self.live = None            # DM view only: was a View live at the last probe
        self.next_probe = 0.0


class Logger:
    def __init__(self, cfg, source, camera=None, store=None):
        self.cfg = cfg
        self.source = source
        self.sources = []
        self.camera = camera
        self.dm_camera = None
        self.store = store or Store(cfg["log_root"], cfg["subdir"], cfg["spool_dir"],
                                    png_level=cfg["camera"]["png_level"])
        pj = cfg["pyjem"]
        self.readings = set(pj["readings"])
        self.tol = pj["tolerance"]
        self.derived = [(k, v[:v.index("[")], int(v[v.index("[") + 1:-1])) for k, v in pj.get("derived", {}).items()]
        self.warned = set()
        self.state = None            # last recorded state
        self.state_t = None          # time of the line that recorded it
        self.available = None
        self.next_pyjem_try = 0.0
        self.last_heartbeat = time.monotonic()
        self.poll_ms = []
        self.day = None
        self.frames_today = self.bytes_today = 0
        self.free_gb, self.next_disk_check = None, 0.0
        self.running = True
        self.n_polls = 0

    def _set_source(self, name, camera, cfg, folder, when):
        self.sources = [x for x in self.sources if x.name != name]
        if camera is not None:
            self.sources.append(FrameSource(name, camera, cfg, folder, when))

    @property
    def camera(self):
        return next((x.camera for x in self.sources if x.name == "screen"), None)

    @camera.setter
    def camera(self, cam):
        self._set_source("screen", cam, self.cfg["camera"], "frames", lambda s: self.frame_ok("screen", s))

    @property
    def dm_camera(self):
        return next((x.camera for x in self.sources if x.name == "dm_view"), None)

    @dm_camera.setter
    def dm_camera(self, cam):
        self._set_source("dm_view", cam, self.cfg["dm_view"], "frames_dm", lambda s: self.frame_ok("dm_view", s))

    # ---- records
    def record(self, event, t=None, **fields):
        t = t or now()
        rec = {"t": iso(t), "event": event, **fields}
        try:
            self.store.append(t, rec)
        except Exception as e:  # noqa: BLE001 - neither NAS nor local disk: keep polling
            log.error("could not write %s record: %s", event, e)
        pending, self.store.events = self.store.events, []
        for what, info in pending:
            self.record("status", what=what, **info)
        return t

    def status(self, what, **info):
        log.info("status %s %s", what, info or "")
        self.record("status", what=what, **info)

    # ---- the gun
    def thermionic(self, s):
        th = self.cfg["thermionic"]
        return s.get(th["beam_switch"]) == 1

    def frame_ok(self, name, s):
        """Every condition of cfg["frames"][name] holds; a getter without a value does not block."""
        for key, allowed in self.cfg["frames"].get(name, {}).items():
            if allowed is None or key.startswith("_"):     # null in config.json drops a default condition
                continue
            v = s.get(key)
            if v is None:
                if key not in self.warned:
                    self.warned.add(key)
                    log.warning("%s frames: condition on %s ignored, this getter has no value", name, key)
                continue
            if v not in (allowed if isinstance(allowed, list) else [allowed]):
                return False
        return True

    def derive(self, s):
        """Add the derived keys (elements of tuple readings tracked as settings)."""
        for key, getter, i in self.derived:
            v = s.get(getter)
            s[key] = v[i] if isinstance(v, list) and len(v) > i else None
        return s

    # ---- one poll
    def poll(self):
        mono = time.monotonic()
        if not self.available and mono < self.next_pyjem_try:
            return
        try:
            s, errors, ms = timed_read(self.source)
        except Unavailable as e:
            if self.available is not False:
                self.status("pyjem_unavailable", error=str(e)[:300], retry_s=self.cfg["pyjem"]["retry_s"])
            self.available = False
            self.next_pyjem_try = time.monotonic() + self.cfg["pyjem"]["retry_s"]
            return
        t = now()
        mono = time.monotonic()  # acquisition may have taken seconds; use its completion time
        self.n_polls += 1
        self.poll_ms.append(ms)
        s = self.derive(s)
        if self.available is False:
            self.status("pyjem_available")
        self.available = True
        if errors:
            log.debug("getter errors: %s", errors)

        if self.state is None:
            self.state, self.state_t = s, t
            self.record("start", t, state=s, version=__version__, thermionic=self.thermionic(s),
                        config=self.public_config())
            self.mark_pending()
        else:
            changed = {k: [self.state.get(k), v] for k, v in s.items()
                       if k not in self.readings and differs(self.state.get(k), v, self.tol.get(k))}
            # keep the last known value of a getter that failed this poll
            merged = {**self.state, **{k: v for k, v in s.items() if v is not None}}
            if changed:
                self.state, self.state_t = merged, t
                self.record("change", t, changed=changed, state=merged, thermionic=self.thermionic(merged))
                self.mark_pending()
                self.last_heartbeat = mono
            else:
                self.state = merged      # newest readings, for the heartbeat
        self.after_poll(t, mono)

    def mark_pending(self):
        for x in self.sources:
            x.pending = True

    def after_poll(self, t, mono):
        for x in self.sources:
            if not x.when(self.state):
                x.pending = False
                x.live = None
                x.camera.release_if_idle()
                continue
            if x.name == "dm_view" and not x.pending and mono >= x.next_probe and mono >= x.next_try:
                # no change: is a View live now? the first live probe after a gap owes one frame
                x.next_probe = mono + x.cfg["probe_s"]
                live = x.camera.live()
                if live and x.live is False:
                    x.pending = True
                x.live = live
            if x.pending and mono - x.last_frame >= x.cfg["min_interval_s"] and mono >= x.next_try:
                self.take_frame(x, t, mono)
            else:
                x.camera.release_if_idle()
        if mono - self.last_heartbeat >= self.cfg["heartbeat_s"]:
            self.heartbeat(t, mono)

    def take_frame(self, x, t, mono):
        if mono >= self.next_disk_check:
            self.free_gb, self.next_disk_check = self.store.free_gb(), mono + 300
            if self.free_gb is not None and self.free_gb < self.cfg["min_free_gb"]:
                self.status("frames_paused_low_disk", free_gb=round(self.free_gb, 1))
        if self.free_gb is not None and self.free_gb < self.cfg["min_free_gb"]:
            x.pending = False
            return
        try:
            img, meta, sig = x.camera.grab()
            ft = now()
            text = {"Software": f"utem_logger {__version__}", "utem_time": iso(ft), "utem_source": x.name,
                    "utem_camera": json.dumps(meta), "utem_state": json.dumps(self.state)}
            rel, size = self.store.save_frame(ft, img, text, sig, x.folder)
        except NotLive as e:               # DM has no live View: nothing to log
            log.debug("dm view: %s", e)
            x.pending, x.live = False, False
            return
        except Exception as e:  # noqa: BLE001 - camera busy / unplugged / SDK or DM helper missing
            if not x.failed:               # once per outage, not once per change
                self.status("camera_error", source=x.name, error=f"{type(e).__name__}: {e}"[:300],
                            retry_s=x.cfg["retry_s"])
            x.failed = True
            x.camera.reset()
            x.next_try = mono + x.cfg["retry_s"]
            return
        if x.failed:
            self.status("camera_ok", source=x.name)
            x.failed = False
        self.roll_day(ft)
        self.frames_today += 1
        self.bytes_today += size
        self.record("frame", ft, source=x.name, file=rel, bytes=size, state_t=iso(self.state_t), meta=meta)
        x.pending, x.last_frame, x.live = False, mono, True

    def heartbeat(self, t, mono):
        self.roll_day(t)
        ms = self.poll_ms[-300:]
        self.record("heartbeat", t, state=self.state, thermionic=self.thermionic(self.state),
                    poll_ms={"median": round(statistics.median(ms), 1), "max": round(max(ms), 1)} if ms else None,
                    frames_today=self.frames_today, bytes_today=self.bytes_today,
                    on_nas=self.store.on_nas)
        self.last_heartbeat = mono

    def roll_day(self, t):
        if self.day != t.date():
            self.day, self.frames_today, self.bytes_today = t.date(), 0, 0

    def public_config(self):
        c = {k: v for k, v in self.cfg.items() if k not in ("pyjem",)}
        c["pyjem"] = {k: v for k, v in self.cfg["pyjem"].items() if k != "getters"}
        c["source"] = getattr(self.source, "name", type(self.source).__name__)
        return c

    # ---- run
    def run(self, duration_s=None):
        self.store.check()
        self.roll_day(now())
        t_end = None if duration_s is None else time.monotonic() + duration_s
        period = float(self.cfg["poll_interval_s"])
        nxt = time.monotonic()
        log.info("logging to %s every %.1f s", self.store.primary, period)
        try:
            while self.running and (t_end is None or time.monotonic() < t_end):
                try:
                    self.poll()
                except Exception:  # noqa: BLE001 - never die on one bad poll
                    log.exception("poll failed")
                nxt += period
                delay = nxt - time.monotonic()
                if delay < 0:              # fell behind (slow poll): skip, don't burst
                    nxt, delay = time.monotonic(), 0
                time.sleep(delay)
        finally:
            for x in self.sources:
                x.camera.release_if_idle(force=True)
            self.record("stop")
            self.source.close()
            log.info("stopped")

    def stop(self, *_):
        self.running = False


def make_camera(cfg, state_fn=None):
    cam = cfg["camera"]
    if not cam["enabled"]:
        return None
    if cam["kind"] == "mock":
        return MockCamera(cam, state_fn)
    if cam["kind"] == "thorlabs":
        return ThorlabsCamera(cam)
    return BrioCamera(cam)


def make_dm_camera(cfg):
    return DMViewCamera(cfg["dm_view"]) if cfg["dm_view"]["enabled"] else None


def install_signals(logger):
    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), logger.stop)
