"""
Verify the Amaranth core against a bit-exact numpy reference.

The reference mirrors the gateware's arithmetic exactly -- arithmetic shifts,
no rounding, strike added after the update, saturate not truncate -- so any
mismatch is a real difference in the mesh, not a modelling choice. Run at 16x16
so a few hundred samples finish in a couple of minutes.
"""

import numpy as np
from amaranth.sim import Simulator
from mesh_core import Mesh, WIDTH, FRAC

N = 16
import sys
PRESET = (6, 2, 0, 0) if "--ring" in sys.argv else (6, 0, 0, 0)
LOSS = 13
SAMPLES = 120
HI, LO = (1 << (WIDTH - 1)) - 1, -(1 << (WIDTH - 1))


def reference(n, outer, inner, loss, strike_node, samples):
    cx = cy = n // 2
    yy, xx = np.ogrid[:n, :n]
    d2 = (xx - cx) ** 2 + (yy - cy) ** 2
    mask = (d2 <= outer * outer) & (d2 > inner * inner)

    u = np.zeros((n, n), dtype=np.int64)
    up = np.zeros((n, n), dtype=np.int64)
    amp = int(0.9 * (1 << FRAC))
    inner_c = inner
    pickup_r = (inner_c + outer) >> 1
    px, py = cx, cy + pickup_r

    out = []
    for k in range(samples):
        nb = np.zeros_like(u)
        nb[1:, :] += u[:-1, :]
        nb[:-1, :] += u[1:, :]
        nb[:, 1:] += u[:, :-1]
        nb[:, :-1] += u[:, 1:]
        base = (nb >> 1) - up
        nxt = base - (base >> loss)
        if k == 0:
            nxt[strike_node[1], strike_node[0]] += amp
        nxt = np.clip(nxt, LO, HI)
        nxt = np.where(mask, nxt, 0)
        up, u = u, nxt
        out.append(int(u[py, px]))
    return out


def run_gateware(samples):
    dut = Mesh(n=N, loss_shift=LOSS, presets=[PRESET])
    sim = Simulator(dut)
    sim.add_clock(1e-6)
    got = []

    async def tb(ctx):
        ctx.set(dut.o.ready, 1)
        for k in range(samples):
            ctx.set(dut.i.payload[0], 8000 if k == 0 else 0)   # strike gate
            ctx.set(dut.i.payload[1], 0)                       # position
            ctx.set(dut.i.payload[2], 0)                       # preset
            ctx.set(dut.i.payload[3], 0)                       # geom FM
            ctx.set(dut.i.valid, 1)
            while not (ctx.get(dut.i.valid) and ctx.get(dut.i.ready)):
                await ctx.tick()
            await ctx.tick()
            ctx.set(dut.i.valid, 0)
            while not ctx.get(dut.o.valid):
                await ctx.tick()
            got.append(ctx.get(dut.pickup_dbg))
            await ctx.tick()

    sim.add_testbench(tb)
    sim.run()
    return got


if __name__ == "__main__":
    outer, inner, _, _ = PRESET
    cx = cy = N // 2
    strike_r = min(inner + 1 + 0, outer - 1)
    strike = (cx + strike_r, cy)
    print(f"grid {N}x{N}  outer={outer} inner={inner}  strike at {strike}")

    ref = reference(N, outer, inner, LOSS, strike, SAMPLES)
    got = run_gateware(SAMPLES)

    ref = np.array(ref, dtype=np.int64)
    got = np.array(got, dtype=np.int64)
    n = min(len(ref), len(got))
    diff = ref[:n] - got[:n]
    print(f"samples compared: {n}")
    print(f"reference range: {ref[:n].min()} .. {ref[:n].max()}")
    print(f"gateware  range: {got[:n].min()} .. {got[:n].max()}")
    bad = np.flatnonzero(diff)
    if len(bad) == 0:
        print("MATCH: bit-exact against the reference")
    else:
        print(f"MISMATCH: {len(bad)}/{n} samples differ, first at {bad[0]}")
        for k in bad[:8]:
            print(f"  s{k}: ref={ref[k]} got={got[k]}")
