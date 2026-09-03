"""
Reference model for a 2D FDTD membrane, in the exact arithmetic the gateware
will use. Two jobs:

  1. Decide whether this is musically worth building before any gateware exists.
  2. Fix the fixed-point widths, damping shifts and rounding, and prove the
     loop is stable, so the Amaranth core has a reference to be tested against.

The update is the leapfrog scheme at the Courant limit, which is what makes it
multiplier-free:

    u_next = ((u[N] + u[S] + u[E] + u[W]) >> 1) - u_prev

Damping and frequency-dependent loss are shifts, not multiplies.
"""

import numpy as np

FS = 48000
# 24-bit signed state. 18 bits is not enough: the loss term is a right shift by
# `loss_shift`, so it does nothing at all once |u| < 2**loss_shift -- with 17
# fractional bits and loss_shift=13 that dead zone sits at -24 dBFS, which
# freezes the tail and leaves audible DC. 22 fractional bits pushes it below the
# noise floor. Cost in gateware: 4096 nodes x 24 bits x 2 histories = 197 kbit
# of the ECP5-25F's 1008 kbit, and slightly wider adders. No multipliers either way.
FRAC = 22
ONE = 1 << FRAC


def circular_mask(n, radius):
    y, x = np.ogrid[:n, :n]
    c = (n - 1) / 2.0
    return ((x - c) ** 2 + (y - c) ** 2) <= radius ** 2


class Membrane:
    """
    Fixed-point 2D mesh. `loss_shift` sets overall decay (larger = longer),
    `air_shift` sets frequency-dependent loss (highs decay faster), which is
    most of what separates a real membrane from a ringing metal plate.
    """

    def __init__(self, n=64, radius=30, loss_shift=13, air_shift=63,
                 wrap=False, aniso=None, gain_mask=None, mask=None):
        """
        `wrap`      -- torus topology instead of a fixed rim: no reflections, so
                       waves circulate indefinitely and the modes are no longer
                       Bessel at all.
        `aniso`     -- (east_west_weight, north_south_weight) as eighths, summing
                       to 4 for stability. (2,2) is isotropic; (3,1) is a
                       membrane stretched harder in one axis than any real one.
        `gain_mask` -- boolean region that AMPLIFIES instead of damping. Energy
                       pumps in, clipping saturates it, and the thing never
                       decays: a self-oscillating surface.
        `mask`      -- arbitrary boundary shape. Any bitmap is a valid drum.
        """
        self.n = n
        self.mask = circular_mask(n, radius) if mask is None else mask
        self.wrap = wrap
        self.aniso = aniso
        self.gain_mask = gain_mask
        self.u = np.zeros((n, n), dtype=np.int64)
        self.u_prev = np.zeros((n, n), dtype=np.int64)
        self.loss_shift = loss_shift
        self.air_shift = air_shift

    def strike(self, x, y, amp=0.9, width=2.0):
        yy, xx = np.ogrid[:self.n, :self.n]
        blob = np.exp(-(((xx - x) ** 2 + (yy - y) ** 2) / (2 * width ** 2)))
        self.u += (blob * amp * ONE).astype(np.int64) * self.mask

    def step(self, inject=0, inject_at=None, tension_shift=1):
        u, up = self.u, self.u_prev

        if self.wrap:
            ns = np.roll(u, 1, 0) + np.roll(u, -1, 0)
            ew = np.roll(u, 1, 1) + np.roll(u, -1, 1)
        else:
            ns = np.zeros_like(u)
            ns[1:, :] += u[:-1, :]
            ns[:-1, :] += u[1:, :]
            ew = np.zeros_like(u)
            ew[:, 1:] += u[:, :-1]
            ew[:, :-1] += u[:, 1:]

        if self.aniso is None:
            neigh = ns + ew
        else:
            # Weights in eighths summing to 4, so the >> tension_shift below
            # still lands at the Courant limit. Multiplying by 3 is a shift and
            # an add, so this stays multiplier-free.
            we, wn = self.aniso
            neigh = ((ew * we) + (ns * wn)) >> 1

        # (sum >> 1) - u_prev, with rounding rather than truncation: truncation
        # in this loop is what produces limit cycles and DC drift.
        nxt = ((neigh + (1 << (tension_shift - 1))) >> tension_shift) - up

        # Overall decay: u - (u >> loss_shift). With 22 fractional bits the
        # shift's dead zone is far below the noise floor, so no minimum-decrement
        # hack is needed -- that trick works, but it imposes a linear decay that
        # eats the quiet tail, which is most of what makes a struck membrane
        # sound alive.
        if self.gain_mask is None:
            nxt -= np.sign(nxt) * (np.abs(nxt) >> self.loss_shift)
        else:
            # Damp everywhere, amplify inside the gain region.
            dec = np.sign(nxt) * (np.abs(nxt) >> self.loss_shift)
            nxt = np.where(self.gain_mask, nxt + dec, nxt - dec)

        # Frequency-dependent loss. NOTE: pulling each node toward the average
        # of its neighbours does NOT do this -- `nxt` is u[n+1] while the
        # neighbour average is u[n], so their difference is dominated by the
        # temporal term and it damps broadband, 66 dB in half a second. Proper
        # HF loss needs the Laplacian of the velocity, i.e. a neighbour sum of
        # u_prev as well as of u, which doubles the memory traffic per node.
        # Deferred: air_shift >= 63 disables it, and the tilt comes from a
        # one-pole on the output tap for now.
        if self.air_shift < 32:
            smooth = (neigh + (1 << 1)) >> 2
            nxt -= (nxt - smooth) >> self.air_shift

        if inject and inject_at is not None:
            nxt[inject_at[1], inject_at[0]] += inject

        nxt *= self.mask
        np.clip(nxt, -(1 << 27), (1 << 27) - 1, out=nxt)

        self.u_prev, self.u = self.u, nxt

    def pickup(self, x, y):
        return self.u[y, x] / ONE


def render(seconds=2.0, strike_at=(32, 32), pickup_at=(20, 38), n=64, radius=30,
           loss_shift=None, air_shift=None, tension_env=None, excite=None,
           capture_frames=None, membrane=None):
    # Defaults are None, not literals: an earlier version repeated the Membrane
    # defaults here, so fixing them in Membrane silently did nothing and every
    # demo rendered with the broken broadband damping still on -- a 10 ms click
    # and three seconds of silence. Never restate a default in two places.
    if membrane is not None:
        m = membrane
    else:
        kw = {}
        if loss_shift is not None:
            kw["loss_shift"] = loss_shift
        if air_shift is not None:
            kw["air_shift"] = air_shift
        m = Membrane(n=n, radius=radius, **kw)
    if excite is None:
        m.strike(*strike_at)

    total = int(FS * seconds)
    out = np.zeros(total, dtype=np.float64)
    frames = []
    frame_every = max(1, total // (capture_frames or 1)) if capture_frames else None

    for i in range(total):
        ts = 1 if tension_env is None else tension_env(i / total)
        inj = 0
        if excite is not None:
            inj = int(excite[i] * 0.25 * ONE)
        m.step(inject=inj, inject_at=strike_at, tension_shift=ts)
        out[i] = m.pickup(*pickup_at)
        if frame_every and i % frame_every == 0 and len(frames) < (capture_frames or 0):
            frames.append(m.u.copy() / ONE)

    return out, frames


def write_wav(path, data, fs=FS):
    import wave
    peak = np.max(np.abs(data)) or 1.0
    pcm = np.clip(data / peak * 0.89, -1, 1)
    pcm = (pcm * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(pcm.tobytes())
    print(f"wrote {path}  peak={peak:.4f}")
