"""
Does changing the preset CV require a rebuild? No -- the geometry is registers
and comparators, not fabric. This measures how fast a preset change actually
takes effect: strike, let it ring, switch preset, strike again.
"""

import numpy as np
from amaranth.sim import Simulator
from mesh_core import Mesh

N = 16
PRESETS = [(6, 0, 0, 0), (6, 3, 0, 0)]     # disc, ring
STRIKE_A, STRIKE_B = 0, 60
TOTAL = 130


def run(preset_cv_after):
    dut = Mesh(n=N, loss_shift=13, presets=PRESETS)
    sim = Simulator(dut)
    sim.add_clock(1e-6)
    got = []

    async def tb(ctx):
        ctx.set(dut.o.ready, 1)
        for k in range(TOTAL):
            gate = 8000 if k in (STRIKE_A, STRIKE_B) else 0
            # Preset CV changes one sample before the second strike.
            pre = preset_cv_after if k >= STRIKE_B - 1 else 0
            ctx.set(dut.i.payload[0], gate)
            ctx.set(dut.i.payload[1], 0)
            ctx.set(dut.i.payload[2], pre)
            ctx.set(dut.i.payload[3], 0)
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
    return np.array(got, dtype=np.int64)


stay = run(0)          # both strikes on preset 0 (disc)
switch = run(4096)     # second strike on preset 1 (ring)

pre = np.abs(stay[:STRIKE_B - 1] - switch[:STRIKE_B - 1]).max()
post = np.abs(stay[STRIKE_B:] - switch[STRIKE_B:]).max()
first_diff = np.flatnonzero(stay != switch)

print(f"identical before the CV change:      {pre == 0}")
print(f"first differing sample:              {first_diff[0] if len(first_diff) else None}"
      f"  (CV changed at sample {STRIKE_B - 1})")
print(f"max divergence after the switch:     {post}")
print(f"second strike, disc vs ring peak:    {np.abs(stay[STRIKE_B:]).max()} vs "
      f"{np.abs(switch[STRIKE_B:]).max()}")
print()
print(f"latency of a preset change: 1 sample = {1e6/48000:.1f} us at 48 kHz")
print("no bitstream reconfiguration is involved: `outer`/`inner`/`square_hole`/")
print("`slit` are registers, and the mask is two comparisons against them,")
print("re-evaluated for every node of every scan.")


# --- and the honest caveat -------------------------------------------------
# Switching while the mesh still holds energy is not free: nodes that leave the
# mask have their energy discarded, and nodes that enter it start from zero.
# That is the same hard-mask discontinuity documented in ../README.md. Measure
# it with no second strike, so the only event is the geometry change.

def run_switch_only(cv_at):
    dut = Mesh(n=N, loss_shift=13, presets=PRESETS)
    sim = Simulator(dut); sim.add_clock(1e-6)
    got = []

    async def tb(ctx):
        ctx.set(dut.o.ready, 1)
        for k in range(TOTAL):
            ctx.set(dut.i.payload[0], 8000 if k == 0 else 0)
            ctx.set(dut.i.payload[1], 0)
            ctx.set(dut.i.payload[2], 4096 if (cv_at is not None and k >= cv_at) else 0)
            ctx.set(dut.i.payload[3], 0)
            ctx.set(dut.i.valid, 1)
            while not (ctx.get(dut.i.valid) and ctx.get(dut.i.ready)):
                await ctx.tick()
            await ctx.tick()
            ctx.set(dut.i.valid, 0)
            while not ctx.get(dut.o.valid):
                await ctx.tick()
            got.append(ctx.get(dut.pickup_dbg))
            await ctx.tick()
    sim.add_testbench(tb); sim.run()
    return np.array(got, dtype=np.int64)


never = run_switch_only(None)
mid = run_switch_only(60)
step = np.abs(np.diff(mid))[58:62].max()
typical = np.abs(np.diff(never)).max()
print()
print("switching mid-ring, with no second strike:")
print(f"  largest sample-to-sample step at the switch: {step}")
print(f"  largest step anywhere in the undisturbed ring: {typical}")
print(f"  ratio: {step/typical:.2f}x  (>1 means the switch is an audible click)")
