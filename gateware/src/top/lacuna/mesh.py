# A 2D membrane mesh on a masked domain.
#
# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# Shared by LACUNA, which listens to the membrane through two pickup nodes, and
# by ORBITA, which evolves it slowly and scans circular paths through it as
# wavetables. Everything about how the membrane behaves lives here; everything
# about what the CVs mean and what comes out lives in the top level.
#
# Because it is shared, a change made for one instrument can break the other's
# timing while every test still passes. Build both after touching this file.
#
# THE UPDATE. The membrane is
#
#     u_next = lam2 * (N + S + E + W - 4u) + 2u - u_prev
#
# stable for lam2 <= 0.5. lam2 is tension: for a membrane c^2 = T/sigma and
# lam = c*dt/dx, so lam2 is proportional to T, and the stability limit is the
# tension at which a wave would cross more than a cell per sample. At exactly
# 0.5 the 2u and -4*lam2*u terms cancel and the update needs no multiplier --
# which is a special case, not the general rule. Scaling only the neighbour sum
# does not lower the pitch, it piles energy up at Nyquist.
#
# MEMORY. Two banks swap roles each update. The bank holding u is read as a
# raster stream running one row ahead of the node being computed, and a 2N-deep
# delay line off that stream yields every neighbour without a second read port:
#
#     tap 0     = u[j+N] -> S      tap N+1 = u[j-1] -> W
#     tap N-1   = u[j+1] -> E      tap 2N  = u[j-N] -> N
#     tap N     = u[j]   -> centre, needed by the update and by the pickup
#
# The bank holding u_prev is read at the node address and overwritten with
# u_next in the same pass, which is safe because nothing reads a node after it
# has been written.
#
# BORDERS. Every preset keeps the mask two cells clear of the array edge, so
# the taps that wrap across a row boundary always carry masked-off zeros, and
# the first row -- whose N tap holds values left over from the previous scan --
# is masked off too. The assert in __init__ is load-bearing, not decorative.
#
# CONTROL TIMING. `preset_i`, `fm` and `strike_cv` feed a three-deep register
# pipeline (geometry -> squared radii -> span/nodes) before the scan reads them.
# Hold them stable for at least three cycles before pulsing `step`, or the first
# nodes of a scan see stale geometry. Both top levels satisfy this comfortably.

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out


# 24-bit state with 22 fractional bits. 18 is not enough: the loss term is a
# right shift, so it stops working below |u| < 2**loss_shift, and at 17
# fractional bits that dead zone sits at -24 dBFS, freezing the tail at a DC
# offset.
WIDTH, FRAC = 24, 22
LAM_FRAC = 26                # lam2 fixed-point

# (outer, inner, square_hole, slit, inv_mu_q). The inv_mu values are the
# fundamental eigenvalue of the discrete Laplacian on each masked domain,
# computed against the mask this file actually builds -- see research/mesh.
PRESETS = [
    (14,  0, 0, 0, 36410),    # drum head
    (10,  0, 0, 0, 19196),    # medium head
    ( 7,  0, 0, 0,  9324),    # small head
    (14,  3, 0, 0, 14811),    # narrow hole
    (14,  7, 0, 0,  6410),    # wide ring
    (14, 11, 0, 0,  1535),    # thin ring
    (14,  5, 1, 0,  9858),    # square hole
    (14,  8, 0, 1,  4765),    # slit ring
]

# The same eight geometries on a 48x48 grid, from research/mesh/presets.py.
# The radii scale with the array -- the assert below keeps every preset two
# cells clear of the edge -- but inv_mu does NOT scale with them: mu is a
# property of the masked domain, so each one is recomputed against the mask
# this file builds. That generator regenerates the 32x32 table above exactly,
# which is what says it can be trusted for any other size.
#
# A bigger membrane is a lower one. The largest 1/-mu here is 88328 against
# 36410, so the tension at which a wave would cross a cell per sample now
# arrives at 582 Hz rather than 906, and lacuna.py drops the 1 V/oct range an
# octave to suit. That is the physics of a wider drum, not a compromise.
PRESETS_48 = [
    (22,  0, 0, 0,  88328),   # drum head
    (16,  0, 0, 0,  47046),   # medium head
    (11,  0, 0, 0,  22720),   # small head
    (22,  5, 0, 0,  33756),   # narrow hole
    (22, 11, 0, 0,  14480),   # wide ring
    (22, 17, 0, 0,   3494),   # thin ring
    (22,  8, 1, 0,  21160),   # square hole
    (22, 13, 0, 1,   9788),   # slit ring
]

PRESETS_BY_N = {32: PRESETS, 48: PRESETS_48}


def _raw(v):
    """ASQ is a fixed-point type in-tree and a plain signed(16) standalone."""
    return v.as_value() if hasattr(v, "as_value") else v


class Mesh(wiring.Component):

    """One membrane. Pulse `step` to advance it by one update."""

    def __init__(self, n=32, presets=PRESETS, video=False, snapshot=False,
                 mallet=0, lanes=1):
        # `mallet` here is the MAXIMUM radius; the live value is an input.
        assert n % 2 == 0
        # `lanes`: how many cells the scan retires per cycle.
        #
        # At one lane the membrane costs one cycle per node, so 32x32 spends
        # 1037 of the 1250 cycles a 48 kHz sample allows and nothing larger
        # fits: 48x48 would need 2304 and 64x64 about 4100. More clock does not
        # reach it either -- 4096 nodes at 48 kHz is a 197 MHz sync domain and
        # this pipeline closes at 65-68. Retiring several nodes a cycle is the
        # only way to a bigger membrane at audio rate, and it is the one thing
        # here a CPU cannot buy at any clock.
        #
        # The memory holds `lanes` cells to a word, the delay line shifts by a
        # whole word, and the update is instantiated once per lane. A power of
        # two, so a cell address splits into word and lane by slicing rather
        # than dividing; at least two words to a row, so the E and W spill taps
        # land on different words from the rows above and below.
        assert lanes >= 1 and lanes & (lanes - 1) == 0, "lanes must be a power of two"
        assert n % lanes == 0, f"{lanes} lanes do not divide a {n}-cell row"
        assert n // lanes >= 2, "need at least two words per row"
        self.lanes = lanes
        self.n = n
        # `video`: also keep an 8-bit snapshot of the mesh for the display. It
        # costs one BRAM and is written from the scan that already passes every
        # node once per update, so the mesh itself is unaffected. Off by default
        # so the core still elaborates where there is no `dvi` domain -- the
        # standalone tests, and any audio-only top level.
        self.video = video
        # `snapshot`: keep a 16-bit copy of the mesh readable from the audio
        # domain. ORBITA scans a path through it at audio rate; the display
        # snapshot above is a separate, narrower memory in the `dvi` domain
        # because a BRAM has two ports and the writer already holds one.
        self.snapshot = snapshot
        # `mallet`: radius of the strike, in cells. A single-cell impulse
        # excites every spatial mode equally hard, including the cell-to-cell
        # checkerboard, and nothing damps that preferentially -- so the membrane
        # stays as rough as whatever hit it. Measured on a ring: a one-cell
        # strike leaves neighbouring cells agreeing in sign 61% of the time,
        # which is spatial white noise; radius 3 takes it to 92%.
        #
        # It matters far more for ORBITA, which reads the membrane's shape
        # directly, than for LACUNA, which hears one point over time.
        self.mallet = mallet
        self.presets = presets
        for outer, _, _, _, _ in presets:
            assert outer <= n // 2 - 2, (
                f"outer radius {outer} reaches the array border on a {n}x{n} "
                f"grid; the delay-line taps wrap across rows and rely on those "
                f"nodes being masked off")
        super().__init__({
            # --- run control ---
            "step":       In(1),      # pulse: advance the membrane one update
            "running":    Out(1),
            "done":       Out(1),     # pulse: that update has finished
            # --- how the membrane behaves ---
            "lam2":       In(LAM_FRAC),
            "loss_shift": In(range(24)),
            "preset_i":   In(range(len(presets))),
            "fm":         In(signed(16)),   # live hole-radius modulation
            # --- excitation ---
            "strike":     In(1),      # pulse: strike on the next update
            "strike_cv":  In(4),      # hub..rim across the available span
            # Live mallet radius, 0..mallet. Small is a hard stick: bright, and
            # rough enough to read as noise. Large is a soft mallet: smooth and
            # dull. It is the same axis, so it belongs on a control.
            "mallet_r":   In(range(max(2, mallet + 1))),
            # How hard. A one-shot pluck is a single pulse at full amplitude; a
            # drone is a small amplitude pulsed every update, which is what
            # keeps a lossy membrane alive without letting it run away.
            "strike_amp": In(signed(WIDTH + 4)),
            # --- what comes out ---
            "pickup":     Out(signed(WIDTH)),
            # A second pickup at 45 degrees, same radius. Not the mirror of the
            # first, which reads identically on every symmetric preset and would
            # give mono; not +x, which sits inside the slit on the slit preset
            # and would be silent there; not -x, which is where the strike is.
            # Angular modes differ between 45 and 90 degrees while the radially
            # symmetric ones are common to both -- which is what a struck drum
            # actually does.
            "pickup2":    Out(signed(WIDTH)),
            # The live geometry, after the preset and in3 have had their say.
            # A top level needs it to scale a control across the membrane that
            # actually exists rather than across absolute cell counts.
            "geo_inner":  Out(6),
            "geo_outer":  Out(6),
            # Where the strike lands and where the pickup listens, as cell
            # addresses, so a display can show them. Both move with the live
            # geometry, which is most of why they are worth seeing.
            "strike_at":  Out(range(n * n)),
            "pickup_at":  Out(range(n * n)),
            "pickup2_at": Out(range(n * n)),
            # --- display snapshot, read from the `dvi` domain ---
            "disp_addr":  In(range(n * n)),
            "disp_data":  Out(8),
            # --- wide snapshot, read from the audio domain ---
            "snap_addr":  In(range(n * n)),
            "snap_data":  Out(signed(16)),
        })

    def elaborate(self, platform):
        m = Module()
        n, cells = self.n, self.n * self.n
        cx = cy = n // 2
        AW = Shape.cast(range(cells)).width

        # --- scan width ------------------------------------------------------
        # L cells to a memory word. WPR words to a row; the delay line spans two
        # rows plus one word either side of the centre, which is where the E
        # neighbour of the last lane and the W neighbour of the first come from.
        L = self.lanes
        LSH = (L - 1).bit_length()          # cell address -> word, lane
        WPR = n // L                        # words per row
        words = cells // L
        WAW = Shape.cast(range(words)).width

        def lane(word, i):
            """Cell i of a packed word, signed."""
            return word[i * WIDTH:(i + 1) * WIDTH].as_signed()

        # --- memory banks ----------------------------------------------------
        rds, wrs = [], []
        for k in range(2):
            mem = Memory(shape=unsigned(L * WIDTH), depth=words,
                         init=[0] * words)
            m.submodules[f"bank{k}"] = mem
            rds.append(mem.read_port())
            wrs.append(mem.write_port())
        phase = Signal()      # 0: bank0 holds u (streamed), bank1 holds u_prev

        # --- geometry --------------------------------------------------------
        outer = Signal(unsigned(6))
        inner = Signal(unsigned(6))
        square_hole = Signal()
        slit = Signal()
        with m.Switch(self.preset_i):
            for p, (o_, i_, sq, sl, _im) in enumerate(self.presets):
                with m.Case(p):
                    m.d.comb += [outer.eq(o_), inner.eq(i_),
                                 square_hole.eq(sq), slit.eq(sl)]
            with m.Default():
                o_, i_, sq, sl, _im = self.presets[0]
                m.d.comb += [outer.eq(o_), inner.eq(i_),
                             square_hole.eq(sq), slit.eq(sl)]

        inner_eff = Signal(signed(8))
        inner_c = Signal(unsigned(6))
        # Round to nearest, not floor. `fm >> 12` on a signed value floors
        # toward -inf, so a single negative ADC count -- which is what an idle
        # jack reads -- already subtracted a whole cell from the hole radius,
        # while the positive side needed a full volt to add one. The rounding
        # term puts a symmetric +/- 0.5 V dead zone around zero instead.
        m.d.comb += inner_eff.eq(inner + ((self.fm + (1 << 11)) >> 12))
        with m.If(inner_eff < 0):
            m.d.comb += inner_c.eq(0)
        with m.Elif(inner_eff > outer - 2):
            m.d.comb += inner_c.eq(outer - 2)
        with m.Else():
            m.d.comb += inner_c.eq(inner_eff)

        # Geometry is a per-update quantity -- constant across all cells of a
        # scan -- but the mask consumes it once per node, so left combinational
        # the whole chain
        #
        #     preset_i -> inner + fm -> clamp -> inner_c^2 -> d2 > inner2 -> mask
        #
        # lands in a single cycle: 19.2 ns, against 16.7 ns at 60 MHz. Two
        # register stages move the multiply and the preset mux off the per-node
        # path. Everything the scan reads comes from stage 2, so the radii, the
        # squared radii and the shape flags stay coherent with each other.
        g1_inner  = Signal(unsigned(6))
        g1_outer  = Signal(unsigned(6))
        g1_square = Signal()
        g1_slit   = Signal()
        m.d.sync += [g1_inner.eq(inner_c), g1_outer.eq(outer),
                     g1_square.eq(square_hole), g1_slit.eq(slit)]

        g_inner  = Signal(unsigned(6))
        g_outer  = Signal(unsigned(6))
        g_square = Signal()
        g_slit   = Signal()
        outer2   = Signal(unsigned(16))
        inner2   = Signal(unsigned(16))
        g_nohole = Signal()
        m.d.sync += [g_inner.eq(g1_inner), g_outer.eq(g1_outer),
                     g_square.eq(g1_square), g_slit.eq(g1_slit),
                     g_nohole.eq(g1_inner == 0),
                     outer2.eq(g1_outer * g1_outer),
                     inner2.eq(g1_inner * g1_inner)]

        # Strike and pickup radii follow the live geometry. A pickup at a fixed
        # radius falls inside the hole on the thin-ring preset and reads zero
        # forever, which is indistinguishable from a dead core.
        #
        # strike_cv sweeps the strike across whatever radial room the geometry
        # leaves, rather than being an offset added to the hole edge. As an
        # offset it clamped against the rim almost at once: on the wide ring only
        # a third of the sweep did anything, and on the thin ring a single step.
        # `span` is registered because leaving it combinational put
        # g_inner -> subtract -> multiply -> clamp into one cycle, which took the
        # sync domain under 60 MHz on its own.
        m.d.comb += [self.geo_inner.eq(g_inner), self.geo_outer.eq(g_outer)]

        strike_r = Signal(unsigned(6))
        pickup_r = Signal(unsigned(6))
        strike_raw = Signal(unsigned(7))
        span = Signal(unsigned(6))
        m.d.sync += span.eq(g_outer - g_inner - 1)
        # strike_raw is registered: span -> multiply -> clamp -> strike_node in
        # one cycle is ~17 ns and no placer seed closed 60 MHz with it. The
        # strike node is not compared against `j` until roughly halfway through
        # a scan, so the extra cycle costs nothing.
        m.d.sync += strike_raw.eq(g_inner + 1 + ((self.strike_cv * span) >> 4))
        m.d.comb += [
            strike_r.eq(Mux(strike_raw > g_outer - 1, g_outer - 1, strike_raw)),
            pickup_r.eq((g_inner + g_outer) >> 1),
        ]

        # --- scan ------------------------------------------------------------
        DRAIN = 8
        w = Signal(range(words + DRAIN + 1))    # word counter, was the cell one
        wx = Signal(range(WPR))                 # word within the row
        jy = Signal(range(n))
        scanning = Signal()

        # One row ahead, wrapping. The old form was `(j + n)[:AW]`, which is
        # only the modulo when `cells` is a power of two: true at 32x32 (1024)
        # and 64x64 (4096), false at 48x48 (2304), where it would have wrapped
        # to the wrong cell and quietly computed a different membrane.
        stream_addr = Signal(WAW)
        ahead = Signal(WAW + 1)
        m.d.comb += [
            ahead.eq(w + WPR),
            stream_addr.eq(Mux(ahead >= words, ahead - words, ahead)),
        ]

        cur_rd = Signal(unsigned(L * WIDTH))
        old_rd = Signal(unsigned(L * WIDTH))
        m.d.comb += [
            rds[0].addr.eq(Mux(phase, w[:WAW], stream_addr)),
            rds[1].addr.eq(Mux(phase, stream_addr, w[:WAW])),
            cur_rd.eq(Mux(phase, rds[1].data, rds[0].data)),
            old_rd.eq(Mux(phase, rds[0].data, rds[1].data)),
        ]

        # Word-wise delay line: tap[k] is the word read k cycles ago, so
        # tap[WPR] is the word being computed, tap[0] the row below and
        # tap[2*WPR] the row above. The same geometry as before, in words.
        tap = [Signal(unsigned(L * WIDTH), name=f"tap{k}")
               for k in range(2 * WPR + 1)]
        m.d.sync += tap[0].eq(cur_rd)
        for k in range(1, 2 * WPR + 1):
            m.d.sync += tap[k].eq(tap[k - 1])

        def taps_for(i):
            """centre, N, S, E, W for lane i.

            At one lane these are tap[n], tap[2n], tap[0], tap[n-1], tap[n+1],
            exactly as before. With more, E of the last lane and W of the first
            spill into the adjacent words, one tap either side of the centre.
            """
            centre = lane(tap[WPR], i)
            north = lane(tap[2 * WPR], i)
            south = lane(tap[0], i)
            east = lane(tap[WPR], i + 1) if i < L - 1 else lane(tap[WPR - 1], 0)
            west = lane(tap[WPR], i - 1) if i > 0 else lane(tap[WPR + 1], L - 1)
            return centre, north, south, east, west

        # Node offset and its squared radius. Registering these splits the one
        # path that was left combinational from the scan counters all the way
        # into the mask: jy -> dy -> dy*dy -> d2 -> compare was ~16 ns and, once
        # the video logic was sharing the die, left the sync domain with almost
        # no margin. `inside` therefore describes the node one cycle later, so
        # its delay chain below is one shorter to keep the alignment identical.
        # dy is common to every lane -- they are all on the same row -- so only
        # dx, the squared radius and the mask replicate.
        dy_c = Signal(signed(8))
        dy = Signal(signed(8))
        m.d.comb += dy_c.eq(jy - cy)
        m.d.sync += dy.eq(dy_c)

        dx_c = [Signal(signed(8), name=f"dx_c{i}") for i in range(L)]
        dx = [Signal(signed(8), name=f"dx{i}") for i in range(L)]
        d2 = [Signal(unsigned(16), name=f"d2_{i}") for i in range(L)]
        inside = [Signal(name=f"inside{i}") for i in range(L)]
        for i in range(L):
            m.d.comb += dx_c[i].eq(wx * L + i - cx)
            m.d.sync += [dx[i].eq(dx_c[i]),
                         d2[i].eq(dx_c[i] * dx_c[i] + dy_c * dy_c)]

        for i in range(L):
            in_square = Signal(name=f"in_square{i}")
            in_slit = Signal(name=f"in_slit{i}")
            m.d.comb += [
                in_square.eq((dx[i] < g_inner.as_signed()) & (dx[i] > -g_inner.as_signed())
                             & (dy < g_inner.as_signed()) & (dy > -g_inner.as_signed())),
                in_slit.eq((dy < 2) & (dy > -2) & (dx[i] > 0)),
                inside[i].eq((d2[i] <= outer2)
                      # inner == 0 means a solid head. Without the guard the
                      # test is d2 > 0, which punches a one-cell hole through
                      # dead centre -- exactly the fundamental's antinode, and
                      # enough to pull a full disc nearly three semitones sharp
                      # and wreck its mode ratios.
                      & Mux(g_square, ~in_square, g_nohole | (d2[i] > inner2))
                      & ~(g_slit & in_slit)),
            ]

        # The strike sits at -x and the pickup at +y. -x rather than +x because
        # the slit preset removes |dy| < 2 for dx > 0, which is exactly where a
        # +x strike lands: every strike was zeroed as it was written and that
        # preset made no sound at all. Every other preset is mirror-symmetric in
        # x and the pickup is on the mirror axis, so the move is bit-exact for
        # them.
        #
        # Registered: both are per-update values, but they are compared against
        # `j` once per node, so leaving them combinational puts the strike
        # multiply above on the per-node path.
        strike_node = Signal(AW)
        pickup_node = Signal(AW)
        pickup2_node = Signal(AW)
        # 181/256 is 1/sqrt(2) to within a thousandth: the second pickup sits at
        # the same radius as the first, 45 degrees round -- (cx + r/sqrt2,
        # cy + r/sqrt2) against the first's (cx, cy + r). NOT a quarter turn:
        # 90 degrees from +y lands on +x or -x, and the paragraph below rules
        # out both. Verified inside the membrane and clear of both the slit and
        # the square hole on all eight presets.
        #
        # The thin ring looks unbalanced over a short simulation -- 1% of the
        # first pickup at 500 samples -- but that is the wave not having arrived
        # yet rather than a bad position. It is the slowest preset by a long way
        # (lam2 0.005) and the second pickup is 135 degrees round from the
        # strike against the first's 90. By 1500 samples, 31 ms, the two are
        # within 6% of each other.
        #
        # Registered in two stages for the same reason strike_raw is: geometry,
        # multiply and node address in one cycle does not close 60 MHz.
        p2 = Signal(unsigned(6))
        m.d.sync += p2.eq((pickup_r * 181 + 128) >> 8)
        m.d.sync += [
            strike_node.eq(cy * n + cx - strike_r),
            pickup_node.eq((cy + pickup_r) * n + cx),
            pickup2_node.eq((cy + p2) * n + (cx + p2)),
        ]

        # --- pipeline --------------------------------------------------------
        # A synchronous read gives data one cycle after the address and the
        # delay line registers it, so during cycle T the taps describe node
        # j(T)-2. Everything else is aligned to that. The multiply adds two
        # stages over the fixed-tension version, so these depths moved with it
        # -- an off-by-one here computes a mesh with the wrong topology and
        # still runs, sounding plausible but wrong.
        def delay(sig, k, name):
            out = sig
            for d in range(k):
                r = Signal.like(sig, name=f"{name}_d{d+1}")
                m.d.sync += r.eq(out)
                out = r
            return out

        # The strike covers a disc of `mallet` cells around the strike node.
        # dx/dy describe the node one cycle after j, so this is a stage later
        # than the old `j == strike_node` test and its delay chain below is one
        # shorter to compensate. At mallet 0 the disc is the single cell where
        # the node offset equals the strike offset, which is the same node.
        M = self.mallet
        MSQ = Array([C(v * v, unsigned(10)) for v in range(M + 2)])
        sdy = Signal(signed(9))
        ady = Signal(range(M + 2))
        strike_pending = Signal()
        strike_hit = [Signal(name=f"strike_hit{i}") for i in range(L)]
        # adx/ady are registered: node offset -> add -> abs -> clamp -> square
        # lookup -> compare in one cycle left the sync domain at 60.6 MHz once
        # ORBITA's scan shared the die. That puts the test a further stage on,
        # so the delay chain below is one shorter again.
        m.d.comb += sdy.eq(dy)
        m.d.sync += ady.eq(Mux(abs(sdy) > M, M + 1, abs(sdy)))
        msq = Signal(range((M + 2) * (M + 2)))
        m.d.sync += msq.eq(self.mallet_r * self.mallet_r)
        for i in range(L):
            sdx = Signal(signed(9), name=f"sdx{i}")
            adx = Signal(range(M + 2), name=f"adx{i}")
            m.d.comb += sdx.eq(dx[i] + strike_r)
            m.d.sync += adx.eq(Mux(abs(sdx) > M, M + 1, abs(sdx)))
            m.d.comb += strike_hit[i].eq(strike_pending
                                         & (MSQ[adx] + MSQ[ady] <= msq))
        with m.If(self.strike):
            m.d.sync += strike_pending.eq(1)

        val_al = delay(scanning & (w < words), 5, "val")

        # Saturate rather than truncate: wrapping a node turns a loud hit into a
        # full-scale sign flip that the mesh then propagates.
        HI, LO = (1 << (WIDTH - 1)) - 1, -(1 << (WIDTH - 1))

        written = [Signal(signed(WIDTH), name=f"written{i}") for i in range(L)]
        msk_w = [Signal(name=f"msk_w{i}") for i in range(L)]
        wr_valid = Signal()

        # One update per lane. Everything above this point that differs between
        # lanes -- the taps, the mask, the strike test -- is already indexed;
        # the tension, the loss and the clamp bounds are shared.
        for i in range(L):
            centre, north, south, east, west = taps_for(i)

            sum_r = Signal(signed(WIDTH + 3), name=f"sum_r{i}")
            cen_r = Signal(signed(WIDTH), name=f"cen_r{i}")
            m.d.sync += [
                sum_r.eq(south + east + west + north),
                cen_r.eq(centre),
            ]

            lap_r = Signal(signed(WIDTH + 3), name=f"lap_r{i}")
            cen2 = Signal(signed(WIDTH), name=f"cen2_{i}")
            m.d.sync += [lap_r.eq(sum_r - (cen_r << 2)), cen2.eq(cen_r)]

            prod_r = Signal(signed(WIDTH + 3 + LAM_FRAC), name=f"prod_r{i}")
            cen3 = Signal(signed(WIDTH), name=f"cen3_{i}")
            m.d.sync += [prod_r.eq(lap_r * self.lam2), cen3.eq(cen2)]

            old_in = Signal(signed(WIDTH), name=f"old_in{i}")
            m.d.comb += old_in.eq(lane(old_rd, i))
            old_al = delay(old_in, 4, f"old{i}")
            msk_al = delay(inside[i], 4, f"msk{i}")
            strk_al = delay(strike_hit[i], 3, f"strk{i}")

            base = Signal(signed(WIDTH + 4), name=f"base{i}")
            nxt = Signal(signed(WIDTH + 4), name=f"nxt{i}")
            m.d.comb += [
                base.eq((prod_r >> LAM_FRAC) + (cen3 << 1) - old_al),
                nxt.eq(base - (base >> self.loss_shift)
                       + Mux(strk_al, self.strike_amp, 0)),
            ]

            clamped = Signal(signed(WIDTH), name=f"clamped{i}")
            with m.If(nxt > HI):
                m.d.comb += clamped.eq(HI)
            with m.Elif(nxt < LO):
                m.d.comb += clamped.eq(LO)
            with m.Else():
                m.d.comb += clamped.eq(nxt)

            m.d.sync += [written[i].eq(Mux(msk_al, clamped, 0)),
                         msk_w[i].eq(msk_al)]

        m.d.sync += wr_valid.eq(val_al)
        wr_addr = delay(w[:WAW], 6, "jw")

        for k in range(2):
            m.d.comb += [
                wrs[k].addr.eq(wr_addr),
                wrs[k].data.eq(Cat(*written)),
                wrs[k].en.eq(wr_valid & (phase == (0 if k == 1 else 1))),
            ]

        # --- display tap -----------------------------------------------------
        # The scan already carries every node past this point once per update,
        # so a second, narrower memory written from the same address and strobe
        # is a free copy of the mesh for the video side to read. It is a plain
        # dual port BRAM with the read port in the `dvi` domain: the two sides
        # are asynchronous and a frame may catch a half-updated mesh, which for
        # a 48 kHz mesh on a 60 Hz display is invisible.
        #
        # DISP_SHIFT sets how much mesh amplitude fills the 8-bit range.
        # Measured peaks are around 2**19.7, so 13 puts a loud strike near full
        # scale and saturates rather than wrapping -- a wrapped node would read
        # as a bright speck exactly where the mesh is loudest.
        #
        # 0 is reserved to mean "outside the membrane", so the display can draw
        # the shape of the current preset even when nothing is ringing.
        if self.video:
            DISP_SHIFT = 13
            m.submodules.disp = disp = Memory(
                shape=unsigned(8 * L), depth=words, init=[0] * words)
            disp_wr = disp.write_port()
            disp_vals = []
            for i in range(L):
                lvl = Signal(signed(WIDTH), name=f"lvl{i}")
                disp_val = Signal(unsigned(8), name=f"disp_val{i}")
                m.d.comb += lvl.eq(written[i] >> DISP_SHIFT)
                with m.If(~msk_w[i]):
                    m.d.comb += disp_val.eq(0)      # outside the membrane
                with m.Elif(lvl > 127):
                    m.d.comb += disp_val.eq(255)
                with m.Elif(lvl < -127):
                    m.d.comb += disp_val.eq(1)
                with m.Else():
                    # in-membrane values live in 1..255, never reading as 0
                    m.d.comb += disp_val.eq(
                        Mux(lvl + 128 == 0, 1, (lvl + 128)[:8]))
                disp_vals.append(disp_val)
            m.d.comb += [
                disp_wr.addr.eq(wr_addr),
                disp_wr.data.eq(Cat(*disp_vals)),
                disp_wr.en.eq(wr_valid),
            ]
            disp_rd = disp.read_port(domain="dvi")
            if L == 1:
                m.d.comb += [
                    disp_rd.addr.eq(self.disp_addr),
                    self.disp_data.eq(disp_rd.data),
                ]
            else:
                # The read is synchronous, so the lane select has to arrive with
                # the data rather than with the address.
                dsel = Signal(LSH)
                m.d.dvi += dsel.eq(self.disp_addr[:LSH])
                m.d.comb += [
                    disp_rd.addr.eq(self.disp_addr[LSH:]),
                    self.disp_data.eq(disp_rd.data.word_select(dsel, 8)),
                ]

        # --- wide snapshot ------------------------------------------------
        # The same free copy as the display tap, but 16 bits and read from the
        # audio domain: ORBITA indexes it with a circle ROM to scan a closed
        # path through the membrane at audio rate. Cells outside the membrane
        # were written as 0, so a scan path that strays into the hole or past
        # the rim reads silence, which is the behaviour we want.
        if self.snapshot:
            m.submodules.snap = snap = Memory(
                shape=unsigned(16 * L), depth=words, init=[0] * words)
            snap_wr = snap.write_port()
            m.d.comb += [
                snap_wr.addr.eq(wr_addr),
                snap_wr.data.eq(Cat(*[(written[i] >> (WIDTH - 16))[:16]
                                      for i in range(L)])),
                snap_wr.en.eq(wr_valid),
            ]
            snap_rd = snap.read_port()
            if L == 1:
                m.d.comb += [
                    snap_rd.addr.eq(self.snap_addr),
                    self.snap_data.eq(snap_rd.data.as_signed()),
                ]
            else:
                ssel = Signal(LSH)
                m.d.sync += ssel.eq(self.snap_addr[:LSH])
                m.d.comb += [
                    snap_rd.addr.eq(self.snap_addr[LSH:]),
                    self.snap_data.eq(
                        snap_rd.data.word_select(ssel, 16).as_signed()),
                ]

        m.d.comb += [self.strike_at.eq(strike_node),
                     self.pickup_at.eq(pickup_node),
                     self.pickup2_at.eq(pickup2_node)]

        # The pickups are cell addresses and the scan now writes L cells to one
        # word, so match the word and select the lane.
        def at_node(node, i):
            if L == 1:
                return wr_addr == node
            return (wr_addr == node[LSH:]) & (node[:LSH] == i)

        for i in range(L):
            with m.If(wr_valid & at_node(pickup_node, i)):
                m.d.sync += self.pickup.eq(written[i])
            with m.If(wr_valid & at_node(pickup2_node, i)):
                m.d.sync += self.pickup2.eq(written[i])

        # --- run control -----------------------------------------------------
        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.step):
                    m.d.sync += [w.eq(0), wx.eq(0), jy.eq(0)]
                    m.next = "SCAN"

            with m.State("SCAN"):
                m.d.comb += [scanning.eq(1), self.running.eq(1)]
                m.d.sync += w.eq(w + 1)
                with m.If(wx == WPR - 1):
                    m.d.sync += [wx.eq(0), jy.eq(jy + 1)]
                with m.Else():
                    m.d.sync += wx.eq(wx + 1)
                with m.If(w == words + DRAIN):
                    m.d.sync += [phase.eq(~phase), strike_pending.eq(0)]
                    m.d.comb += self.done.eq(1)
                    m.next = "IDLE"

        return m
