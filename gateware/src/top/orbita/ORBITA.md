# Orbita

The membrane as a wavetable, not as a drum.

```bash
cd gateware
AMARANTH_nextpnr_opts="--timing-allow-fail --seed 6" \
    pdm orbita build --modeline 1280x720p60
pdm flash archive build/orbita-r5/orbita-<tag>-r5.tar.gz --slot <n>
```

| jack | |
|---|---|
| in0 | drive — a gate edge plucks; a held level keeps it alive as a drone |
| in1 | pitch — 1 V/oct, 0 V is 55 Hz, eight octaves to 7040 Hz |
| in2 | radius — the scan circle, inner edge to outer edge, 256 steps |
| in3 | geometry — audio-rate modulation of the hole radius |
| out0 | scan |
| GPDI | the membrane, the scan circle, and the circle unrolled |
| encoder | short press cycles the preset; a 3 s hold still reboots |

Same eight presets and the same membrane as
[LACUNA](../lacuna/LACUNA.md) — [`mesh.py`](../lacuna/mesh.py) is shared. The
difference is entirely in how it is driven.

## What it is

LACUNA listens to the mesh: the membrane vibrates at audio rate and a pickup
node is the output. ORBITA does the opposite. The membrane evolves *slowly* —
one update every 64 audio samples — and a closed circular path through it is
read at audio rate. The path's values are one cycle of a waveform and the scan
rate is the pitch, so timbre and pitch are independent and a held note morphs.

This is Verplank/Mathews/Shaw scanned synthesis. The direct ancestor is
[`daisy_scanned`][ds], a 64-mass ring running on a Daisy in the same rack.

[ds]: https://github.com/Eight4aWish/eurorack_daisy_patch_init/tree/main/daisy_scanned

## Why a circle

A line across the membrane has two ends, and the wrap from the last sample back
to the first is a step that buzzes once per cycle. Closing it means mirroring —
reading out and back — which halves the useful resolution and imposes a symmetry
on every waveform. A circle has no seam.

It is also exactly the Daisy engine's ring, except each point is coupled
*radially* into a membrane as well as to its neighbours, so energy leaves the
scan path and comes back. That coupling is the entire argument for doing this on
an FPGA rather than on the Daisy that already does the 1D version.

At radius 10 the circumference is about 63 cells, so 64 points is close to one
per cell — the same table length as the Daisy ring, and not a coincidence.

## What the hole does

A concentric circle never crosses a concentric hole, so there is no flat segment
and no duty cycle. Instead the annulus is a window the radius sweeps through,
and the asymmetric presets are the interesting ones, because a concentric circle
*does* cross those:

| preset | what the scan meets |
|---|---|
| solid heads | nothing — smooth, close to a sine |
| rings | nothing; the annulus is just a narrower window |
| **slit ring** | the slit, once per revolution — one notch, full harmonic series |
| **square hole** | four corners — four notches, emphasising the fourth harmonic |

So the symmetry order of the hole picks which harmonics the scan emphasises.
That is a more structural relationship between geometry and timbre than a duty
cycle, and it is audible: the slit is the bright one.

## Three things that make or break it

**The whole membrane must be sub-audio, not just its fundamental.** The highest
spatial mode is about 17× the fundamental, so a fundamental at 8 Hz put the
cell-to-cell checkerboard at 143 Hz — squarely audible, and heard as noise on
top of the scanned tone.

| F_EVOLVE | fundamental | 10th mode | checkerboard |
|---|---|---|---|
| 8 Hz | 8 Hz | 23 Hz | **143 Hz** |
| 1 Hz | 1 Hz | 2.9 Hz | **17 Hz** |

`F_EVOLVE = 1.0`. A held note then morphs over about a second, which is what
scanned synthesis is for. Faster and you are listening to the membrane rather
than to the shape it makes.

**A single-cell strike is a spatial white-noise generator.** ORBITA reads the
membrane's *shape*, so roughness in space is noise in the waveform. Measured by
neighbouring cells agreeing in sign, where 50% is white noise:

| mallet radius | drive every | agreement |
|---|---|---|
| 0 (one cell) | 1 update | 61% |
| 2 | 1 | 88% |
| **3** | **1** | **92%** |

A frequency-dependent damping term was tried first, on the theory that uniform
damping was letting the checkerboard survive. Measured, it changed 62% to 64%
and made the output waveform *rougher*. It was removed. The excitation was the
problem, not the damping.

**Drive continuously, not in bursts.** Driving one update in eight measured
slightly smoother (96%), but at a 750 Hz update rate that is a kick every
93.75 Hz — a periodic step in the wavetable, right in the audio band.

## Between the cells, not on them

The scan carries its position in Q4 cells and blends the four cells around it,
rather than snapping to the nearest. That matters more than it sounds: on a
perfectly smooth field, nearest-cell addressing measures **0.14** roughness
against **0.039** for bilinear, and on the real thing it measured **0.579**.
After bilinear the same test gives **0.029–0.094**. It was the difference
between an instrument that wanted reverb over it and one that does not.

The angle is interpolated between adjacent ROM entries too, so the scan
position moves continuously rather than stepping 64 times a cycle. And since
the circle no longer has to land on a cell, the radius CV gets 256 steps across
the membrane instead of 16.

## Cost

Whole build: 4039 LUT (16%), 10 BRAM, **19 of 28 multipliers**, both PLLs.
Sync closes at 69.07 MHz, dvi at 80.04, dvi5x at 471.03 — on seed 4. The
multipliers are the tightest resource, and bilinear sampling took six of them:
two to interpolate the angle, two to scale it by the radius, and three to blend
(one shared). A stereo pair of scan circles would need three more.

## Verification

`python test_orbita.py`. Checks the circle ROM is actually circular, that a
pluck makes sound where silence preceded it, that the radius sweep behaves
across the membrane, and that a held drive sustains rather than decaying.

The membrane itself is covered by `test_lacuna.py`, which stays bit-exact —
`mesh.py` is shared, so a change for ORBITA that breaks LACUNA's arithmetic
fails there.

**Neither test checks timing**, and the shared mesh has broken LACUNA's timing
twice from ORBITA-side changes. Build both after touching `mesh.py`.

## Limitations

- **λ² is a per-preset constant**, so there is no tension control. All four
  jacks are spoken for and this is the parameter that lost.
- **The strike always enters at the inner edge.** The mesh supports a strike
  position and ORBITA does not drive it, for the same reason.
- **Sixty-four points per revolution.** Above roughly 2 kHz the table's own
  harmonics begin to fold. Audible as character rather than as a fault, but it
  is there.
- **19 of 28 multipliers used.** That is the resource that will run out first,
  and it is what makes stereo a real cost rather than a free addition.

## Where it goes next

- **Stereo.** Two scan circles at different radii are two genuinely different
  wavetables — unlike two points on the same circle, which is only a phase
  offset and sums to mono. Costs four more reads, three multipliers and about
  seven states.
- **An offset scan circle** would cross a *concentric* hole, recovering the
  flat-segment behaviour on every preset rather than only on the slit and the
  square. Hole radius would become duty cycle.
- **Tension on a control**, if a fifth input can be found — a second encoder
  page, or trading geometry for it.
- **A larger mesh.** 64×64 quadruples the state and the scan, both of which
  fit, and would put far more distinct modes inside the audio band.
