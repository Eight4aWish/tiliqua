# Lacuna

A 2D membrane mesh whose hole is the instrument.

```bash
cd gateware
pdm dsp build --dsp-core lacuna
```

| jack | |
|---|---|
| in0 | strike — rising edge above ~1 V |
| in1 | tension — 1 V/oct, 55–880 Hz |
| in2 | position — strike position, hub to rim |
| in3 | geometry — audio-rate modulation of the hole radius |
| out0 | mesh |
| encoder | short press cycles the preset; a 3 s hold still reboots |

Presets: full disc, narrow hole, wide ring, thin ring, square hole, slit ring.

## Why gateware

The boundary is a comparator, not an array. On a CPU the mask is 1024 elements
you rebuild when the shape changes, so shape is a control-rate parameter at
best; here it can change every sample, which makes geometry a modulation
destination. That is what in3 is for, and it is the one thing about this module
that cannot be done on the CPU-based modules in the same rack.

Measured: a preset change takes effect on the sample the CV arrives, 20.8 µs.

## Cost

| | |
|---|---|
| grid | 32×32, one node per cycle in raster order |
| cycles per sample | **1035** measured, against 1250 at 60 MHz / 48 kHz |
| state | 2 banks × 1024 × 24 bit = 48 kbit of 1008 |
| multipliers | one per node for tension, plus a few off the audio path |

Build at 48 kHz. At 192 kHz the budget is 312 cycles and this does not fit.

## Tension is pitch

For a membrane `c² = T/σ` and `λ = c·dt/dx`, so the scheme's `λ²` *is*
tension, and the stability limit `λ² ≤ 0.5` is the tension at which a wave
would cross more than a cell per sample. The membrane has a maximum tension
before it tears.

Tuning is derived, not calibrated. For a mode with discrete-Laplacian
eigenvalue μ:

```
f = (fs/2π)·arccos(1 + λ²μ/2)      →      λ² = 2(cos ω − 1)/μ
```

μ is a property of the masked domain, so each preset has its own, and folding
`1/−μ` into the table normalises pitch: a given CV is the same note on every
geometry. Without it the thin ring sits two and a half octaves above the full
disc. The normalised top end is capped by the preset with the smallest μ — the
full disc, at 903 Hz — which is why the range stops at 880.

Verified: octave ratios 2.0000 across the table, and λ² stays under 0.5 on
every preset.

## Three things that will bite

**The multiplier-free update is a special case.** At λ² = 0.5 the `2u` and
`−4λ²u` terms cancel. Below the limit that centre term is load-bearing —
scaling only the neighbour sum does not lower pitch, it piles energy up at
Nyquist. A nominal one-octave drop measured 10.5 kHz.

**Damping has to track pitch.** `loss_shift` is a per-sample decay, so at low
pitch a cycle spans far more samples and the mode is over-damped. Left fixed it
looks exactly like a tuning error, and was the entire apparent low-end pitch
error during development (+79% four octaves down) — not dispersion.

**Pipeline alignment.** A synchronous read gives data a cycle after the address
and the delay line registers it, so during cycle T the taps describe node
`j(T) − 2`. Every other signal is aligned with explicit delays of 4, 5 and 6
stages, and the tension multiply moved all of them. An off-by-one here computes
a mesh with the wrong topology and still runs, sounding plausible but wrong.

Related: strike and pickup radii follow the live geometry rather than being
fixed, because a pickup at a constant radius sits inside the hole on the
thin-ring preset and reads zero forever.

## Verification

`python test_lacuna.py` (standalone, no toolchain needed — `shims.py` stands in
for the tree's types). Checks the mesh bit-exact against a numpy reference at
two tension settings, that the tuning table is 1 V/oct and within the stability
limit on every preset, and that the scan fits the cycle budget.

**Untested on hardware.** In particular the in-tree ASQ path — standalone the
test runs with `ASQ = signed(16)`, so the fixed-point payload assignment in the
FSM's EMIT state is the first thing to check if the first build misbehaves.

## Not yet

- No video. The surface state is already in the memory the audio is read from,
  so drawing it should be nearly free. That is the next build, on the
  `vectorscope_no_soc` skeleton, and it is what makes the module distinctive.
- No feedback path. Per-sample injection of the module's own output would make
  the surface part of a patch rather than an endpoint.
- Single-node strike, not a blob. Cheaper, slightly harsher.
- No frequency-dependent damping, so all modes decay together.
- Geometry FM uses a hard mask; the discontinuity is broadband noise. An
  energy-conserving moving boundary is an open problem, not a coding task.
