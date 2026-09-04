# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""Measure the mesh's modal ratios against an ideal circular membrane.

This is the number that decides whether the thing can sound like a drum. A
rectilinear FDTD mesh has direction-dependent dispersion: waves travel slower
diagonally than along the axes, so the upper modes land flat of where an ideal
membrane puts them. How flat is the question.
"""

import numpy as np
from scipy.signal import find_peaks
from mesh_model import Membrane, FS

# Ideal circular-membrane mode ratios are j_mn / j_01 (Bessel zeros). Computed
# rather than hand-typed, so the comparison does not silently run out of table.
from scipy.special import jn_zeros

_modes = []
for m in range(9):
    for k, z in enumerate(jn_zeros(m, 7), start=1):
        _modes.append((z, f"{m}{k}"))
_modes.sort()
_j01 = _modes[0][0]
IDEAL = [z / _j01 for z, _ in _modes]
IDEAL_NAMES = [nm for _, nm in _modes]


def modes(radius=30, secs=4.0, n_modes=8):
    # Undamped, struck off-centre so both symmetric and asymmetric modes ring.
    m = Membrane(n=64, radius=radius, loss_shift=63, air_shift=63)
    m.strike(24, 30, amp=0.5, width=1.2)
    n = int(FS * secs)
    out = np.zeros(n)
    for i in range(n):
        m.step()
        out[i] = m.pickup(40, 38)

    seg = out[int(0.02 * FS):] * np.hanning(len(out) - int(0.02 * FS))
    nfft = 1 << 20
    spec = np.abs(np.fft.rfft(seg, nfft))
    freqs = np.fft.rfftfreq(nfft, 1 / FS)
    spec[freqs < 200] = 0
    spec[freqs > 6000] = 0
    df = freqs[1] - freqs[0]
    idx, props = find_peaks(spec, distance=int(8 / df),
                            prominence=spec.max() * 0.01)
    order = np.argsort(props["prominences"])[::-1][:n_modes]
    return sorted(freqs[idx[order]])


found = modes(n_modes=10)
f0 = found[0]
print(f"fundamental: {f0:.1f} Hz   (analytic prediction for R=30 at the Courant "
      f"limit: 433 Hz)")
print()
# Assign each measured peak to its NEAREST ideal mode, not to the ideal list by
# index: a strike and pickup at particular points simply will not excite every
# mode, so the found peaks are a subset and matching by position mislabels them.
print(f"{'mode':>6} {'measured':>10} {'ratio':>8} {'ideal':>8} {'error':>9}")
errs = []
for f in found:
    r = f / f0
    j = int(np.argmin([abs(r - i) for i in IDEAL]))
    err = (r / IDEAL[j] - 1) * 100
    errs.append(abs(err))
    print(f"{IDEAL_NAMES[j]:>6} {f:9.1f}Hz {r:8.3f} {IDEAL[j]:8.3f} {err:+8.1f}%")
print()
print(f"mean |error| across matched modes: {np.mean(errs):.1f}%")
print(f"worst: {np.max(errs):.1f}%   (a semitone is 5.9%)")
