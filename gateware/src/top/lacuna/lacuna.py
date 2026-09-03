# Lacuna -- a 2D membrane mesh whose hole is the instrument.
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# A finite-difference membrane on a 32x32 grid, one node per cycle in raster
# order. What makes it worth doing in gateware rather than on a CPU is that the
# boundary is a comparator rather than an array: the shape of the instrument
# can change every single sample, so geometry is a modulation destination.
#
#     in0  strike     rising edge above ~1 V
#     in1  tension    1 V/oct, 55-880 Hz
#     in2  position   strike position, hub to rim
#     in3  geometry   audio-rate modulation of the hole radius
#     out0 mesh
#     encoder short press cycles the preset (a 3 s hold still reboots)
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
# TUNING is exact rather than calibrated. For a mode whose discrete-Laplacian
# eigenvalue is mu, the scheme's dispersion relation gives
#
#     f = (fs / 2pi) * arccos(1 + lam2*mu/2)      ->      lam2 = 2*(cos(w)-1)/mu
#
# so the LUT below is computed, not measured. mu is a property of the masked
# domain, so each preset has its own; folding 1/-mu in normalises pitch, and a
# given CV means the same note on every geometry. Without it the thin ring
# sits two and a half octaves above the full disc.
#
# DAMPING TRACKS PITCH. loss_shift is a per-sample decay, so at low pitch a
# cycle spans far more samples and the mode is over-damped. Left fixed it looks
# exactly like a tuning error -- it was the entire apparent low-end pitch error
# during development (+79% four octaves down), not dispersion.
#
# MEMORY. Two banks swap roles each sample. The bank holding u is read as a
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

import math

from amaranth import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out

try:
    from tiliqua.dsp import ASQ
    from tiliqua.build.types import BitstreamHelp
except ImportError:                       # standalone simulation
    from shims import ASQ, BitstreamHelp


# 24-bit state with 22 fractional bits. 18 is not enough: the loss term is a
# right shift, so it stops working below |u| < 2**loss_shift, and at 17
# fractional bits that dead zone sits at -24 dBFS, freezing the tail at a DC
# offset.
WIDTH, FRAC = 24, 22
LAM_FRAC = 26                # lam2 fixed-point
K_FRAC = 30                  # tuning table fixed-point
INV_MU_FRAC = 10             # 1/-mu fixed-point
LAM_MAX = 1 << (LAM_FRAC - 1)   # lam2 = 0.5, the stability limit

FS = 48000
F_LO, OCTAVES = 55.0, 4       # 55-880 Hz; 880 is under every preset's limit
CV_BITS = 10                  # 1024 steps over 4 octaves, ~4.7 cents each
VOCT_Q16 = 4194               # 256 steps per 4000 counts (1 V), in Q16

# (outer, inner, square_hole, slit, inv_mu_q). The inv_mu values are the
# fundamental eigenvalue of the discrete Laplacian on each masked domain,
# computed by tiliqua_mesh/pitch.py -- see the repo this core came from.
PRESETS = [
    (14, 0,  0, 0, 36652),    # full disc
    (14, 3,  0, 0, 14852),    # narrow hole
    (14, 7,  0, 0,  6330),    # wide ring
    (14, 11, 0, 0,  1598),    # thin ring
    (14, 5,  1, 0,  8807),    # square hole
    (14, 8,  0, 1,  4651),    # slit ring
]


def tuning_table():
    """K(f) = 2*(1 - cos(2*pi*f/fs)), so lam2 = K * (1/-mu). Exponential in
    the index, which is what makes the CV 1 V/oct."""
    out = []
    for i in range(1 << CV_BITS):
        f = F_LO * (2.0 ** (OCTAVES * i / (1 << CV_BITS)))
        k = 2.0 * (1.0 - math.cos(2.0 * math.pi * f / FS))
        out.append(int(round(k * (1 << K_FRAC))))
    return out


def _raw(v):
    """ASQ is a fixed-point type in-tree and a plain signed(16) standalone."""
    return v.as_value() if hasattr(v, "as_value") else v


class Lacuna(wiring.Component):

    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))
    # Optional input: CoreTop connects the encoder if the core asks for it,
    # following the same `hasattr` convention it uses for i_midi.
    button: In(1)

    bitstream_help = BitstreamHelp(
        brief="Lacuna: membrane mesh, hole is the instrument",
        io_left=['strike', 'tension', 'position', 'geometry',
                 'mesh', '', '', ''],
        io_right=['preset', '', '', '', '', '']
    )

    def __init__(self, n=32, base_loss=13, presets=PRESETS):
        assert n % 2 == 0
        self.n = n
        self.base_loss = base_loss
        self.presets = presets
        for outer, _, _, _, _ in presets:
            assert outer <= n // 2 - 2, (
                f"outer radius {outer} reaches the array border on a {n}x{n} "
                f"grid; the delay-line taps wrap across rows and rely on those "
                f"nodes being masked off")
        # Exposed so a testbench can compare mesh state against the reference
        # without going through the output scaling.
        self.pickup_dbg = Signal(signed(WIDTH))
        super().__init__()

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

        m.submodules.tuning = tuning = Memory(
            shape=unsigned(K_FRAC), depth=1 << CV_BITS, init=tuning_table())
        tune_rd = tuning.read_port()

        # --- preset, cycled by the encoder ------------------------------------
        preset_i = Signal(range(len(self.presets)))
        btn_sync = Signal(2)
        btn_stable = Signal()
        btn_prev = Signal()
        debounce = Signal(20)          # ~17 ms at 60 MHz
        m.d.sync += btn_sync.eq(Cat(self.button, btn_sync[0]))
        with m.If(btn_sync[1] == btn_stable):
            m.d.sync += debounce.eq(0)
        with m.Else():
            m.d.sync += debounce.eq(debounce + 1)
            with m.If(debounce.all()):
                m.d.sync += [btn_stable.eq(btn_sync[1]), debounce.eq(0)]
        m.d.sync += btn_prev.eq(btn_stable)
        with m.If(btn_stable & ~btn_prev):
            with m.If(preset_i == len(self.presets) - 1):
                m.d.sync += preset_i.eq(0)
            with m.Else():
                m.d.sync += preset_i.eq(preset_i + 1)

        outer = Signal(unsigned(6))
        inner = Signal(unsigned(6))
        square_hole = Signal()
        slit = Signal()
        inv_mu = Signal(unsigned(17))
        with m.Switch(preset_i):
            for p, (o_, i_, sq, sl, im) in enumerate(self.presets):
                with m.Case(p):
                    m.d.comb += [outer.eq(o_), inner.eq(i_), square_hole.eq(sq),
                                 slit.eq(sl), inv_mu.eq(im)]
            with m.Default():
                o_, i_, sq, sl, im = self.presets[0]
                m.d.comb += [outer.eq(o_), inner.eq(i_), square_hole.eq(sq),
                             slit.eq(sl), inv_mu.eq(im)]

        # --- geometry, live ----------------------------------------------------
        fm = Signal(signed(16))
        inner_eff = Signal(signed(8))
        inner_c = Signal(unsigned(6))
        m.d.comb += inner_eff.eq(inner + (fm >> 12))
        with m.If(inner_eff < 0):
            m.d.comb += inner_c.eq(0)
        with m.Elif(inner_eff > outer - 2):
            m.d.comb += inner_c.eq(outer - 2)
        with m.Else():
            m.d.comb += inner_c.eq(inner_eff)

        # Geometry is a per-sample quantity -- it is constant across all cells
        # of a scan -- but the mask consumes it once per node, so left
        # combinational the whole chain
        #
        #     preset_i -> inner + fm -> clamp -> inner_c^2 -> d2 > inner2 -> mask
        #
        # lands in a single cycle: 19.2 ns, against 16.7 ns at 60 MHz. Two
        # register stages move the multiply and the preset mux off the per-node
        # path. Everything the scan reads comes from stage 2, so the radii, the
        # squared radii and the shape flags stay coherent with each other; a
        # geometry change now takes effect 2 cycles later, out of the 1035 in a
        # sample period.
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
        m.d.sync += [g_inner.eq(g1_inner), g_outer.eq(g1_outer),
                     g_square.eq(g1_square), g_slit.eq(g1_slit),
                     outer2.eq(g1_outer * g1_outer),
                     inner2.eq(g1_inner * g1_inner)]

        # Strike and pickup radii follow the live geometry. A pickup at a fixed
        # radius falls inside the hole on the thin-ring preset and reads zero
        # forever, which is indistinguishable from a dead core.
        strike_cv = Signal(unsigned(4))
        strike_r = Signal(unsigned(6))
        pickup_r = Signal(unsigned(6))
        strike_raw = Signal(unsigned(7))
        m.d.comb += [
            strike_raw.eq(g_inner + 1 + strike_cv),
            strike_r.eq(Mux(strike_raw > g_outer - 1, g_outer - 1, strike_raw)),
            pickup_r.eq((g_inner + g_outer) >> 1),
        ]

        # --- tension ------------------------------------------------------------
        tension_q = Signal(signed(16))
        lam2 = Signal(unsigned(LAM_FRAC))
        loss_shift = Signal(range(24))
        cv_index = Signal(CV_BITS)
        octave = Signal(range(OCTAVES))

        # tune_rd.data is a BRAM output (5.6 ns clk-to-q) and inv_mu comes off
        # the preset mux, so feeding both into a multiplier and then straight
        # into the clamp below put ~19 ns of logic in one cycle -- on its own
        # enough to miss 60 MHz. Registering the product splits that in half at
        # a cost of one extra state in the FSM below.
        lam_prod = Signal(unsigned(K_FRAC + 17))
        m.d.sync += lam_prod.eq(tune_rd.data * inv_mu)

        lam_wide = Signal(unsigned(K_FRAC + 17))
        m.d.comb += lam_wide.eq(lam_prod >> (K_FRAC + INV_MU_FRAC - LAM_FRAC))

        # --- scan ---------------------------------------------------------------
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

        dx = Signal(signed(8))
        dy = Signal(signed(8))
        d2 = Signal(unsigned(16))
        m.d.comb += [dx.eq(jx - cx), dy.eq(jy - cy),
                     d2.eq(dx * dx + dy * dy)]

        inside = Signal()
        in_square = Signal()
        in_slit = Signal()
        m.d.comb += [
            in_square.eq((dx < g_inner.as_signed()) & (dx > -g_inner.as_signed())
                         & (dy < g_inner.as_signed()) & (dy > -g_inner.as_signed())),
            in_slit.eq((dy < 2) & (dy > -2) & (dx > 0)),
            inside.eq((d2 <= outer2)
                      & Mux(g_square, ~in_square, d2 > inner2)
                      & ~(g_slit & in_slit)),
        ]

        strike_node = Signal(AW)
        pickup_node = Signal(AW)
        m.d.comb += [
            strike_node.eq(cy * n + cx + strike_r),
            pickup_node.eq((cy + pickup_r) * n + cx),
        ]

        # --- pipeline ------------------------------------------------------------
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

        strike_pending = Signal()
        strike_hit = Signal()
        m.d.comb += strike_hit.eq(strike_pending & (j == strike_node))

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
        m.d.sync += [prod_r.eq(lap_r * lam2), cen3.eq(cen2)]

        old_al = delay(old_rd, 4, "old")
        msk_al = delay(inside, 5, "msk")
        strk_al = delay(strike_hit, 5, "strk")
        val_al = delay(scanning & (j < cells), 5, "val")

        base = Signal(signed(WIDTH + 4))
        nxt = Signal(signed(WIDTH + 4))
        strike_amp = C(int(0.9 * (1 << FRAC)), signed(WIDTH + 4))
        m.d.comb += [
            base.eq((prod_r >> LAM_FRAC) + (cen3 << 1) - old_al),
            nxt.eq(base - (base >> loss_shift) + Mux(strk_al, strike_amp, 0)),
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
        m.d.sync += [written.eq(Mux(msk_al, clamped, 0)), wr_valid.eq(val_al)]
        wr_addr = delay(j[:AW], 6, "jw")

        for k in range(2):
            m.d.comb += [
                wrs[k].addr.eq(wr_addr),
                wrs[k].data.eq(written),
                wrs[k].en.eq(wr_valid & (phase == (0 if k == 1 else 1))),
            ]

        with m.If(wr_valid & (wr_addr == pickup_node)):
            m.d.sync += self.pickup_dbg.eq(written)

        out_payload = Signal(signed(16))
        m.d.comb += out_payload.eq(self.pickup_dbg >> (FRAC - 15))

        # --- sample-rate FSM ------------------------------------------------------
        m.d.comb += tune_rd.addr.eq(cv_index)

        with m.FSM():
            with m.State("WAIT"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    gate = Signal(signed(16))
                    tension = Signal(signed(16))
                    pos = Signal(signed(16))
                    m.d.comb += [
                        gate.eq(_raw(self.i.payload[0])),
                        tension.eq(_raw(self.i.payload[1])),
                        pos.eq(_raw(self.i.payload[2])),
                    ]
                    gate_prev = Signal()
                    m.d.sync += gate_prev.eq(gate > 4000)
                    with m.If((gate > 4000) & ~gate_prev):
                        m.d.sync += strike_pending.eq(1)
                    m.d.sync += [
                        strike_cv.eq(pos[10:14]),
                        fm.eq(_raw(self.i.payload[3])),
                        tension_q.eq(tension),
                    ]
                    m.next = "IDX"

            with m.State("IDX"):
                # 1 V/oct: 1 V is 4000 counts (4 counts/mV, ASQ full scale is
                # 8.192 V) and the table is 256 steps to the octave, so a volt has
                # to buy 256 steps. A plain >>4 buys 250 and runs ~28 cents flat
                # per volt, compounding to about a semitone at 4 V. The rounding
                # term keeps 1 V landing on 256 rather than 255.
                #
                # This gets its own state because tension arrives from the CODEC
                # calibrator: calibrator -> multiply -> clamp -> cv_index in one
                # cycle is ~17.5 ns and misses 60 MHz on its own.
                idx = Signal(signed(20))
                m.d.comb += idx.eq((tension_q * VOCT_Q16 + (1 << 15)) >> 16)
                with m.If(idx < 0):
                    m.d.sync += cv_index.eq(0)
                with m.Elif(idx > (1 << CV_BITS) - 1):
                    m.d.sync += cv_index.eq((1 << CV_BITS) - 1)
                with m.Else():
                    m.d.sync += cv_index.eq(idx)
                m.next = "TUNE"

            with m.State("TUNE"):
                # One cycle for the table read, one for the multiply, one for
                # the clamp, then the damping shift follows the octave so decay
                # per cycle stays put.
                m.d.sync += octave.eq(cv_index[CV_BITS - 2:])
                m.next = "TUNE2"

            with m.State("TUNE2"):
                # Nothing to do but let lam_prod register the multiply.
                m.next = "TUNE3"

            with m.State("TUNE3"):
                m.d.sync += [
                    lam2.eq(Mux(lam_wide > LAM_MAX, LAM_MAX, lam_wide)),
                    loss_shift.eq(self.base_loss + ((OCTAVES - 1 - octave) >> 1)),
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
