"""
The boundary as an audio-rate parameter.

On a CPU the mask is a 4096-element array you rebuild whenever the shape
changes, so the shape is a control-rate parameter at best. In gateware the mask
is a comparator inline in address generation -- two radius registers -- so it
costs nothing to change it EVERY SAMPLE.

That makes the shape of the instrument a modulation destination. There is no
acoustic object whose geometry oscillates at 200 Hz, and no other platform in
this rack can express one.
"""

import numpy as np
from mesh_model import Membrane, write_wav, FS

N = 64
_y, _x = np.ogrid[:N, :N]
_c = (N - 1) / 2.0
D2 = ((_x - _c) ** 2 + (_y - _c) ** 2)     # precomputed once, as gateware would


def ring_mask(outer, inner):
    return (D2 <= outer * outer) & (D2 > inner * inner)


def play(mod_hz, depth, base=12.0, outer=30, secs=4.0, loss=16,
         gain_mask=None, excite=None, strike=(32, 6), pickup=(52, 32)):
    """`mod_hz` modulates the hole radius. Everything else held constant."""
    m = Membrane(n=N, mask=ring_mask(outer, base), loss_shift=loss,
                 gain_mask=gain_mask)
    if excite is None:
        m.strike(*strike, amp=0.9, width=1.6)
    n = int(FS * secs)
    out = np.zeros(n)
    w = 2 * np.pi * mod_hz / FS
    for k in range(n):
        inner = base + depth * np.sin(w * k)
        m.mask = ring_mask(outer, inner)
        inj = int(excite[k] * 0.3 * (1 << 22)) if excite is not None else 0
        m.step(inject=inj, inject_at=strike)
        out[k] = m.pickup(*pickup)
    return out


def describe(name, x):
    env = np.convolve(np.abs(x), np.ones(480) / 480, mode="same")
    pk = env.max()
    b = np.where(env < pk / 100)[0]
    b = b[b > 480]
    seg = x[int(FS * 0.5):int(FS * 1.5)] * np.hanning(FS)
    sp = np.abs(np.fft.rfft(seg))
    fr = np.fft.rfftfreq(FS, 1 / FS)
    cen = float((sp * fr).sum() / sp.sum())
    print(f"  {name:24s} audible {(b[0]/FS if len(b) else len(x)/FS):4.1f}s  "
          f"centroid {cen:5.0f} Hz  rms {np.sqrt((x**2).mean()):.3f}")


if __name__ == "__main__":
    jobs = [
        ("u1_geom_lfo_3hz",    dict(mod_hz=3.0,   depth=7.0)),
        ("u2_geom_fm_60hz",    dict(mod_hz=60.0,  depth=5.0)),
        ("u3_geom_fm_220hz",   dict(mod_hz=220.0, depth=4.0)),
        ("u4_geom_fm_700hz",   dict(mod_hz=700.0, depth=3.0)),
    ]
    for name, kw in jobs:
        x = play(**kw)
        write_wav(f"{name}.wav", x)
        describe(name, x)

    # A drone: geometry oscillating while a region pumps energy in. It never
    # decays and never repeats.
    gm = (D2 <= 8 * 8)
    gm = np.zeros((N, N), bool)
    gm[24:30, 44:50] = True
    x = play(mod_hz=37.0, depth=6.0, loss=15, gain_mask=gm, secs=6.0)
    write_wav("u5_geometric_drone.wav", x)
    describe("u5_geometric_drone", x)

    # And the same geometry modulation driving external audio rather than a
    # strike: the resonator whose shape is itself a signal.
    t = np.arange(int(FS * 5.0)) / FS
    rng = np.random.default_rng(11)
    src = rng.normal(0, 1, len(t)) * (0.25 + 0.75 * (np.sin(2 * np.pi * 0.7 * t) > 0))
    x = play(mod_hz=110.0, depth=5.0, secs=5.0, loss=16, excite=src)
    write_wav("u6_shaped_resonator.wav", x)
    describe("u6_shaped_resonator", x)


# --- soft boundary ---------------------------------------------------------
# Hard masking at audio rate makes nodes appear and vanish between samples, and
# that discontinuity is broadband noise: the tonal share of energy falls from
# 0.675 (static annulus) to 0.287. Giving the rim a two-cell ramp instead of a
# cliff lets nodes fade in and out. In gateware this is still one comparator
# chain producing a 0-3 weight and a multiply-by-3-then->>2, i.e. shifts and
# adds, no DSP tile.

def soft_ring_weight(outer, inner):
    """Weight in quarters (0..4) instead of a boolean mask."""
    w = np.zeros((N, N), dtype=np.int64)
    for step in range(4):
        w += ((D2 <= (outer - step) ** 2) &
              (D2 > (inner + step) ** 2)).astype(np.int64)
    return w  # 0 outside, 4 well inside, 1-3 across the two-cell rim


def play_soft(mod_hz, depth, base=12.0, outer=30, secs=4.0, loss=16,
              strike=(32, 6), pickup=(52, 32)):
    # Two separate things, which is the trap here: `Membrane.mask` is the
    # SUPPORT (boolean -- does this node exist), and the weight is the rim
    # taper applied after. Handing the 0-4 weight to Membrane as its mask means
    # step() multiplies by up to 4 every sample and the whole thing saturates
    # in milliseconds.
    weight = soft_ring_weight(outer, base)
    m = Membrane(n=N, mask=(weight > 0), loss_shift=loss)
    m.strike(*strike, amp=0.9, width=1.6)
    n = int(FS * secs)
    out = np.zeros(n)
    w = 2 * np.pi * mod_hz / FS
    for k in range(n):
        inner = base + depth * np.sin(w * k)
        weight = soft_ring_weight(outer, inner)
        m.mask = weight > 0
        m.step()
        # (u * weight) >> 2 : a node on the rim is attenuated, not deleted.
        m.u = (m.u * weight) >> 2
        out[k] = m.pickup(*pickup)
    return out


def play_born(mod_hz, depth, base=12.0, outer=30, secs=4.0, loss=16,
              strike=(32, 6), pickup=(52, 32)):
    """
    Moving boundary without the noise. The problem with a hard mask is not the
    edge shape, it is that a node entering the mask arrives holding zero while
    its neighbours are mid-swing -- a step discontinuity injected every sample.

    Tapering the rim does not fix it: a per-sample multiply by 3/4 is a decay of
    0.75**48000, which annihilates the signal (measured rms 0.042 -> 0.004).

    Instead, a node that comes into existence is born holding its neighbours'
    average, so it joins the wave already in phase. In gateware that is the
    neighbour sum already being computed, shifted -- no extra memory traffic.
    """
    mask = ring_mask(outer, base)
    m = Membrane(n=N, mask=mask, loss_shift=loss)
    m.strike(*strike, amp=0.9, width=1.6)
    n = int(FS * secs)
    out = np.zeros(n)
    w = 2 * np.pi * mod_hz / FS
    prev_mask = mask
    for k in range(n):
        inner = base + depth * np.sin(w * k)
        mask = ring_mask(outer, inner)
        born = mask & ~prev_mask
        if born.any():
            for arr in (m.u, m.u_prev):
                nb = np.zeros_like(arr)
                nb[1:, :] += arr[:-1, :]
                nb[:-1, :] += arr[1:, :]
                nb[:, 1:] += arr[:, :-1]
                nb[:, :-1] += arr[:, 1:]
                arr[born] = (nb[born] >> 2)
        m.mask = mask
        prev_mask = mask
        m.step()
        out[k] = m.pickup(*pickup)
    return out
