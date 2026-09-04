# A 2D membrane mesh on a masked domain.
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# Shared by LACUNA, which listens to the membrane through a pickup node, and by
# ORBITA, which evolves it slowly and scans a circular path through it as a
# wavetable. Everything about how the membrane behaves lives here; everything
# about what the CVs mean and what comes out lives in the top level.
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


def _raw(v):
    """ASQ is a fixed-point type in-tree and a plain signed(16) standalone."""
    return v.as_value() if hasattr(v, "as_value") else v


class Mesh(wiring.Component):

    """One membrane. Pulse `step` to advance it by one update."""

    def __init__(self, n=32, presets=PRESETS, video=False, snapshot=False,
                 mallet=0):
        # `mallet` here is the MAXIMUM radius; the live value is an input.
        assert n % 2 == 0
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

        # --- memory banks ----------------------------------------------------
        rds, wrs = [], []
        for k in range(2):
            mem = Memory(shape=signed(WIDTH), depth=cells, init=[0] * cells)
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
        j = Signal(range(cells + DRAIN + 1))
        jx = Signal(range(n))
        jy = Signal(range(n))
        scanning = Signal()

        stream_addr = Signal(AW)
        m.d.comb += stream_addr.eq((j + n)[:AW])

        cur_rd = Signal(signed(WIDTH))
        old_rd = Signal(signed(WIDTH))
        m.d.comb += [
            rds[0].addr.eq(Mux(phase, j[:AW], stream_addr)),
            rds[1].addr.eq(Mux(phase, stream_addr, j[:AW])),
            cur_rd.eq(Mux(phase, rds[1].data, rds[0].data)),
            old_rd.eq(Mux(phase, rds[0].data, rds[1].data)),
        ]

        tap = [Signal(signed(WIDTH), name=f"tap{k}") for k in range(2 * n + 1)]
        m.d.sync += tap[0].eq(cur_rd)
        for k in range(1, 2 * n + 1):
            m.d.sync += tap[k].eq(tap[k - 1])

        # Node offset and its squared radius. Registering these splits the one
        # path that was left combinational from the scan counters all the way
        # into the mask: jy -> dy -> dy*dy -> d2 -> compare was ~16 ns and, once
        # the video logic was sharing the die, left the sync domain with almost
        # no margin. `inside` therefore describes the node one cycle later, so
        # its delay chain below is one shorter to keep the alignment identical.
        dx_c = Signal(signed(8))
        dy_c = Signal(signed(8))
        dx = Signal(signed(8))
        dy = Signal(signed(8))
        d2 = Signal(unsigned(16))
        m.d.comb += [dx_c.eq(jx - cx), dy_c.eq(jy - cy)]
        m.d.sync += [dx.eq(dx_c), dy.eq(dy_c),
                     d2.eq(dx_c * dx_c + dy_c * dy_c)]

        inside = Signal()
        in_square = Signal()
        in_slit = Signal()
        m.d.comb += [
            in_square.eq((dx < g_inner.as_signed()) & (dx > -g_inner.as_signed())
                         & (dy < g_inner.as_signed()) & (dy > -g_inner.as_signed())),
            in_slit.eq((dy < 2) & (dy > -2) & (dx > 0)),
            inside.eq((d2 <= outer2)
                      # inner == 0 means a solid head. Without the guard the
                      # test is d2 > 0, which punches a one-cell hole through
                      # dead centre -- exactly the fundamental's antinode, and
                      # enough to pull a full disc nearly three semitones sharp
                      # and wreck its mode ratios.
                      & Mux(g_square, ~in_square, g_nohole | (d2 > inner2))
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
        m.d.sync += [
            strike_node.eq(cy * n + cx - strike_r),
            pickup_node.eq((cy + pickup_r) * n + cx),
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
        sdx = Signal(signed(9))
        sdy = Signal(signed(9))
        adx = Signal(range(M + 2))
        ady = Signal(range(M + 2))
        strike_pending = Signal()
        strike_hit = Signal()
        # adx/ady are registered: node offset -> add -> abs -> clamp -> square
        # lookup -> compare in one cycle left the sync domain at 60.6 MHz once
        # ORBITA's scan shared the die. That puts the test a further stage on,
        # so the delay chain below is one shorter again.
        m.d.comb += [
            sdx.eq(dx + strike_r),
            sdy.eq(dy),
        ]
        m.d.sync += [
            adx.eq(Mux(abs(sdx) > M, M + 1, abs(sdx))),
            ady.eq(Mux(abs(sdy) > M, M + 1, abs(sdy))),
        ]
        msq = Signal(range((M + 2) * (M + 2)))
        m.d.sync += msq.eq(self.mallet_r * self.mallet_r)
        m.d.comb += strike_hit.eq(strike_pending
                                  & (MSQ[adx] + MSQ[ady] <= msq))
        with m.If(self.strike):
            m.d.sync += strike_pending.eq(1)

        sum_r = Signal(signed(WIDTH + 3))
        cen_r = Signal(signed(WIDTH))
        m.d.sync += [
            sum_r.eq(tap[0] + tap[n - 1] + tap[n + 1] + tap[2 * n]),
            cen_r.eq(tap[n]),
        ]

        lap_r = Signal(signed(WIDTH + 3))
        cen2 = Signal(signed(WIDTH))
        m.d.sync += [lap_r.eq(sum_r - (cen_r << 2)), cen2.eq(cen_r)]

        prod_r = Signal(signed(WIDTH + 3 + LAM_FRAC))
        cen3 = Signal(signed(WIDTH))
        m.d.sync += [prod_r.eq(lap_r * self.lam2), cen3.eq(cen2)]

        old_al = delay(old_rd, 4, "old")
        msk_al = delay(inside, 4, "msk")
        strk_al = delay(strike_hit, 3, "strk")
        val_al = delay(scanning & (j < cells), 5, "val")

        base = Signal(signed(WIDTH + 4))
        nxt = Signal(signed(WIDTH + 4))
        m.d.comb += [
            base.eq((prod_r >> LAM_FRAC) + (cen3 << 1) - old_al),
            nxt.eq(base - (base >> self.loss_shift)
                   + Mux(strk_al, self.strike_amp, 0)),
        ]

        # Saturate rather than truncate: wrapping a node turns a loud hit into a
        # full-scale sign flip that the mesh then propagates.
        HI, LO = (1 << (WIDTH - 1)) - 1, -(1 << (WIDTH - 1))
        clamped = Signal(signed(WIDTH))
        with m.If(nxt > HI):
            m.d.comb += clamped.eq(HI)
        with m.Elif(nxt < LO):
            m.d.comb += clamped.eq(LO)
        with m.Else():
            m.d.comb += clamped.eq(nxt)

        written = Signal(signed(WIDTH))
        wr_valid = Signal()
        msk_w = Signal()
        m.d.sync += [written.eq(Mux(msk_al, clamped, 0)), wr_valid.eq(val_al),
                     msk_w.eq(msk_al)]
        wr_addr = delay(j[:AW], 6, "jw")

        for k in range(2):
            m.d.comb += [
                wrs[k].addr.eq(wr_addr),
                wrs[k].data.eq(written),
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
                shape=unsigned(8), depth=cells, init=[0] * cells)
            disp_wr = disp.write_port()
            lvl = Signal(signed(WIDTH))
            disp_val = Signal(unsigned(8))
            m.d.comb += lvl.eq(written >> DISP_SHIFT)
            with m.If(~msk_w):
                m.d.comb += disp_val.eq(0)          # outside the membrane
            with m.Elif(lvl > 127):
                m.d.comb += disp_val.eq(255)
            with m.Elif(lvl < -127):
                m.d.comb += disp_val.eq(1)
            with m.Else():
                # in-membrane values live in 1..255, so they never read as 0
                m.d.comb += disp_val.eq(Mux(lvl + 128 == 0, 1, (lvl + 128)[:8]))
            m.d.comb += [
                disp_wr.addr.eq(wr_addr),
                disp_wr.data.eq(disp_val),
                disp_wr.en.eq(wr_valid),
            ]
            disp_rd = disp.read_port(domain="dvi")
            m.d.comb += [
                disp_rd.addr.eq(self.disp_addr),
                self.disp_data.eq(disp_rd.data),
            ]

        # --- wide snapshot ------------------------------------------------
        # The same free copy as the display tap, but 16 bits and read from the
        # audio domain: ORBITA indexes it with a circle ROM to scan a closed
        # path through the membrane at audio rate. Cells outside the membrane
        # were written as 0, so a scan path that strays into the hole or past
        # the rim reads silence, which is the behaviour we want.
        if self.snapshot:
            m.submodules.snap = snap = Memory(
                shape=signed(16), depth=cells, init=[0] * cells)
            snap_wr = snap.write_port()
            m.d.comb += [
                snap_wr.addr.eq(wr_addr),
                snap_wr.data.eq(written >> (WIDTH - 16)),
                snap_wr.en.eq(wr_valid),
            ]
            snap_rd = snap.read_port()
            m.d.comb += [
                snap_rd.addr.eq(self.snap_addr),
                self.snap_data.eq(snap_rd.data),
            ]

        m.d.comb += [self.strike_at.eq(strike_node),
                     self.pickup_at.eq(pickup_node)]

        with m.If(wr_valid & (wr_addr == pickup_node)):
            m.d.sync += self.pickup.eq(written)

        # --- run control -----------------------------------------------------
        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.step):
                    m.d.sync += [j.eq(0), jx.eq(0), jy.eq(0)]
                    m.next = "SCAN"

            with m.State("SCAN"):
                m.d.comb += [scanning.eq(1), self.running.eq(1)]
                m.d.sync += j.eq(j + 1)
                with m.If(jx == n - 1):
                    m.d.sync += [jx.eq(0), jy.eq(jy + 1)]
                with m.Else():
                    m.d.sync += jx.eq(jx + 1)
                with m.If(j == cells + DRAIN):
                    m.d.sync += [phase.eq(~phase), strike_pending.eq(0)]
                    m.d.comb += self.done.eq(1)
                    m.next = "IDLE"

        return m
