"""Final demo renders: audio you can judge by ear, plus the membrane state."""

import numpy as np
from mesh_model import render, write_wav, FS

# loss_shift 15 rings for several seconds; 13 is a tighter, drier hit.
LONG, TIGHT = 15, 13

print("centre strike (tight)")
a, frames = render(seconds=3.0, strike_at=(32, 32), pickup_at=(20, 38),
                   loss_shift=TIGHT, capture_frames=8)
write_wav("01_centre_strike.wav", a)

print("off-centre strike, same patch -- position is timbre")
b, _ = render(seconds=3.0, strike_at=(46, 32), pickup_at=(20, 38),
              loss_shift=TIGHT)
write_wav("02_offcentre_strike.wav", b)

print("long decay, struck near the rim")
c, _ = render(seconds=4.0, strike_at=(48, 38), pickup_at=(20, 26),
              loss_shift=LONG)
write_wav("03_rim_long.wav", c)

print("smaller membrane -- higher pitch, same code")
d, _ = render(seconds=3.0, n=64, radius=18, strike_at=(32, 32),
              pickup_at=(24, 38), loss_shift=LONG)
write_wav("04_small_high.wav", d)

print("driven by external audio -- resonator, not a drum")
t = np.arange(int(FS * 3.0)) / FS
rng = np.random.default_rng(3)
burst = rng.normal(0, 1, len(t)) * np.exp(-((t % 0.75) * 25.0))
e, _ = render(seconds=3.0, strike_at=(32, 32), pickup_at=(20, 38),
              loss_shift=LONG, excite=burst)
write_wav("05_resonator_driven.wav", e)

# --- membrane state, which is what the screen would show ------------------
from PIL import Image

n = frames[0].shape[0]
strip = Image.new("RGB", (n * len(frames) + 4 * (len(frames) - 1), n), (12, 12, 14))
for i, f in enumerate(frames):
    v = np.clip(f / (np.max(np.abs(f)) or 1), -1, 1)
    r = np.clip(np.where(v > 0, v, 0) * 255 * 1.6, 0, 255)
    g = np.clip(np.abs(v) * 255 * 0.55, 0, 255)
    bl = np.clip(np.where(v < 0, -v, 0) * 255 * 1.9, 0, 255)
    rgb = np.dstack([r, g, bl]).astype(np.uint8)
    strip.paste(Image.fromarray(rgb), (i * (n + 4), 0))
strip = strip.resize((strip.width * 3, strip.height * 3), Image.NEAREST)
strip.save("membrane_frames.png")
print(f"wrote membrane_frames.png ({len(frames)} frames)")
