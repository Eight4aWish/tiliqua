# Gold at 48×48 — the noise, and what it is not

## Closed, 7 September: it was not in the gateware

The noise was **the external modulation source**. Bisecting the patch settled
it in two moves: unplug in2 and in3 and Gold is clean at every preset and
every radius; put a different, more stable source on in2 and it stays clean.
The scan circle drawn on screen visibly jittered in and out with the original
source patched, which is the same signal arriving at the display and the audio
path — so the module was faithfully rendering a wobbly CV.

Everything below is kept because the measurements are real and cost a day to
make. **Read it as a record of what the gateware was cleared of, not as an open
hunt.** Two genuine faults did surface along the way and are listed at the end.

The general lesson, which was expensive: *six hypotheses in, nobody had
unplugged a cable.* The investigation stayed inside the design because the
design was what could be measured, and the one experiment that separated
"module" from "rack" needed no instruments at all.

---

The original investigation follows.
**Six hypotheses were tested and five are dead.** The measurements are in this
file; re-deriving them costs a day.

## The symptom

Reported by ear, 7 September, on Gold 48×48 with damping on in3:

- A broadband noise floor about 25 dB below the partials — audible as static
- **Across the whole range of in2**, not only at the extremes
- **Independent of damping** — "100% sure"
- Rare or absent on the 32×32 instrument, and there possibly associated with
  moving the centre hole

Measured from a Retrospective capture of the direct outputs (ch12/13 — the pair
measuring +0.05 correlation, which is Gold's signature; ch6/7 at +0.71 is the
stereo mix bus and shows the same floor, so the noise is not the recording
chain):

| band | partials | floor under them | separation |
| --- | --- | --- | --- |
| 80–300 Hz | 0.0 dB | −24.7 | **24.7 dB** |
| 300 Hz–1 kHz | −2.0 | −26.5 | 24.5 dB |
| 1–3 kHz | −8.8 | −29.5 | 20.7 dB |
| 3–8 kHz | −20.3 | −35.0 | 14.7 dB |

Also worth knowing: **there is no tonal content above about 2–3 kHz.** Mean and
median spectra converge there. The move from 64 to 128 scan points was made to
push the table's folding limit from ~2 kHz to ~4 kHz, and the spectrum says
there was nothing up there to fold. That change appears to have bought nothing.

## Dead — do not re-test

| hypothesis | how it died |
| --- | --- |
| Drive accumulation with damping | The noise is damping-independent by ear. The mechanism requires it to scale with `LOSS_SHIFT` |
| Mallet too small for the bigger membrane | 48×48 measures **smoother** than 32×32 at the same mallet — 93.2% neighbour agreement against 86.7%. Larger mallets (4, 5, 6) buy nothing: 92.7–93.2% |
| Output clipping | 3 isolated samples ≥0.99 in 60 s, longest run **1 sample**, all at one instant. 39 samples of 2.6M exceed half scale |
| Internal membrane saturation | Simulated with Gold's drone path at a 5 V gate: peak node 12–30% of full scale, **zero clamp events**, at both sizes and at `LOSS_SHIFT` 10 and 14 |
| 64 → 128 scan points | Measured identical roughness pickup at both counts, every radius, both sizes |

## Real, but does not fit the symptom

**Boundary grazing.** The mask is hard — a cell is in the membrane or it is
zero — and a scan one cell from a boundary has its bilinear footprint straddle
that step. Measured against the same field with no mask:

| grid | at one cell of clearance | at two cells |
| --- | --- | --- |
| 32×32 | artifact 42.6 dB below signal | clean (>60 dB) |
| 48×48 | artifact **33.1 dB** below signal | clean (>60 dB) |

Nine to ten dB worse on the wider membrane, because a longer boundary means
more of the scan touches it. Holed presets have an inner boundary as well as a
rim, and both measure the same.

**This is worth fixing** — two cells of clearance at each end makes every
preset clean at both sizes. `rad_span` becomes `outer − inner − 4`, the base
becomes `inner + 2`, and `rad_max` becomes `outer − 2`, with a guard for the
32×32 thin ring, which is three cells wide and has no clean radius at all.

**But it is not the reported symptom**, because it only bites at the extreme
ends of in2's travel and the noise is heard across the whole range. Fix it on
its own merits, not as the answer to this.

## Still open

`CIRC_SCALE = 6` caps the scan's own accuracy at 26–33 dB, which brackets the
measured floor — so the circle ROM's six-bit cosine table is plausibly the
dominant noise source. Raising it to 8 measured 5–8 dB better, and needs the
ROM widened from 16 bits to 32 so cos and sin get 16 each.

**It does not explain the novelty**: it degrades only 2–3 dB between the two
grid sizes, and the symptom is described as new rather than slightly worse.

## The strongest lead, found last — and wrong

Kept because the underlying observation is true and still matters: **the design
is marginal at 60 MHz and seed choice decides whether it closes.** It was not
the cause of the static, but it is a real fragility.

Measured across builds at the 60.00 MHz target: 52.75, 55.92, 63.40, 63.48,
65.83, 66.74. That is a 14 MHz spread from placement luck alone on the same
source, and roughly half the seeds tried do not close at all. Any build that
ships needs its timing report read, not assumed.

The reasoning that made this look like the answer:

Trying to build Gold at 32x32 for an A/B, all five seeds failed `sync`:
55.9, 57.0, 58.0, 58.0, 59.3 MHz against 60.00. Consistent across seeds, so it
is structural rather than placement luck -- and this is the *smaller* design.
The 48x48 build currently flashed passed at 60.59, which is 1% margin, and now
looks like the lucky seed rather than a healthy one.

The timeline fits:

| build | reported |
| --- | --- |
| addrfix, constant `LOSS_SHIFT` | circle and sound agree; lost some natural reverb. **No static** |
| damping on in3 | reverb-free richness back, **and static** |

A sync domain intermittently missing timing produces wrong arithmetic now and
then, which sounds like static, has no dependence on scan position, and does
not care what the damping CV is set to. That satisfies every constraint the
signal-path hypotheses could not: whole range, damping-independent, new.

**Test first, before anything else:** rebuild 48x48 with `LOSS_SHIFT` back to a
compile-time constant. It closed at 63-65 MHz before the CV existed. If the
static goes, it was timing all along, and the damping control has to be bought
back another way -- a pipeline stage in the update, fewer shift values, or the
encoder rather than a jack.

Every hypothesis above assumed the arithmetic was correct and hunted for a
signal-path cause. None of them considered that the arithmetic might simply be
wrong.

## Two warnings for whoever picks this up

**Beware the metric.** Two measurements in this investigation gave confident
numbers and were measuring the wrong thing: one counted a larger circle's
legitimate harmonic complexity as noise, and one used a smoothed reference that
removed real spatial structure along with the roughness. Always compare against
the *same* field read a *better* way, never against a different field.

**The 48×48 build that "sounded rich" was broken.** It read a scrambled path
through the membrane — `<< (n-1).bit_length()` is a multiply by n only when n
is a power of two. So "it used to sound better" may be comparing against that,
and the correctly-addressed scan may simply expose what was always there.

## The two real faults the hunt turned up

Neither caused the static, and both are worth having found.

**Silver's decay ran off the end of its own table.** `base_loss` is derived
from the grid size and comes out at 14 for 48×48; the octave term adds one more
on the bottom half of the pitch range, giving `loss_shift` 15. `LOSS_MAX` was
14, so the eight-entry `Array` in `decay()` was indexed at 8 and returned zero —
**no decay at all**, on every note below the halfway point of the tension CV.
That is why decay appeared to vary with pitch, and why the low notes could be
driven into sustained feedback. `LOSS_MAX` is now 15 and the index is clamped
at both ends, so the table can no longer be walked off.

Worth knowing before "fixing" this any further: the runaway was *musically
liked* — an undamped membrane makes striking visual patterns and a genuine
feedback howl. If that is wanted back it should be a control that asks for it,
not an array overflow that varies with which octave you happen to be playing.

**Boundary grazing** — see above. Still unfixed, still worth fixing on its own
merits: two cells of clearance at each end makes every preset clean at both grid
sizes. It bites only at the extremes of in2's travel, which is why it was never
the reported symptom.
