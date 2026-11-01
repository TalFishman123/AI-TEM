"""The cameras: the Logitech BRIO webcam on the phosphor screen (default, see brio.py), a Thorlabs CS165CU (1440 x 1080, 10-bit colour CMOS) on the phosphor screen.

Driven by the Thorlabs Scientific Camera SDK (``thorlabs_tsi_sdk``, from the ThorCam install:
Scientific Camera Interfaces/SDK/Python Toolkit). The camera is opened on the first frame, kept armed
in software-trigger mode while frames keep coming, and released after ``release_after_s`` without a
frame so ThorCam or an alignment script can use it. Only one program can hold the camera at a time:
when it is busy the logger skips frames and tries again later.

``grab()`` returns the image to save (by ``format``) and a dict of what the frame was taken with.

``DMViewCamera`` is the Gatan Orius as DigitalMicrograph shows it: it asks the helper running inside
DM's Python (``dm_helper/utem_view_server.py``) for a copy of the live View image. Nothing is acquired,
so the operator's view is never interrupted; when no View window is open there is no frame (``NotLive``).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time

import numpy as np

log = logging.getLogger("utem_logger")


def bin_mean(img: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return img
    h, w = img.shape[:2]
    h, w = h - h % k, w - w % k
    a = img[:h, :w].astype(np.float32)
    a = a.reshape(h // k, k, w // k, k, *a.shape[2:]).mean(axis=(1, 3))
    return np.round(a).astype(img.dtype)


class Camera:
    """Shared logic: exposure control, conversion to the saved format, open/release."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.exposure_ms = float(cfg["exposure_ms"])
        self.is_open = False
        self.last_used = 0.0

    # subclasses: _open(), _close(), _raw(exposure_ms) -> uint16 (H,W) sensor counts,
    # _rgb48(raw) -> uint16 (H,W,3) colour in sensor counts (0 .. 2**bit_depth - 1), bit_depth, info()
    def grab(self):
        if not self.is_open:
            self._open()
            self.is_open = True
            log.info("camera opened: %s", self.info())
        exposure = self.exposure_ms
        raw = self._raw(exposure)
        self.last_used = time.monotonic()
        full = (1 << self.bit_depth) - 1
        sat = float(np.mean(raw >= 0.98 * full))
        p999 = float(np.percentile(raw[::4, ::4], 99.9))
        meta = {"exposure_ms": round(exposure, 4), "gain": self.cfg["gain"], "bit_depth": self.bit_depth,
                "sensor_shape": list(raw.shape), "raw_mean": round(float(raw.mean()), 2),
                "raw_p999": round(p999, 1), "saturated_fraction": round(sat, 6), **self.info()}
        if self.cfg["auto_exposure"]:
            self._adjust(sat, p999, full)
        img, sig = self.convert(raw)
        meta.update(format=self.cfg["format"], shape=list(img.shape), significant_bits=sig)
        return img, meta, sig

    def convert(self, raw):
        """-> (image to save, significant bits for the PNG sBIT chunk)."""
        fmt, b = self.cfg["format"], self.bit_depth
        # 16-bit formats store the sensor counts as they are (0 .. 2**b - 1): ~22 % smaller files than
        # values shifted up to fill 16 bits; the sBIT chunk says b bits are significant
        if fmt == "raw16":
            return raw.astype(np.uint16), b
        rgb = self._rgb48(raw)
        if fmt == "rgb16":
            img, sig = rgb.astype(np.uint16), b
        elif fmt == "rgb8":
            img, sig = np.right_shift(rgb, b - 8).astype(np.uint8), None
        elif fmt == "grey8":
            img = np.round(rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32))
            img, sig = np.clip(img / (1 << (b - 8)), 0, 255).astype(np.uint8), None
        else:
            raise ValueError(f"unknown camera format {fmt!r}")
        return bin_mean(img, int(self.cfg["downscale"])), sig

    def _adjust(self, sat, p999, full):
        """Next frame's exposure: halve when over 0.1 % of pixels saturate, raise when the brightest
        pixels sit under a quarter of full scale. Recorded with every frame."""
        e = self.exposure_ms
        if sat > 1e-3:
            e *= 0.5
        elif p999 < 0.25 * full:
            e *= min(4.0, 0.6 * full / max(p999, 1.0))
        self.exposure_ms = float(np.clip(e, self.cfg["exposure_min_ms"], self.cfg["exposure_max_ms"]))

    def release_if_idle(self, force=False):
        if self.is_open and (force or time.monotonic() - self.last_used > self.cfg["release_after_s"]):
            try:
                self._close()
            except Exception as e:  # noqa: BLE001
                log.warning("camera close: %s", e)
            self.is_open = False
            log.info("camera released")

    def reset(self):
        """After an error: drop the handle so the next grab reopens."""
        self.release_if_idle(force=True)


class ThorlabsCamera(Camera):
    _sdk = None          # one TLCameraSDK per process, kept for its lifetime
    _color_sdk = None

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cam = self.proc = None
        self.bit_depth = 10
        self.serial = "?"

    def _open(self):
        dll_dir = self.cfg.get("dll_dir")
        if dll_dir:
            os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(dll_dir)
        from thorlabs_tsi_sdk.tl_camera import TLCameraSDK
        from thorlabs_tsi_sdk.tl_camera_enums import SENSOR_TYPE
        if ThorlabsCamera._sdk is None:
            ThorlabsCamera._sdk = TLCameraSDK()
        found = ThorlabsCamera._sdk.discover_available_cameras()
        if not found:
            raise RuntimeError("no Thorlabs camera found")
        serial = self.cfg.get("serial")
        if serial and str(serial) not in found:
            raise RuntimeError(f"camera {serial} not found (found {found})")
        self.serial = str(serial or found[0])
        cam = ThorlabsCamera._sdk.open_camera(self.serial)
        try:
            cam.frames_per_trigger_zero_for_unlimited = 1
            cam.image_poll_timeout_ms = 500
            cam.exposure_time_us = int(self.exposure_ms * 1000)
            try:
                cam.gain = int(self.cfg["gain"])
            except Exception as e:  # noqa: BLE001 - some models have no gain control
                log.info("camera gain not set: %s", e)
            self.bit_depth = int(cam.bit_depth)
            self.proc = None
            if cam.camera_sensor_type == SENSOR_TYPE.BAYER:
                from thorlabs_tsi_sdk.tl_mono_to_color_processor import MonoToColorProcessorSDK
                if ThorlabsCamera._color_sdk is None:
                    ThorlabsCamera._color_sdk = MonoToColorProcessorSDK()
                self.proc = ThorlabsCamera._color_sdk.create_mono_to_color_processor(
                    SENSOR_TYPE.BAYER, cam.color_filter_array_phase, cam.get_color_correction_matrix(),
                    cam.get_default_white_balance_matrix(), cam.bit_depth)
            cam.arm(2)
        except Exception:
            cam.dispose()
            raise
        self.cam = cam

    def _raw(self, exposure_ms):
        cam = self.cam
        cam.exposure_time_us = int(round(exposure_ms * 1000))
        cam.image_poll_timeout_ms = int(exposure_ms + 2000)
        cam.issue_software_trigger()
        frame = cam.get_pending_frame_or_null()
        if frame is None:
            raise RuntimeError("camera returned no frame")
        h, w = cam.image_height_pixels, cam.image_width_pixels
        return np.array(frame.image_buffer, dtype=np.uint16).reshape(h, w)

    def _rgb48(self, raw):
        h, w = raw.shape
        if self.proc is None:  # monochrome sensor
            return np.repeat(raw[:, :, None], 3, axis=2)
        # the SDK's colour processing (demosaic, colour correction, white balance), 16 bits per
        # channel in the camera's bit depth. Confirm once on the PC: meta "raw_p999" vs the PNG.
        return np.asarray(self.proc.transform_to_48(raw, w, h), dtype=np.uint16).reshape(h, w, 3)

    def _close(self):
        try:
            self.cam.disarm()
        finally:
            self.cam.dispose()
            if self.proc is not None:
                self.proc.dispose()
            self.cam = self.proc = None

    def info(self):
        return {"camera": "Thorlabs", "serial": self.serial}


class MockCamera(Camera):
    """A phosphor screen without a microscope: a green disc that moves with beam shift (``state``
    is set by the mock microscope), on a dark, noisy background. 10-bit, 1440 x 1080."""

    def __init__(self, cfg, state_fn=None):
        super().__init__(cfg)
        self.bit_depth = 10
        self.state_fn = state_fn or (lambda: {})
        self.rng = np.random.default_rng(1)
        self.fail_next = 0

    def _open(self):
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("mock camera busy")

    def _close(self):
        pass

    def _raw(self, exposure_ms):
        h, w = 1080, 1440
        s = self.state_fn()
        bx, by = s.get("Def3.GetCLA1") or [32768, 32768]
        cx, cy = w / 2 + (bx - 32768) / 20.0, h / 2 + (by - 32768) / 20.0
        r = 150 + abs((s.get("Lens3.GetCL3") or 48146) - 48146) / 10.0
        yy, xx = np.ogrid[:h, :w]
        disc = ((xx - cx) ** 2 + (yy - cy) ** 2 < r * r) * (600.0 * exposure_ms / 20.0)
        img = 12 + disc + self.rng.normal(0, 3, (h, w))
        return np.clip(img, 0, 1023).astype(np.uint16)

    def _rgb48(self, raw):
        return np.stack([raw // 3, raw, raw // 4], axis=2).astype(np.uint16)   # phosphor green

    def info(self):
        return {"camera": "mock"}


class NotLive(Exception):
    """The DM helper answered, but no live View image is there to copy."""


class DMViewCamera:
    """The Orius live View, copied from DigitalMicrograph through the helper's local socket."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.is_open = False

    def _ask(self, cmd, timeout):
        with socket.create_connection((self.cfg["host"], int(self.cfg["port"])), timeout=timeout) as c:
            c.sendall((json.dumps({"cmd": cmd}) + "\n").encode("utf-8"))
            f = c.makefile("rb")
            h = json.loads(f.readline().decode("utf-8") or "{}")
            if not h.get("ok"):
                raise RuntimeError("DM helper: %s" % h.get("error", "no answer"))
            data = f.read(h["nbytes"]) if "nbytes" in h else b""
            if len(data) != h.get("nbytes", 0):
                raise RuntimeError("DM helper: short read (%d of %d bytes)" % (len(data), h["nbytes"]))
            return h, data

    def live(self):
        """Is a View window open in DM right now? False too when DM or the helper is not there."""
        try:
            return bool(self._ask("probe", self.cfg["timeout_s"])[0].get("live"))
        except OSError:
            return False

    def grab(self):
        h, data = self._ask("grab", self.cfg["timeout_s"])
        if not h.get("live"):
            raise NotLive(h.get("note") or "no View window named %r" % h.get("name"))
        arr = np.frombuffer(data, dtype=np.dtype(h["dtype"]).newbyteorder("<")).reshape(h["shape"])
        meta = {"camera": "DM view", "dm_image": h.get("name"), "dm_dtype": h["dtype"],
                "same_as_last": h.get("same_as_last"),      # sampled statistics match the previous grab; not proof of freshness
                "sensor_shape": list(arr.shape), "raw_mean": round(float(arr.mean()), 2),
                "raw_max": round(float(arr.max()), 1), **{"dm_" + k: v for k, v in h.get("tags", {}).items()}}
        # DM view images are usually float32 after gain normalisation; the counts fit 16 bits
        img = np.clip(np.round(arr.astype(np.float64)), 0, 65535).astype(np.uint16)
        img = bin_mean(img, int(self.cfg.get("downscale", 1)))
        meta.update(format="grey16", shape=list(img.shape), significant_bits=16)
        return img, meta, None

    def release_if_idle(self, force=False):
        pass

    def reset(self):
        pass


class BrioCamera(Camera):
    """The Logitech BRIO through Windows (brio.py), in this process when pywinrt is importable here,
    otherwise through the camera server (``python -m utem_logger --camera-server`` in an environment
    that has it). 8-bit RGB frames; saved as rgb8 or grey8 PNG."""

    def __init__(self, cfg: dict, grabber=None):
        super().__init__(cfg)
        self.bit_depth = 8
        self.b = cfg["brio"]
        self.grabber = grabber          # in-process BrioGrabber, or None for the server
        self.mode = None

    def _open(self):
        from . import brio
        if self.grabber is None and self.b.get("in_process", "auto") != "never":
            try:
                import winrt.windows.media.capture  # noqa: F401
                self.grabber = brio.BrioGrabber(brio.load_settings(self.b.get("settings_file")))
            except ImportError:
                if self.b.get("in_process") == "always":
                    raise
        self.mode = "in-process" if self.grabber is not None else "server %s:%s" % (self.b["host"], self.b["port"])
        if self.grabber is not None:
            self.grabber.open()

    def _close(self):
        if self.grabber is not None:
            self.grabber.close()
        else:
            try:
                from . import brio
                brio.ask(self.b["host"], self.b["port"], "release", self.b["timeout_s"])
            except OSError:
                pass

    def grab(self):
        if not self.is_open:
            self._open()
            self.is_open = True
            log.info("camera opened: %s", self.info())
        if self.grabber is not None:
            rgb, meta = self.grabber.grab()
        else:
            from . import brio
            h, data = brio.ask(self.b["host"], self.b["port"], "grab", self.b["timeout_s"])
            rgb = np.frombuffer(data, np.uint8).reshape(h["shape"])
            meta = h["meta"]
        self.last_used = time.monotonic()
        fmt = self.cfg["format"]
        if fmt in ("grey8", "grey16"):
            img = np.round(rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)).astype(np.uint8)
            fmt = "grey8"
        else:
            img, fmt = rgb, "rgb8"           # the webcam is 8-bit: rgb16 would only pad
        img = bin_mean(img, int(self.cfg["downscale"]))
        g = rgb.astype(np.float32).mean(axis=2)
        meta = {**meta, **self.info(), "bit_depth": 8, "sensor_shape": list(rgb.shape[:2]),
                "raw_mean": round(float(g.mean()), 2), "saturated_fraction": round(float(np.mean(g >= 250)), 6),
                "format": fmt, "shape": list(img.shape), "significant_bits": None}
        return img, meta, None

    def info(self):
        return {"via": self.mode}          # the camera's own name comes with the frame's meta
