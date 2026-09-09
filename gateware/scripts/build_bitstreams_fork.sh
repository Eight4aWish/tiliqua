#!/bin/bash
#
# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# Build this fork's own bitstreams: Silver and Gold, both video modes.
#
# Separate from upstream's build_bitstreams_{soc,no_soc}.sh so that rebasing
# onto apfaudio/tiliqua never conflicts here. Sequential rather than using GNU
# parallel, which upstream's scripts assume: four builds do not need it and it
# is not installed by default on macOS.
#
# The pinned placer seeds are not optional. These designs sit close enough to
# the ECP5's routing limit that identical RTL places very differently run to
# run -- across five seeds Silver's sync domain came out 65.7-68.5 MHz and the
# 1280x720 serialiser 324-406 MHz, two of them failing outright, on changes
# that cannot affect either. An unpinned build once shipped at 63.25 MHz
# against a 60 MHz constraint and coincided with a full device crash on
# hardware. Re-check the seeds after any change of size.
#
# Run from the `gateware` directory. Extra arguments are passed through, so
# e.g. `--hw=r5` works as it does upstream.

set -e

# The released size. Silver needs two lanes to fit 2304 nodes into the 1250 cycles
# a 48 kHz sample allows; Gold updates at 750 Hz and does not read MESH_LANES at
# all. Set here rather than left to the code's default, which is 32 -- without
# this the script cheerfully built a size that has never shipped.
export MESH_N=${MESH_N:-48}
export MESH_LANES=${MESH_LANES:-2}

# Seeds verified at 48x48. Gold needs a different one per modeline: at 720x720
# seed 2 closed with 0.85% margin, which is not enough to ship, and seed 4 gives
# 4.9%.
#
# Gold at 1280x720 is the hard one, and worth knowing before you burn an evening
# on it. Of eight seeds tried, ONE closed. The binding constraint is dvi_clk at
# 74.25 MHz, and the design lands either side of it more or less at random --
# 72.25, 73.12, 73.95, 74.15, 74.36, 76.38 across builds of identical RTL. The
# 720x720 modeline only asks 39.07 MHz of the same clock, which is why that
# variant closes on almost any seed.
#
# Re-check all four after ANY change, not just a change of size. Renaming the
# bitstream is enough: the name is rendered into the video path, so "Gold" is a
# different netlist from "ORBITA" and every seed verified before a rename is
# void. That is how this list was invalidated.
SILVER_SEED=4
SILVER7_SEED=4
GOLD_SEED=13
GOLD7_SEED=4

fail=0
archives=()

build () {                       # build <bitstream> <outdir> <seed> <modeline> [extra...]
  local name=$1 outdir=$2 seed=$3 modeline=$4; shift 4
  echo "=== $outdir @ $modeline (seed $seed) ==="
  AMARANTH_nextpnr_opts="--timing-allow-fail --seed $seed" \
    pdm "$name" build --modeline "$modeline" "$@"

  # --name is passed for every build, not just the 720x720 ones. Left off, the
  # default is the folder name in capitals -- SILVER, GOLD -- which matches the
  # other bitstreams in the bootloader but not the way the range is written
  # everywhere else (Joy, Sorrow, Girl). Title case wins; it is the module's name.
  #
  # NOTE the output directory follows --name, not the bitstream, so it has to
  # be passed in. Deriving it from $name silently reads the previous build's
  # report and tells you a design closed timing when it was never checked.
  local tim="build/$outdir-r5/top.tim"
  if [ ! -f "$tim" ]; then
    echo "!!! no timing report at $tim"; fail=1; echo; return
  fi

  # --timing-allow-fail means a build that misses timing still succeeds, so the
  # exit code proves nothing. Check the LAST four frequency lines, which are the
  # post-route result; the earlier report is a pre-route estimate that routinely
  # says FAIL on a design that closes.
  if grep -iE 'Max frequency' "$tim" | tail -4 | grep -q 'FAIL'; then
    echo "!!! $outdir @ $modeline MISSES TIMING:"
    fail=1
  fi
  grep -iE 'Max frequency' "$tim" | tail -4
  archives+=("$(ls -t build/$outdir-r5/*.tar.gz | head -1)")
  echo
}

# 1280x720p60 is the standard timing a capture card will lock to.
build silver silver "$SILVER_SEED" 1280x720p60 --name Silver "$@"
build gold gold "$GOLD_SEED" 1280x720p60 --name Gold "$@"

# 720x720p60r2 is the Waveshare panel. A cheap HDMI dongle will not accept it:
# it is not a standard timing.
build silver silver7 "$SILVER7_SEED" 720x720p60r2 --name Silver7 "$@"
build gold gold7 "$GOLD7_SEED" 720x720p60r2 --name Gold7 "$@"

echo "Archives from this run:"
printf '  %s\n' "${archives[@]}"

if [ "$fail" != "0" ]; then
  echo
  echo "At least one build missed timing. Try another seed before shipping."
  exit 1
fi
