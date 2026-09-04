# Lacuna -- a 2D membrane mesh whose hole is the instrument.
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# The membrane itself lives in mesh.py, shared with ORBITA. This file is what
# makes it an instrument you strike and listen to: the CV mapping, the pitch
# table, the encoder, and a pickup node for the output.
#
#     in0  strike     rising edge above ~1 V
#     in1  tension    1 V/oct, 55-880 Hz
#     in2  position   strike position, hub to rim
#     in3  geometry   audio-rate modulation of the hole radius
#     out0 mesh
#     encoder short press cycles the preset (a 3 s hold still reboots)
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
# during development (+79% four octaves down), not dispersion. It tracks at half
# a shift per octave rather than a full one: at a full shift the bottom octave
# rang for 2.7 s against 0.34 s at the top, and dominated everything.

import math

from amaranth import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out

from mesh import Mesh, PRESETS, WIDTH, FRAC, LAM_FRAC, _raw

try:
    from tiliqua.dsp import ASQ
    from tiliqua.build.types import BitstreamHelp
except ImportError:                       # standalone simulation
    from shims import ASQ, BitstreamHelp


K_FRAC = 30                  # tuning table fixed-point
INV_MU_FRAC = 10             # 1/-mu fixed-point
LAM_MAX = 1 << (LAM_FRAC - 1)   # lam2 = 0.5, the stability limit

FS = 48000
F_LO, OCTAVES = 55.0, 4       # 55-880 Hz; 880 is under every preset's limit
CV_BITS = 10                  # 1024 steps over 4 octaves, ~4.7 cents each
VOCT_Q16 = 4194               # 256 steps per 4000 counts (1 V), in Q16


def tuning_table():
    """K(f) = 2*(1 - cos(2*pi*f/fs)), so lam2 = K * (1/-mu). Exponential in
    pitch, so the table is the whole 1 V/oct map and each preset only needs its
    own 1/-mu to land on the same note."""
    out = []
    for i in range(1 << CV_BITS):
        f = F_LO * 2.0 ** (OCTAVES * i / (1 << CV_BITS))
        k = 2.0 * (1.0 - math.cos(2.0 * math.pi * f / FS))
        out.append(int(k * (1 << K_FRAC)))
    return out


class Lacuna(wiring.Component):

    bitstream_help = BitstreamHelp(
        brief="Lacuna: membrane mesh, hole is the instrument",
        io_left=['strike', 'tension', 'position', 'geometry',
                 'mesh', '', '', ''],
        io_right=['preset', '', 'video (fixed)', '', '', '']
    )

    def __init__(self, n=32, base_loss=13, presets=PRESETS, video=False):
        self.n = n
        self.video = video
        self.base_loss = base_loss
        self.presets = presets
        # Exposed so a testbench can compare mesh state against the reference
        # without going through the output scaling.
        self.pickup_dbg = Signal(signed(WIDTH))
        super().__init__({
            "i": In(stream.Signature(data.ArrayLayout(ASQ, 4))),
            "o": Out(stream.Signature(data.ArrayLayout(ASQ, 4))),
            # Optional input: a top level connects the encoder if the core asks
            # for it, following the same `hasattr` convention used for i_midi.
            "button": In(1),
            # Display tap, read from the `dvi` domain. Reads 0 unless `video`.
            "disp_addr": In(range(n * n)),
            "disp_data": Out(8),
            "strike_at": Out(range(n * n)),
            "pickup_at": Out(range(n * n)),
        })

    def elaborate(self, platform):
        m = Module()

        m.submodules.mesh = mesh = Mesh(
            n=self.n, presets=self.presets, video=self.video)
        m.d.comb += [
            mesh.disp_addr.eq(self.disp_addr),
            self.disp_data.eq(mesh.disp_data),
            self.pickup_dbg.eq(mesh.pickup),
            self.strike_at.eq(mesh.strike_at),
            self.pickup_at.eq(mesh.pickup_at),
            # A struck instrument: one pulse, near full scale.
            mesh.strike_amp.eq(C(int(0.9 * (1 << FRAC)), signed(WIDTH + 4))),
        ]

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
        m.d.comb += mesh.preset_i.eq(preset_i)

        inv_mu = Signal(unsigned(17))
        with m.Switch(preset_i):
            for p, (_o, _i, _sq, _sl, im) in enumerate(self.presets):
                with m.Case(p):
                    m.d.comb += inv_mu.eq(im)
            with m.Default():
                m.d.comb += inv_mu.eq(self.presets[0][4])

        # --- tension ------------------------------------------------------------
        tension_q = Signal(signed(16))
        lam2 = Signal(unsigned(LAM_FRAC))
        loss_shift = Signal(range(24))
        cv_index = Signal(CV_BITS)
        octave = Signal(range(OCTAVES))
        m.d.comb += [mesh.lam2.eq(lam2), mesh.loss_shift.eq(loss_shift)]

        # tune_rd.data is a BRAM output (5.6 ns clk-to-q) and inv_mu comes off
        # the preset mux, so feeding both into a multiplier and then straight
        # into the clamp below put ~19 ns of logic in one cycle -- on its own
        # enough to miss 60 MHz. Registering the product splits that in half at
        # a cost of one extra state in the FSM below.
        lam_prod = Signal(unsigned(K_FRAC + 17))
        m.d.sync += lam_prod.eq(tune_rd.data * inv_mu)

        lam_wide = Signal(unsigned(K_FRAC + 17))
        m.d.comb += lam_wide.eq(lam_prod >> (K_FRAC + INV_MU_FRAC - LAM_FRAC))

        # Saturate, do not truncate. pickup is 24-bit and the mesh clamps it at
        # +/-2**23, so pickup >> 7 reaches +/-65535 -- assigning that into a
        # signed(16) keeps the low bits and wraps, turning a loud moment into a
        # full-scale flip of the opposite sign. An occasional click that only
        # happens when it is loud.
        out_payload = Signal(signed(16))
        out_wide = Signal(signed(20))
        m.d.comb += out_wide.eq(mesh.pickup >> (FRAC - 15))
        with m.If(out_wide > 32767):
            m.d.comb += out_payload.eq(32767)
        with m.Elif(out_wide < -32768):
            m.d.comb += out_payload.eq(-32768)
        with m.Else():
            m.d.comb += out_payload.eq(out_wide)

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
                        m.d.comb += mesh.strike.eq(1)
                    m.d.sync += [
                        # pos is signed, so a bare bit-slice reads 15 -- hard
                        # rim, the loudest and most inharmonic strike there is --
                        # for an idle jack sitting one count below zero, and
                        # folds back around above 4 V. Clamp both ends.
                        mesh.strike_cv.eq(Mux(pos < 0, 0,
                                          Mux(pos > 16383, 15, pos[10:14]))),
                        mesh.fm.eq(_raw(self.i.payload[3])),
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
                ]
                m.d.comb += mesh.step.eq(1)
                m.next = "SCAN"

            with m.State("SCAN"):
                with m.If(mesh.done):
                    m.next = "EMIT"

            with m.State("EMIT"):
                m.d.comb += self.o.valid.eq(1)
                for k in range(4):
                    m.d.comb += _raw(self.o.payload[k]).eq(
                        out_payload if k == 0 else 0)
                with m.If(self.o.ready):
                    m.next = "WAIT"

        return m
