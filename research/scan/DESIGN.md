# ORBITA — the mesh as a wavetable, not as a drum

Design note, started 2026-09-03, path topology and name settled 2026-09-04.
Nothing here is built yet.

**ORBITA** — Latin for a wheel-track or circuit, from *orbis*, a circle; the
ancestor of "orbit", and it names the path traced rather than the shape. It also
carries the sense of a rut worn by repeated passage, which suits something going
round the same circle a couple of hundred times a second. Sits next to LACUNA in
the bootloader as its counterpart: the gap, and the circuit around it.

## The idea

Lacuna listens to the mesh: the membrane vibrates at audio rate and a pickup node
becomes the output. **Scan** does the opposite. The mesh evolves *slowly* — tens
of Hz, sub-audio — and a closed path through it is read at audio rate. The path's
values are one cycle of a waveform; the scan rate is the pitch. Timbre and pitch
are then independent, so a held note morphs while staying in tune.

This is Verplank/Mathews/Shaw scanned synthesis, and it is the technique already
implemented for the Daisy in [`daisy_scanned`][ds] (a 64-mass ring, selected as
**SCAN** inside `daisy_multiosc`). This note is about doing it on the Tiliqua,
where the slow system is a 2D membrane rather than a 1D ring.

[ds]: https://github.com/Eight4aWish/eurorack_daisy_patch_init/tree/main/daisy_scanned

## The path is a circle

The scan follows a **circular trajectory concentric with the mesh**, not a line
cutting across it. Three reasons:

1. **It closes naturally.** A line has two ends, and the wrap from the last
   sample back to the first is a step discontinuity that buzzes once per cycle.
   Closing a line means mirroring it — reading it out and back — which halves the
   useful resolution and imposes an artificial symmetry on every waveform. A
   circle has no seam.

2. **It is exactly `daisy_scanned`'s ring, upgraded.** The Daisy engine is 64
   masses in a ring coupled only to their neighbours. A circle through the mesh
   is the same ring, but each point is also coupled *radially* into a membrane.
   Energy can leave the scan path and come back, so the waveform evolves in ways
   a 1D ring cannot produce. That is the whole argument for doing it here.

3. **The radius becomes a control.** Sweeping the scan circle from the hub to the
   rim is a natural "scan position" parameter with no analogue in the Daisy
   version.

At radius 10 on a 32×32 grid the circumference is about 63 cells, so **64 points
is close to one point per cell** — the same table length as the Daisy ring, and
not a coincidence.

### Addressing

A ROM of 64 unit-circle offsets `(dx, dy)` in fixed point, scaled by the radius:

    addr = (cy + ((dy * r) >> k)) * n + (cx + ((dx * r) >> k))

Two multiplies, a 64-entry ROM, and the radius is free to be a CV. The ROM is
tiny; the multiplies are affordable (16 free).

## How geometry shapes the waveform

This is where the circle differs from the line, and the earlier draft of this
note had it wrong.

A **line** across an annulus crosses the hole, so the hole appears as a flat
segment in the middle of the waveform and its radius reads as duty cycle.

A **concentric circle never crosses a concentric hole.** Instead the hole sets an
inner boundary, and the radius control sweeps through three regimes:

| radius | what the scan sees |
|---|---|
| `r < inner` | inside the hole — all zeros, silence |
| `inner < r < outer` | membrane — signal |
| `r > outer` | outside the rim — silence |

So the annulus width is the window the scan can live in, and hole radius (on
`in3`, at audio rate) squeezes or opens that window. Sweeping `r` past either
boundary fades the voice out, which is a usable amplitude gesture in itself.

**The asymmetric presets are the interesting ones**, because a concentric circle
*does* cross those:

- **slit ring** — the slit removes `|dy| < 2, dx > 0`, so the circle passes
  through it once per revolution: one notch per cycle, giving a full harmonic
  series.
- **square hole** — between the square's half-width and its half-diagonal, the
  circle dips into the four corners: four notches per revolution, emphasising
  the fourth harmonic and its multiples.

That is a genuinely nice result: **the symmetry order of the hole sets which
harmonics the scan emphasises.** One-fold for the slit, four-fold for the square,
none for a plain annulus. It is a different and more structural relationship
between geometry and timbre than the line's duty cycle.

An **offset** scan circle would cross a concentric hole and recover the
duty-cycle behaviour as well — worth keeping as a later control, but not in v1.

## Why it is mostly already built

`daisy_scanned` and `lacuna` are the same mathematics. The leapfrog integration,
the tension control and the pitch-tracked damping all exist in
`gateware/src/top/lacuna/lacuna.py`:

| `ScannedVoice` (Daisy) | lacuna (Tiliqua) |
|---|---|
| `pos_[64]` / `prev_[64]`, ring of masses | the two mesh banks, 32×32 |
| `tension_` — wave speed | `lam2`, from the tuning table |
| `damping_` | `loss_shift` |
| `hammer_` + excitation shapes | the strike, with position CV |
| `center_` — restoring force | *not present* |
| — | **the hole, and the scan radius** |

## What is missing

**1. Decouple the mesh update from the audio rate.** Today the FSM runs one full
scan per incoming audio sample — 1037 cycles against a 1250 budget at 60 MHz /
48 kHz. Scan wants the mesh updated once every N samples (N a CV, say 4…256)
with the FSM idling between, while the output is produced every sample from the
snapshot. This is the structural change and it touches the state machine that
took three rounds of timing work to stabilise, so it belongs on its own branch.

**2. A full-width snapshot.** The display snapshot added for lacuna's video is
already a free copy of the mesh, written from the scan that carries every node
past `wr_addr` / `written` / `wr_valid` once per update — but it is quantised to
8 bits for the palette. Scan needs the same trick at 16 bits or more, with a read
port in the audio domain. One BRAM.

**3. Phase accumulator, ROM lookup and interpolation.** A plain NCO whose phase
indexes the circle ROM, with linear interpolation between adjacent points.

Note that pitch stops going through the tuning table entirely. In lacuna, CV →
`lam2` → mode frequency. In scan, CV → phase increment, and `lam2` becomes a pure
timbre control ("tension"). That is simpler than what is there now, not harder.

## Cost

Comfortable. Current occupancy of the whole lacuna + video build on the
LFE5U-25F:

| | used | free |
|---|---|---|
| LUT | 3157 (12%) | 21k |
| FF | 2945 (12%) | 21k |
| BRAM | 7 (12%) | 49 |
| MULT18X18D | 12 (42%) | 16 |
| PLL | 2 (100%) | 0 |

Scan adds roughly one BRAM, three multiplies (two for addressing, one for
interpolation) and a few hundred LUTs.

## Open questions

- **DC offset.** The mesh carries a DC component and the waveform inherits it.
  `daisy_scanned` runs a DC blocker (`dc_x1_`/`dc_y1_`); we will need the same.
- **Radius quantisation.** At small radii the circle passes through very few
  distinct cells, so the 64 points alias onto each other and the waveform
  coarsens. Probably musical rather than a problem, but it means the low end of
  the radius sweep behaves differently from the high end.
- **Amplitude decay.** A struck mesh decays, so the waveform fades. For a drone
  the Daisy version self-excites continuously; we need an equivalent, or
  normalisation against the path's own RMS.
- **Hard edges at slit and corner crossings.** A notch is a step into zero and
  back, which is bright. That is the point, but it may want slewing — worth
  hearing first.
- **Where it lives.** A separate top-level (`top/orbita/`) reusing a factored-out
  mesh module, rather than a mode inside lacuna — it is a different instrument.
  All eight flash slots are currently full, so something has to give.

## First steps

1. Branch off `lacuna`.
2. Factor the mesh update out of `lacuna.py` so both top-levels share it.
3. Widen the snapshot to 16 bits and give it an audio-domain read port; confirm
   lacuna's video still looks right and `test_lacuna.py` still passes bit-exact.
4. Add the update divider, still with divider = 1, and confirm bit-exactness
   again — that isolates the FSM change from everything else.
5. Add the circle ROM, the NCO and the radius control, and listen before touching
   anything else.
