# 2D FDTD membrane mesh for Tiliqua, as a drop-in core for the `dsp` top-level.
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# The update is the leapfrog scheme at the Courant limit:
#
#     u_next[j] = ((N + S + E + W) >> 1) - u_prev[j]
#
# Three adds, a shift and a subtract per node, and no multiplier in the audio
# path, so the ECP5's 28 DSP tiles stay free for whatever comes next.
#
# One node per cycle in raster order. At 60 MHz sync and 48 kHz there are 1250
# cycles per sample, and a 32x32 grid costs 1024 + 32 + pipeline. 64x64 needs
# four parallel datapaths and banked memory; that is the next step, not this.
#
# MEMORY. Two banks swap roles every sample. The bank holding u is read as a
# raster stream running one row ahead of the node being computed; a 2N-deep
# delay line off that stream yields every neighbour without a second read port:
#
#     tap 0      = u[j+N]  -> S        tap N+1 = u[j-1]  -> W
#     tap N-1    = u[j+1]  -> E        tap 2N  = u[j-N]  -> N
#     tap N      = u[j]    -> centre (the pickup reads this)
#
# The bank holding u_prev is read at the node address and written with u_next in
# the same pass. Safe: nothing reads a node after it has been written.
#
# BORDERS. Every preset keeps the mask at least two cells clear of the array
# edge, so the taps that wrap across a row boundary always carry masked-off
# zeros, and the first row -- whose N tap holds values left over from the
# previous scan -- is masked off too. That is why the assert in __init__ is
# load-bearing rather than decorative.

from amaranth import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out

try:
    from tiliqua.dsp import ASQ
    from tiliqua.build.types import BitstreamHelp
except ImportError:                       # standalone simulation
    from shims import ASQ, BitstreamHelp


# 24-bit state with 22 fractional bits. 18 bits is not enough: the loss term is
# a right shift, so it stops working below |u| < 2**loss_shift, and at 17
# fractional bits that dead zone sits at -24 dBFS, freezing the tail at a DC
# offset. Established in the numpy model -- see ../README.md.
WIDTH = 24
FRAC = 22
ASQ_FRAC = 15
UPSHIFT = FRAC - ASQ_FRAC

# The annulus family as presets: (outer, inner, square_hole, slit), radii in
# cells for a 32x32 grid. Radii are stored unsquared so the strike and pickup
# positions can be derived from them -- a pickup at a fixed radius lands inside
# the hole for the thin-ring preset and reads zero forever, which looks exactly
# like a dead core.
PRESETS = [
    (14, 0,  0, 0),    # 0 full disc
    (14, 3,  0, 0),    # 1 narrow hole
    (14, 7,  0, 0),    # 2 wide ring
    (14, 11, 0, 0),    # 3 thin ring
    (14, 5,  1, 0),    # 4 square hole
    (14, 8,  0, 1),    # 5 slit ring
]


def _raw(v):
    """ASQ is a fixed-point type in-tree and a plain signed(16) standalone."""
    return v.as_value() if hasattr(v, "as_value") else v


class Mesh(wiring.Component):

    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))

    bitstream_help = BitstreamHelp(
        brief="2D membrane mesh, annulus presets",
        io_left=['strike', 'position', 'preset', 'geom FM',
                 'mesh out', '', '', ''],
        io_right=['', '', '', '', '', '']
    )

    def __init__(self, n=32, loss_shift=13, presets=PRESETS):
        assert n % 2 == 0
        self.n = n
        self.loss_shift = loss_shift
        self.presets = presets
        # Exposed so the testbench can compare the mesh state directly against
        # the numpy reference, without going through the ASQ output scaling.
        self.pickup_dbg = Signal(signed(WIDTH))
        for outer, inner, _, _ in presets:
            assert outer <= n // 2 - 2, (
                f"outer radius {outer} reaches the array border on a {n}x{n} "
                f"grid; the delay-line taps wrap across rows and rely on those "
                f"nodes being masked off")
        super().__init__()

    def elaborate(self, platform):
        m = Module()
        n, cells = self.n, self.n * self.n
        cx = cy = n // 2
        AW = Shape.cast(range(cells)).width

        # --- memory banks ----------------------------------------------------
        banks, rds, wrs = [], [], []
        for k in range(2):
            mem = Memory(shape=signed(WIDTH), depth=cells, init=[0] * cells)
            m.submodules[f"bank{k}"] = mem
            banks.append(mem)
            rds.append(mem.read_port())
            wrs.append(mem.write_port())

        phase = Signal()          # 0: bank0 holds u (streamed), bank1 holds u_prev

        # --- control latched at the start of each sample ---------------------
        strike_cv = Signal(unsigned(4))   # quantised strike position CV
        preset_i = Signal(range(len(self.presets)))
        fm = Signal(signed(16))
        strike_pending = Signal()
        gate_prev = Signal()

        outer = Signal(unsigned(6))
        inner = Signal(unsigned(6))
        square_hole = Signal()
        slit = Signal()
        with m.Switch(preset_i):
            for p, (o_, i_, sq, sl) in enumerate(self.presets):
                with m.Case(p):
                    m.d.comb += [outer.eq(o_), inner.eq(i_),
                                 square_hole.eq(sq), slit.eq(sl)]
            with m.Default():
                o_, i_, sq, sl = self.presets[0]
                m.d.comb += [outer.eq(o_), inner.eq(i_),
                             square_hole.eq(sq), slit.eq(sl)]

        # Geometry FM: the hole radius moves every sample. This is the whole
        # point of doing it in gateware -- the mask is a comparator, so the
        # shape of the instrument is a modulation destination.
        inner_eff = Signal(signed(8))
        inner_c = Signal(unsigned(6))
        m.d.comb += inner_eff.eq(inner + (fm >> 12))
        with m.If(inner_eff < 0):
            m.d.comb += inner_c.eq(0)
        with m.Elif(inner_eff > outer - 2):
            m.d.comb += inner_c.eq(outer - 2)
        with m.Else():
            m.d.comb += inner_c.eq(inner_eff)

        outer2 = Signal(unsigned(16))
        inner2 = Signal(unsigned(16))
        m.d.comb += [outer2.eq(outer * outer), inner2.eq(inner_c * inner_c)]

        # Strike and pickup radii are derived from the live geometry, so both
        # always sit on material whatever the preset and FM are doing.
        strike_r = Signal(unsigned(6))
        pickup_r = Signal(unsigned(6))
        strike_raw = Signal(unsigned(7))
        m.d.comb += [
            strike_raw.eq(inner_c + 1 + strike_cv),
            strike_r.eq(Mux(strike_raw > outer - 1, outer - 1, strike_raw)),
            pickup_r.eq((inner_c + outer) >> 1),
        ]

        # --- scan ------------------------------------------------------------
        DRAIN = 6
        j = Signal(range(cells + DRAIN + 1))
        jx = Signal(range(n))
        jy = Signal(range(n))
        scanning = Signal()

        # Stream runs one row ahead. Past the end it wraps into row 0, whose
        # nodes are masked off, so the stale data never reaches an output.
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

        # --- neighbour delay line --------------------------------------------
        # tap[k] is the stream value from k cycles ago.
        tap = [Signal(signed(WIDTH), name=f"tap{k}") for k in range(2 * n + 1)]
        m.d.sync += tap[0].eq(cur_rd)
        for k in range(1, 2 * n + 1):
            m.d.sync += tap[k].eq(tap[k - 1])

        # --- mask, computed from the node's own coordinates ------------------
        dx = Signal(signed(8))
        dy = Signal(signed(8))
        d2 = Signal(unsigned(16))
        m.d.comb += [dx.eq(jx - cx), dy.eq(jy - cy),
                     d2.eq(dx * dx + dy * dy)]

        inside = Signal()
        in_square = Signal()
        in_slit = Signal()
        m.d.comb += [
            in_square.eq((dx < inner_c.as_signed()) & (dx > -inner_c.as_signed())
                         & (dy < inner_c.as_signed()) & (dy > -inner_c.as_signed())),
            in_slit.eq((dy < 2) & (dy > -2) & (dx > 0)),
            inside.eq((d2 <= outer2)
                      & Mux(square_hole, ~in_square, d2 > inner2)
                      & ~(slit & in_slit)),
        ]

        strike_node = Signal(AW)
        pickup_node = Signal(AW)
        m.d.comb += [
            strike_node.eq(cy * n + cx + strike_r),
            # Pickup on the perpendicular axis, so it is never the strike node.
            pickup_node.eq((cy + pickup_r) * n + cx),
        ]

        # --- pipeline ---------------------------------------------------------
        # Timing, worked out rather than assumed. A synchronous read port gives
        # data one cycle after the address, and the delay line registers that,
        # so during cycle T:
        #
        #     tap[k] == u[j(T) - 2 - k + n]
        #
        # which means the taps describe node J = j(T) - 2. Everything else has
        # to be aligned to that, and the depths below are not interchangeable:
        # an off-by-one here silently computes a mesh with the wrong topology
        # rather than failing.

        def delay(sig, k, name):
            out = sig
            for d in range(k):
                nxt_r = Signal.like(sig, name=f"{name}_d{d+1}")
                m.d.sync += nxt_r.eq(out)
                out = nxt_r
            return out

        strike_hit = Signal()
        m.d.comb += strike_hit.eq(strike_pending & (j == strike_node))

        # sum registered once: at T+1 it describes node j(T)-2.
        sum_r = Signal(signed(WIDTH + 3))
        m.d.sync += sum_r.eq(tap[0] + tap[n - 1] + tap[n + 1] + tap[2 * n])

        old_al = delay(old_rd, 2, "old")        # u_prev[j(T)-2] at T+1
        msk_al = delay(inside, 3, "msk")        # mask for j(T)-2 at T+1
        strk_al = delay(strike_hit, 3, "strk")
        val_al = delay(scanning & (j < cells), 3, "val")

        base = Signal(signed(WIDTH + 3))
        nxt = Signal(signed(WIDTH + 3))
        strike_amp = C(int(0.9 * (1 << FRAC)), signed(WIDTH + 3))
        m.d.comb += [
            base.eq((sum_r >> 1) - old_al),
            nxt.eq(base - (base >> self.loss_shift)
                   + Mux(strk_al, strike_amp, 0)),
        ]

        # Saturate rather than truncate. Wrapping a node's displacement turns a
        # loud hit into a full-scale sign flip that the mesh then propagates.
        HI = (1 << (WIDTH - 1)) - 1
        LO = -(1 << (WIDTH - 1))
        clamped = Signal(signed(WIDTH))
        with m.If(nxt > HI):
            m.d.comb += clamped.eq(HI)
        with m.Elif(nxt < LO):
            m.d.comb += clamped.eq(LO)
        with m.Else():
            m.d.comb += clamped.eq(nxt)

        written = Signal(signed(WIDTH))
        wr_valid = Signal()
        m.d.sync += [
            written.eq(Mux(msk_al, clamped, 0)),
            wr_valid.eq(val_al),
        ]
        wr_addr = delay(j[:AW], 4, "jw")        # j(T)-2 at T+2

        for k in range(2):
            m.d.comb += [
                wrs[k].addr.eq(wr_addr),
                wrs[k].data.eq(written),
                # The bank holding u_prev is the one being overwritten.
                wrs[k].en.eq(wr_valid & (phase == (0 if k == 1 else 1))),
            ]

        pickup = self.pickup_dbg
        with m.If(wr_valid & (wr_addr == pickup_node)):
            m.d.sync += pickup.eq(written)

        # --- sample-rate FSM --------------------------------------------------
        out_payload = Signal(signed(16))
        m.d.comb += out_payload.eq(pickup >> UPSHIFT)

        with m.FSM() as fsm:
            with m.State("WAIT"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    gate = Signal(signed(16))
                    pos = Signal(signed(16))
                    pre = Signal(signed(16))
                    m.d.comb += [
                        gate.eq(_raw(self.i.payload[0])),
                        pos.eq(_raw(self.i.payload[1])),
                        pre.eq(_raw(self.i.payload[2])),
                    ]
                    # Gate: rising edge above ~1 V (4000 counts at 8.192 V FS).
                    m.d.sync += gate_prev.eq(gate > 4000)
                    with m.If((gate > 4000) & ~gate_prev):
                        m.d.sync += strike_pending.eq(1)
                    # Position: 0-8 V sweeps the strike from hub to rim.
                    m.d.sync += [
                        strike_cv.eq(pos[10:14]),
                        preset_i.eq(pre[12:15]),
                        fm.eq(_raw(self.i.payload[3])),
                        j.eq(0), jx.eq(0), jy.eq(0),
                    ]
                    m.next = "SCAN"

            with m.State("SCAN"):
                m.d.comb += scanning.eq(1)
                m.d.sync += j.eq(j + 1)
                with m.If(jx == n - 1):
                    m.d.sync += [jx.eq(0), jy.eq(jy + 1)]
                with m.Else():
                    m.d.sync += jx.eq(jx + 1)
                with m.If(j == cells + DRAIN):
                    m.d.sync += [phase.eq(~phase), strike_pending.eq(0)]
                    m.next = "EMIT"

            with m.State("EMIT"):
                m.d.comb += self.o.valid.eq(1)
                for k in range(4):
                    m.d.comb += _raw(self.o.payload[k]).eq(
                        out_payload if k == 0 else 0)
                with m.If(self.o.ready):
                    m.next = "WAIT"

        return m
