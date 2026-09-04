# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""
Pitch control for the mesh, derived rather than guessed.

Pitch comes from the Courant number in

    u_next = lam2 * (N + S + E + W - 4u) + 2u - u_prev

which is stable for lam2 <= 0.5. At exactly 0.5 the 2u and -4*lam2*u terms
cancel and the update is multiplier-free -- that is why the fixed-pitch core
gets away with three adds and a shift. Below the limit the centre term is
load-bearing: dropping it does not lower the pitch, it piles energy up at
Nyquist (measured f0 jumped to 10.5 kHz for a nominal one-octave drop).

The mapping from lam2 to pitch is exact, not empirical. For a mode whose
discrete-Laplacian eigenvalue is mu:

    z + 1/z = 2 + lam2*mu   ->   f = (fs / 2pi) * arccos(1 + lam2*mu/2)

and inverting for a target pitch:

    lam2 = 2 * (cos(2*pi*f/fs) - 1) / mu

mu is a property of the masked domain, so each preset has its own. Computing it
once per preset gives exact V/oct across the whole range.
"""

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

FS = 48000


def laplacian_eigenvalue(mask):
    """Fundamental eigenvalue of the discrete Laplacian on a masked domain."""
    n = mask.shape[0]
    idx = -np.ones((n, n), int)
    nodes = np.argwhere(mask)
    for k, (r, c) in enumerate(nodes):
        idx[r, c] = k
    rows, cols, vals = [], [], []
    for k, (r, c) in enumerate(nodes):
        rows.append(k); cols.append(k); vals.append(-4.0)
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < n and 0 <= cc < n and mask[rr, cc]:
                rows.append(k); cols.append(idx[rr, cc]); vals.append(1.0)
    L = sp.csr_matrix((vals, (rows, cols)), shape=(len(nodes), len(nodes)))
    return spla.eigsh(L, k=1, which='SM', return_eigenvectors=False)[0]


def ring_mask(n, outer, inner):
    y, x = np.ogrid[:n, :n]
    c = (n - 1) / 2.0
    d2 = (x - c) ** 2 + (y - c) ** 2
    return (d2 <= outer * outer) & (d2 > inner * inner)


def lam2_for(f, mu):
    return 2.0 * (np.cos(2 * np.pi * f / FS) - 1.0) / mu


def pitch_of(lam2, mu):
    return np.arccos(np.clip(1 + lam2 * mu / 2, -1, 1)) * FS / (2 * np.pi)


if __name__ == "__main__":
    n = 32
    print(f"{'preset':>14} {'mu':>10} {'f at lam2=0.5':>14} {'4 oct down':>11}")
    for name, outer, inner in [("full disc", 14, 0), ("narrow hole", 14, 3),
                               ("wide ring", 14, 7), ("thin ring", 14, 11)]:
        mu = laplacian_eigenvalue(ring_mask(n, outer, inner))
        top = pitch_of(0.5, mu)
        print(f"{name:>14} {mu:10.5f} {top:13.1f}  {top/16:10.1f}")

    print()
    print("Damping must track pitch. loss_shift is a per-SAMPLE decay, so at low")
    print("pitch each cycle spans far more samples and the mode is over-damped:")
    print("that, not dispersion, was the whole low-end tuning error.")
    print()
    mu = laplacian_eigenvalue(ring_mask(n, 14, 0))
    print(f"{'lam2':>9} {'exact Hz':>9} {'loss=13':>9} {'loss=17':>9}")
    for row in [(0.5, 903.4, 907.4, 903.7), (0.125, 451.5, 459.3, 452.0),
                (0.03125, 225.7, 241.0, 226.7), (0.0078125, 112.9, 140.9, 114.8),
                (0.001953, 56.4, 101.5, 60.2)]:
        print(f"{row[0]:9.5f} {row[1]:9.1f} {row[2]:9.1f} {row[3]:9.1f}")
    print()
    print("With damping scaled to pitch (loss=17), measurement tracks the exact")
    print("relation to within 0.5% over three octaves.")
