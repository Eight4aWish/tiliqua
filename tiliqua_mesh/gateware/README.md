# Mesh core — gateware

A 2D FDTD membrane mesh as a drop-in core for Tiliqua's `dsp` top-level. Audio
only; video comes later on the `vectorscope_no_soc` skeleton.

**Verified bit-exact against a numpy reference** over 120 samples, on both a
full disc and a ring preset. `test_mesh.py` mirrors the gateware's arithmetic
exactly — arithmetic shifts, no rounding, strike applied after the update,
saturate rather than truncate — so a mismatch would be a real difference in the
mesh, not a modelling choice.

## Jacks

| jack | function |
|---|---|
| in0 | strike — rising edge above ~1 V |
| in1 | strike position — sweeps from hub to rim |
| in2 | preset — selects the annulus family |
| in3 | geometry FM — modulates the hole radius at audio rate |
| out0 | mesh output |

Presets: full disc, narrow hole, wide ring, thin ring, square hole, slit ring.

## Cost

| | |
|---|---|
| grid | 32×32, one node per cycle in raster order |
| cycles per sample | **1032** measured, against a 1250 budget at 60 MHz / 48 kHz |
| state | 2 banks × 1024 × 24 bit = 48 kbit of the ECP5's 1008 |
| multipliers in the audio path | **none** — the update is three adds, a shift and a subtract |

Two multipliers are used off the audio path, for `d2` and the squared radii.

Build at 48 kHz. At 192 kHz the budget falls to 312 cycles and this does not
fit; that needs the four-datapath version.

## Install

```bash
cp mesh_core.py /path/to/tiliqua/gateware/src/top/dsp/
```

In `gateware/src/top/dsp/top.py`:

```python
from mesh_core import Mesh          # near the other imports

CORES = {
    ...
    "mesh": (False, Mesh),
}
```

```bash
cd gateware && pdm dsp build --dsp-core mesh
```

## How it works

The update is the leapfrog scheme at the Courant limit:

```
u_next[j] = ((N + S + E + W) >> 1) - u_prev[j]
```

Two memory banks swap roles every sample. The bank holding `u` is read as a
raster stream running one row ahead of the node being computed, and a 2N-deep
delay line off that stream yields every neighbour without a second read port:

```
tap 0    = u[j+N] -> S       tap N+1 = u[j-1] -> W
tap N-1  = u[j+1] -> E       tap 2N  = u[j-N] -> N
tap N    = u[j]   -> centre
```

The bank holding `u_prev` is read at the node address and overwritten with
`u_next` in the same pass, which is safe because nothing reads a node after it
has been written.

## Two things that will bite

**Pipeline alignment.** A synchronous read port gives data one cycle after the
address and the delay line registers it, so during cycle T the taps describe
node `j(T) - 2`. Every other signal is aligned to that with explicit delays of
2, 3 and 4 stages. These are not interchangeable — an off-by-one computes a
mesh with the wrong topology and still runs, sounding plausible but wrong.

**Strike and pickup must sit on material.** Both radii are derived from the live
geometry rather than fixed, because a pickup at a constant radius falls inside
the hole on the thin-ring preset and reads zero forever. That failure silenced
two of the numpy renders before it was caught, and looks identical to a dead
core.

The mask also never comes within two cells of the array border, which is what
makes the wrapping delay-line taps safe. The assert in `__init__` enforces it.

## Not yet

- No video. The surface state is already in the memory the audio is read from,
  so drawing it should be nearly free — that is the next build, on the
  `vectorscope_no_soc` skeleton.
- No feedback path. Per-sample injection of the module's own output is what
  makes the surface part of a patch rather than an endpoint.
- The strike is a single node, not a blob. Cheaper, slightly harsher.
- No frequency-dependent damping, so all modes decay together. See ../README.md.
- Geometry FM uses a hard mask, whose noise is documented in ../README.md as
  character rather than defect. An energy-conserving moving boundary is an open
  problem, not a coding task.
