"""Off-instrument tests: a scripted microscope and the mock camera. Run from utem-logger/:

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from utem_logger import config, png                       # noqa: E402
from utem_logger.camera import MockCamera                 # noqa: E402
from utem_logger.logger import Logger                     # noqa: E402
from utem_logger.mock import PROBE                        # noqa: E402
from utem_logger.pyjem_source import Unavailable          # noqa: E402


def decode_png(path):
    data = pathlib.Path(path).read_bytes()
    i = data.find(b"sBIT")
    return png.load(path), (data[i + 4] if i > 0 else None)


class Scripted:
    """Returns the states it is given, one per poll; an Exception item makes that poll fail."""
    name = "scripted"

    def __init__(self, states):
        self.states = list(states)
        self.last = None

    def read(self):
        s = self.states.pop(0) if self.states else self.last
        if isinstance(s, Exception):
            raise s
        self.last = s
        return json.loads(json.dumps(s)), {}

    def close(self):
        pass


def state(**kw):
    s = json.loads(json.dumps(PROBE))
    for k, v in kw.items():
        s[k.replace("__", ".")] = v
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.cfg = config.load(None, log_root=str(d / "nas"), spool_dir=str(d / "spool"),
                               service_log_dir=str(d / "local"))
        self.cfg["camera"]["kind"] = "mock"
        self.root = d / "nas" / "utem-state"

    def tearDown(self):
        self.tmp.cleanup()

    def run_polls(self, states, camera=True, **cfg):
        self.cfg.update(cfg)
        lg = Logger(self.cfg, Scripted(states))
        if camera:
            lg.camera = MockCamera(self.cfg["camera"], lambda: lg.state or {})
        lg.store.check()
        for _ in range(len(states)):
            lg.poll()
        return lg

    def records(self):
        files = list(self.root.rglob("state.jsonl"))
        return [json.loads(line) for f in files for line in f.read_text().splitlines()]


class TestChanges(Base):
    def test_change_only_records_and_frames(self):
        on = {"GUN3__GetBeamSw": 1}
        states = [
            state(),                                                    # start, beam off: no frame
            state(GUN3__GetEmissionCurrentValue=3.0),                   # a reading: not a change
            state(**{"Stage3__GetPos": [26905.0, -315.28, -794.74, 19.76, 0.0]}),  # within tolerance
            state(**on),                                                # beam on: change + frame
            state(**on),                                                # nothing
            state(**on, Def3__GetCLA1=[32000, 30880]),                  # change + frame
            state(**on, Def3__GetCLA1=[32100, 30880], Lens3__GetCL3=48000),  # one change, one frame
            state(Def3__GetCLA1=[32100, 30880], Lens3__GetCL3=48000),   # beam off: change, no frame
            state(Def3__GetCLA1=[32200, 30880], Lens3__GetCL3=48000),   # change, beam off: no frame
        ]
        self.run_polls(states)
        recs = self.records()
        events = [r["event"] for r in recs]
        self.assertEqual(events, ["start", "change", "frame", "change", "frame", "change", "frame",
                                  "change", "change"])
        self.assertEqual(set(recs[5]["changed"]), {"Def3.GetCLA1", "Lens3.GetCL3"})
        self.assertEqual(recs[5]["changed"]["Lens3.GetCL3"], [48146, 48000])
        frames = sorted(self.root.rglob("*.png"))
        self.assertEqual(len(frames), 3)
        self.assertEqual(recs[4]["state_t"], recs[3]["t"])

    def test_frame_is_16bit_colour_with_state(self):
        self.cfg["camera"]["format"] = "rgb16"           # the Thorlabs-style 10-bit mock
        self.run_polls([state(GUN3__GetBeamSw=1)])
        f = next(self.root.rglob("*.png"))
        img, sbit = decode_png(f)
        self.assertEqual(img.shape, (1080, 1440, 3))
        self.assertEqual(img.dtype, np.uint16)
        self.assertEqual(sbit, 10)
        self.assertLessEqual(int(img.max()), 1023)                # the 10-bit sensor counts as they are
        self.assertGreater(int(img[540, 720, 1]), int(img[10, 10, 1]))  # the disc is brighter
        try:                                                      # an independent reader agrees
            from PIL import Image
            np.testing.assert_array_equal(np.asarray(Image.open(f)), (img >> 8).astype(np.uint8))
        except ImportError:
            pass
        text = png.read_text(f)
        self.assertEqual(json.loads(text["utem_state"])["GUN3.GetBeamSw"], 1)
        self.assertEqual(json.loads(text["utem_camera"])["format"], "rgb16")

    def test_other_formats(self):
        cam = MockCamera(dict(self.cfg["camera"], auto_exposure=False))
        raw = cam._raw(20.0)
        for fmt, shape, dtype in [("raw16", (1080, 1440), np.uint16), ("rgb8", (1080, 1440, 3), np.uint8),
                                  ("grey8", (1080, 1440), np.uint8)]:
            cam.cfg["format"] = fmt
            img, sig = cam.convert(raw)
            self.assertEqual((img.shape, img.dtype), (shape, dtype), fmt)
            d = pathlib.Path(self.tmp.name) / f"{fmt}.png"
            png.save(d, img, {}, significant_bits=sig)
            back, _ = decode_png(d)
            np.testing.assert_array_equal(back, img)
        cam.cfg.update(format="rgb16", downscale=2)
        self.assertEqual(cam.convert(raw)[0].shape, (540, 720, 3))

    def test_heartbeat(self):
        self.cfg["heartbeat_s"] = 0
        self.run_polls([state(), state(GUN3__GetEmissionCurrentValue=3.0)], camera=False)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["start", "heartbeat", "heartbeat"])   # heartbeat_s 0: every poll
        self.assertEqual(recs[-1]["state"]["GUN3.GetEmissionCurrentValue"], 3.0)   # readings are logged


class TestFailures(Base):
    def test_pyjem_lost_and_back(self):
        self.cfg["pyjem"]["retry_s"] = 0
        states = [state(), Unavailable("TEMCenter not answering"), Unavailable("again"),
                  state(Def3__GetCLA2=[1, 2])]
        self.run_polls(states, camera=False)
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["start", "status", "status", "change"])
        self.assertEqual([r.get("what") for r in recs[1:3]], ["pyjem_unavailable", "pyjem_available"])

    def test_camera_error_keeps_logging(self):
        on = {"GUN3__GetBeamSw": 1}
        self.cfg["camera"]["retry_s"] = 0
        lg = Logger(self.cfg, Scripted([state(**on), state(**on, Def3__GetCLs=[1, 1])]))
        lg.camera = MockCamera(self.cfg["camera"], lambda: lg.state or {})
        lg.camera.fail_next = 1
        lg.store.check()
        lg.poll()
        lg.poll()
        events = [(r["event"], r.get("what")) for r in self.records()]
        self.assertEqual(events, [("start", None), ("status", "camera_error"), ("change", None),
                                  ("status", "camera_ok"), ("frame", None)])

    def test_nas_down_spools_then_moves(self):
        d = pathlib.Path(self.tmp.name)
        (d / "nas").write_text("not a folder")          # the NAS path cannot be written
        on = {"GUN3__GetBeamSw": 1}
        lg = Logger(self.cfg, Scripted([state(**on), state(**on, Def3__GetCLs=[1, 1]),
                                        state(**on, Def3__GetCLs=[2, 2])]))
        lg.camera = MockCamera(self.cfg["camera"], lambda: lg.state or {})
        lg.store.retry_s = 0
        lg.store.check()
        lg.poll()
        self.assertFalse(lg.store.on_nas)
        self.assertTrue(list((d / "spool").rglob("*.png")))
        (d / "nas").unlink()                            # the NAS is back
        lg.poll()
        lg.poll()
        self.assertTrue(lg.store.on_nas)
        self.assertFalse([p for p in (d / "spool").rglob("*") if p.is_file()])
        events = [(r["event"], r.get("what")) for r in self.records()]
        self.assertEqual(events[0], ("start", None))
        self.assertIn(("status", "nas_unavailable"), events)
        self.assertIn(("status", "nas_available"), events)
        self.assertEqual(sum(e == "frame" for e, _ in events), 3)
        self.assertEqual(len(list(self.root.rglob("*.png"))), 3)


class FakeDM:
    """The bits of the DigitalMicrograph module the helper uses, over a dict of open images."""

    def __init__(self):
        self.images = {}      # id -> (name, array, tags)
        self.tags = {}

    class _Tags:
        def __init__(self, d):
            self.d = d

        def GetTagAsFloat(self, k):
            v = self.d.get(k)
            return (True, float(v)) if isinstance(v, (int, float)) else (False, 0.0)

        def GetTagAsString(self, k):
            v = self.d.get(k)
            return (True, str(v)) if v is not None else (False, "")

        def SetTagAsString(self, k, v):
            self.d[k] = v

    class _Image:
        def __init__(self, name, arr, tags):
            self._name, self._arr, self._tags = name, arr, tags

        def GetName(self):
            return self._name

        def GetNumArray(self):
            return self._arr

        def GetTagGroup(self):
            return FakeDM._Tags(self._tags)

    def ExecuteScriptString(self, code):
        if "CountImageDocuments" in code:
            self.tags["utem_view_server:count"] = len(self.images)
            for index, (image_id, (name, _, _)) in enumerate(self.images.items()):
                self.tags[f"utem_view_server:img{index}:id"] = image_id
                self.tags[f"utem_view_server:img{index}:name"] = name
        elif "GetApplicationVersion" in code:
            for key, value in zip(("major", "minor", "bug"), (3, 44, 3448)):
                self.tags["utem_view_server:gms_" + key] = value
        else:
            raise AssertionError("Unexpected DM script")

    def GetPersistentTagGroup(self):
        return FakeDM._Tags(self.tags)

    def FindImageByID(self, i):
        name, arr, tags = self.images[i]
        return FakeDM._Image(name, arr, tags)

    def Result(self, s):
        pass


class TestDMView(Base):
    """The real helper (dm_helper/utem_view_server.py) on a fake DM, over a real socket."""
    PORT = 47690

    @classmethod
    def setUpClass(cls):
        import importlib
        import os
        import threading
        import types
        cls.dm = FakeDM()
        cls.previous_dm = sys.modules.get("DigitalMicrograph")
        cls.previous_autostart = os.environ.get("UTEM_VIEW_SERVER_NO_AUTOSTART")
        sys.modules["DigitalMicrograph"] = cls.dm
        os.environ["UTEM_VIEW_SERVER_NO_AUTOSTART"] = "1"
        spec = importlib.util.spec_from_file_location(
            "utem_view_server", pathlib.Path(__file__).resolve().parents[1] / "dm_helper" / "utem_view_server.py")
        cls.srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.srv)
        cls.srv.PORT, cls.srv.COPY_S, cls.srv.QUIET_START = 0, 0, True
        ready = threading.Event()
        cls.srv._log = lambda msg: ready.set() if msg.startswith("listening") else None
        cls.server_thread = threading.Thread(target=cls.srv.serve, daemon=True)
        cls.server_thread.start()
        if not ready.wait(2):
            raise RuntimeError("DM test server did not start")
        cls.PORT = cls.srv._run["socket"].getsockname()[1]

    @classmethod
    def tearDownClass(cls):
        import os
        cls.srv._run["stop"] = True
        cls.srv._run["socket"].close()
        cls.server_thread.join(3)
        if cls.previous_dm is None:
            sys.modules.pop("DigitalMicrograph", None)
        else:
            sys.modules["DigitalMicrograph"] = cls.previous_dm
        if cls.previous_autostart is None:
            os.environ.pop("UTEM_VIEW_SERVER_NO_AUTOSTART", None)
        else:
            os.environ["UTEM_VIEW_SERVER_NO_AUTOSTART"] = cls.previous_autostart

    def setUp(self):
        super().setUp()
        self.dm.images.clear()
        self.cfg["dm_view"].update(port=self.PORT, probe_s=0, retry_s=0)
        self.rng = np.random.default_rng(0)

    def new_frame(self):
        """DM's View image got a new frame."""
        arr = (self.rng.normal(300, 20, (512, 512)) + 2000 * (np.hypot(*np.ogrid[-256:256, -256:256]) < 60)).astype(np.float32)
        self.dm.images[7] = ("Orius SC200 View", arr, {"Acquisition:Parameters:High Level:Exposure (s)": 0.1,
                                                      "Acquisition:Parameters:High Level:Binning": 4,
                                                      "Acquisition:Device:Name": "Orius"})

    def test_dm_frames_follow_screen_position_and_open_view(self):
        from utem_logger.camera import DMViewCamera
        up = {"Detector3__GetScreen": 2}
        lg = Logger(self.cfg, Scripted([
            state(**up), state(**up, Def3__GetCLA1=[1, 1]),
            state(**up, Def3__GetCLA1=[2, 2]), state(**up, Def3__GetCLA1=[3, 3]),
            state(GUN3__GetBeamSw=1, Detector3__GetScreen=0, Def3__GetCLA1=[4, 4])]))
        lg.dm_camera = DMViewCamera(self.cfg["dm_view"])
        lg.store.check()
        lg.poll()  # no image open
        self.new_frame()
        lg.poll()
        lg.poll()  # stopped View is still open; its unchanged image remains available
        self.new_frame()
        lg.poll()
        lg.poll()  # screen down: no DM frame
        recs = self.records()
        frames = [r for r in recs if r["event"] == "frame"]
        self.assertEqual(len(frames), 3)
        self.assertTrue(frames[1]["meta"]["same_as_last"])
        self.assertFalse(frames[2]["meta"]["same_as_last"])
        fr = frames[0]
        self.assertTrue(fr["file"].startswith("frames_dm/"))
        self.assertEqual(fr["meta"]["dm_binning"], 4.0)
        day = self.root / fr["t"][:4] / fr["t"][:7] / fr["t"][:10]
        img, _ = decode_png(day / fr["file"])
        self.assertEqual((img.shape, img.dtype), ((512, 512), np.uint16))
        self.assertGreater(int(img[256, 256]), 1500)
        self.assertEqual(png.read_text(day / fr["file"])["utem_source"], "dm_view")

    def test_view_becoming_live_owes_a_frame(self):
        from utem_logger.camera import DMViewCamera
        lg = Logger(self.cfg, Scripted([state(Detector3__GetScreen=2)] * 3))
        lg.dm_camera = DMViewCamera(self.cfg["dm_view"])
        lg.store.check()
        lg.poll()                                        # start, no view
        lg.poll()                                        # probe: not live
        self.new_frame()
        lg.poll()                                        # probe: live now -> one frame, no change needed
        self.assertEqual([r["event"] for r in self.records()], ["start", "frame"])

    def test_helper_missing_is_one_status_line(self):
        from utem_logger.camera import DMViewCamera
        self.cfg["dm_view"]["port"] = 1                  # nothing listens there
        lg = Logger(self.cfg, Scripted([state(Detector3__GetScreen=2), state(Detector3__GetScreen=2, Def3__GetCLA1=[1, 1]), state(Detector3__GetScreen=2, Def3__GetCLA1=[2, 2])]))
        lg.dm_camera = DMViewCamera(self.cfg["dm_view"])
        lg.store.check()
        for _ in range(3):
            lg.poll()
        ev = [(r["event"], r.get("what")) for r in self.records()]
        self.assertEqual(ev.count(("status", "camera_error")), 1)
        self.assertEqual(ev.count(("change", None)), 2)


class FakeControl:
    def __init__(self, lo, hi, step, default, auto_supported=False, supported=True, value=None, auto=False):
        class Caps:
            pass
        self.capabilities = Caps()
        c = self.capabilities
        c.min, c.max, c.step, c.default, c.auto_mode_supported, c.supported = lo, hi, step, default, auto_supported, supported
        self.value = default if value is None else value
        self.auto = auto
        self.refuse = False

    def try_get_value(self):
        return (True, self.value)

    def try_get_auto(self):
        return (True, self.auto)

    def try_set_value(self, v):
        if self.refuse:
            return False
        self.value = v
        return True

    def try_set_auto(self, a):
        self.auto = bool(a)
        return True


class FakeVDC:
    def __init__(self):
        self.exposure = FakeControl(-11, -2, 1, -5, auto_supported=True, auto=True)
        self.focus = FakeControl(0, 255, 5, 0, auto_supported=True, auto=True)
        self.brightness = FakeControl(0, 255, 1, 128)
        self.white_balance = FakeControl(2000, 7500, 10, 4000, auto_supported=True, auto=True)
        self.zoom = FakeControl(100, 500, 1, 100, supported=False)
        self.contrast = FakeControl(0, 255, 1, 128)
        self.contrast.refuse = True


class FakeGrabber:
    """Stands in for brio.BrioGrabber: no WinRT, frames are a green disc on a dark screen."""

    def __init__(self, settings=None):
        from utem_logger.brio import apply_controls
        self.settings = settings or {}
        self.cam = None
        self.vdc = FakeVDC()
        self.applied = None
        self.opens = 0
        self.apply_controls = apply_controls
        self.rng = np.random.default_rng(0)
        self.fail = False

    def open(self):
        self.opens += 1
        self.cam = object()
        self.applied = self.apply_controls(self.vdc, self.settings.get("controls") or {})

    def close(self):
        self.cam = None

    def grab(self):
        if self.cam is None:
            self.open()
        if self.fail:
            raise RuntimeError("camera unplugged")
        h, w = 1080, 1920
        yy, xx = np.ogrid[:h, :w]
        g = 10 + 200 * ((xx - 960) ** 2 + (yy - 540) ** 2 < 150 ** 2) + self.rng.normal(0, 3, (h, w))
        g = np.clip(g, 0, 255).astype(np.uint8)
        rgb = np.stack([g // 3, g, g // 4], axis=2)
        return rgb, {"camera": "Logitech BRIO", "format": "1920x1080 @ 5 fps  MJPG",
                     "controls": {"exposure": {"value": self.vdc.exposure.value, "auto": self.vdc.exposure.auto}}}

    def dump(self):
        from utem_logger.brio import read_controls
        if self.cam is None:
            self.open()
        return {"camera": "BRIO", "format": "1920x1080 @ 5 fps  MJPG", "formats_available": ["1920x1080 @ 5 fps  MJPG"],
                "controls": read_controls(self.vdc)}


class TestBrio(Base):
    PORT = 47691

    def test_apply_and_read_controls(self):
        from utem_logger.brio import apply_controls, read_controls
        vdc = FakeVDC()
        done = apply_controls(vdc, {"exposure": {"auto": False, "value": -6.4}, "focus": 12, "zoom": 300,
                                    "white_balance": {"auto": True}, "contrast": 100})
        self.assertEqual(done["exposure"], {"auto": True, "value": -6.0})      # snapped to the step
        self.assertFalse(vdc.exposure.auto)
        self.assertEqual(vdc.exposure.value, -6.0)
        self.assertEqual(done["focus"]["value"], 10.0)                          # step 5
        self.assertFalse(vdc.focus.auto)
        self.assertEqual(done["zoom"], "unsupported")
        self.assertTrue(vdc.white_balance.auto)
        self.assertEqual(done["contrast"]["value"], "refused")
        self.assertEqual(vdc.brightness.value, 128)                             # untouched: not in the file
        r = read_controls(vdc)
        self.assertNotIn("zoom", r)
        self.assertEqual(r["exposure"], {"value": -6.0, "auto": False, "min": -11, "max": -2, "step": 1, "default": -5})
        self.assertEqual(read_controls(vdc, values_only=True)["brightness"], {"value": 128})

    def test_logger_frames_through_brio_in_process(self):
        from utem_logger.camera import BrioCamera
        grabber = FakeGrabber({"controls": {"exposure": -6}})
        on = {"GUN3__GetBeamSw": 1}
        lg = Logger(self.cfg, Scripted([state(**on), state(**on, Def3__GetCLA1=[1, 1])]))
        lg.camera = BrioCamera(self.cfg["camera"], grabber)
        lg.store.check()
        lg.poll()
        lg.poll()
        recs = self.records()
        self.assertEqual([r["event"] for r in recs], ["start", "frame", "change", "frame"])
        fr = recs[1]
        self.assertEqual(fr["meta"]["format"], "rgb8")
        self.assertEqual(fr["meta"]["controls"]["exposure"], {"value": -6.0, "auto": False})
        img, _ = decode_png(self.root / "2026" / fr["t"][:7] / fr["t"][:10] / fr["file"])
        self.assertEqual((img.shape, img.dtype), ((1080, 1920, 3), np.uint8))
        self.assertGreater(int(img[540, 960, 1]), 150)
        self.assertEqual(grabber.opens, 1)                                      # kept open between frames

    def test_brio_through_camera_server(self):
        import threading
        from utem_logger import brio
        from utem_logger.camera import BrioCamera
        grabber = FakeGrabber()
        threading.Thread(target=brio.serve, args=(grabber, "127.0.0.1", self.PORT), daemon=True).start()
        import time
        time.sleep(0.2)
        self.cfg["camera"]["brio"].update(port=self.PORT, in_process="never", timeout_s=5)
        self.cfg["camera"]["downscale"] = 2
        cam = BrioCamera(self.cfg["camera"])
        img, meta, _ = cam.grab()
        self.assertEqual(img.shape, (540, 960, 3))
        self.assertEqual(meta["camera"], "Logitech BRIO")
        self.assertTrue(meta["via"].startswith("server"))
        h, _ = brio.ask("127.0.0.1", self.PORT, "dump", 5)
        self.assertIn("exposure", h["controls"])
        cam.release_if_idle(force=True)
        self.assertIsNone(grabber.cam)                                          # released on request
        grabber.fail = True
        with self.assertRaises(RuntimeError):
            cam.grab()


class TestMockRun(Base):
    def test_cli_mock_run(self):
        from utem_logger.__main__ import main
        cfgfile = pathlib.Path(self.tmp.name) / "c.json"
        cfgfile.write_text(json.dumps({"log_root": str(pathlib.Path(self.tmp.name) / "nas"),
                                       "spool_dir": self.cfg["spool_dir"],
                                       "service_log_dir": self.cfg["service_log_dir"],
                                       "poll_interval_s": 0.05, "single_instance_port": 47699}))
        self.assertEqual(main(["--mock", "--config", str(cfgfile), "--duration", "2"]), 0)
        events = [r["event"] for r in self.records()]
        self.assertEqual((events[0], events[-1]), ("start", "stop"))


if __name__ == "__main__":
    unittest.main()

