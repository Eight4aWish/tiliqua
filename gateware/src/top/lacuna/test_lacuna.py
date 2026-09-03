"""
Verification for the Lacuna core.

1. The mesh is bit-exact against a numpy reference implementing the same
   arithmetic, including the tension multiply and the pitch-tracked damping.
2. The tuning table really is 1 V/oct, and lam2 stays under the stability
   limit on every preset.
3. The scan fits the cycle budget.

Run:  python test_lacuna.py
"""

import math
import numpy as np
from amaranth.sim import Simulator

from lacuna import (Lacuna, tuning_table, PRESETS, WIDTH, FRAC, LAM_FRAC,
                    K_FRAC, INV_MU_FRAC, LAM_MAX, CV_BITS, OCTAVES, F_LO, FS,
                    VOCT_Q16)

N = 32
BASE_LOSS = 13
HI, LO = (1 << (WIDTH - 1)) - 1, -(1 << (WIDTH - 1))
TABLE = tuning_table()


def _set_raw(ctx, port, v):
    """Mirror of lacuna._raw: write raw bits whether ASQ is fixed or plain."""
    ctx.set(port.as_value() if hasattr(port, "as_value") else port, v)


def controls(tension_cv, preset):
    """Mirror the core's per-sample control arithmetic exactly."""
    idx = (tension_cv * VOCT_Q16 + (1 << 15)) >> 16
    idx = max(0, min((1 << CV_BITS) - 1, idx))
    inv_mu = PRESETS[preset][4]
    lam = (TABLE[idx] * inv_mu) >> (K_FRAC + INV_MU_FRAC - LAM_FRAC)
    lam = min(lam, LAM_MAX)
    octave = idx >> (CV_BITS - 2)
    return lam, BASE_LOSS + ((OCTAVES - 1 - octave) >> 1)


def reference(preset, tension_cv, samples, strike_cv=0):
    outer, inner, square, slit, _ = PRESETS[preset]
    cx = cy = N // 2
    yy, xx = np.ogrid[:N, :N]
    dx, dy = xx - cx, yy - cy
    d2 = dx * dx + dy * dy
    mask = d2 <= outer * outer
    if square:
        mask &= ~((np.abs(dx) < inner) & (np.abs(dy) < inner))
    else:
        mask &= d2 > inner * inner
    if slit:
        mask &= ~((np.abs(dy) < 2) & (dx > 0))

    lam, loss = controls(tension_cv, preset)
    strike_r = min(inner + 1 + strike_cv, outer - 1)
    pickup_r = (inner + outer) >> 1

    u = np.zeros((N, N), dtype=np.int64)
    up = np.zeros_like(u)
    amp = int(0.9 * (1 << FRAC))
    out = []
    for k in range(samples):
        nb = np.zeros_like(u)
        nb[1:, :] += u[:-1, :]
        nb[:-1, :] += u[1:, :]
        nb[:, 1:] += u[:, :-1]
        nb[:, :-1] += u[:, 1:]
        lap = nb - (u << 2)
        base = ((lap * lam) >> LAM_FRAC) + (u << 1) - up
        nxt = base - (base >> loss)
        if k == 0:
            nxt[cy, cx + strike_r] += amp
        nxt = np.clip(nxt, LO, HI)
        nxt = np.where(mask, nxt, 0)
        up, u = u, nxt
        out.append(int(u[cy + pickup_r, cx]))
    return out


def run_core(tension_cv, samples):
    dut = Lacuna(n=N, base_loss=BASE_LOSS)
    sim = Simulator(dut)
    sim.add_clock(1e-6)
    got, cycles = [], []

    async def tb(ctx):
        ctx.set(dut.o.ready, 1)
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


def check_tuning():
    print("tuning table")
    ok = True
    for p, (_, _, _, _, inv_mu) in enumerate(PRESETS):
        mu = -(1 << INV_MU_FRAC) / inv_mu
        worst = 0
        for i in (0, 341, 682, 1023):
            lam = (TABLE[i] * inv_mu) >> (K_FRAC + INV_MU_FRAC - LAM_FRAC)
            worst = max(worst, lam)
            want = F_LO * 2 ** (OCTAVES * i / (1 << CV_BITS))
            arg = 1 + (lam / (1 << LAM_FRAC)) * mu / 2
            got = math.acos(max(-1, min(1, arg))) * FS / (2 * math.pi)
            cents = 1200 * math.log2(got / want)
            if abs(cents) > 5:
                ok = False
        print(f"  preset {p}: max lam2 = {worst/(1<<LAM_FRAC):.4f}"
              f"  {'OK' if worst <= LAM_MAX else 'OVER LIMIT'}")
        if worst > LAM_MAX:
            ok = False
    # spot-check that pitch is exponential in the CV, i.e. actually 1 V/oct
    mu = -(1 << INV_MU_FRAC) / PRESETS[0][4]
    def f_at(i):
        lam = (TABLE[i] * PRESETS[0][4]) >> (K_FRAC + INV_MU_FRAC - LAM_FRAC)
        return math.acos(1 + (lam / (1 << LAM_FRAC)) * mu / 2) * FS / (2 * math.pi)
    for i in (0, 256, 512, 768):
        print(f"  index {i:4d} -> {f_at(i):7.1f} Hz")
    print(f"  octave ratios: {f_at(256)/f_at(0):.4f} {f_at(512)/f_at(256):.4f} "
          f"{f_at(768)/f_at(512):.4f}   (want 2.0000)")
    return ok


if __name__ == "__main__":
    tuning_ok = check_tuning()

    print("\nbit-exactness vs reference")
    all_ok = True
    for cv, label in [(16000, "top of range"), (4000, "one volt")]:
        ref = np.array(reference(0, cv, 40), dtype=np.int64)
        got, cycles = run_core(cv, 40)
        got = np.array(got, dtype=np.int64)
        bad = np.flatnonzero(ref != got)
        lam, loss = controls(cv, 0)
        print(f"  {label:14s} lam2={lam/(1<<LAM_FRAC):.4f} loss_shift={loss}  "
              f"range {ref.min()}..{ref.max()}  "
              f"{'MATCH' if not len(bad) else f'MISMATCH at {bad[0]}'}")
        if len(bad):
            all_ok = False
            for k in bad[:5]:
                print(f"     s{k}: ref={ref[k]} got={got[k]}")

    print(f"\ncycles per sample: {cycles[0]}  (budget 1250 at 60 MHz / 48 kHz)")
    print(f"\ntuning: {'OK' if tuning_ok else 'FAIL'}   "
          f"bit-exact: {'OK' if all_ok else 'FAIL'}   "
          f"budget: {'OK' if cycles[0] < 1250 else 'FAIL'}")
