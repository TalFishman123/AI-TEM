"""Reads the microscope state through PyJEM, read-only (Get* calls only).

A whole poll runs in a worker thread with a timeout, so a hung TEMCenter connection shows up as
"unavailable" instead of freezing the logger. Getters that keep failing are dropped until the
next reconnect, so they cost nothing on every poll.
"""
from __future__ import annotations

import concurrent.futures
import importlib
import logging
import threading
import time

log = logging.getLogger("utem_logger")


def split_key(key):
    """'Class.Method' or 'Class.Method(1, 2)' -> (cls, method, args)."""
    cls, meth = key.split(".", 1)
    args = ()
    if meth.endswith(")") and "(" in meth:
        meth, inner = meth[:-1].split("(", 1)
        args = tuple(int(a) for a in inner.split(",") if a.strip())
    return cls, meth, args


def plain(v):
    """PyJEM values (tuples, numpy scalars) -> JSON-ready python."""
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    if hasattr(v, "tolist"):
        return v.tolist()
    if isinstance(v, (bool, int, float, str)) or v is None:
        return v
    return repr(v)


class Unavailable(Exception):
    pass


class PyJEMSource:
    name = "pyjem"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.getters = list(cfg["getters"])
        fast = [g for g in cfg.get("fast") or [] if g in self.getters]
        self.fast = fast
        self.slow = [g for g in self.getters if g not in set(fast)] if fast else []
        self.slow_per_poll = int(cfg.get("slow_per_poll", 0) or 0)
        self.slow_i = 0
        self.timeout = float(cfg["timeout_s"])
        self.slow_poll = float(cfg.get("slow_poll_s", 10))
        self.last_slow_warning = 0.0
        self.objs = None
        self.fails = {}
        self.dropped = set()
        self.pool = None
        self.future = None
        self.future_full = False
        self.future_started = 0.0
        self.future_limit = 0.0
        self.timed_out = False
        self.closed = threading.Event()
        self.current_getter = None
        self.getter_ms = {}          # time of each getter in the last read, for --once

    def _connect(self):
        mod = "PyJEM.offline.TEM3" if self.cfg.get("offline") else "PyJEM.TEM3"
        TEM3 = importlib.import_module(mod)
        objs = {}
        for cls in sorted({split_key(g)[0] for g in self.getters}):
            name = cls if hasattr(TEM3, cls) else next((n for n in dir(TEM3) if n.lower() == cls.lower()), cls)
            try:
                objs[cls] = getattr(TEM3, name)()
            except Exception as e:  # noqa: BLE001
                log.warning("PyJEM %s() failed: %s", cls, e)
        if not objs:
            raise Unavailable("no PyJEM class could be created")
        return objs

    def poll_size(self):
        """How many getters an ordinary poll reads."""
        if not self.fast:
            return len(self.getters)
        return len(self.fast) + min(self.slow_per_poll, len(self.slow))

    def read_timeout(self, full):
        """timeout_s covers an ordinary poll; a full read (every getter, right after connecting) gets
        timeout_s for the connection plus timeout_s per ordinary poll's worth of getters."""
        if not full:
            return self.timeout
        return self.timeout * (1 + len(self.getters) / max(1, self.poll_size()))

    def _keys(self, full):
        """Every getter on a full read; otherwise the fast ones plus the next few slow ones in rotation."""
        if full or not self.fast:
            return list(self.getters)
        n = min(self.slow_per_poll, len(self.slow))
        picked = [self.slow[(self.slow_i + j) % len(self.slow)] for j in range(n)]
        self.slow_i = (self.slow_i + n) % len(self.slow) if self.slow else 0
        return self.fast + picked

    def _read(self):
        if self.closed.is_set():
            raise Unavailable("source is closed")
        full = self.objs is None
        if full:
            self.objs = self._connect()
            self.fails, self.dropped = {}, set()
            log.info("PyJEM connected (%d getters, %d every poll + %d of the other %d)",
                     len(self.getters), len(self.fast or self.getters), self.slow_per_poll if self.fast else 0,
                     len(self.slow))
        state, errors = {}, {}
        self.getter_ms = {}
        for key in self._keys(full):
            if self.closed.is_set():
                raise Unavailable("source is closed")
            if key in self.dropped:
                continue
            cls, meth, args = split_key(key)
            self.current_getter = key
            t0 = time.perf_counter()
            try:
                state[key] = plain(getattr(self.objs[cls], meth)(*args))
                self.fails[key] = 0
            except Exception as e:  # noqa: BLE001
                state[key] = None
                errors[key] = f"{type(e).__name__}: {e}"[:200]
                self.fails[key] = self.fails.get(key, 0) + 1
                if self.fails[key] == 1:
                    log.warning("getter failed: %s: %s", key, errors[key])
                if self.fails[key] >= self.cfg["disable_after_errors"]:
                    self.dropped.add(key)
                    log.warning("dropping %s until the next reconnect: %s", key, errors[key])
            self.getter_ms[key] = (time.perf_counter() - t0) * 1e3
            self.current_getter = None
        if state and all(v is None for v in state.values()):
            raise Unavailable("every getter failed: " + next(iter(errors.values()), ""))
        return state, errors

    def read(self):
        """One outstanding read at most, including after a timeout.

        A running native call cannot be cancelled by ThreadPoolExecutor. Keep its future and
        connection until it finishes; never submit another read behind it or replace its pool.
        Discard a late result so old state is not timestamped as a fresh observation.
        """
        if self.closed.is_set():
            raise Unavailable("source is closed")
        if self.future is not None and not self.future.done():
            raise Unavailable("previous PyJEM read is still running; no new connection or read started"
                              " (getter: %s)" % (self.current_getter or "connecting"))
        if self.pool is None:
            self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pyjem")
        if self.future is None:
            self.future_full = self.objs is None
            self.future_limit = self.read_timeout(self.future_full)
            self.future_started = time.perf_counter()
            self.timed_out = False
            log.debug("PyJEM %s started (timeout %.1f s)",
                      "full read" if self.future_full else "poll", self.future_limit)
            self.future = self.pool.submit(self._read)
        fut = self.future
        full, limit = self.future_full, self.future_limit
        try:
            result = fut.result(timeout=limit)
            took = time.perf_counter() - self.future_started
            self.future = None
            if self.timed_out:
                log.warning("previous PyJEM read finished; late result discarded; connection retained")
                raise Unavailable("late PyJEM result discarded; next attempt will read fresh state")
            if took > self.slow_poll and time.monotonic() - self.last_slow_warning > 60:
                self.last_slow_warning = time.monotonic()
                slowest = sorted(self.getter_ms.items(), key=lambda kv: -kv[1])[:5]
                log.warning("slow %s: %.1f s (limit %.0f s); slowest getters: %s",
                            "full read" if full else "poll", took, limit,
                            ", ".join("%s %.0f ms" % (k, v) for k, v in slowest))
            return result
        except concurrent.futures.TimeoutError:
            if fut.done():  # TimeoutError raised by the getter/worker itself, not our wait
                self.future, self.objs = None, None
                raise Unavailable("PyJEM worker raised TimeoutError")
            self.timed_out = True
            log.warning("PyJEM read exceeded %.1f s at %s; keeping worker; no reconnect",
                        limit, self.current_getter or "connecting")
            raise Unavailable(f"{'connecting and reading everything' if full else 'poll'} took longer than {limit:.0f} s; waiting for existing worker")
        except Unavailable:
            if self.future is not None and fut.done():
                self.future, self.objs = None, None
            raise
        except Exception as e:  # noqa: BLE001 - import errors, COM errors, ...
            self.future, self.objs = None, None
            raise Unavailable(f"{type(e).__name__}: {e}")

    def close(self):
        self.closed.set()
        if self.pool is not None:
            if self.future is not None:
                self.future.cancel()
                if not self.future.done():
                    log.warning("PyJEM call still running at shutdown; Python cannot interrupt a native call")
            self.pool.shutdown(wait=False)


def timed_read(source):
    t = time.perf_counter()
    state, errors = source.read()
    return state, errors, (time.perf_counter() - t) * 1e3
