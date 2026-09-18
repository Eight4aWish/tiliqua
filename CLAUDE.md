# tiliqua

A fork of [apfaudio/tiliqua](https://github.com/apfaudio/tiliqua) adding two bitstreams of
my own. **Everything upstream is unchanged**, and the root `README.md` is upstream's — the
fork's own landing page is [`.github/README.md`](.github/README.md), which is the first
thing to read.

## Working here — things that have caught people out

- **Silver and Gold** are the instruments. LACUNA and ORBITA were their working names and
  still appear in older notes; `gateware/src/top/lacuna/` is a leftover directory.
- **Two grid sizes ship, 32×32 and 48×48.** Do not assume one — the cost tables in
  `SILVER.md` / `GOLD.md` give both, and they differ a lot.
- **The ECP5-25F has 28 multipliers**, and they are the tight resource before LUTs. Figures
  like 56 are wrong and have been published in error before.
- **Pin the placer seed.** These designs sit near the routing limit and identical RTL
  places very differently run to run; across five seeds Silver's sync domain came out
  65.7–68.5 MHz with some failing outright. Seeds go through
  `AMARANTH_nextpnr_opts`, never by editing upstream's `cli.py` — that would be a standing
  rebase conflict.
- **The mesh is shared, so build *both* instruments after touching it.** No test checks
  timing; regressions have only ever shown up in a build.
- **Two video modes.** `1280x720p60` for capture cards and monitors, `720x720p60r2` only
  for the round Waveshare panel — a cheap HDMI dongle will not lock to 720×720.
- **The board CAD is not in this repo.** Schematic PDFs only; the KiCad files are in
  [`apfaudio/tiliqua-hardware`](https://github.com/apfaudio/tiliqua-hardware).
- **Limitations are design choices, not a to-do list.** Each instrument's doc separates
  them from development directions; the bug lists in older notes are history, already
  fixed. Read the current `Limitations` section before repeating a caveat.

## Contributing upstream

Upstream has [a policy on AI-assisted contributions](CONTRIBUTING.md#use-of-llms--ai-in-contributions)
and **nothing here has been proposed for upstream inclusion.** It does not accept AI
assistance in shared library or platform code, pedagogical top-levels, documentation text
or hardware; it may accept it in non-pedagogical top-level bitstreams and test harnesses,
with clear provenance. Commit messages and PR text must be hand-written. Everything is
CERN-OHL-S-2.0, which applies to bitstreams as well as boards.

## Module inventory

Before answering anything about which modules exist, what hardware is in the rack, or
which repo a module lives in, read the canonical inventory:

**`MODULES.md`** in the [`eight4awish`](https://github.com/Eight4aWish/eight4awish) repo
— <https://github.com/Eight4aWish/eight4awish/blob/main/MODULES.md>

If that repo is checked out alongside this one, read it from disk; otherwise fetch the URL.

It covers all ten repos: the released modules, the built-but-undrafted ones, the
purchased rack with HP and function, companion software, and what is deliberately *not*
a module. No single repo sees all of it, so do not infer the full picture from this one.
