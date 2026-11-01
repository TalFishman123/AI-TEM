"""python -m utem_logger [--config config.json] [--mock] [--once] [--duration S] [--log-root DIR]

    --once           read the state once, print it and exit (first check on the microscope PC)
    --mock           pretend microscope and camera (no PyJEM, no camera needed)
    --camera-dump    write the BRIO's current format and controls to the camera settings file, and exit
    --camera-test    take one BRIO frame with the settings file applied, save it as PNG, and exit
    --camera-server  serve BRIO frames to a logger running in another Python environment
    --dm-status      ask the DM helper what it sees, and exit
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import pathlib
import socket
import sys

from . import config as config_mod
from .logger import Logger, install_signals, make_camera, make_dm_camera
from .camera import DMViewCamera
from .pyjem_source import PyJEMSource, Unavailable, timed_read


def setup_logging(cfg, verbose):
    d = pathlib.Path(cfg["service_log_dir"])
    d.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(d / "logger.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger("utem_logger")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(fh)
    if sys.stderr is not None:                   # pythonw.exe has no console
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def below_normal_priority():
    if sys.platform == "win32":
        import ctypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x4000
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)


def camera_commands(a, cfg):
    from . import brio, png
    b = cfg["camera"]["brio"]
    if a.camera_server:
        grabber = brio.BrioGrabber(brio.load_settings(b.get("settings_file")))
        brio.serve(grabber, b["host"], int(b["port"]), idle_release_s=cfg["camera"]["release_after_s"])
        return 0
    try:
        import winrt.windows.media.capture  # noqa: F401
        grabber = brio.BrioGrabber(brio.load_settings(b.get("settings_file")))
        via = "in this process"
    except ImportError:
        grabber, via = None, "through the camera server at %s:%s" % (b["host"], b["port"])
    print("camera", via)
    try:
        if a.camera_dump:
            d = grabber.dump() if grabber else brio.ask(b["host"], b["port"], "dump", b["timeout_s"])[0]
            d.pop("ok", None)
            path = pathlib.Path(b.get("settings_file") or "camera_settings.json")
            if path.exists():
                backup = path.with_suffix(".bak.json")
                path.replace(backup)
                print("previous settings kept as", backup)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
            print("wrote", path)
            print(json.dumps(d, indent=1))
        else:
            from .camera import BrioCamera
            cam = BrioCamera(cfg["camera"], grabber)
            img, meta, _ = cam.grab()
            cam.release_if_idle(force=True)
            size = png.save(a.camera_test, img, {"utem_camera": json.dumps(meta)}, cfg["camera"]["png_level"])
            print("saved %s: %s %s, %.2f MB" % (a.camera_test, img.shape, img.dtype, size / 1e6))
            print(json.dumps(meta, indent=1))
    finally:
        if grabber is not None:
            grabber.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m utem_logger", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="JSON file overriding the defaults (see config.example.json)")
    ap.add_argument("--log-root", help="overrides log_root")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dm-status", action="store_true", help="ask the DM helper what it sees, and exit")
    ap.add_argument("--camera-dump", action="store_true", help="write the camera's current settings to the settings file")
    ap.add_argument("--camera-test", metavar="OUT.png", nargs="?", const="camera_test.png",
                    help="grab one frame with the settings applied and save it")
    ap.add_argument("--camera-server", action="store_true", help="serve camera frames on camera.brio.port")
    ap.add_argument("--duration", type=float, help="stop after this many seconds (tests)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    cfg = config_mod.load(a.config, log_root=a.log_root)
    if a.mock:
        cfg["camera"]["kind"] = "mock"
        cfg["dm_view"]["enabled"] = False
    setup_logging(cfg, a.verbose)
    log = logging.getLogger("utem_logger")

    if a.mock:
        from .mock import MockSource
        source = MockSource()
    else:
        source = PyJEMSource(cfg["pyjem"])

    if a.camera_dump or a.camera_test or a.camera_server:
        return camera_commands(a, cfg)

    if a.dm_status:
        cam = DMViewCamera(cfg["dm_view"])
        try:
            print(json.dumps(cam._ask("status", cfg["dm_view"]["timeout_s"])[0], indent=1))
            print(json.dumps(cam._ask("probe", cfg["dm_view"]["timeout_s"])[0], indent=1))
        except Exception as e:  # noqa: BLE001
            print("DM helper not reachable at %s:%s: %s" % (cfg["dm_view"]["host"], cfg["dm_view"]["port"], e))
            return 1
        return 0

    if a.once:
        # the first read connects and reads every getter; the second is an ordinary poll
        try:
            full, errors, connect_ms = timed_read(source)
            part, errors2, ms = timed_read(source)
        except Unavailable as e:
            print("PyJEM unavailable:", e)
            return 1
        finally:
            source.close()
        by_time = sorted(getattr(source, "getter_ms", {}).items(), key=lambda kv: -kv[1])
        print(json.dumps({"poll_ms": round(ms, 1), "poll_getters": len(part),
                          "connect_and_full_read_ms": round(connect_ms, 1),
                          "getter_ms": {k: round(v, 1) for k, v in by_time},     # slowest first
                          "state": {**full, **{k: v for k, v in part.items() if v is not None}},
                          "errors": {**errors, **errors2}}, indent=1))
        return 0

    try:
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lock.bind(("127.0.0.1", int(cfg["single_instance_port"])))   # held for the process lifetime
    except OSError:
        log.error("another utem_logger is already running on this PC: exiting")
        return 2

    if cfg["below_normal_priority"]:
        try:
            below_normal_priority()
        except Exception as e:  # noqa: BLE001
            log.warning("could not lower priority: %s", e)

    logger = Logger(cfg, source)
    logger.camera = make_camera(cfg, state_fn=lambda: logger.state or {})
    logger.dm_camera = make_dm_camera(cfg)
    install_signals(logger)
    logger.run(a.duration)
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
