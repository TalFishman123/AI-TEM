# Checks to run in DM, once, with a live View running

Three scripts, each a minute. Run each from DM's script window (File > Open, pick the language shown,
Execute) with the View window in front, and copy the Output window text into the project thread.

| Script | Language | What it decides |
|---|---|---|
| `check_frame_listener.py` | Python | Option 1: does a data-changed listener fire on every new frame? Then the helper records the last frame's time directly instead of sampling pixels. |
| `check_view_tags.s` | DM-script | Option 2: does the View image carry a per-frame counter or timestamp in its tags? It prints the whole tag tree twice; anything that ticks between the prints is usable. |
| `check_python_thread.py` | Python | Whether the helper's background thread can call DM. If not, set `BLOCKING = True` in `utem_view_server.py`. |

What is verified and what is not:

- The listener API in `check_frame_listener.py` (`DM.Py_ScriptObject`, `HandleDataChangedEvent`,
  `ImageHandleDataChangedEvent`) is taken from Gatan's own published Python examples
  ([Live Simple Copy.py](https://www.gatan.com/sites/default/files/Scripts/Live%20Simple%20Copy.py),
  [Live Radial Profile.py](https://www.gatan.com/sites/default/files/Scripts/Live%20Radial%20Profile.py)),
  which update a copy of the live image on every frame this way. Gatan warns there that GMS 3.5.0 and
  3.5.1 crash on listeners; the script checks the version first.
- No tag names of the live View image were verified against Gatan documentation (none is public),
  which is why `check_view_tags.s` prints every tag instead of looking for guessed names. The tag
  names the helper reads for exposure and binning (`Acquisition:Parameters:High Level:...`) are also
  unverified: the printed tree shows the real ones, and `TAG_KEYS` in `utem_view_server.py` is
  edited to match.
- The DM-script tag-iteration calls in `check_view_tags.s` and the thread behaviour tested by
  `check_python_thread.py` are from memory of the DM-script and GMS Python APIs, not from documentation.
  A wrong name there fails at compile time with a clear message, not silently.

Two facts from Gatan's "Run a Python script" page that matter for the helper: only one Python script
can run in DM at a time (so the helper must return and keep working from its background thread and
listeners, as Gatan's own live examples do; `BLOCKING = True` would stop anyone else running Python in
DM), and scripts run on a background thread by default (the checkbox at the bottom of the script
window), which is what the helper expects.

Also useful, from the logger's side once the helper runs: `python -m utem_logger --dm-status --config config.json`
lists the images DM has open (to set `VIEW_NAME` if the View window is not called `...View...`).

Until these are done the helper uses pixel sampling to tell whether the View is live, which needs none
of the above.
