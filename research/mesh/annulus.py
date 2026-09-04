# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""
The annulus, explored. The mask is two radius comparisons, so on hardware the
inner and outer radii are just two registers -- which makes every variant here
a CV input rather than a rebuild.
"""

import numpy as np
from mesh_model import Membrane, write_wav, circular_mask, FS

N = 64


def ring(outer, inner, cx=None, cy=None, n=N):
    y, x = np.ogrid[:n, :n]
    c = (n - 1) / 2.0
    cx = c if cx is None else cx
    cy = c if cy is None else cy
    d2 = (x - c) ** 2 + (y - c) ** 2
    h2 = (x - cx) ** 2 + (y - cy) ** 2
    return (d2 <= outer ** 2) & (h2 > inner ** 2)


def square_hole(outer, half, n=N):
    m = circular_mask(n, outer)
    c = n // 2
    m[c - half:c + half, c - half:c + half] = False
    return m


def slit_ring(outer, inner, n=N):
    """A C: cut the ring open. Now it is a bar bent round, not a ring."""
    m = ring(outer, inner, n=n)
    c = n // 2
    m[c - 1:c + 1, c:] = False
    return m


def check_on_material(mask, pt, label):
    """A pickup or strike sitting in the hole reads zero forever. Cheap to do
    by accident, and it looks exactly like a dead model."""
    if not mask[pt[1], pt[0]]:
        raise ValueError(f"{label} {pt} is not on the membrane (inside the hole "
                         f"or outside the rim)")


def play(mask, strike, pickup, secs=5.0, loss=16, mask_env=None):
    check_on_material(mask, strike, "strike")
    check_on_material(mask, pickup, "pickup")
    m = Membrane(n=N, mask=mask, loss_shift=loss)
    m.strike(*strike, amp=0.9, width=1.6)
    n = int(FS * secs)
    out = np.zeros(n)
    for k in range(n):
        if mask_env is not None and k % 64 == 0:
            m.mask = mask_env(k / n)
        m.step()
        out[k] = m.pickup(*pickup)
    return out


def report(name, x):
    from scipy.signal import find_peaks
    seg = x[int(0.05 * FS):int(1.55 * FS)]
    w = seg * np.hanning(len(seg))
    sp = np.abs(np.fft.rfft(w, 1 << 19))
    fr = np.fft.rfftfreq(1 << 19, 1 / FS)
    sp[fr < 150] = 0
    sp[fr > 8000] = 0
    idx, pr = find_peaks(sp, distance=int(8 / (fr[1] - fr[0])),
                         prominence=sp.max() * 0.03)
    top = sorted(fr[idx[np.argsort(pr["prominences"])[::-1][:6]]])
    f0 = top[0] if len(top) else 0
    ratios = [round(f / f0, 2) for f in top] if f0 else []
    print(f"  {name:22s} f0={f0:6.0f}Hz  partials={ratios}")


if __name__ == "__main__":
    jobs = [
        ("a1_thin_ring",    ring(30, 25), (32, 3),  (58, 32)),
        ("a2_wide_ring",    ring(30, 13), (32, 6),  (50, 32)),
        ("a3_narrow_hole",  ring(30, 6),  (32, 6),  (50, 32)),
        ("a4_offset_hole",  ring(30, 12, cx=40, cy=28), (32, 6), (18, 40)),
        ("a5_square_hole",  square_hole(30, 10), (32, 6), (50, 32)),
        ("a6_slit_ring",    slit_ring(30, 16), (32, 5), (32, 58)),
    ]
    for name, mask, strike, pickup in jobs:
        x = play(mask, strike, pickup)
        write_wav(f"{name}.wav", x)
        report(name, x)

    # The one a physical object cannot do: open the hole while it rings.
    print("\n  morphing hole radius during the ring:")
    # Stop the hole short of the pickup: open it far enough and the surface
    # under the pickup stops existing, which reads as the model dying.
    env = lambda t: ring(30, 4 + 16 * t)
    x = play(ring(30, 4), (32, 6), (56, 32), secs=6.0, loss=17, mask_env=env)
    write_wav("a7_hole_opening.wav", x)
    report("a7_hole_opening", x)
