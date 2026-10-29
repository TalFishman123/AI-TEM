# Checklist: from the twin to the real UTEM

The twin uses JEOL catalogue specs and defaults found online, and the lab's UTEM differs from them. The camera-feedback aligners (`alignment/align.py`, `alignment/joint.py`) measure every gain on the instrument, so they don't depend on those specs. What does depend on them: the probing step sizes, the camera tolerances, the twin-in-the-loop aligner (`twinloop.py`) and all the benchmark numbers. This checklist measures what the twin guessed, in that order, before any aligner moves the real beam.

Paths are relative to `utem-twin/` unless they start with `probes/` or `docs/`.

## 1. Before the session (desk work)

- [ ] **Write down what is known to differ** from [SPECS.md](../utem-twin/SPECS.md): pole piece, condenser and objective apertures fitted, camera on the phosphor screen, emitter and how it is driven, the high voltage (HT) used for alignment. Every row tagged **D** is a guess.
- [x] **Python on the microscope PC.** PyJEM runs in Python 3.8.10 (probe of 2026-10-13). The code that runs on the instrument needs only NumPy and is 3.8-compatible (the two dict `|` merges are gone).
- [x] **Instrument entry point.** `python -m alignment.run_instrument check|noise|calibrate|align` (from `utem-twin/`). Each command that moves something says what it will do, waits for Enter, and offers to put every knob back on an error or Ctrl+C. Results go to `alignment/results/instrument/`.
- [x] **Camera.** The webcam on the viewing window (`alignment/webcam.py`), used with `--webcam <config>`. For now the room lights are off: one lights-off, no-beam frame is subtracted from every frame, the oblique view is mapped to the flat screen in mm, and only the screen disc is kept. Needs OpenCV (`pip install opencv-python`) in the PyJEM Python.
- [x] **Robustness run on the twin** (done by the team). Benchmark `joint.py` with the "instrument" twin perturbed (lens positions, pole piece, CLA size, source size, deflector gains off by 2 to 10x, low dose) while the aligner's assumptions stay nominal. This shows which mismatches matter before beam time.

## 2. At the microscope: read-only

- [ ] Run `python probes/pyjem/probe_pyjem_readonly.py`, turn one knob a little, run it again, and confirm the matching value changed. Do this for beam shift, beam tilt, gun shift, gun tilt, condenser stigmator, the two pivot balances, and brightness (CL3).
- [x] **Confirm gun shift is live** (done; `Def3.GetGunA1` had read (0, 0) on 2026-10-13).
- [ ] `python -m alignment.run_instrument check`: lists the Set* handles this PyJEM lacks and prints every knob. Nothing moves.
- [ ] **Webcam setup** (once, and again if the webcam moves): record a lights-off, no-beam frame and a frame with the screen edge and ticks visible (`grab_screen_camera.m`, MODE = 'beam'), then `python -m alignment.webcam_setup --background bg.png --geometry lit.png --tick-mm <spacing> --exposure <value>` and click the screen edge and every tick.

## 3. At the microscope: small, supervised moves

Objective aperture out, beam on the screen, an operator at the console.

- [ ] **Signal and noise** (first: calibration uses it). `python -m alignment.run_instrument noise --webcam <config>` takes 20 frames of a still beam (nothing moves, the beam is never blanked) and reports the jitter and drift of the centroid, size, ellipticity and current, how many frames to average for each tolerance, and the camera gain.
- [ ] **Step sizes.** `python -m alignment.run_instrument calibrate --frame-fn ... --noise <noise json>` steps every knob on both axes, finds a step that is clearly above the noise without losing the beam (at most `--max-dac` counts from the start), measures +step, -step and the return, and puts the knob back. It reports each knob's response matrix, linearity, hysteresis and the camera rotation, and writes the suggested `SCALE`; pass it to later commands with `--scale <calibrate json>`. Pivot trims are measured through the wobble they cancel. `IL_focus` is left out by default: the driver reads `GetIL1` but writes `SetDiffFocus`; confirm that pair, then add `--knobs IL_focus`.
- [ ] **Camera geometry.** `calibrate` gives the camera rotation against the beam-shift and beam-tilt axes. Still by hand: the scale (µm per pixel at the sample, mrad per pixel in diffraction) at each magnification or camera length used, and any perspective distortion from viewing the screen at an angle.
- [ ] **Tolerances.** Convert `TOL` in `align.py` (camera pixels, set on the twin) to physical units using that scale, and agree with the operators what counts as "aligned". Until then, `noise --tol-scale` (twin px per camera px) gives the frame-averaging verdict.
- [ ] **Source.** Record the spot 1 → 5 behaviour and the current vs gun tilt curve. The width of that curve replaces `gun_angular_sigma_mrad` (a 0.5 mrad guess). In photoemission, also check how much the source moves with laser pointing.
- [ ] **Procedure assumptions.** Compare the HT-wobbler signal in image mode with the beam tilt read in diffraction mode. The script uses diffraction mode only because the wobbler signal was weak in the twin; if the real wobbler is clear, swap the residual in `Aligner.beam_tilt`.

## 4. First supervised alignment

- [ ] `python -m alignment.run_instrument align --frame-fn ... --noise <noise json> --scale <calibrate json>`, with the operator centring the condenser aperture against the marker and the console in reach.
- [ ] The run's log, knob values and cost are saved in `alignment/results/instrument/`. Compare the frame count and end state with the twin benchmark (median 526 frames).

## 5. After the session: make the twin match

- [ ] Fit the twin to the session's frames: per-mode lumped transfer matrices first, physical lens positions later, with SPECS.md values only as starting guesses.
- [ ] Update every **D** row in SPECS.md that was measured, with its value and the date.
- [ ] Rerun the twin benchmarks with the fitted twin. Only after this are `twinloop.py`'s diagnosis and any RL training meaningful for the real column.
