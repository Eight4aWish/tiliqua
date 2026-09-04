# Lacuna

A 2D membrane mesh whose hole is the instrument.

```bash
cd gateware
AMARANTH_nextpnr_opts="--timing-allow-fail --seed 2" \
    pdm lacuna build --modeline 1280x720p60
pdm flash archive build/lacuna-r5/lacuna-<tag>-r5.tar.gz --slot <n>
```

| jack | |
|---|---|
| in0 | strike — rising edge above ~1 V |
| in1 | tension — 1 V/oct, 55–880 Hz |
| in2 | position — strike position, hub to rim |
| in3 | geometry — audio-rate modulation of the hole radius |
| out0 | mesh |
| GPDI | the membrane, drawn live |
| encoder | short press cycles the preset; a 3 s hold still reboots |

Eight presets: three solid drum heads (r14, r10, r7), then narrow hole, wide
ring, thin ring, square hole, slit ring.

The membrane itself lives in [`mesh.py`](mesh.py), shared with
[ORBITA](../orbita/ORBITA.md), which drives the same mesh a thousand times
slower and reads a circle through it as a wavetable.

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
| cycles per sample | **1037** measured, against 1250 at 60 MHz / 48 kHz |
| state | 2 banks × 1024 × 24 bit = 48 kbit of 1008 |
| with video | 3157 LUT (12%), 7 BRAM, 12 multipliers, both PLLs |

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
disc.

Verified: octave ratios 2.0000 across the table, and λ² stays under 0.5 on
every preset.

## The video

No framebuffer, no PSRAM, no CPU. The audio scan already carries every node
past one point once per sample, so a second narrow memory written from the same
address and strobe is a free 32×32 snapshot; the top level colours each pixel
from it a few microseconds before it goes down the cable, the way `beamrace`
does. Costs about 620 LUTs, one BRAM and the second PLL.

Blue and red are the two signs of displacement, so what you see is the mode
pattern rather than a brightness envelope — which matters, because mode beating
reads as the pattern *precessing*, and a waveform display cannot show that at
all. The strike is marked in green and the pickup in amber: both move with in2
and with the geometry underneath them, and they are otherwise invisible
controls.

## Six things that bit

**The multiplier-free update is a special case.** At λ² = 0.5 the `2u` and
`−4λ²u` terms cancel. Below the limit that centre term is load-bearing —
scaling only the neighbour sum does not lower pitch, it piles energy up at
Nyquist. A nominal one-octave drop measured 10.5 kHz.

**Damping has to track pitch.** `loss_shift` is a per-sample decay, so at low
pitch a cycle spans far more samples and the mode is over-damped. Left fixed it
looks exactly like a tuning error, and was the entire apparent low-end pitch
error during development (+79% four octaves down) — not dispersion. It tracks at
half a shift per octave; at a full shift the bottom octave rang for 2.7 s
against 0.34 s at the top and dominated everything.

**The loss term is a shift, so it stops working.** Below `|u| < 2**loss_shift`
the shift yields zero and decay stops dead. At one point that floor sat at
−30 dBFS against a −24 dBFS peak: the tail fell 6 dB and froze. It made a
gorgeous sustained drone entirely by accident, and it was still a bug.

**`inner == 0` did not mean solid.** The hole test is `d2 > inner²`, which at
inner 0 excludes the single cell at dead centre — a pinhole through the
fundamental's antinode, pulling the disc nearly three semitones sharp and
flattening its mode ratios. A solid head measures 1.00 / 1.59 / 2.13, the
textbook circular membrane; the pinholed one did not.

**Controls must scale across the geometry, never absolute cells.** The strike
position was `inner + 1 + cv`, clamped at the rim — so on the wide ring only a
third of the CV range moved anything and on the thin ring one step of it did.
It now spans the available radius, and tracks as in3 opens the hole.

**Pipeline alignment.** A synchronous read gives data a cycle after the address
and the delay line registers it, so during cycle T the taps describe node
`j(T) − 2`. Every other signal is aligned with explicit delays. Four separate
changes have since moved those depths, each time compensated by shortening a
chain. An off-by-one here computes a mesh with the wrong topology and still
runs, sounding plausible but wrong.

## Verification

`python test_lacuna.py` (standalone, no toolchain needed — `shims.py` stands in
for the tree's types). Checks the mesh bit-exact against a numpy reference at
two tension settings, that every preset actually rings, that the tuning table is
1 V/oct and within the stability limit, and that the scan fits the cycle budget.

The preset check exists because the slit preset was silent from the day it was
written: the slit masks `|dy| < 2, dx > 0`, which is exactly where the strike
landed, so every strike was zeroed in the pass that wrote it. The bit-exactness
test only ever drove preset 0.

**What the test cannot tell you is whether it still meets timing.** Three
separate regressions have been caught only by building — the test passes
bit-exact throughout. Always build before believing a change is free.

**Pin the placer seed.** These designs sit close enough to the routing limit
that the same RTL places very differently run to run: across five seeds the sync
domain came out 65.7–68.5 MHz and the 371 MHz serialiser 324–406, two failing
outright. One unpinned build shipped at 63.25 MHz against 60 and coincided with
a full device crash.

## Not yet

- No feedback path. Per-sample injection of the module's own output would make
  the surface part of a patch rather than an endpoint.
- Single-node strike. `mesh.py` supports a mallet radius and LACUNA leaves it at
  zero; a wider strike is rounder and much less bright, which is the trade.
- No frequency-dependent damping, so all modes decay together. A diffusion term
  was tried and measurably did nothing.
- Geometry FM uses a hard mask; the discontinuity is broadband noise. An
  energy-conserving moving boundary is an open problem, not a coding task.
