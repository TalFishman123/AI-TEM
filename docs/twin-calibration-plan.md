# UTEM Twin Calibration Plan

AdQuanta UTEM (JEOL JEM-2100 Plus) · digital twin `utem-twin` · draft for expert review · October 2026

## 1. Twin parameters to calibrate

The twin has about 120 unknown parameters. Knob settings (DAC values) and coil-balance settings are **read from PyJEM**, so they are known inputs, not unknowns. The calibration fits how the column responds to them.

| Group | # | What it is | Examples | Found in |
|---|---|---|---|---|
| Element heights | ~20 | Position of each lens, aperture and deflector along the column | CL1 at 0.86 m, PL at 0.27 m above the screen (current estimates) | Prior from a column drawing; refined last, jointly with E6, E9 |
| Lens strengths | 11 | Strength of each lens, as a curve vs its DAC value (2–3 constants per lens) | CL3 focusing power vs brightness DAC; IL1 power across magnifications | E1 (DAC table) + E5, E6, E7, E9 |
| Lens rotation | per lens | Image rotation from magnetic lenses (**not yet in the twin**) | Image rotation between two magnification steps | E6, E9 |
| Lens offsets | 22 | Lateral misalignment (x, y) of each lens | CL3 offset; objective pre-field offset | E3, E5, E7 |
| Deflectors | 32 | DAC → shift (µm) / tilt (mrad) matrices, cross-talk, pivots | Beam-shift gain and rotation; shift→tilt leakage | E3, E4 |
| Stigmators | 12 | Stigmator gain matrices and intrinsic astigmatism | Condenser stigmator gain; objective astigmatism at zero setting | E5, E7 |
| Apertures | 9 | Radius and lateral offset | Condenser aperture offset from the axis | E5 |
| Source and gun | 7 | Source size, emission cone, gun offset and tilt | Gun tilt that gives maximum current | E4, E7 |
| Camera | 7 | Webcam-on-screen mapping, blur, gain, background | Webcam pixels → screen mm; screen rotation | E2 |
| Aberrations | 3–4 | Spherical aberration, measured per lens: objective Cs (matters for TEM) and probe-forming condenser Cs (matters for STEM); chromatic (Cc, not yet in the twin) | Objective Cs; condenser Cs | E10a, E10b, E8 (all optional) |

Notes: heights and lens strengths trade off against each other, so they are always fitted together, never alone. Parameters the data cannot pin down keep their prior value with a wide uncertainty, which later sets the randomization range for the AI agent.

<div style="page-break-after: always;"></div>

## 2. Calibration experiments

Ten scripts, each started by the operator, ordered from most to least important. Every script normalizes the lenses first, logs the full PyJEM state with every frame, and restores all knobs at the end. Frames come from a webcam imaging the fluorescent screen.

Samples: **A** = holey amorphous carbon on a Cu grid; **B** = polycrystalline Au standard (ideally a cross-grating replica with Au).

| # | Experiment | Parameters found | Sample | Mode and frames |
|---|---|---|---|---|
| E1 | Lens table | Lens DACs used at each console setting | None (beam blanked) | Step through every mag, camera length, spot and α; read lens DACs only |
| E2 | Webcam + baseline | Camera mapping, noise, drift rate, restore check | A, hole edge / hole | Spread beam; exposure series; ~20 frames over minutes at fixed settings |
| E3 | Deflectors | Beam shift/tilt and image-shift gains, rotation, cross-talk, pivots | A, in a hole | Small ± steps of each deflector in TEM mode, diffraction mode and at a known defocus |
| E4 | Gun | Gun offset and tilt, emission cone width | A, in a hole | Gun-tilt raster (total brightness); spot 1 ↔ 5 position vs gun shift |
| E5 | Condenser | CL1–3 strengths, condenser axis, aperture offset, condenser stigmator, convergence angle | A, in a hole | Brightness (CL3) sweep; spot 1–5; spread-beam disc vs focused spot; stigmator steps; diffraction-disc size per setting |
| E6 | Magnification & rotation | Projector strengths, image rotation per setting, image-shift gain | A, grid bars (or B cross-grating) | One image per magnification setting; image-shift steps |
| E7 | Objective | Objective strength vs DAC, objective stigmator, source size | A, carbon film | High mag; focus series at large defocus (Thon rings); stigmator steps; Fresnel fringes. Starts with a check that rings are visible |
| E8 | Cc (optional) | Chromatic aberration | A, carbon film | Focus or image shift vs small HT offset with a tilted beam (or PINEM sidebands) |
| E9 | Diffraction | Camera length, diffraction rotation, distortion | B | One ring pattern per camera length + one image of the same area |
| E10a | Objective Cs (stretch) | Spherical aberration of the objective lens, important for TEM | A, carbon film | TEM, high mag: diffractograms (Fourier transforms of images of the amorphous film) at several defocus values; the Thon-ring radii give defocus and Cs |
| E10b | Condenser Cs (stretch) | Spherical aberration of the probe-forming condenser, important for STEM | A, carbon film | STEM, probe focused on the film, diffraction plane on the screen: Ronchigram; the size of its central "sweet spot" against defocus gives Cs |

Cs is measured lens by lens, following the expert review: the objective by the diffractogram method (the simplest for TEM), the probe-forming condenser by the Ronchigram (the usual method for STEM). Both are stretch goals because they need the webcam to resolve Thon rings or Ronchigram detail. The twin currently has a Cs only in the objective, so a condenser Cs must be added to it before E10b can be fitted.

After the experiments: one joint fit of all data in the twin, with an identifiability check. Per-session check: a short E3 subset. Re-run E4 (and the source part of E7) after laser or gun work.

Deliberately out of scope for now: lens hysteresis (future), multi-voltage tests (200 kV only), Si-fringe high-mag calibration, 4D-STEM methods.

<div style="page-break-after: always;"></div>

## 3. Open questions

**For us (lab facts)**

1. Webcam: what resolution and frame rate? Can auto-exposure be locked? Does it see the whole screen?
2. Which source do we calibrate with, LaB6 or laser photoemission?
3. Do we have a holey carbon grid and a polycrystalline Au standard?
4. Which magnifications, camera lengths and spot sizes do we actually use?

**For the TEM experts**

5. What brightness and dwell limits keep a focused spot from burning the screen?
6. Can PyJEM write the gun, deflectors, CL3 and focus, and can it run lens normalization, HT offset/wobble and mode switching?
7. Is the condenser aperture motorized or manual?
8. Are Thon rings at large defocus visible on the phosphor screen?
9. Check each experiment's calibration procedure, and tell us if any standard calibration is missing.
10. What are the typical settling times after changing a knob, lens or mode? And what is the typical drift rate, i.e. how long can a sweep run before drift matters?
