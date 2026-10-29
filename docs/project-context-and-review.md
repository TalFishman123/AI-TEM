# AI TEM: project context and review

First review Oct 13, 2026. Updated Oct 14, 2026 against the code in this repo.

## Goal
Automate beam alignment on the AdQuanta UTEM (JEOL JEM-2100 Plus, Kaminer lab). Pipeline: digital twin -> calibrate twin from phosphor-screen camera measurements -> optimise on the twin (Jacobian / gradient descent / differentiable policy -> RL) -> fine-tune on the real microscope.

## Repo layout
- `utem-twin/`: the digital twin and the alignment work.
  - `utem_twin/`: `specs.py` (specs with S/V/D provenance), `column.py` (NumPy + TemGym Basic, lens-excitation solver), `diff.py` (differentiable JAX twin), `plot.py`.
  - `run_twin.py` (four reference modes -> `figures/`, `results.json`), `demo_diff.py` (calibration + policy demos -> `diff_results.json`).
  - `alignment/`: untrained camera-feedback aligners (`align.py`, `joint.py`, `twinloop.py`), the twin microscope and misalignment model (`microscope.py`), the hidden-truth scorecard (`truth.py`), the PyJEM driver draft (`pyjem_microscope.py`), benchmarks in `results/`. See `alignment/README.md` and `alignment/approaches.md`.
  - `visualizer/`: live beam visualizer plan (`PLAN.md`), backend prototype, HTML mockup.
- `probes/`: read-only probes of the instrument PC. `dm/probe_em_readonly.s` (DigitalMicrograph), `pyjem/probe_pyjem_readonly.py` and its first online output.
- `docs/`: this file and `operator-alignment-procedure.txt` (the operator's alignment steps, which `alignment/align.py` follows block by block).

Not in the repo: the literature review (`Measuring TEM Optics.md`) and the sim-to-real plan (`RL Alignment Sim-to-Real Plan`). Findings about them below are from the Oct 13 review and have not been rechecked.

## What exists now (Oct 14)
- **Twin.** `column.py` solves four modes (TEM x20k, diffraction L = 0.5 m, STEM probe spot 5, objective-off x500). `diff.py` traces the same column in JAX with every instrument parameter and operator knob differentiable. Added for alignment: CLA-aimed importance sampling with a Gaussian emission profile, gun deflectors referenced to the source plane, the OA plane at the true back focal plane, knobs `ht_wobble`, `IL_focus`, `pivot_shift`, `pivot_tilt`, per-spot CL1/CL2, a scattering test sample, adaptive splatting, `trace(record=True)` for per-plane rays.
- **Misalignment model and scorecard.** `microscope.MISALIGNMENT` draws source offset/tilt, condenser axis, CL3 and objective pre-field offsets, CLA offset, deflector scale/rotation, pivot errors, condenser astigmatism, camera rotation/scale and a random console state. `truth.py` scores the result on hidden quantities with tolerances in `TRUTH_TOL`.
- **Three aligners**, all untrained, camera-only, with the condenser aperture centred by the operator against a live marker. Benchmark on the twin, 20 random starts (seeds 100-119):

  | | procedure (`align.py`) | joint Gauss-Newton (`joint.py`) | twin in the loop (`twinloop.py`) |
  |---|---|---|---|
  | hidden truth passed | 20/20 | 20/20 | 18/20 |
  | frames, median | 1083 | 526 | 641 |
  | wall time per start | 3 min | 1 min | 20 min |

  `joint.py` is the best default. `twinloop.py` additionally fits the 24 misalignment parameters, which gives a physical diagnosis of the column. Of the candidate speed-ups, parametric fits in place of scans paid off; a stored Jacobian saved nothing on the twin.
- **Instrument access.** The PyJEM probe ran online on Oct 13 (PyJEM 1.1.0.2305, Python 3.8). All 20 curated reads worked and 55 of 70 getters returned values. The knob mapping is: beam shift = `Def3.CLA1`, beam tilt = `Def3.CLA2`, condenser stigmator = `Def3.CLs`, gun shift/tilt = `Def3.GunA1/GunA2`, pivots = `Def3.ShifBal/TiltBal`, brightness = `Lens3.CL3`. The instrument was at 200 kV, LOWMAG x150, beam blanked. `GunA1` read (0, 0) and needs checking that it is live. PyJEM 1.1 has no HT wobbler and no camera.
- **PyJEM driver** (`alignment/pyjem_microscope.py`) is a draft that has not been run on the instrument. The Set* names are assumed (`check()` verifies them), the DAC step sizes in `SCALE` are guesses (`calibrate()` measures them), and the camera frame has to come from a callback (DM or PyJEM detector).

## Oct 13 findings: status
1. **Missing files** (`specs.py`, `plot.py`, `run_twin.py`, package layout). Resolved: all present.
2. **Sim-to-real plan's "what the twin can/cannot simulate" table is stale.** Not rechecked: the plan isn't in the repo. The twin has moved on further since (see above), so it is staler now.
3. **Ray sampling bug in `diff.py`** (angles sampled in 2x the CLA acceptance regardless of source position, so `gun_semi_angle` had no effect and gun tilt walked the bundle off the CLA). Fixed in the way proposed: with the CLA in, rays are aimed at uniform points of the aperture (`_aim_at_cla`) and weighted by a Gaussian emission profile (`gun_angular_sigma_mrad`) inside a soft gun cone, so `w.mean()` is the transmitted fraction of the emitted current and gun tilt changes the current. Remaining:
   - The emission width that now matters is `gun_angular_sigma_mrad` = 0.5 mrad, a DEFAULT with no source. It sets how sharply current peaks with gun tilt, which is what alignment step 2 maximises. Measure it (current vs gun tilt) before trusting rung 4.
   - `gun_semi_angle_mrad` (5 mrad) is 10 sigma out, so it still has practically no effect or gradient.
   - With the CLA out or `aim_cla=False`, the old cone sampling (sized from the first mode) is still the fallback.
4. **Demo 1 is an inverse crime.** Still open: `demo_diff.py` renders the "real" frames with the same twin and the same ray samples it fits with, and the loss is still image MSE. The alignment benchmarks are better: the aligners are model-free, and `twinloop.py` fits a 4096-ray model to a noisy 16384-ray instrument. But the misalignment family and the truth are still the same model, so the twin-in-the-loop fit quality (weighted rms 1.5-4) is optimistic for the real column.
5. **One camera rotation/scale shared across modes.** Still open: `params["camera"]` is a single rotation/scale for all modes, and magnetic image rotation is not modelled. On the instrument image rotation differs per mode and magnification. The aligners measure their own Jacobians, so they tolerate it, but `identify.py` and any calibration fit do not.
6. **Poisson NLL, staged calibration, Laplace posterior, Fisher-information experiment design.** Partly: `identify.py` fits features (centroid, rms size, ellipticity, current) with priors instead of raw images, which is a form of staging. No Poisson likelihood, posterior or designed probe set yet; `twinloop.py`'s 28 + 11 probe frames are hand-chosen.
7. **Laser pointing drift = source-offset drift.** Partly: source offset is randomised per start, but not drifting within a run.

## Lit review (Oct 13, not rechecked)
- Strong: LOCO/response-matrix framing, lumped per-mode transfer matrices before physical lens positions, posterior -> domain randomisation, calibration sequence, PINEM-sideband Cc idea.
- Reference numbering broken: in-text numbers are the original IDs; markdown auto-renumbered the list after inserting 20b, 33b, 33c. Text [n] = list n for n<=20, n+1 for 21-33, n+3 for 34+. Use explicit labels.
- Mis-citations even after offset: main point 4 "JEOL column drawing [31]" -> Vilsmeier (identifiability) paper; table "spot-size mapping [11]" -> Kalinin aberration BO; section 8 "grid homography [37]" -> detector DQE paper; section 3 "[27] lens and stage hysteresis" -> van Hulst 2026 is stage positioning.
- Spot-checked existence (OK): arXiv 2607.29411 (Pfeifer), 2603.16549 (van Hulst VAE-EM), 2608.03669 (van Hulst positioning), 2601.18972 (Pratiush/Kalinin), 2403.08538 (Weber).
- "Screen camera enough? Yes" is optimistic for Thon rings / Zemlin tableau / Ronchigram: phosphor grain + camera through the viewing window probably can't resolve them. Test on real footage early; otherwise these belong to the Gatan-camera phase.
- For RL rungs 1-3 only steps 1 (camera), 2 (mag/rotation) and 4 (deflector/stigmator response) of the calibration sequence matter; Cs/Cc/source can wait.
- Condenser stigmator (rung 2) is calibrated from spot ellipticity, not Thon rings (that's the objective stigmator). The aligners do it this way.
- Gaps: classic camera-based TEM autotuning/autoalignment literature (Delft/Philips late 1980s-90s, Krivanek & Fan slow-scan CCD control) and vendor auto-functions; specimen-free beam-only calibration; model-discrepancy (Kennedy-O'Hagan) and optimal experiment design.

## Open questions
- **Priority task.** Illumination alignment (the operator procedure) is now the de facto first task. Confirm.
- **Mode.** Aligners cover TEM (MAG1) + diffraction. Objective-off (the group's EELS/PINEM mode) is not covered.
- **Voltage.** The instrument read 200 kV on Oct 13 and the twin defaults to 200 kV. Confirm which voltage the group aligns at (often 80 kV).
- **Tolerances.** Numeric "aligned" tolerances now exist in the twin (`align.TOL`, `truth.TRUTH_TOL`) but were chosen in the twin. Confirm them with the operators.
- **Operator logs.** Still none.
- **Camera.** Which camera feeds `frame()` (DM or the PyJEM detector module), and its frame rate. A streaming camera would enable lock-in probing (approach 6).

## Next steps on the instrument
1. Run `PyJEMMicroscope.check()` to confirm the Set* names; confirm `GunA1` is live.
2. Wire a camera frame callback; run `calibrate()` per knob to replace the `SCALE` guesses.
3. Measure current vs gun tilt to set `gun_angular_sigma_mrad`, and image rotation per mode/mag.
4. First supervised run of `joint.py` on the instrument.
