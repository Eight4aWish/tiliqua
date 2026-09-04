# Tiliqua — two instruments on a membrane

A fork of [apfaudio/tiliqua](https://github.com/apfaudio/tiliqua) adding two
bitstreams of my own. Everything upstream is unchanged and documented in
[the root README](../README.md) and at
[apfaudio.github.io/tiliqua](https://apfaudio.github.io/tiliqua/).

Both instruments are the same 32×32 finite-difference membrane, in
[`mesh.py`](../gateware/src/top/lacuna/mesh.py). They differ entirely in how it
is driven.

| | [**LACUNA**](../gateware/src/top/lacuna/LACUNA.md) | [**ORBITA**](../gateware/src/top/orbita/ORBITA.md) |
|---|---|---|
| | a struck membrane | a scanned wavetable |
| the mesh | runs at 48 kHz | runs at 750 Hz |
| the output | a pickup node, heard over time | a circular path, read at audio rate |
| pitch is | tension of the membrane | the scan rate |
| in0 | strike | drive / drone |
| in1 | tension, 1 V/oct | pitch, 1 V/oct |
| in2 | strike position | scan radius |
| in3 | geometry — the hole | geometry — the hole |

## The idea

The membrane's boundary is a **comparator, not an array**. On a CPU the mask is
1024 elements you rebuild whenever the shape changes, so shape is a control-rate
parameter at best. Here it is two comparisons re-evaluated for every node of
every scan, which means the geometry of the instrument can change every single
sample — and the hole becomes a modulation destination rather than a setting.

That is the one thing about this that a CPU module in the same rack cannot
follow: an H7 has no video output either, and no way to put audio and pixels in
the same clock domain.

ORBITA takes the same idea into scanned synthesis. A concentric scan circle
never crosses a concentric hole, so the *asymmetric* geometries are the
interesting ones: the slit gives one notch per revolution and a full harmonic
series, the square hole four notches and a fourth-harmonic emphasis. The
symmetry order of the hole picks the harmonics.

## The video

Both draw the membrane live, and it is worth explaining why that is not a
gimmick. **There is no framebuffer, no PSRAM and no CPU** — the audio scan
already carries every node past one point once per sample, so a second narrow
memory written from the same address and strobe is a free snapshot, and each
pixel is coloured from it a few microseconds before it goes down the cable.
About 620 LUTs and one BRAM.

Blue and red are the two signs of displacement, so what is on screen is the mode
pattern rather than a brightness envelope. That matters because the mesh's most
characteristic behaviour — mode beating between near-degenerate pairs — appears
as the pattern *precessing*, which no waveform display can show.

ORBITA additionally draws the scan circle over the membrane and the same circle
*unrolled* as a waveform strip beneath it, phase-locked by construction, so a
feature at an angle on the ring sits directly above the sample it produced.

## Building

```bash
cd gateware
pdm install

# both instruments, both video modes, seeds pinned
./scripts/build_bitstreams_fork.sh

# or one at a time
AMARANTH_nextpnr_opts="--timing-allow-fail --seed 2" \
    pdm lacuna build --modeline 1280x720p60
AMARANTH_nextpnr_opts="--timing-allow-fail --seed 3" \
    pdm orbita build --modeline 1280x720p60
```

Flash a built archive into one of the bootloader's eight slots:

```bash
pdm flash status                                    # what is in the slots now
pdm flash archive build/lacuna-r5/lacuna-<tag>-r5.tar.gz --slot 3
```

**Pin the placer seed.** These designs sit close enough to the ECP5's routing
limit that identical RTL places very differently run to run — across five seeds
LACUNA's sync domain came out 65.7–68.5 MHz and the 1280×720 serialiser
324–406 MHz, with two seeds failing outright, on changes that cannot affect
them. An unpinned build shipped at 63.25 MHz against a 60 MHz constraint and
coincided with a full device crash. The seed is set through the environment
rather than by editing upstream's `cli.py`, which would be a standing rebase
conflict. Re-check it after any change of size.

Two video modes are built: `1280x720p60` for a capture card, `720x720p60r2` for
the Waveshare panel. A cheap HDMI capture dongle will not lock to 720×720 — it
is not a standard timing.

## Testing

```bash
cd gateware/src/top/lacuna
python test_lacuna.py     # the membrane: bit-exact, tuning, every preset rings
python test_orbita.py     # the scan: circle, pluck, radius sweep, drone
```

Both run standalone with no FPGA toolchain — `shims.py` stands in for the tree's
fixed-point types.

**Neither test checks timing**, and three separate timing regressions have been
caught only by building while the tests stayed bit-exact throughout. The mesh is
shared, so build *both* instruments after touching it.

## Research

[`research/`](../research) holds the exploration behind them, kept out of
`gateware/` so rebases onto upstream never touch it:

- [`research/mesh/`](../research/mesh) — the fixed-point reference model, the
  annulus family, mode analysis, and the audio that came out of it
- [`research/scan/DESIGN.md`](../research/scan/DESIGN.md) — the ORBITA design
  note, written before it was built
- [`research/wavefield/`](../research/wavefield) — an earlier beamracing
  experiment

## Licence

Upstream is CERN-OHL-S-2.0 and these follow it. See
[LICENSE](../LICENSE) and [CONTRIBUTING.md](../CONTRIBUTING.md), including
upstream's policy on AI/LLM usage.
