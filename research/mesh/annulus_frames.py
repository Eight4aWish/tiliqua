"""Surface state for the annulus family, plus the morphing one over its sweep."""

import numpy as np
from PIL import Image
from mesh_model import Membrane, FS
from annulus import ring, square_hole, slit_ring, N

MS = [0.3, 1.5, 3.0, 5.0, 8.0, 14.0, 22.0, 35.0]
ROWS = [
    ("thin ring",   ring(30, 25),               (32, 3)),
    ("wide ring",   ring(30, 13),               (32, 6)),
    ("offset hole", ring(30, 12, cx=40, cy=28), (32, 6)),
    ("square hole", square_hole(30, 10),        (32, 6)),
    ("slit ring",   slit_ring(30, 16),          (32, 5)),
]


def capture(mask, strike, ms_list, mask_env=None, secs=None):
    m = Membrane(n=N, mask=mask, loss_shift=16)
    m.strike(*strike, amp=0.9, width=1.6)
    want = [int(ms * FS / 1000) for ms in ms_list]
    frames, i, k = [], 0, 0
    while k < len(want):
        if mask_env is not None and i % 64 == 0:
            m.mask = mask_env(i / want[-1])
        m.step()
        i += 1
        if i >= want[k]:
            frames.append(m.u.copy())
            k += 1
    return frames


def grid(rows, path, scale_per_row=True):
    n, pad = N, 4
    cols = len(rows[0][1])
    img = Image.new("RGB", (cols * n + (cols - 1) * pad,
                            len(rows) * n + (len(rows) - 1) * pad), (10, 10, 12))
    for r, (_, frames) in enumerate(rows):
        sc = max(np.max(np.abs(f)) for f in frames) * 0.4
        for c, f in enumerate(frames):
            v = np.clip(f / sc, -1, 1)
            pos, neg = np.clip(v, 0, 1), np.clip(-v, 0, 1)
            rgb = np.dstack([pos * 255 + neg * 40, pos * 120 + neg * 90,
                             pos * 30 + neg * 255]).astype(np.uint8)
            img.paste(Image.fromarray(rgb), (c * (n + pad), r * (n + pad)))
    img = img.resize((img.width * 3, img.height * 3), Image.NEAREST)
    img.save(path)
    print("wrote", path)


grid([(nm, capture(mask, st, MS)) for nm, mask, st in ROWS], "annulus_family.png")
print("rows:", ", ".join(r[0] for r in ROWS))

# The morph, sampled across the whole 6 s sweep rather than the first 35 ms.
sweep_ms = [20, 700, 1400, 2100, 2800, 3500, 4200, 5200]
frames = capture(ring(30, 4), (32, 6), sweep_ms,
                 mask_env=lambda t: ring(30, 4 + 16 * min(t, 1.0)))
grid([("morph", frames)], "annulus_morph.png")
