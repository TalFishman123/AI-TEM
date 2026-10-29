"""Settings: the defaults below, overridden by a JSON file (see ../config.example.json).

Python 3.8 compatible (PyJEM 1.1 runs on 3.8).
"""
from __future__ import annotations

import copy
import json
import os
import pathlib

# Every getter that answered in the read-only probe of 2026-10-13 (probes/pyjem/), grouped by class.
# Get* only: the logger never calls anything that moves the microscope.
GETTERS = [
    # high tension and gun
    "HT3.GetHtValue",
    "GUN3.GetBeamSw", "GUN3.GetFilamentVal", "GUN3.GetHtStts", "GUN3.GetHtTargetValue",
    "GUN3.GetEmissionCurrentValue", "GUN3.GetFilamentCurrentValue",
    "GUN3.GetAnode1CurrentValue", "GUN3.GetAnode2CurrentValue",
    "GUN3.GetHtCurrentValue", "GUN3.GetHtMonitorValue",
    # optics mode
    "EOS3.GetFunctionMode", "EOS3.GetProbeMode", "EOS3.GetTemStemMode", "EOS3.GetMagValue",
    "EOS3.GetCurrentMagSelectorID", "EOS3.GetSpotSize", "EOS3.GetAlpha", "EOS3.GetAlphaSelectorEx",
    "EOS3.GetBrightnessZoom", "EOS3.GetStemCamValue",
    # lenses
    "Lens3.GetCL1", "Lens3.GetCL2", "Lens3.GetCL3", "Lens3.GetCM", "Lens3.GetOLc", "Lens3.GetOLf",
    "Lens3.GetOM", "Lens3.GetOM2", "Lens3.GetOM2Flag", "Lens3.GetIL1", "Lens3.GetIL2", "Lens3.GetIL3",
    "Lens3.GetIL4", "Lens3.GetPL1", "Lens3.GetPL2", "Lens3.GetPL3", "Lens3.GetFLc", "Lens3.GetFLf",
    "Lens3.GetFLcomp1", "Lens3.GetFLcomp2",
    # deflectors
    "Def3.GetBeamBlank", "Def3.GetGunA1", "Def3.GetGunA2", "Def3.GetCLA1", "Def3.GetCLA2", "Def3.GetCLs",
    "Def3.GetSpotA", "Def3.GetShifBal", "Def3.GetTiltBal", "Def3.GetAngBal", "Def3.GetOLs", "Def3.GetILs",
    "Def3.GetIS1", "Def3.GetIS2", "Def3.GetPLA", "Def3.GetFLA1", "Def3.GetFLA2", "Def3.GetFLs1",
    "Def3.GetFLs2", "Def3.GetStemIS", "Def3.GetCorrection", "Def3.GetMagAdjust", "Def3.GetOffset",
    "Def3.GetRotation",
    # stage and apertures
    "Stage3.GetPos", "Stage3.GetStatus", "Stage3.GetHolderStts", "Stage3.GetDrvMode", "Stage3.GetPiezoPosi",
    "Apt3.GetKind", "Apt3.GetPosition",
]

# Measured values that drift on their own: recorded in every line, but a change in them alone is not
# a "state change" (it would fire every second).
READINGS = [
    "GUN3.GetEmissionCurrentValue", "GUN3.GetFilamentCurrentValue",
    "GUN3.GetAnode1CurrentValue", "GUN3.GetAnode2CurrentValue",
    "GUN3.GetHtCurrentValue", "GUN3.GetHtMonitorValue",
    "Stage3.GetStatus",
]

DEFAULTS = {
    # where the logs go: <log_root>/<subdir>/YYYY/YYYY-MM/YYYY-MM-DD/. Prefer the UNC path of the NAS
    # (\\nas\share\data\logs): a mapped drive letter is not visible to a task that starts before log-on.
    "log_root": "Z:/data/logs",
    "subdir": "utem-state",
    # used while the NAS is unreachable; moved to the NAS when it is back
    "spool_dir": None,               # default: %LOCALAPPDATA%/utem-logger/spool
    "service_log_dir": None,         # default: %LOCALAPPDATA%/utem-logger (the logger's own log, rotating)

    "poll_interval_s": 1.0,
    "heartbeat_s": 300,              # a full state line every 5 min even without a change
    "below_normal_priority": True,   # Windows: run at below-normal CPU priority
    "single_instance_port": 47613,   # a second copy on the same PC exits at once
    "min_free_gb": 20,               # below this free space on the NAS, frames pause (state goes on)

    "pyjem": {
        "offline": False,            # PyJEM's own simulator
        "getters": GETTERS,
        "readings": READINGS,
        "tolerance": {"Stage3.GetPos": 10.0, "Stage3.GetPiezoPosi": 1.0},   # abs, per element
        "timeout_s": 10,             # a whole poll slower than this: PyJEM is treated as unavailable
        "retry_s": 30,               # reconnect interval while unavailable
        "disable_after_errors": 20,  # a getter failing this many polls in a row is dropped until reconnect
    },

    # thermionic = the beam (filament) switch is on; the filament is not heated in photoemission
    "thermionic": {"beam_switch": "GUN3.GetBeamSw"},

    # the Orius live View, copied from DigitalMicrograph through dm_helper/utem_view_server.py, in
    # photoemission mode (beam switch off) while someone has a View running in DM
    "dm_view": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 47614,
        "timeout_s": 3.0,
        "downscale": 1,
        "min_interval_s": 0.0,
        "retry_s": 60,               # after an error (DM or the helper not running)
        "probe_s": 5.0,              # how often to ask whether a View is live when there is no change
    },

    "camera": {
        "enabled": True,
        "kind": "brio",              # "brio" (Logitech BRIO through Windows), "thorlabs" or "mock"
        # BRIO: its settings (format and controls) come from settings_file, see camera_settings.example.json
        "brio": {
            "settings_file": "camera_settings.json",   # relative to the working directory (utem-logger/)
            "in_process": "auto",    # "auto": in this process when pywinrt imports, else the server below;
                                     # "always" / "never"
            "host": "127.0.0.1",
            "port": 47615,           # python -m utem_logger --camera-server, in the environment with pywinrt
            "timeout_s": 8.0,
        },
        # Thorlabs only:
        "serial": None,              # None: the first camera found
        "dll_dir": None,             # folder with the Thorlabs native DLLs, if not already on PATH
        "exposure_ms": 20.0,
        "gain": 0,                   # camera gain index (0 = lowest)
        "auto_exposure": True,       # adjust the exposure for the next frame (saturated / too dark)
        "exposure_min_ms": 0.1,
        "exposure_max_ms": 2000.0,
        # BRIO (8-bit): "rgb8" (default) or "grey8". Thorlabs (10-bit): "rgb16" 16-bit colour PNG |
        # "raw16" the sensor's Bayer mosaic, 16-bit grey PNG (~1/3 the size, needs debayering) | "rgb8" |
        # "grey8"; 16-bit values are the sensor counts (sBIT chunk = 10), which Fiji and Python show fine.
        "format": "rgb8",
        "downscale": 1,              # integer binning for rgb16/rgb8/grey8 (2 = half width and height)
        "png_level": 1,              # zlib level 1-9: 1 is ~5x faster than 6 for ~10 % bigger files
        "min_interval_s": 0.0,       # optional floor between frames; 0 = a frame on every detected change
        "release_after_s": 120,      # close the camera after this long without frames (frees it for ThorCam)
        "retry_s": 60,               # reopen interval after a camera error
    },
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k.startswith("_"):        # "_comment" keys in the JSON file
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def local_dir() -> pathlib.Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return pathlib.Path(base) / "utem-logger"


def load(path=None, **overrides) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        with open(path, encoding="utf-8") as f:
            cfg = _merge(cfg, json.load(f))
    cfg = _merge(cfg, {k: v for k, v in overrides.items() if v is not None})
    cfg["spool_dir"] = cfg["spool_dir"] or str(local_dir() / "spool")
    cfg["service_log_dir"] = cfg["service_log_dir"] or str(local_dir())
    return cfg
