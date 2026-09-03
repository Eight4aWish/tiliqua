# Scan — the mesh as a wavetable, not as a drum

Design note, 2026-09-03. Nothing here is built yet.

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
| — | **the hole: geometry as a live parameter** |

## What is missing

Three things, in rough order of effort.

**1. Decouple the mesh update from the audio rate.** Today the FSM runs one full
scan per incoming audio sample — 1037 cycles against a 1250 budget at 60 MHz /
48 kHz. Scan wants the mesh updated once every N samples (N a CV, say 4…256)
with the FSM idling between, while the output is produced every sample from the
path buffer. This is the structural change and it touches the state machine that
took three rounds of timing work to stabilise, so it belongs on its own branch.

**2. A path buffer.** The scan already carries every node past `wr_addr` /
`written` / `wr_valid` once per update, so capturing one row costs a single
comparator on the high bits of `wr_addr` and a 32×24-bit memory — the same trick
the display snapshot uses. Read the row forward then backward to get a **closed**
64-point loop. Closing it matters: an open path steps discontinuously at the wrap
and buzzes once per cycle.

**3. Phase accumulator and interpolation.** A plain NCO indexing the path buffer,
with linear interpolation between adjacent points. One multiply.

Note that pitch stops going through the tuning table entirely. In lacuna, CV →
`lam2` → mode frequency. In scan, CV → phase increment, and `lam2` becomes a pure
timbre control ("tension"). That is simpler than what is there now, not harder.

## The part worth building it for

Take the path **through the hole**. A horizontal line across an annulus crosses
rim → membrane → hole → membrane → rim, and the hole cells are hard zeros. So the
hole is a **flat segment in the middle of the waveform**, and its radius is duty
cycle.

Geometry is already an audio-rate modulation destination in lacuna, which means
`in3` becomes pulse-width modulation of a wavetable that is itself a vibrating
membrane. A 1D ring has no geometry to modulate; this is the one thing the Daisy
version cannot follow us into, and it is the Lacuna premise — the hole is the
instrument — transplanted into the scan domain.

The preset shapes then read as waveform families rather than drum shapes: solid
head = no flat segment, narrow hole = narrow pulse, thin ring = mostly flat with
two narrow excursions, slit ring = asymmetric.

## Cost

Comfortable. Current occupancy of the whole lacuna + video build on the LFE5U-25F:

| | used | free |
|---|---|---|
| LUT | 3157 (12%) | 21k |
| FF | 2945 (12%) | 21k |
| BRAM | 7 (12%) | 49 |
| MULT18X18D | 12 (42%) | 16 |
| PLL | 2 (100%) | 0 |

Scan adds roughly one BRAM, one multiplier and a few hundred LUTs.

## Open questions

- **DC offset.** The mesh carries a DC component and the waveform inherits it.
  `daisy_scanned` runs a DC blocker (`dc_x1_`/`dc_y1_`); we will need the same.
- **Hard edges at the hole.** The zero segment meets the membrane in a step,
  which is bright and buzzy. That is partly the point, but it may want slewing —
  worth hearing before deciding.
- **Amplitude decay.** A struck mesh decays, so the waveform fades. For a drone
  the Daisy version self-excites continuously; we need an equivalent, or
  normalisation against the path's own RMS.
- **Which path.** A row is trivial to capture. A ring around the hole would be
  more natural for an annulus but needs an address ROM and a comparison per node.
  Start with the row.
- **Where it lives.** A separate top-level (`top/scan/`) reusing a factored-out
  mesh module, rather than a mode inside lacuna — it is a different instrument.
  All eight flash slots are currently full, so something has to give.

## First steps

1. Branch off `lacuna`.
2. Factor the mesh update out of `lacuna.py` so both top-levels share it.
3. Add the update divider and confirm the mesh still passes `test_lacuna.py`
   bit-exact when the divider is 1.
4. Add the row buffer, the NCO and the mirror, and listen before touching
   anything else.
