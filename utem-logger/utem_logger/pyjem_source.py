"""Reads the microscope state through PyJEM, read-only (Get* calls only).

A whole poll runs in a worker thread with a timeout, so a hung TEMCenter connection shows up as
"unavailable" instead of freezing the logger. Getters that keep failing are dropped until the
next reconnect, so they cost nothing on every poll.
"""
from __future__ import annotations

import concurrent.futures
import importlib
import logging
import time

log = logging.getLogger("utem_logger")


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
        self.timeout = float(cfg["timeout_s"])
        self.objs = None
        self.fails = {}
        self.dropped = set()
        self.pool = None

    def _connect(self):
        mod = "PyJEM.offline.TEM3" if self.cfg.get("offline") else "PyJEM.TEM3"
        TEM3 = importlib.import_module(mod)
        objs = {}
        for cls in sorted({g.split(".")[0] for g in self.getters}):
            try:
                objs[cls] = getattr(TEM3, cls)()
            except Exception as e:  # noqa: BLE001
                log.warning("PyJEM %s() failed: %s", cls, e)
        if not objs:
            raise Unavailable("no PyJEM class could be created")
        return objs

    def _read(self):
        if self.objs is None:
            self.objs = self._connect()
            self.fails, self.dropped = {}, set()
            log.info("PyJEM connected (%d getters)", len(self.getters))
        state, errors = {}, {}
        for key in self.getters:
            if key in self.dropped:
                continue
            cls, meth = key.split(".", 1)
            try:
                state[key] = plain(getattr(self.objs[cls], meth)())
                self.fails[key] = 0
            except Exception as e:  # noqa: BLE001
                state[key] = None
                errors[key] = f"{type(e).__name__}: {e}"[:200]
                self.fails[key] = self.fails.get(key, 0) + 1
                if self.fails[key] >= self.cfg["disable_after_errors"]:
                    self.dropped.add(key)
                    log.warning("dropping %s until the next reconnect: %s", key, errors[key])
        if state and all(v is None for v in state.values()):
            raise Unavailable("every getter failed: " + next(iter(errors.values()), ""))
        return state, errors

    def read(self):
        """-> (state, errors). Raises Unavailable."""
        if self.pool is None:
            self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pyjem")
        fut = self.pool.submit(self._read)
        try:
            return fut.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            # the worker is stuck inside PyJEM: leave it behind and start clean next time
            self.pool.shutdown(wait=False)
            self.pool, self.objs = None, None
            raise Unavailable(f"poll took longer than {self.timeout:.0f} s")
        except Unavailable:
            self.objs = None
            raise
        except Exception as e:  # noqa: BLE001 - import errors, COM errors, ...
            self.objs = None
            raise Unavailable(f"{type(e).__name__}: {e}")

    def close(self):
        if self.pool is not None:
            self.pool.shutdown(wait=False)


def timed_read(source):
    t = time.perf_counter()
    state, errors = source.read()
    return state, errors, (time.perf_counter() - t) * 1e3
