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
    # viewing screen angle: 0 = 0 deg (down), 1 = 7 deg (down, tilted), 2 = 90 deg (up). Not in the probe
    # (Detector3 was skipped there); if this PyJEM lacks it, it is dropped and frame conditions ignore it.
    "Detector3.GetScreen",
    # screen current density (pA/cm2, TEMCenter's screen current display). Not in the probe (Camera3 was
    # skipped there); dropped if this PyJEM lacks it. Tracked with a relative tolerance, see "tolerance".
    "Camera3.GetCurrentDensity",
    # vacuum: ready/air flags and valves are settings (a specimen exchange shows up here); the Penning
    # (Peg) and Pirani (Pig) gauges are raw 0-4095 readings with a status (0 off, 1 high, 2 low, 3 ready).
    # Indices follow TEMCenter's gauge numbering; one that does not exist is dropped after 20 failures.
    # Not in the probe (Vacuum was not scanned); the class is VACUUM3 in the PyJEM reference.
    "VACUUM3.GetColumnReady", "VACUUM3.GetSpecimenReady", "VACUUM3.GetCameraReady",
    "VACUUM3.GetColumnAir", "VACUUM3.GetSpecimenAir", "VACUUM3.GetCameraAir",
    # GetSpecimenPreEvacStart excluded: runtime error 25610 and multi-second delays in the UTEM log.
    # Restore only after a separate compatibility check; this is diagnostic, not a beep diagnosis.
    "VACUUM3.GetValveStatus",
    "VACUUM3.GetPegInfo(0)", "VACUUM3.GetPegInfo(1)", "VACUUM3.GetPegInfo(2)", "VACUUM3.GetPegInfo(3)",
    "VACUUM3.GetPigInfo(0)", "VACUUM3.GetPigInfo(1)", "VACUUM3.GetPigInfo(2)", "VACUUM3.GetPigInfo(3)",
]

# Every Get* call is a round trip to TEMCenter of about 44 ms on the JEM-2100 Plus PC (measured
# 2026-10-22), so the 73 getters take 3.2 s in series and TEMCenter refuses parallel callers. The getters
# below (what an operator changes with knobs and buttons, plus the frame conditions) are read every poll;
# the rest rotate (pyjem.slow_per_poll): 63 slow getters over 13 polls, about 6.5 min at 30 s.
FAST = [
    "GUN3.GetBeamSw", "Def3.GetBeamBlank", "Detector3.GetScreen",          # frame conditions
    "GUN3.GetHtCurrentValue", "GUN3.GetFilamentVal",                       # bias (derived below), filament
    "Camera3.GetCurrentDensity",                                           # screen current: sampled once per poll
    "EOS3.GetFunctionMode", "EOS3.GetMagValue", "EOS3.GetSpotSize", "EOS3.GetAlpha",
    "Lens3.GetCL3", "Lens3.GetOLf", "Lens3.GetOLc", "Lens3.GetIL1",        # brightness, focus, diff focus
    "Def3.GetGunA1", "Def3.GetGunA2", "Def3.GetCLA1", "Def3.GetCLA2",      # gun and beam alignment
    "Def3.GetCLs", "Def3.GetOLs", "Def3.GetILs",                           # stigmators
    "Def3.GetIS1", "Def3.GetIS2", "Def3.GetPLA",                           # image shift, projector align
    "Stage3.GetPos", "Apt3.GetPosition",
]

# Values picked out of the gun's tuples and tracked as settings under their own key. PyJEM 1.1 has no
# GetBiasCurrentValue; the bias sits inside GetHtCurrentValue / GetHtTargetValue (layouts from the
# PyJEM 1.4 reference: current = [ht, a1, a2, bias, filament, energyshift, dark, emission],
# target = [ht, a1, a2, bias, filament, energyshift]).
DERIVED = {
    "gun.bias": "GUN3.GetHtCurrentValue[3]",
    "gun.bias_target": "GUN3.GetHtTargetValue[3]",
}

# Measured values that drift on their own: recorded in every line, but a change in them alone is not
# a "state change" (it would fire every second).
READINGS = [
    "GUN3.GetEmissionCurrentValue", "GUN3.GetFilamentCurrentValue",
    "GUN3.GetAnode1CurrentValue", "GUN3.GetAnode2CurrentValue",
    "GUN3.GetHtCurrentValue", "GUN3.GetHtMonitorValue",
    "Stage3.GetStatus",
    "VACUUM3.GetPegInfo(0)", "VACUUM3.GetPegInfo(1)", "VACUUM3.GetPegInfo(2)", "VACUUM3.GetPegInfo(3)",
    "VACUUM3.GetPigInfo(0)", "VACUUM3.GetPigInfo(1)", "VACUUM3.GetPigInfo(2)", "VACUUM3.GetPigInfo(3)",
]

DEFAULTS = {
    # where the logs go: <log_root>/<subdir>/YYYY/YYYY-MM/YYYY-MM-DD/. Prefer the UNC path of the NAS
    # (\\nas\share\data\logs): a mapped drive letter is not visible to a task that starts before log-on.
    "log_root": "Z:/data/logs",
    "subdir": "utem-state",
    # used while the NAS is unreachable; moved to the NAS when it is back
    "spool_dir": None,               # default: %LOCALAPPDATA%/utem-logger/spool
    "service_log_dir": None,         # default: %LOCALAPPDATA%/utem-logger (the logger's own log, rotating)

    "poll_interval_s": 30.0,
    "heartbeat_s": 300,              # a full state line every 5 min even without a change
    "below_normal_priority": True,   # Windows: run at below-normal CPU priority
    "single_instance_port": 47613,   # a second copy on the same PC exits at once
    "min_free_gb": 20,               # below this free space on the NAS, frames pause (state goes on)

    "pyjem": {
        "offline": False,            # PyJEM's own simulator
        "getters": GETTERS,
        "fast": FAST,                # read every poll; the other getters rotate, slow_per_poll per poll
        "slow_per_poll": 5,          # 26 fast + 5 = 31 calls = about 1.4 s per poll on the TEM PC
        "readings": READINGS,
        # a number: absolute, per element; {"rel": r, "min": m} is also accepted (beyond r of the larger
        # value and beyond m). Screen current: a change of 0.3 pA/cm2 or more counts (0.25 so that a
        # 0.3 step is never lost to float rounding; 0.2 and less is noise).
        "tolerance": {"Stage3.GetPos": 10.0, "Stage3.GetPiezoPosi": 1.0, "Camera3.GetCurrentDensity": 0.25},
        "derived": DERIVED,          # {key: "Getter[index]"}: elements of a reading tracked as settings
        "timeout_s": 60,             # ordinary read wait limit; keep a timed-out worker until it finishes.
                                     # No parallel read/reconnect. Initial full read gets a scaled limit.
                                     # Earlier logs show periodic 10 s timeouts; 60 s needs instrument validation.
        "slow_poll_s": 10,           # a poll slower than this is logged as a warning with its slowest getters
        "retry_s": 30,               # reconnect interval while unavailable
        "disable_after_errors": 20,  # a getter failing this many polls in a row is dropped until reconnect
    },

    # thermionic = the beam (filament) switch is on; the filament is not heated in photoemission
    "thermionic": {"beam_switch": "GUN3.GetBeamSw"},

    # When a frame is taken (on a change, when every listed getter has one of the allowed values).
    # A getter this PyJEM does not answer is ignored (logged once), so a missing GetScreen does not
    # stop the frames.
    "frames": {
        "screen": {                  # the webcam on the phosphor screen
            "GUN3.GetBeamSw": 1,     # thermionic
            "Def3.GetBeamBlank": 0,  # beam not blanked
            "Detector3.GetScreen": [0, 1],   # screen down (0 or 7 deg)
        },
        "dm_view": {                 # the Orius through DM (and only while DM's View window is open)
            "Def3.GetBeamBlank": 0,  # beam not blanked
            "Detector3.GetScreen": 2,        # screen up, otherwise the Orius sees nothing
        },
    },

    # the Orius live View, copied from DigitalMicrograph through dm_helper/utem_view_server.py, while
    # DM's View window is open, the beam is not blanked and the screen is up (frames.dm_view above)
    "dm_view": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 47614,
        "timeout_s": 3.0,
        "downscale": 1,
        "min_interval_s": 0.0,
        "retry_s": 60,               # after an error (DM or the helper not running)
        "probe_s": 5.0,              # how often to ask whether a View window is open when there is no change
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
