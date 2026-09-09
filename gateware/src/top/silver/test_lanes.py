# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""
A wider scan must compute the same membrane.

`lanes` changes how many cells the mesh retires per cycle, and nothing else:
the same nodes, in the same raster order, with the same arithmetic. So a
multi-lane build has to be bit-identical to the one-lane build it replaces,
sample for sample, on every preset -- and it has to cost proportionally fewer
cycles, which is the entire point.

This is the safety net for the work towards a larger membrane. test_silver.py
proves one lane against a numpy reference; this proves every other lane count
against one lane.

    python test_lanes.py
"""

import sys

from amaranth.sim import Simulator

from silver import Silver, PRESETS

# Import the driving helpers from the existing test rather than restating them,
# so the two stay in step.
from test_silver import N, BASE_LOSS, _set_raw

LANE_COUNTS = [1, 2, 4]
SAMPLES = 24


def run(lanes, preset, tension_cv, samples=SAMPLES):
    """Pickup value and cycle count per sample, for one lane count."""
    dut = Silver(n=N, base_loss=BASE_LOSS, lanes=lanes)
    sim = Simulator(dut)
    sim.add_clock(1e-6)
    got, cycles = [], []

    async def tb(ctx):
        ctx.set(dut.o.ready, 1)
        for _ in range(preset):          # short press cycles the preset
            ctx.set(dut.button, 1)
            await ctx.tick()
            ctx.set(dut.button, 0)
            for _ in range(4):
                await ctx.tick()
        for k in range(samples):
            _set_raw(ctx, dut.i.payload[0], 8000 if k == 0 else 0)
            _set_raw(ctx, dut.i.payload[1], tension_cv)
            _set_raw(ctx, dut.i.payload[2], 0)
            _set_raw(ctx, dut.i.payload[3], 0)
            ctx.set(dut.i.valid, 1)
            while not (ctx.get(dut.i.valid) and ctx.get(dut.i.ready)):
                await ctx.tick()
            await ctx.tick()
            ctx.set(dut.i.valid, 0)
            c = 0
            while not ctx.get(dut.o.valid):
                await ctx.tick(); c += 1
            cycles.append(c)
            got.append(ctx.get(dut.pickup_dbg))
            await ctx.tick()

    sim.add_testbench(tb)
    sim.run()
    return got, cycles


def main():
    print(f"A wider scan against one lane, {N}x{N}, {SAMPLES} samples\n")
    ok = True

    for preset in range(len(PRESETS)):
        ref, ref_cyc = run(1, preset, 3000)
        line = f"  preset {preset} ({PRESETS[preset][0]:2d},{PRESETS[preset][1]:3d})"
        line += f"  1 lane {ref_cyc[0]:5d} cyc"
        for lanes in LANE_COUNTS[1:]:
            got, cyc = run(lanes, preset, 3000)
            same = got == ref
            ok &= same
            line += f" | {lanes} lanes {cyc[0]:5d} cyc {'MATCH' if same else 'DIFFER'}"
        print(line)

    print("\n  tension sweep on the full disc")
    for cv in (0, 1000, 2500, 4000):
        ref, _ = run(1, 0, cv)
        row = f"    cv {cv:5d}"
        for lanes in LANE_COUNTS[1:]:
            got, _ = run(lanes, 0, cv)
            same = got == ref
            ok &= same
            row += f" | {lanes} lanes {'MATCH' if same else 'DIFFER'}"
        print(row)

    _, c1 = run(1, 0, 3000)
    print(f"\n  cycles per sample: ", end="")
    for lanes in LANE_COUNTS:
        _, c = run(lanes, 0, 3000)
        print(f"{lanes} lane{'s' if lanes > 1 else ' '} {c[0]:5d}   ", end="")
    print(f"\n  budget 1250 at 60 MHz / 48 kHz")

    print("\n" + ("all lane counts bit-identical to one lane"
                  if ok else "MISMATCH -- a wider scan is not the same membrane"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
