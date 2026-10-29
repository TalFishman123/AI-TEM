# UTEM state logger

Runs 24/7 on the microscope PC. Every second it reads the UTEM state through PyJEM (read-only: Get* calls
only, nothing is ever set). When a setting changes it writes the change and the full state to the NAS.
If the gun is thermionic (beam switch on), it also saves a frame from the webcam that looks at the
phosphor screen (a Logitech BRIO, driven through Windows with the settings of a JSON file). In
photoemission (beam switch off) it saves instead a copy of the Orius live View from DigitalMicrograph,
whenever someone has a View running.

Python 3.9 or newer, in its own conda environment (see "Environment"): numpy, PyJEM (JEOL's tarball), and
pywinrt for the webcam. pandas only for `read.to_dataframe`.

## What it writes

Under `<log_root>/utem-state/` (the default `log_root` is the NAS `data/logs`):

```
2026/2026-10/2026-10-15/
    state.jsonl                          one JSON object per line, append-only
    frames/2026-10-15_16-20-01.123.png   one webcam frame of the screen per change while thermionic
    frames_dm/2026-10-15_17-02-11.456.png   one Orius frame per change in photoemission, while a View is live
```

One folder per day, so a day can be archived or deleted as a whole. Each line of `state.jsonl` has `t`
(local time with UTC offset, ms) and `event`:

| event | when | fields |
|---|---|---|
| `start` | logger started | full `state`, `thermionic`, `version`, `config` |
| `change` | a setting changed since the last poll | `changed` {getter: [old, new]}, full `state`, `thermionic` |
| `frame` | right after a change (and at start, beam on, or a View becoming live) | `source` (`screen` or `dm_view`), `file`, `bytes`, `state_t` (the `t` of the line whose state it shows), camera `meta` (the webcam's format and control values as read back, saturation; or DM's exposure and binning) |
| `heartbeat` | every 5 min without a change | full `state`, `poll_ms` (median and max of one PyJEM read), `frames_today`, `bytes_today`, `on_nas` |
| `status` | something went away or came back | `what`: `pyjem_unavailable`, `pyjem_available`, `camera_error` / `camera_ok` (with `source`), `nas_unavailable`, `nas_available`, `frames_paused_low_disk` |
| `stop` | logger stopped | |

`state` holds about 70 PyJEM getters, keyed `Class.Method`, with values as PyJEM returns them (DAC
counts, mode tuples). The list is `GETTERS` in `utem_logger/config.py`. It contains every getter that
answered in the 2026-10-13 probe.

Some values drift on their own: emission, filament and anode currents, the HT monitor values and stage
status. These `READINGS` are recorded in every line but don't count as a change. The stage position
counts as changed only beyond a tolerance of 10 units per axis. Changes that happen within the same
1 s poll become one `change` line and one frame.

## Screen frames: the BRIO webcam and its settings file

The webcam is opened through Windows (WinRT, `utem_logger/brio_camera.py`), exactly as the lab's
`brio_gui.py` does: the colour sensor only, with exclusive control so its controls can be set. Only one
program can hold it: while `brio_gui.py`, MATLAB's `webcam` or a meeting app has it, the logger skips
frames (one `camera_error` line per outage) and retries every minute. The logger opens the camera on the
first frame and releases it after `release_after_s` (2 min) without frames.

**Settings come from `camera_settings.json`** (path in `config.json`, `camera.brio.settings_file`;
example in `camera_settings.example.json`), read when the camera is opened:

```json
{
  "camera": "BRIO",
  "format": "1920x1080 @ 5 fps  MJPG",
  "controls": {
    "exposure": {"auto": false, "value": -6},
    "focus": {"auto": false, "value": 0},
    "white_balance": {"auto": false, "value": 4000},
    "brightness": 128
  }
}
```

`format` is written as `brio_gui.py --colour` takes it (`1920x1080`, `1920x1080@5`, or the full label;
the lowest frame rate that matches is used, it is the lightest on USB). `controls` are the sliders of
`brio_gui.py` by name (`brightness`, `contrast`, `hue`, `white_balance`, `backlight_compensation`,
`exposure`, `focus`, `zoom`, `pan`, `tilt`, `roll`), in the camera's own units; a control is
`{"auto": bool, "value": n}` or a bare number (manual at that value). Values are snapped to the
control's step and clipped to its range. Controls not in the file are left as the camera has them.

The easy way to make the file: set the camera up in `brio_gui.py`, close it, then

```
python -m utem_logger --camera-dump --config config.json
```

writes the camera's current format and every control (value, auto, and its min/max/step/default for
reference) into the settings file, keeping the previous file as `.bak.json`.
`python -m utem_logger --camera-test --config config.json` then takes one frame with the file applied
and saves it as `camera_test.png`, printing the controls as read back. Every logged frame also records
the control values as read back from the camera at that moment.

Frames are saved as 8-bit colour PNG (`rgb8`; `grey8` and `downscale` are available), about 0.3 MB
each for a 640x480 lab frame and about 2 MB for 1920x1080, depending on how much of the screen is lit.
The `heartbeat` line reports `frames_today` and `bytes_today`. If free space on the NAS falls below
`min_free_gb` (20 GB), frames pause and the state logging continues. (The earlier Thorlabs CS165CU code
is still there: `camera.kind = "thorlabs"`, 16-bit formats.)

Camera in another environment: if PyJEM is only available for a Python that pywinrt does not support
(3.8), run the camera as its own process in the environment that has pywinrt,
`python -m utem_logger --camera-server --config config.json`, and the logger talks to it over
`127.0.0.1:47615` (it does this on its own whenever pywinrt is not importable in its environment, see
`camera.brio.in_process`). `install_task.ps1 -CameraPython <pythonw.exe of that env>` registers it as a
second task. `--camera-dump` and `--camera-test` work through the server too.

## Orius frames in photoemission (through DM)

The Orius is only reachable through DigitalMicrograph, so a helper runs inside DM's Python:
`dm_helper/utem_view_server.py`. It listens on `127.0.0.1:47614` and, when the logger asks, copies the
image DM is already showing in its live View window (pixels and the exposure and binning tags). It
never acquires, so the operator's live view is not interrupted: a request is a memory copy of a few
MB, milliseconds, and the camera is not involved. A background thread in the helper samples one pixel
in 64 of the View image twice a second to know when it last changed; "live" means changed in the last
2 s. The logger asks for a frame on every change while the beam switch is off, and once when a View
becomes live. When no View is live there is simply no frame. When DM or the helper is not running, a
`camera_error` line with `source: dm_view` is written once, then it retries every minute.

The frame is saved as a 16-bit grey PNG in `frames_dm/`, the DM values rounded to integer counts
(DM's View image is usually float32 after gain normalisation), with the state and DM's tags embedded.
At the operator's usual binning (2 or 4) that is 0.5-2 MB per frame.

Installing the helper in DM (GMS 3.4 or newer with Python):

1. Copy `dm_helper/utem_view_server.py` to the PC (DM's plugin folder, or anywhere).
2. In DM: File > Open the file, choose Python as the language, Execute. The Output window shows
   `utem view server: listening on 127.0.0.1:47614`, and the script returns at once; the server keeps
   running in the background until DM is closed. No terminal and no window to keep open.
3. To have it start with DM: File > Install Script... on `dm_helper/utem_view_server_startup.s` as a
   **Library** script (library scripts run at every DM start). If the Output window then says it cannot
   launch Python from DM-script, install `utem_view_server.py` itself as a **Menu command** and click it
   once after DM opens.
4. Check from the logger's side: `python -m utem_logger --dm-status --config config.json` prints what
   the helper sees (the images DM has open and whether a View is live).
5. If the live View window is not named `...View...` on this GMS, set `VIEW_NAME` at the top of the
   helper to a word from its name (`--dm-status` lists the names).

If DM refuses DM calls from a background thread (an error in the Output window on the first request),
set `BLOCKING = True` at the top of the helper and leave the script running; it then holds the script
window but nothing else.

## Load

- **PC:** one PyJEM read per second, then it sleeps. It runs at below-normal Windows priority.
  - A frame costs one exposure and a PNG compression: about 0.15 s of one core at `png_level` 1, measured
    off the instrument.
  - A 2-minute mock run with a change (and a frame) every ~3 s used 9% of one core and 90 MB of memory.
    Without frames the loop is well under 1%.
- **Microscope:** one read is about 70 Get* queries to TEMCenter per second. This is read-only.
  - Panel knobs are handled by the microscope's own controller, so a read in flight should not make it
    miss a knob turn. That is inferred, not measured.
  - Check it once on the instrument (below). If reads turn out slow, shorten `pyjem.getters` or raise
    `poll_interval_s`.

## When things fail

The logger keeps running whatever fails, and records each failure once:

- **PyJEM not importable, TEMCenter not answering, or a read hanging:** a `pyjem_unavailable` line, then a
  reconnect every 30 s. A read that takes longer than 10 s is abandoned.
- **A getter that fails 20 polls in a row:** dropped until the next reconnect.
- **NAS unreachable:** writes go to `%LOCALAPPDATA%\utem-logger\spool` with the same layout. They are
  moved to the NAS when it is back (the JSONL is appended), and then logging continues there.
- **Camera missing or held by another program, pywinrt not installed and no camera server, DM or its
  helper not running:** frames from that source
  are skipped and retried every minute, one status line per outage. The state logging goes on.
- **A second copy:** it exits at once.

The logger's own log is `%LOCALAPPDATA%\utem-logger\logger.log`, rotating at 5 x 2 MB on the local disk.

## Environment

One conda environment for the logger, made once from `environment.yml` (Python 3.10: PyJEM lists 3.9
and 3.10 as supported, pywinrt needs 3.9 or newer, so the 3.8 `vjem38` environment cannot drive the
webcam in-process). Same steps as for `vjem38`, with PyJEM installed from JEOL's tarball:

```
conda env create -f environment.yml          # makes "utem-logger": numpy + pywinrt (requirements.txt)
conda activate utem-logger
pip install PyJEM-1.1.0.2305.tar.gz          # PyJEM is not on PyPI; the tarball used for vjem38
python -c "import PyJEM, winrt.windows.media.capture; print('ok')"
```

That is the whole setup. Afterwards nothing needs activating: the scheduled task (below) runs the
environment's `pythonw.exe` directly, and for a console check `conda activate utem-logger` is enough.
To use another name, add `-n <name>` to `conda env create` and pass `-Python <that env>\pythonw.exe` to
`install_task.ps1`.

If `pip install` of the PyJEM tarball fails on Python 3.10, fall back to two environments: keep the
logger in `vjem38` (it needs only numpy there; the tests pass on Python 3.8.10 with numpy 1.20.3) and
run the camera server from `utem-logger` (see "Camera in another environment" above);
`install_task.ps1 -Python <vjem38 pythonw.exe> -CameraPython <utem-logger pythonw.exe>` registers both.
`requirements.txt` alone (`pip install -r requirements.txt`) sets up any 3.9+ environment you made
yourself.

## Install on the microscope PC

1. Copy this folder to the PC, for example `C:\utem-logger`, and make the environment (above).
2. Copy `config.example.json` to `config.json`:
   - Set `log_root` to the NAS `data\logs`. A UNC path (`\\NAS\share\data\logs`) is safer than a
     drive letter.
3. Camera settings: set the camera up in `brio_gui.py`, close it, run
   `python -m utem_logger --camera-dump --config config.json`, then
   `python -m utem_logger --camera-test --config config.json` and look at `camera_test.png`.
4. First PyJEM check, in a console:
   ```
   cd C:\utem-logger
   python -m utem_logger --once --config config.json
   ```
   It prints one full state, any getter errors, and `poll_ms`, the time of one full read.
5. Run it in the console for a few minutes, `python -m utem_logger --config config.json`, then check the
   NAS folder. Ctrl+C stops it cleanly.
6. Install it as a scheduled task:
   `powershell -ExecutionPolicy Bypass -File install\install_task.ps1`
   (`-Python <pythonw.exe>` if the environment is not `utem-logger` under `%USERPROFILE%\.conda\envs`).
   - The task starts at log-on of the current user and restarts within a minute if it stops.
   - It uses `pythonw.exe`, so no console window.
   - It runs in the user session, like TEMCenter, so PyJEM, the camera and the NAS credentials are all
     available. The PC has to stay logged on, which is already the case for TEMCenter.
   - Check it with `Get-ScheduledTask "UTEM state logger"` and the `logger.log`. Stop it with
     `Stop-ScheduledTask`. Remove it with `Unregister-ScheduledTask "UTEM state logger"`.

   A Windows service (NSSM, for example) also works. A service runs in session 0, though, which may not
   reach TEMCenter, the camera or the NAS credentials, so the task is recommended.

## Checks to do once on the instrument

- [ ] `--once`: what is `poll_ms`? It should stay well under a second.
- [ ] With the logger running, turn one knob (brightness, for example) a counted number of clicks. Check
      that the final logged value matches the value with the logger stopped.
- [ ] Turn the beam switch on and confirm that `GUN3.GetBeamSw` reads 1 and frames appear.
- [ ] `--camera-test` with the beam on: the frame is not saturated, and the controls printed match the
      settings file (a control the camera refused is reported in `logger.log` at the first frame).
- [ ] Stage position units: confirm that a 10-unit tolerance is right.
- [ ] DM helper: `--dm-status` shows the View image while a View runs; a frame appears in `frames_dm/`
      on a knob change with the beam switch off; the operator sees no hiccup in the live view.

## Reading the logs

```python
from utem_logger.read import load_day, load_frame, frame_info, to_dataframe
day = r"\\NAS\share\data\logs\utem-state\2026\2026-10\2026-10-15"
recs = load_day(day)
frames = [r for r in recs if r["event"] == "frame"]
img = load_frame(day, frames[0]["file"])          # uint16 (1080, 1440, 3)
df = to_dataframe(recs)                           # pandas: one row per state line
```

## Testing off the instrument

```
python -m unittest discover -s tests -v           # scripted microscope, mock camera, the DM helper on a fake DM
python -m utem_logger --mock --config config.json --duration 60
```

`--mock` replaces PyJEM and the camera with a pretend microscope (the mock camera is a 10-bit
Thorlabs-like one; the BRIO path is tested with a fake grabber, in-process and through the server). It starts from the probe's values,
the operator turns knobs in bursts, and a green disc on the screen moves with beam shift.
