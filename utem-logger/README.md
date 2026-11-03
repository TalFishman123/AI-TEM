# UTEM state logger — repair package, 2026-10-29

## Components and startup

- TEMCenter provides microscope access through PyJEM. Get* calls only; no settings are changed.
- DigitalMicrograph (DM) controls the Orius camera. The helper copies an already open Orius image;
  it does not start a camera acquisition. Open the live View manually when required.
- The instrument's existing start_utem_logger.bat (not tracked in this repository) starts the state logger in vjem38 and the BRIO server in utem-logger.
  It does not start TEMCenter, DM, or the helper inside DM.
- Keep the existing environments. PyJEM installation in Python 3.10 failed because the supplied
  package lacked Py310/TEM3.pyd; the two-environment setup avoids that installation path.

## Sampling and output

config.py and config.example.json select a 30-second polling interval.
For an existing installation, set poll_interval_s to 30.0 in its local config.json; a local
override of 1.0 would otherwise keep one-second polling. Keep site-specific settings local. An initial full read contains
89 getters; ordinary polls contain 26 fast getters plus up to 5 rotating getters. The remaining
63 getters take 13 ordinary polls to refresh (about 6.5 minutes, longer during delays).
A poll is sequential, not an instantaneous microscope snapshot. Transient changes between samples
may be missed. This is change logging, not a guaranteed frame or record every 30 seconds.

On a detected setting change the logger writes state.jsonl and requests eligible camera frames:
- BRIO: beam switch on, beam unblanked, screen down (0 or 1).
- DM Orius image: beam unblanked, screen up (2), matching image window open.
Unknown condition getters are ignored by the existing frame rules and produce a warning.
A full-state heartbeat is written every 300 seconds without changes.

Data is written under \\UTEM-NAS\data\logs\utem-state, in year/month/day folders:
state.jsonl, frames/ (BRIO), and frames_dm/ (DM). If NAS writes fail, local spool storage is used
and later moved to NAS. Low free space pauses frames, not state polling.
Service diagnostics: %LOCALAPPDATA%\utem-logger\logger.log (and rotated logger.log.1 etc.).

## PyJEM timeout repair

Ordinary polls wait up to 60 seconds. The initial full read has a scaled limit (about 232 seconds
with the supplied configuration). On timeout, the logger retains the outstanding future and pool.
It never starts a second read or reconnect while that call remains active. Once the late read
finishes, its state is discarded so it is not labelled as a current measurement. A fresh read is
made on a later retry using the existing connection. Completed failed reads may reconnect.
Retry delays start at failure completion. The first failure of each getter is logged at WARNING.
VACUUM3.GetSpecimenPreEvacStart is excluded after instrument error 25610 and multi-second delays;
this is diagnostic isolation, not proof that it caused the beeps.

A Python thread cannot forcibly stop a stuck native PyJEM call. If it never returns, the source
remains unavailable and shutdown can remain pending. Do not repeatedly launch more copies;
inspect the process and diagnostics before restarting the logger. Hard recovery through an
isolated child process is outside this patch.

## DM helper 0.4.2

Run dm_helper/utem_view_server.py inside DM's Python script window for a manual start.
BLOCKING=True uses that script execution thread and keeps the script window busy.
Stop it before running another Python script. Production code does not use frame listeners.

For automatic startup, place the Python helper in DM's plugin directory and install ONE launcher:
utem_view_server_launch.s OR the compatible utem_view_server_startup.s. Both contain the same
threaded launcher with a 90-second delay. If an older launcher is already installed, replace/remove
that installed copy using DM's plugin management; copying a source file alone does not reinstall it.
If threaded DM calls do not work on this instrument, use the Python script window manually;
do not install a non-threaded never-ending helper as a startup Library script.

QUIET_START now suppresses DM access at launch (including DM.Result, version and image queries).
It defers these until the first external request, and skips the automatic self-test that would
otherwise issue its own request. It is not a guarantee that DM is ready for an early logger request.
Self-test thread logging uses print rather than calling DM.Result from another thread.

The helper's live flag means a matching Orius window is OPEN, not that acquisition is running.
A stopped View can supply its last image. same_as_last is a comparison of sampled statistics,
not an exact pixel comparison or proof of frame freshness. Keep this distinction in twin analysis.

## BRIO settings

Keep your local camera_settings.json. The uploaded instrument settings requested
640x480 @ 30 fps YUY2; this camera stream
rate is separate from the logger's 30-second state polling. --camera-test and --camera-dump remain
available. Keep camera GUIs that hold the BRIO closed while its server is running.

## Validation and installation

See INSTALL_REPAIRS.txt. Local simulated tests cover timeouts, retained
workers, late results, sequential recovery, retry timing, and deferred DM startup. No hardware
validation has been performed, and this patch does not establish the cause of the beeps.
