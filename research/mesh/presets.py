# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""
Generate the PRESETS table in mesh.py for any grid size.

Each preset carries `inv_mu_q`, the fundamental eigenvalue of the discrete
Laplacian on that preset's masked domain, as round(2**INV_MU_FRAC / -mu).
Folding 1/-mu into the tuning table is what normalises pitch across the
geometries: a given CV is the same note on every preset. Without it the thin
ring sits two and a half octaves above the full disc.

mu is a property of the *masked domain*, so it has to be computed against the
mask mesh.py actually builds -- not the idealised annulus in pitch.py, whose
centre sits at (n-1)/2 and which has neither the solid-head guard, the square
hole nor the slit. This file reproduces mesh.py's mask exactly, and asserts it
regenerates the shipped n=32 numbers before it will emit anything else.

    python presets.py            # verify against the shipped table
    python presets.py 48 64      # and emit tables for those grid sizes

Radii scale with n: the shipped set was authored on a 32x32 grid, where the
outer radius is 14 of a possible 14 (the assert in mesh.py keeps every preset
two cells clear of the array edge, because the delay-line taps wrap across
rows and rely on those nodes being masked off).
"""

import sys

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

INV_MU_FRAC = 10        # matches lacuna.py

# The shipped 32x32 table, as (outer, inner, square, slit, inv_mu_q).
# Regenerating these exactly is the test that this file is right.
SHIPPED_N32 = [
    (14,  0, 0, 0, 36410),    # drum head
    (10,  0, 0, 0, 19196),    # medium head
    ( 7,  0, 0, 0,  9324),    # small head
    (14,  3, 0, 0, 14811),    # narrow hole
    (14,  7, 0, 0,  6410),    # wide ring
    (14, 11, 0, 0,  1535),    # thin ring
    (14,  5, 1, 0,  9858),    # square hole
    (14,  8, 0, 1,  4765),    # slit ring
]

NAMES = ["drum head", "medium head", "small head", "narrow hole",
         "wide ring", "thin ring", "square hole", "slit ring"]


def mask_for(n, outer, inner, square, slit):
    """The mask mesh.py builds, cell for cell.

    Mirrors the `inside` expression: the centre is n//2 (an integer, not
    (n-1)/2), `inner == 0` means solid rather than a one-cell pinhole, the
    square hole replaces the circular one, and the slit is cut afterwards.
    """
    jy, jx = np.mgrid[0:n, 0:n]
    cx = cy = n // 2
    dx, dy = jx - cx, jy - cy
    d2 = dx * dx + dy * dy

    inside = d2 <= outer * outer
    if square:
        in_square = (dx < inner) & (dx > -inner) & (dy < inner) & (dy > -inner)
        inside &= ~in_square
    elif inner != 0:                       # inner == 0 is a solid head
        inside &= d2 > inner * inner
    if slit:
        in_slit = (dy < 2) & (dy > -2) & (dx > 0)
        inside &= ~in_slit
    return inside


def laplacian_eigenvalue(mask):
    """Fundamental (smallest-magnitude) eigenvalue of the masked Laplacian."""
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


def inv_mu_q(mask):
    return int(round((1 << INV_MU_FRAC) / -laplacian_eigenvalue(mask)))


def scaled_presets(n):
    """The eight presets with radii scaled from the 32x32 originals.

    mesh.py asserts outer <= n//2 - 2, so the largest preset lands exactly on
    that bound at every size, as it does at 32.
    """
    limit = n // 2 - 2
    out = []
    for outer, inner, square, slit, _ in SHIPPED_N32:
        o = max(1, round(outer * limit / 14))
        i = round(inner * limit / 14) if inner else 0
        if inner and i == 0:
            i = 1                       # never collapse a hole into a solid head
        out.append((o, i, square, slit))
    return out


def emit(n, presets):
    print(f"# {n}x{n}: outer radius bound is {n // 2 - 2}")
    print("PRESETS = [")
    for (o, i, sq, sl), name in zip(presets, NAMES):
        mask = mask_for(n, o, i, sq, sl)
        q = inv_mu_q(mask)
        print(f"    ({o:2d}, {i:2d}, {sq}, {sl}, {q:6d}),    "
              f"# {name}, {int(mask.sum())} cells")
    print("]")


def verify():
    print("Regenerating the shipped 32x32 table:\n")
    print(f"  {'preset':>12} {'shipped':>8} {'computed':>9} {'cells':>7}")
    ok = True
    for (o, i, sq, sl, want), name in zip(SHIPPED_N32, NAMES):
        mask = mask_for(32, o, i, sq, sl)
        got = inv_mu_q(mask)
        flag = "" if got == want else "   <-- MISMATCH"
        ok &= got == want
        print(f"  {name:>12} {want:8d} {got:9d} {int(mask.sum()):7d}{flag}")
    return ok


if __name__ == "__main__":
    if not verify():
        print("\nThe mask or the scale does not match mesh.py. Not emitting "
              "anything until it does.")
        sys.exit(1)
    print("\nAll eight match. The mask and the fixed-point scale are correct.\n")
    for arg in sys.argv[1:]:
        n = int(arg)
        print()
        emit(n, scaled_presets(n))
