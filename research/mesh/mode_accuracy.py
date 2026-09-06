# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""
How closely each grid size reproduces a real circular membrane.

The reason to want a larger mesh is not "more cells". It is that a rectilinear
grid approximates a circle by staircasing it, and the coarser the grid the
worse the mode ratios get -- the drum stops sounding like a drum and starts
sounding like a plate. This measures that directly.

The ideal ratios are the Bessel zeros j(m,n) / j(0,1), which are a property of
the circular membrane and independent of its size. The measured ones come from
the eigenvalues of the discrete Laplacian on the mask mesh.py actually builds,
with f proportional to sqrt(-mu) in the low-tension limit.

Worth knowing before reading the output: research/mesh/README.md's published
accuracy table -- 0.6% worst case out to 9.4x the fundamental -- was measured
at radius 30, which needs a 64x64 grid. LACUNA shipped at radius 14 on 32x32.

    python mode_accuracy.py
"""

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

from presets import mask_for

# j(m,n) / j(0,1) for the circular membrane. Modes with m >= 1 are doubly
# degenerate; the grid splits each pair slightly, which is itself a measure of
# how round the mask is.
IDEAL = [
    ("01", 1.0000), ("11", 1.5933), ("21", 2.1355), ("02", 2.2954),
    ("31", 2.6531), ("12", 2.9173), ("41", 3.1555), ("22", 3.4998),
    ("03", 3.5985), ("51", 3.6474),
]
DEGENERATE = {"11", "21", "31", "41", "51", "12", "22"}


def eigenvalues(mask, k):
    n = mask.shape[0]
    idx = -np.ones((n, n), int)
    nodes = np.argwhere(mask)
    for j, (r, c) in enumerate(nodes):
        idx[r, c] = j
    rows, cols, vals = [], [], []
    for j, (r, c) in enumerate(nodes):
        rows.append(j); cols.append(j); vals.append(-4.0)
        for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < n and 0 <= cc < n and mask[rr, cc]:
                rows.append(j); cols.append(idx[rr, cc]); vals.append(1.0)
    L = sp.csr_matrix((vals, (rows, cols)), shape=(len(nodes), len(nodes)))
    ev = spla.eigsh(L, k=k, sigma=0, which='LM', return_eigenvectors=False)
    return np.sort(-ev)[::-1][::-1]        # ascending magnitude, positive


def measured_ratios(mask, want):
    """Ratios to the fundamental, collapsing each degenerate pair to its mean."""
    ev = eigenvalues(mask, want * 2 + 4)
    f = np.sqrt(ev)
    f = f / f[0]
    out, i = [], 0
    for name, _ in IDEAL[:want]:
        if name in DEGENERATE and i + 1 < len(f) and abs(f[i + 1] - f[i]) < 0.06:
            out.append((0.5 * (f[i] + f[i + 1]), abs(f[i + 1] - f[i])))
            i += 2
        else:
            out.append((f[i], 0.0))
            i += 1
    return out


def report(sizes):
    print("Mode ratios against the ideal circular membrane\n")
    head = "  mode   ideal" + "".join(f"{f'R={r} (n={n})':>18}" for n, r in sizes)
    print(head)
    print("  " + "-" * (len(head) - 2))

    tables = {}
    for n, r in sizes:
        tables[(n, r)] = measured_ratios(mask_for(n, r, 0, 0, 0), len(IDEAL))

    worst = {k: 0.0 for k in tables}
    for j, (name, ideal) in enumerate(IDEAL):
        row = f"  {name:>4}  {ideal:6.4f}"
        for key in tables:
            got, split = tables[key][j]
            err = 100.0 * (got - ideal) / ideal
            worst[key] = max(worst[key], abs(err))
            row += f"{got:9.4f}{err:+7.2f}%"
        print(row)

    print("  " + "-" * (len(head) - 2))
    row = "  worst        "
    for key in tables:
        row += f"{worst[key]:15.2f}%  "
    print(row)

    print("\nSplit of the degenerate pairs (0 would be a perfect circle):")
    for (n, r), tab in tables.items():
        splits = [s for (_, s) in tab if s > 0]
        print(f"  R={r:2d} (n={n}):  mean {np.mean(splits) * 100:5.2f}%   "
              f"max {np.max(splits) * 100:5.2f}%")


if __name__ == "__main__":
    report([(32, 14), (48, 22), (64, 30)])
    print("\nR=14 is what LACUNA ships. R=30 is what research/mesh/README.md's")
    print("published accuracy table was measured on.")
