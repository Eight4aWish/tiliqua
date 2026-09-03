"""Capture the first few milliseconds after a strike -- the wave actually
propagating and reflecting off the rim. This is what the screen would show at
60 fps, straight out of the same BRAM the audio is read from."""

import numpy as np
from PIL import Image
from mesh_model import Membrane, FS

CAPTURE_MS = [0.1, 0.4, 0.8, 1.3, 1.9, 2.6, 3.4, 4.3, 5.3, 6.5, 8.0, 10.0]

m = Membrane(n=64, radius=30, loss_shift=15, air_shift=63)
m.strike(44, 32, amp=0.9, width=1.4)   # off-centre, so reflections are visible

want = [int(ms * FS / 1000) for ms in CAPTURE_MS]
frames, i, k = [], 0, 0
while k < len(want):
    m.step()
    i += 1
    if i >= want[k]:
        frames.append(m.u.copy())
        k += 1

# One shared scale across all frames, so later frames genuinely look quieter.
scale = max(np.max(np.abs(f)) for f in frames) * 0.45

cols, n = 6, frames[0].shape[0]
rows = (len(frames) + cols - 1) // cols
pad = 4
img = Image.new("RGB", (cols * n + (cols - 1) * pad, rows * n + (rows - 1) * pad),
                (10, 10, 12))
for idx, f in enumerate(frames):
    v = np.clip(f / scale, -1, 1)
    pos, neg = np.clip(v, 0, 1), np.clip(-v, 0, 1)
    r = pos * 255 + neg * 40
    g = (pos * 120 + neg * 90)
    b = (pos * 30 + neg * 255)
    rgb = np.dstack([r, g, b]).astype(np.uint8)
    x, y = (idx % cols) * (n + pad), (idx // cols) * (n + pad)
    img.paste(Image.fromarray(rgb), (x, y))

img = img.resize((img.width * 4, img.height * 4), Image.NEAREST)
img.save("membrane_propagation.png")
print("wrote membrane_propagation.png at t =", CAPTURE_MS, "ms")
