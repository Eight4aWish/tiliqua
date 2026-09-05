#!/bin/bash
#
# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# Build this fork's own bitstreams: LACUNA and ORBITA, both video modes.
#
# Separate from upstream's build_bitstreams_{soc,no_soc}.sh so that rebasing
# onto apfaudio/tiliqua never conflicts here. Sequential rather than using GNU
# parallel, which upstream's scripts assume: four builds do not need it and it
# is not installed by default on macOS.
#
# The pinned placer seeds are not optional. These designs sit close enough to
# the ECP5's routing limit that identical RTL places very differently run to
# run -- across five seeds LACUNA's sync domain came out 65.7-68.5 MHz and the
# 1280x720 serialiser 324-406 MHz, two of them failing outright, on changes
# that cannot affect either. An unpinned build once shipped at 63.25 MHz
# against a 60 MHz constraint and coincided with a full device crash on
# hardware. Re-check the seeds after any change of size.
#
# Run from the `gateware` directory. Extra arguments are passed through, so
# e.g. `--hw=r5` works as it does upstream.

set -e

LACUNA_SEED=1
ORBITA_SEED=4

fail=0
archives=()

build () {                       # build <bitstream> <outdir> <seed> <modeline> [extra...]
  local name=$1 outdir=$2 seed=$3 modeline=$4; shift 4
  echo "=== $outdir @ $modeline (seed $seed) ==="
  AMARANTH_nextpnr_opts="--timing-allow-fail --seed $seed" \
    pdm "$name" build --modeline "$modeline" "$@"

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
build lacuna lacuna "$LACUNA_SEED" 1280x720p60 "$@"
build orbita orbita "$ORBITA_SEED" 1280x720p60 "$@"

# 720x720p60r2 is the Waveshare panel. A cheap HDMI dongle will not accept it:
# it is not a standard timing.
build lacuna lacuna7 "$LACUNA_SEED" 720x720p60r2 --name LACUNA7 "$@"
build orbita orbita7 "$ORBITA_SEED" 720x720p60r2 --name ORBITA7 "$@"

echo "Archives from this run:"
printf '  %s\n' "${archives[@]}"

if [ "$fail" != "0" ]; then
  echo
  echo "At least one build missed timing. Try another seed before shipping."
  exit 1
fi
