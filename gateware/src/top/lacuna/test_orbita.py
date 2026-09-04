# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""ORBITA: does the circular scan actually produce a waveform?

The mesh takes ~1040 cycles per update, so the testbench has to pace samples at
a realistic rate rather than free-running, or a second update is requested
before the first has finished and the membrane never advances.
"""
import numpy as np
from amaranth.sim import Simulator

from orbita import Orbita, nco_table, circle_table, CV_BITS
from mesh import PRESETS

SAMPLE_CYCLES = 1250          # 60 MHz / 48 kHz


def _set_raw(ctx, port, v):
    ctx.set(port.as_value() if hasattr(port, "as_value") else port, v)


def run(samples, pitch_cv, radius_cv, gate_of, preset=0, update_div=1):
    dut = Orbita(n=32, update_div=update_div)
    sim = Simulator(dut)
    sim.add_clock(1e-6)
    got = []

    async def tb(ctx):
        ctx.set(dut.o.ready, 1)
        for k in range(samples):
            _set_raw(ctx, dut.i.payload[0], gate_of(k))
            _set_raw(ctx, dut.i.payload[1], pitch_cv)
            _set_raw(ctx, dut.i.payload[2], radius_cv)
            _set_raw(ctx, dut.i.payload[3], 0)
            ctx.set(dut.i.valid, 1)
            while not (ctx.get(dut.i.valid) and ctx.get(dut.i.ready)):
                await ctx.tick()
            await ctx.tick()
            ctx.set(dut.i.valid, 0)
            n = 0
            while not ctx.get(dut.o.valid):
                await ctx.tick(); n += 1
            got.append(ctx.get(dut.o.payload[0].as_value()
                               if hasattr(dut.o.payload[0], "as_value")
                               else dut.o.payload[0]))
            await ctx.tick(); n += 1
            # pace the rest of the sample period so the mesh can finish
            for _ in range(max(0, SAMPLE_CYCLES - n - 4)):
                await ctx.tick()

    sim.add_testbench(tb)
    sim.run()
    return np.array([v - 65536 if v > 32767 else v for v in got], dtype=np.int64)


if __name__ == "__main__":
    ok = True

    # tables
    nt, ct = nco_table(), circle_table()
    print(f"nco: 55 Hz -> {nt[0]}, 880 Hz -> {nt[-1]}")
    xs = [(v & 0xFF) - 256 if (v & 0xFF) > 127 else (v & 0xFF) for v in ct]
    ys = [(v >> 8) - 256 if (v >> 8) > 127 else (v >> 8) for v in ct]
    r = [round((x * x + y * y) ** 0.5) for x, y in zip(xs, ys)]
    print(f"circle radius min/max over 64 points: {min(r)}/{max(r)} (want 64/64)")
    ok &= (min(r) >= 63 and max(r) <= 65)

    # a pluck should produce sound; before it, silence
    print("\npluck")
    out = run(400, pitch_cv=8000, radius_cv=12000,
              gate_of=lambda k: 8000 if 20 <= k < 24 else 0)
    pre, post = out[:18], out[40:]
    print(f"  before the gate: peak {np.abs(pre).max()}")
    print(f"  after  the gate: peak {np.abs(post).max()}")
    ok &= np.abs(pre).max() == 0 and np.abs(post).max() > 100

    # radius inside the hole on a wide ring should be silent
    print("\nradius sweep on the drum head (peak per radius)")
    for rv in (0, 4000, 8000, 12000, 16000):
        o = run(300, pitch_cv=8000, radius_cv=rv,
                gate_of=lambda k: 8000 if 20 <= k < 24 else 0)
        print(f"  in2 {rv/4000:.1f} V -> peak {np.abs(o[40:]).max()}")

    # a held drive should sustain where a pluck decays
    print("\ndrone: held drive")
    o = run(400, pitch_cv=8000, radius_cv=12000, gate_of=lambda k: 8000)
    a, b = np.abs(o[60:160]).max(), np.abs(o[300:]).max()
    print(f"  early peak {a}, late peak {b}")

    print(f"\ntables: {'OK' if ok else 'FAIL'}")
