"""One row per variant: the surface state as the wave develops."""

import numpy as np
from PIL import Image
from mesh_model import FS
from weird import VARIANTS

MS = [0.3, 1.5, 3.0, 5.0, 8.0, 14.0, 22.0, 35.0]
SHOW = ["torus", "twin_lobes", "stretched", "self_oscillating", "annulus"]

rows = []
for name in SHOW:
    cfg = VARIANTS[name]
    m = cfg["membrane"]()
    m.strike(*cfg["strike"], amp=0.9, width=1.6)
    want = [int(ms * FS / 1000) for ms in MS]
    frames, i, k = [], 0, 0
    while k < len(want):
        m.step()
        i += 1
        if i >= want[k]:
            frames.append(m.u.copy())
            k += 1
    scale = max(np.max(np.abs(f)) for f in frames) * 0.4
    rows.append((name, frames, scale))

n, pad = 64, 4
W = len(MS) * n + (len(MS) - 1) * pad
H = len(rows) * n + (len(rows) - 1) * pad
img = Image.new("RGB", (W, H), (10, 10, 12))
for r, (name, frames, scale) in enumerate(rows):
    for c, f in enumerate(frames):
        v = np.clip(f / scale, -1, 1)
        pos, neg = np.clip(v, 0, 1), np.clip(-v, 0, 1)
        rgb = np.dstack([pos * 255 + neg * 40,
                         pos * 120 + neg * 90,
                         pos * 30 + neg * 255]).astype(np.uint8)
        img.paste(Image.fromarray(rgb), (c * (n + pad), r * (n + pad)))
img = img.resize((img.width * 3, img.height * 3), Image.NEAREST)
img.save("weird_frames.png")
print("rows top to bottom:", ", ".join(SHOW))
print("columns at", MS, "ms")
