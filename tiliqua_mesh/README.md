# 2D membrane mesh — reference model

Numpy model of the 2D FDTD membrane, in the exact fixed-point arithmetic the
Amaranth core will use. Two jobs: decide whether this is musically worth
building before any gateware exists, and fix the widths, shifts and rounding so
the gateware has a reference to be tested against.

Nothing here runs on hardware. That is the point — every question below was
settled without a bitstream.

## What it does

The update is the leapfrog scheme at the Courant limit:

```
u_next = ((u[N] + u[S] + u[E] + u[W]) >> 1) - u_prev
```

Three adds, a shift, a subtract. **No multiplies**, which is why this fits the
ECP5 with all 28 DSP tiles left over.

![wave propagation](membrane_propagation.png)

*Strike at 0.1 ms through 10 ms: the ring expands, reflects off the rim,
collapses back through itself and sets up interference. Orange is positive
displacement, blue negative, one shared scale across all twelve frames.*

## Result: the modes are right

Measured against an ideal circular membrane (ratios computed from Bessel zeros,
not a hand-typed table):

| mode | measured | ratio | ideal | error |
|------|----------|-------|-------|-------|
| 01 | 427.7 Hz | 1.000 | 1.000 | +0.0% |
| 11 | 681.4 Hz | 1.593 | 1.593 | −0.0% |
| 02 | 981.5 Hz | 2.295 | 2.295 | −0.0% |
| 12 | 1247.1 Hz | 2.916 | 2.917 | −0.1% |
| 22 | 1496.8 Hz | 3.499 | 3.500 | −0.0% |
| 23 | 2065.5 Hz | 4.829 | 4.832 | −0.1% |
| 04 | 2093.5 Hz | 4.894 | 4.903 | −0.2% |
| 43 | 2545.2 Hz | 5.950 | 5.977 | −0.4% |
| 44 | 3115.7 Hz | 7.284 | 7.325 | −0.6% |
| 36 | 4028.3 Hz | 9.418 | 9.391 | +0.3% |

Worst case 0.6%, a tenth of a semitone, out to 9.4× the fundamental. The
direction-dependent dispersion a rectilinear mesh is known for is real but does
not bite at this radius and in this frequency range. The fundamental also lands
at 427.7 Hz against an analytic prediction of 433 Hz for R=30 at the Courant
limit, which is the geometry checking out independently.

## What the model has already caught

Four things, none of which would have been obvious from the equations:

1. **18-bit state is not enough.** The loss term is `u -= u >> loss_shift`, which
   does nothing at all once `|u| < 2**loss_shift`. At 17 fractional bits with
   `loss_shift=13` that dead zone sits at −24 dBFS: the tail freezes and parks
   at a DC offset. Fixed by going to 24-bit state (22 fractional bits), which
   costs 197 kbit of the ECP5's 1008 kbit for a 64×64 mesh. Still no multipliers.
2. **Forcing a minimum decrement cures the dead zone but ruins the tail.** It
   imposes a linear decay that eats exactly the quiet ringing that makes a
   struck membrane sound alive. Rejected in favour of the wider state.
3. **The frequency-dependent loss term was wrong.** Pulling each node toward the
   average of its neighbours does not damp highs preferentially: `nxt` is
   u[n+1] while the neighbour average is u[n], so the difference is dominated by
   the temporal term and it damps broadband — 66 dB in half a second, measured.
   Proper HF loss needs the Laplacian of the *velocity*, which means a neighbour
   sum of `u_prev` as well as `u` and so doubles memory traffic per node.
   Currently disabled (`air_shift >= 32`); see below.
4. **A Nyquist-frequency limit cycle** sits in the truncation, at roughly
   −85 dBFS. Inaudible in practice but it never dies. Wider state pushes it down;
   a DC/Nyquist blocker on the output tap would remove it outright.

## Status and what is still open

- Frequency-dependent damping is **not implemented**. Without it every mode
  decays at the same rate, which is the largest remaining gap between this and a
  real membrane — real ones lose highs first. Two options: pay for the second
  neighbour sum, or approximate with a one-pole on the output tap.
- No shell, no air cavity, no nonlinear tension modulation. Those are what
  separate "struck membrane" from "recognisable drum"; see the note in the
  session discussion.
- The peak-finding in `analyse_modes.py` needs a strike and pickup that excite
  the modes you want to see. It reports the modes it finds, not all of them.

## Files

| | |
|---|---|
| `mesh_model.py` | the fixed-point model |
| `analyse_modes.py` | modal ratios vs ideal circular membrane |
| `render_finals.py` | the five demo WAVs |
| `render_propagation.py` | the propagation frames above |
| `render_demos.py` | earlier scratch harness, kept for the measurement notes |

```bash
pip install numpy scipy pillow
python analyse_modes.py
python render_finals.py
```
