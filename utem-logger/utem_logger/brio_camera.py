"""Windows (WinRT) access to the Logitech BRIO's colour sensor: open, choose a format, pull frames,
read and set its controls. The minimum of the lab's brio_camera.py / brio_gui.py (2026-10-22) that
the logger needs: colour only, no infrared, no GUI, no Pillow.

Packages (Python 3.9+): winrt-runtime and the winrt-Windows.* namespaces in ../requirements.txt.
Frames are asked from Windows already converted to BGRA; if this pywinrt cannot request that, raw
YUY2 / NV12 frames are decoded here in numpy. MJPG frames without Windows decoding are not handled:
pick a YUY2 or NV12 format in the settings file then.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time

import numpy as np


class CameraError(RuntimeError):
    pass


class AsyncRunner:
    """An asyncio loop on its own thread, so WinRT async calls read as plain functions."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def run(self, awaitable, timeout=15):
        async def wrap():
            return await awaitable
        fut = asyncio.run_coroutine_threadsafe(wrap(), self.loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise TimeoutError("Windows did not answer within %d s; the camera may be stuck after a failed "
                               "start (often USB bandwidth). Unplug and replug it." % timeout) from None

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


def out_param(result):
    """pywinrt returns (ok, value) for methods with an out parameter."""
    return (result[0], result[1]) if isinstance(result, tuple) else (True, result)


def format_label(fmt):
    vf, fr = fmt.video_format, fmt.frame_rate
    fps = fr.numerator / fr.denominator if fr and fr.denominator else 0
    return "%dx%d @ %.0f fps  %s" % (vf.width, vf.height, fps, fmt.subtype)


# Windows BitmapPixelFormat values
PF_RGBA8, PF_BGRA8, PF_GRAY8, PF_NV12, PF_YUY2 = 30, 87, 62, 103, 107


def _yuv_to_rgb(y, u, v):
    c = (y.astype(np.float32) - 16.0) * 1.164
    d = u.astype(np.float32) - 128.0
    e = v.astype(np.float32) - 128.0
    rgb = np.empty(y.shape + (3,), np.float32)
    rgb[..., 0] = c + 1.596 * e
    rgb[..., 1] = c - 0.392 * d - 0.813 * e
    rgb[..., 2] = c + 2.017 * d
    return np.clip(rgb, 0, 255).astype(np.uint8)


def bitmap_to_rgb(sb):
    """SoftwareBitmap -> uint8 (H, W, 3) RGB."""
    from winrt.windows.storage.streams import Buffer
    fmt, w, h = int(sb.bitmap_pixel_format), sb.pixel_width, sb.pixel_height

    def copy(size):
        buf = Buffer(size)
        sb.copy_to_buffer(buf)
        n = buf.length
        return np.frombuffer(memoryview(buf), dtype=np.uint8, count=n).copy(), n

    if fmt in (PF_BGRA8, PF_RGBA8):
        raw, n = copy(w * h * 4)
        px = raw.reshape(h, n // h)[:, : w * 4].reshape(h, w, 4)
        return px[:, :, 2::-1].copy() if fmt == PF_BGRA8 else px[:, :, :3].copy()
    if fmt == PF_NV12:
        raw, n = copy(w * h * 3 // 2)
        stride = int(n / (h * 1.5))
        y = raw[: stride * h].reshape(h, stride)[:, :w]
        uv = raw[stride * h: stride * h + stride * (h // 2)].reshape(h // 2, stride)[:, :w]
        u = uv[:, 0::2].repeat(2, axis=0).repeat(2, axis=1)[:h, :w]
        v = uv[:, 1::2].repeat(2, axis=0).repeat(2, axis=1)[:h, :w]
        return _yuv_to_rgb(y, u, v)
    if fmt == PF_YUY2:
        raw, n = copy(w * h * 2)
        rows = raw.reshape(h, n // h)[:, : w * 2].reshape(h, w // 2, 4)
        return _yuv_to_rgb(rows[:, :, 0::2].reshape(h, w), rows[:, :, 1].repeat(2, axis=1), rows[:, :, 3].repeat(2, axis=1))
    if fmt == PF_GRAY8:
        raw, n = copy(w * h)
        return np.repeat(raw.reshape(h, n // h)[:, :w, None], 3, axis=2)
    raise CameraError("unsupported pixel format %d (choose a YUY2 or NV12 format in the settings file)" % fmt)


def _bgra8_subtype():
    """The 'BGRA8' subtype string, whichever way this pywinrt exposes it."""
    try:
        from winrt.windows.media.mediaproperties import MediaEncodingSubtypes
        v = MediaEncodingSubtypes.bgra8
        if callable(v):
            v = v()
        if isinstance(v, str) and v:
            return v
    except Exception:  # noqa: BLE001
        pass
    return "BGRA8"


class ColourCamera:
    """The camera's colour sensor, opened with exclusive control so its controls can be set."""

    def __init__(self, name_filter="BRIO"):
        from winrt.windows.media.capture import (MediaCapture, MediaCaptureInitializationSettings,
                                                 MediaCaptureMemoryPreference, MediaCaptureSharingMode,
                                                 StreamingCaptureMode)
        from winrt.windows.media.capture.frames import MediaFrameSourceGroup, MediaFrameSourceKind
        self.async_ = AsyncRunner()
        groups = list(self.run(MediaFrameSourceGroup.find_all_async()))

        def colour_info(g):
            return next((i for i in g.source_infos if i.source_kind == MediaFrameSourceKind.COLOR), None)
        group = next((g for g in groups if name_filter.lower() in g.display_name.lower() and colour_info(g)), None)
        if group is None:
            names = [g.display_name for g in groups]
            raise CameraError("no colour camera matching %r (found %s)" % (name_filter, names))
        self.name = group.display_name
        st = MediaCaptureInitializationSettings()
        st.source_group = group
        st.sharing_mode = MediaCaptureSharingMode.EXCLUSIVE_CONTROL
        st.memory_preference = MediaCaptureMemoryPreference.CPU
        st.streaming_capture_mode = StreamingCaptureMode.VIDEO
        self.capture = MediaCapture()
        try:
            self.run(self.capture.initialize_with_settings_async(st))
        except Exception as e:
            raise CameraError("could not open %s (another program holds it: brio_gui, MATLAB, Teams?): %s"
                              % (self.name, e)) from e
        info = colour_info(group)
        sources = self.capture.frame_sources
        try:
            self.src = sources.lookup(info.id)
        except AttributeError:
            self.src = sources[info.id]
        self.reader = None
        self.running = False
        self.lock = threading.Lock()
        self.latest = None
        self.error = None
        self.frames = 0

    def run(self, awaitable, timeout=15):
        return self.async_.run(awaitable, timeout)

    # ---- formats
    def formats(self):
        seen, out = set(), []
        for f in self.src.supported_formats:
            lab = format_label(f)
            if lab not in seen:
                seen.add(lab)
                out.append((lab, f))
        return out

    def current_format_label(self):
        try:
            return format_label(self.src.current_format)
        except Exception:  # noqa: BLE001
            return "?"

    def set_format(self, wanted):
        """'1920x1080', '1920x1080@5', '1920x1080 @ 5 fps  MJPG': the lowest frame rate that matches."""
        key = wanted.lower().replace(" ", "").replace("fps", "")
        matches = [f for lab, f in self.formats() if key in lab.lower().replace(" ", "").replace("fps", "")]
        if not matches:
            raise CameraError("no colour format matches %r; available: %s" % (wanted, [l for l, _ in self.formats()]))

        def fps(f):
            fr = f.frame_rate
            return fr.numerator / fr.denominator if fr and fr.denominator else 0
        self.run(self.src.set_format_async(min(matches, key=fps)))

    # ---- controls (VideoDeviceController: brightness, exposure, focus, white_balance, ...)
    @property
    def controller(self):
        return self.src.controller.video_device_controller

    # ---- streaming
    def start(self):
        from winrt.windows.media.capture.frames import MediaFrameReaderStartStatus
        try:                               # Windows converts whatever the camera sends to BGRA
            self.reader = self.run(self.capture.create_frame_reader_async(self.src, _bgra8_subtype()))
        except TypeError:                  # this pywinrt lacks that overload: decode YUY2 / NV12 here
            self.reader = self.run(self.capture.create_frame_reader_async(self.src))
        status = self.run(self.reader.start_async())
        if status != MediaFrameReaderStartStatus.SUCCESS:
            raise CameraError("frame reader did not start: %s" % status)
        self.running = True
        threading.Thread(target=self._poll, name="brio-frames", daemon=True).start()

    def _poll(self):
        """Pull the newest frame repeatedly (the FrameArrived event does not always reach Python)."""
        last = None
        while self.running:
            ref = None
            try:
                ref = self.reader.try_acquire_latest_frame() if self.reader is not None else None
                if ref is None:
                    time.sleep(0.005)
                    continue
                t = getattr(ref.system_relative_time, "duration", ref.system_relative_time)
                if t is not None and t == last:
                    time.sleep(0.002)
                    continue
                last = t
                vf = ref.video_media_frame
                sb = vf.software_bitmap if vf is not None else None
                if sb is None:
                    self.error = "frames arrive without a picture (MJPG without Windows decoding?)"
                    continue
                rgb = bitmap_to_rgb(sb)
                with self.lock:
                    self.latest = rgb
                    self.frames += 1
                self.error = None
            except Exception as e:  # noqa: BLE001 - one bad frame must not stop the stream
                self.error = "%s: %s" % (type(e).__name__, e)
                time.sleep(0.05)
            finally:
                if ref is not None:
                    try:
                        ref.close()
                    except Exception:  # noqa: BLE001
                        pass

    def take_frame(self):
        """The newest frame not yet taken, or None."""
        with self.lock:
            arr, self.latest = self.latest, None
        return arr

    def close(self):
        self.running = False
        if self.reader is not None:
            try:
                self.run(self.reader.stop_async())
                self.reader.close()
            except Exception:  # noqa: BLE001
                pass
            self.reader = None
        try:
            self.capture.close()
        except Exception:  # noqa: BLE001
            pass
        self.async_.stop()
