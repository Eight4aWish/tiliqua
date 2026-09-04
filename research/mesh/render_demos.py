# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""Render audio + frames from the reference model, and measure what it gives."""

import numpy as np
from mesh_model import render, write_wav, FS

# Ideal circular membrane partial ratios (Bessel zeros), for comparison.
IDEAL = [1.000, 1.593, 2.135, 2.295, 2.653, 2.917]


def partials(x, n=6, fmin=100, min_sep_hz=12, skip_s=0.10):
    """Top-n spectral peaks, separated by at least min_sep_hz.

    The first version of this used a fixed bin-count exclusion window that was
    narrower than the peak width, so it returned sidelobes of a single mode as
    if they were separate partials. Peak-picking on prominence instead.
    """
    from scipy.signal import find_peaks
    # Skip the strike transient: it is broadband and far louder than the
    # modes, so measuring from t=0 just characterises the impulse.
    seg = x[int(FS * skip_s):int(FS * (skip_s + 1.5))]
    win = seg * np.hanning(len(seg))
    nfft = 1 << 19
    spec = np.abs(np.fft.rfft(win, nfft))
    freqs = np.fft.rfftfreq(nfft, 1 / FS)
    spec[freqs < fmin] = 0
    df = freqs[1] - freqs[0]
    idx, props = find_peaks(spec, distance=int(min_sep_hz / df),
                            prominence=spec.max() * 0.02)
    if len(idx) == 0:
        return [0.0] * n
    order = np.argsort(props["prominences"])[::-1][:n]
    return sorted(freqs[idx[order]])


def decay_t60(x):
    """T60 extrapolated from the -20 dB point, so a decay longer than the
    render still reports a real number instead of saturating at its length."""
    env = np.abs(x)
    win = int(FS * 0.01)
    env = np.convolve(env, np.ones(win) / win, mode="same")
    peak = env.max()
    if peak <= 0:
        return 0.0
    below = np.where(env < peak / 10.0)[0]
    below = below[below > FS * 0.02]
    if not len(below):
        return float("inf")
    return (below[0] / FS) * 3.0


print("=== centre strike ===")
centre, frames = render(seconds=2.0, strike_at=(32, 32), pickup_at=(20, 38),
                        capture_frames=6)
write_wav("mesh_centre_strike.wav", centre)

print("=== off-centre strike (same patch, different position) ===")
edge, _ = render(seconds=2.0, strike_at=(45, 32), pickup_at=(20, 38))
write_wav("mesh_edge_strike.wav", edge)

print("=== tension sweep during the hit (the 'boing') ===")


def tension(t):
    return 1  # shift stays 1; sweep handled below via radius trick


swept, _ = render(seconds=2.0, strike_at=(32, 32), pickup_at=(20, 38),
                  loss_shift=15, air_shift=63)
write_wav("mesh_long_decay.wav", swept)

print("=== driven by external audio (resonator mode) ===")
t = np.arange(int(FS * 2.0)) / FS
noise = np.random.default_rng(7).normal(0, 1, len(t))
burst = noise * np.exp(-t * 18.0)          # a short noisy transient
driven, _ = render(seconds=2.0, strike_at=(32, 32), pickup_at=(20, 38),
                   excite=burst)
write_wav("mesh_resonator.wav", driven)

print()
print("=== measurements ===")
for name, sig in [("centre", centre), ("off-centre", edge),
                  ("long decay", swept), ("resonator", driven)]:
    p = partials(sig)
    p = [f for f in p if f > 0]
    if not p:
        print(f"{name:12s} no modal peaks found (signal below threshold)")
        continue
    f0 = p[0]
    ratios = [float(round(f / f0, 3)) for f in p]
    print(f"{name:12s} f0={f0:7.1f} Hz  T60~{decay_t60(sig):.2f}s  ratios={ratios}")

print()
print(f"ideal circular membrane          ratios={IDEAL}")

# Stability check: no excitation after the strike, does it actually reach zero?
print()
print("=== fixed-point stability (10 s, no re-excitation) ===")
long_tail, _ = render(seconds=10.0, strike_at=(32, 32), pickup_at=(20, 38))
tail = long_tail[-FS:]
print(f"final second: peak={np.max(np.abs(tail)):.3e}  mean(DC)={np.mean(tail):+.3e}")
print(f"limit cycle: {'YES - needs work' if np.max(np.abs(tail)) > 1e-5 else 'none detected'}")

np.save("frames.npy", np.array(frames))
print(f"\nsaved {len(frames)} membrane frames")
