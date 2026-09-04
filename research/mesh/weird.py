# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""
The mesh stops being a drum simulation the moment you stop respecting physics.
Every variant below costs the same per-node arithmetic as the realistic one --
adds and shifts, no multipliers -- but none of them can exist as an object.
"""

import numpy as np
from mesh_model import Membrane, render, write_wav, circular_mask, FS, ONE

N = 64


def disc(cx, cy, r, n=N):
    y, x = np.ogrid[:n, :n]
    return ((x - cx) ** 2 + (y - cy) ** 2) <= r ** 2


def band(x0, x1, y0, y1, n=N):
    m = np.zeros((n, n), dtype=bool)
    m[y0:y1, x0:x1] = True
    return m


VARIANTS = {}

# 1. Torus: wrap the edges. No rim to reflect off, so nothing ever comes back
#    to where it started in phase -- the modes are not Bessel and the sound
#    never resolves into a pitch the way a bounded membrane does.
VARIANTS["torus"] = dict(
    membrane=lambda: Membrane(n=N, wrap=True, mask=np.ones((N, N), bool),
                              loss_shift=15),
    strike=(20, 20), pickup=(48, 44), secs=4.0)

# 2. Two lobes joined by a narrow neck. Energy sloshes between them at a rate
#    set by the neck width -- audible beating that no single membrane produces.
_twin = disc(18, 32, 13) | disc(46, 32, 13) | band(28, 37, 30, 35)
VARIANTS["twin_lobes"] = dict(
    membrane=lambda: Membrane(n=N, mask=_twin, loss_shift=16),
    strike=(18, 32), pickup=(46, 32), secs=5.0)

# 3. Stretched four times harder east-west than north-south. A real membrane
#    under anisotropic tension tears; this one just detunes into a mode set
#    with no acoustic counterpart.
VARIANTS["stretched"] = dict(
    membrane=lambda: Membrane(n=N, radius=28, aniso=(3, 1), loss_shift=15),
    strike=(32, 20), pickup=(44, 40), secs=4.0)

# 4. A region that amplifies instead of damping. Energy pumps in until clipping
#    saturates it, so it never decays -- a surface that plays itself.
VARIANTS["self_oscillating"] = dict(
    membrane=lambda: Membrane(n=N, radius=30, loss_shift=14,
                              gain_mask=disc(22, 26, 6)),
    strike=(40, 38), pickup=(30, 44), secs=5.0)

# 5. An annulus. The hole is a second boundary, so the mode set is a Bessel
#    series crossed with a cavity -- gong-adjacent, but not a gong.
_ann = circular_mask(N, 30) & ~circular_mask(N, 13)
VARIANTS["annulus"] = dict(
    membrane=lambda: Membrane(n=N, mask=_ann, loss_shift=16),
    strike=(32, 6), pickup=(50, 32), secs=5.0)


def analyse(name, x):
    env = np.convolve(np.abs(x), np.ones(480) / 480, mode="same")
    pk = env.max()
    below = np.where(env < pk / 100)[0]
    below = below[below > 480]
    audible = below[0] / FS if len(below) else len(x) / FS
    seg = x[:int(FS * 0.5)] * np.hanning(int(FS * 0.5))
    sp = np.abs(np.fft.rfft(seg))
    fr = np.fft.rfftfreq(len(seg), 1 / FS)
    cen = float((sp * fr).sum() / sp.sum())
    print(f"  {name:18s} audible {audible:5.2f}s  centroid {cen:6.0f} Hz  "
          f"rms {np.sqrt((x ** 2).mean()):.3f}  peak {np.max(np.abs(x)):.2f}")


if __name__ == "__main__":
    for i, (name, cfg) in enumerate(VARIANTS.items(), start=1):
        m = cfg["membrane"]()
        m.strike(*cfg["strike"], amp=0.9, width=1.6)
        n = int(FS * cfg["secs"])
        out = np.zeros(n)
        for k in range(n):
            m.step()
            out[k] = m.pickup(*cfg["pickup"])
        write_wav(f"w{i}_{name}.wav", out)
        analyse(name, out)
