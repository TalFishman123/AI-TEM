"""The Logitech BRIO on the phosphor screen, through Windows (WinRT), the way brio_gui.py drives it.

``BrioGrabber`` opens the camera's colour sensor with exclusive control (the infrared sensor is left
alone), applies the controls from the camera settings file, and hands out fresh frames. The settings
file is JSON (see ../camera_settings.example.json)::

    {
      "camera": "BRIO",                 part of the camera's name, as brio_gui.py takes it
      "format": "1920x1080@5",          colour format, as brio_gui.py --colour takes it (or null: camera's current)
      "controls": {                     VideoDeviceController controls, named as in brio_gui.py's sliders
        "exposure": {"auto": false, "value": -6},
        "focus": {"auto": false, "value": 0},
        "white_balance": {"auto": false, "value": 4000},
        "brightness": 128,              a bare number = manual at that value
        "backlight_compensation": 0
      }
    }

Controls not in the file are left as the camera has them. ``dump()`` writes the camera's current
controls in this format (with each one's min, max, step and default alongside), so the workflow is:
set the camera up in brio_gui.py, close it, run ``python -m utem_logger --camera-dump``, done.

The grabber can run inside the logger (same Python environment, pywinrt installed) or as a small
server in another environment (``python -m utem_logger --camera-server``), which the logger talks to
over a local socket with the same one-line JSON protocol as the DM helper. Needs Python 3.9 or newer
for pywinrt.
"""
from __future__ import annotations

import json
import logging
import pathlib
import socket
import threading
import time
import traceback

import numpy as np

log = logging.getLogger("utem_logger")

CONTROLS = ["brightness", "contrast", "hue", "white_balance", "backlight_compensation", "exposure",
            "focus", "zoom", "pan", "tilt", "roll"]


def load_settings(path):
    if not path:
        return {}
    p = pathlib.Path(path)
    if not p.exists():
        log.warning("camera settings file %s not found: camera left as it is", p)
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class BrioGrabber:
    def __init__(self, settings: dict, fresh_timeout_s=3.0):
        self.settings = settings
        self.fresh_timeout_s = fresh_timeout_s
        self.cam = None
        self.stream = None
        self.applied = {}
        self.lock = threading.Lock()

    # ---- open / close
    def open(self):
        from . import brio_camera as bc
        cam = bc.ColourCamera(self.settings.get("camera", "BRIO"))
        try:
            fmt = self.settings.get("format")
            if fmt:
                cam.set_format(fmt)
            cam.start()
            self.applied = apply_controls(cam.controller, self.settings.get("controls") or {})
        except Exception:
            cam.close()
            raise
        self.cam = self.stream = cam
        log.info("BRIO open: %s, format %s, controls %s", cam.name, cam.current_format_label(), self.applied)

    def controller(self):
        return self.cam.controller

    def close(self):
        if self.cam is not None:
            try:
                self.cam.close()
            finally:
                self.cam = self.stream = None

    # ---- frames
    def grab(self):
        """-> (uint8 (H,W,3) RGB, meta). A frame that arrived after this call, so settings and the
        screen are current; the stream's own thread keeps pulling frames meanwhile."""
        with self.lock:
            if self.cam is None:
                self.open()
            s = self.stream
            s.take_frame()                     # drop whatever was waiting
            t0 = time.monotonic()
            while True:
                arr = s.take_frame()
                if arr is not None:
                    break
                if time.monotonic() - t0 > self.fresh_timeout_s:
                    raise RuntimeError("no frame from the BRIO in %.0f s%s" % (
                        self.fresh_timeout_s, (": " + s.error) if s.error else ""))
                time.sleep(0.01)
            meta = {"camera": self.cam.name, "format": s.current_format_label(),
                    "controls": read_controls(self.controller(), values_only=True)}
            return np.ascontiguousarray(arr[:, :, :3]), meta

    def dump(self):
        with self.lock:
            if self.cam is None:
                self.open()
            return {"camera": self.settings.get("camera", "BRIO"),
                    "format": self.stream.current_format_label(),
                    "formats_available": [lab for lab, _ in self.stream.formats()],
                    "controls": read_controls(self.controller())}


# ---- controls, in brio_gui.py's terms
def _out(result):
    return (result[0], result[1]) if isinstance(result, tuple) else (True, result)


def read_controls(vdc, values_only=False):
    out = {}
    for name in CONTROLS:
        try:
            ctrl = getattr(vdc, name)
            caps = ctrl.capabilities
            if not caps.supported:
                continue
            ok, val = _out(ctrl.try_get_value())
            entry = {"value": val if ok else None}
            if caps.auto_mode_supported:
                ok_a, auto = _out(ctrl.try_get_auto())
                entry["auto"] = bool(auto) if ok_a else None
            if not values_only:
                entry.update(min=caps.min, max=caps.max, step=caps.step, default=caps.default)
            out[name] = entry
        except Exception:  # noqa: BLE001 - a control Windows will not talk about
            continue
    return out


def apply_controls(vdc, wanted: dict):
    """Set each control named in `wanted`: {"auto": bool, "value": x} or a bare number (manual).
    Returns what was applied, with "refused" where the camera said no."""
    done = {}
    for name, spec in wanted.items():
        if isinstance(spec, (int, float)):
            spec = {"auto": False, "value": spec}
        try:
            ctrl = getattr(vdc, name)
            caps = ctrl.capabilities
            if not caps.supported:
                done[name] = "unsupported"
                continue
            res = {}
            auto = spec.get("auto")
            if auto is not None and caps.auto_mode_supported:
                res["auto"] = bool(ctrl.try_set_auto(bool(auto)))
            if spec.get("value") is not None and not auto:
                v = float(spec["value"])
                if caps.step and caps.step > 0:
                    v = caps.min + round((v - caps.min) / caps.step) * caps.step
                v = min(max(v, caps.min), caps.max)
                res["value"] = v if ctrl.try_set_value(float(v)) else "refused"
            done[name] = res
        except Exception as e:  # noqa: BLE001
            done[name] = "error: %s" % e
    refused = [k for k, v in done.items() if v in ("unsupported",) or (isinstance(v, dict) and "refused" in v.values())]
    if refused:
        log.warning("camera refused or lacks controls: %s", refused)
    return done


# ---- the server (same protocol as the DM helper: one JSON line in, one JSON line + raw bytes out)
def serve(grabber, host="127.0.0.1", port=47615, idle_release_s=120):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(2)
    srv.settimeout(5.0)
    log.info("camera server listening on %s:%d", host, port)
    last = time.monotonic()
    while True:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            if grabber.cam is not None and time.monotonic() - last > idle_release_s:
                grabber.close()
                log.info("camera released (idle)")
            continue
        last = time.monotonic()
        handle(conn, grabber)


def handle(conn, grabber):
    conn.settimeout(10.0)
    f = conn.makefile("rb")
    try:
        req = json.loads(f.readline().decode("utf-8") or "{}")
        cmd = req.get("cmd")
        if cmd == "grab":
            img, meta = grabber.grab()
            h = {"ok": True, "shape": list(img.shape), "dtype": "uint8", "nbytes": img.nbytes, "meta": meta}
            conn.sendall((json.dumps(h) + "\n").encode("utf-8"))
            conn.sendall(img.tobytes())
        elif cmd == "dump":
            conn.sendall((json.dumps({"ok": True, **grabber.dump()}) + "\n").encode("utf-8"))
        elif cmd == "status":
            conn.sendall((json.dumps({"ok": True, "open": grabber.cam is not None,
                                      "settings": grabber.settings}) + "\n").encode("utf-8"))
        elif cmd == "release":
            grabber.close()
            conn.sendall(b'{"ok": true}\n')
        else:
            conn.sendall((json.dumps({"ok": False, "error": "unknown cmd %r" % cmd}) + "\n").encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log.error("camera server: %s", traceback.format_exc())
        try:
            grabber.close()        # a failed grab usually means the camera is gone: reopen next time
            conn.sendall((json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}) + "\n").encode("utf-8"))
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()


def ask(host, port, cmd, timeout):
    """Client side: -> (header dict, raw bytes)."""
    with socket.create_connection((host, int(port)), timeout=timeout) as c:
        c.sendall((json.dumps({"cmd": cmd}) + "\n").encode("utf-8"))
        f = c.makefile("rb")
        h = json.loads(f.readline().decode("utf-8") or "{}")
        if not h.get("ok"):
            raise RuntimeError("camera server: %s" % h.get("error", "no answer"))
        data = f.read(h["nbytes"]) if "nbytes" in h else b""
        if len(data) != h.get("nbytes", 0):
            raise RuntimeError("camera server: short read")
        return h, data
