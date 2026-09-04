# ORBITA -- the membrane as a wavetable.
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# LACUNA listens to the mesh: the membrane vibrates at audio rate and a pickup
# node is the output. ORBITA does the opposite. The membrane evolves slowly --
# once every UPDATE_DIV audio samples -- and a circular path through it is read
# at audio rate. The path's values are one cycle of a waveform and the scan rate
# is the pitch, so timbre and pitch are independent and a held note morphs.
#
# Verplank/Mathews/Shaw scanned synthesis. See research/scan/DESIGN.md.
#
#     in0  drive      gate edge plucks; a held level keeps it alive as a drone
#     in1  pitch      1 V/oct, 55-880 Hz -- the scan rate, not the mesh
#     in2  radius     scan circle, hub to past the rim
#     in3  geometry   audio-rate modulation of the hole radius
#     out0 scan
#     encoder short press cycles the preset (a 3 s hold still reboots)
#
# WHY A CIRCLE. A line across the membrane has two ends, and the wrap from the
# last sample back to the first is a step that buzzes once per cycle; closing it
# means mirroring, which halves the resolution and forces a symmetry on every
# waveform. A circle has no seam. It is also exactly daisy_scanned's 64-mass
# ring, except each point is coupled radially into a membrane as well as to its
# neighbours, so energy leaves the path and comes back.
#
# WHAT THE HOLE DOES. A concentric circle never crosses a concentric hole, so
# there is no flat segment and no duty cycle -- the annulus is a window the
# radius sweeps through, silent inside the hole and silent past the rim, because
# the mesh writes masked-off cells as zero. The asymmetric presets are where it
# gets interesting: the slit gives one notch per revolution, the square hole
# four, so the symmetry order of the hole picks which harmonics are emphasised.
#
# DRONING. A lossy membrane rings down and the wavetable goes quiet with it, so
# a drone needs energy put back. Rather than switching damping off -- which
# leaves a lossless resonator with a fixed spectrum that stops evolving, and
# that stillness is exactly what scanned synthesis is trying to avoid -- in0
# held above zero injects a small random-sign impulse on *every* mesh update.
# Injection and loss reach an equilibrium, and because the injection is
# broadband the shape never settles. Drive at zero leaves a plain pluck.

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


FS = 48000
F_LO, OCTAVES = 55.0, 4       # scan rate, 55-880 Hz
CV_BITS = 10                  # 1024 steps over 4 octaves, ~4.7 cents each
VOCT_Q16 = 4194               # 256 steps per 4000 counts (1 V), in Q16
PHASE_BITS = 32

N_POINTS = 6                  # 64 points on the circle
CIRC_SCALE = 6                # unit vectors stored as cos*64

# One mesh update every UPDATE_DIV audio samples. This, not lam2, is what makes
# the membrane sub-audio: at 48 kHz / 64 the mesh advances at 750 Hz, so a mode
# that would sit at 880 Hz in LACUNA lands near 14 Hz here.
UPDATE_DIV = 64
UPDATE_RATE = FS / UPDATE_DIV

# Target fundamental for the membrane's own motion, in Hz, at UPDATE_RATE. This
# is the rate at which the wavetable morphs.
F_EVOLVE = 8.0

K_FRAC = 30
INV_MU_FRAC = 10
LAM_MAX = 1 << (LAM_FRAC - 1)

# Per-update decay. At 750 Hz updates, 10 is a time constant near 1.4 s.
LOSS_SHIFT = 10

# Radius of the strike, in cells. ORBITA reads the membrane's shape directly,
# so a one-cell impulse -- which excites the cell-to-cell checkerboard as hard
# as anything else -- is heard as noise in the waveform, not just as brightness.
# Measured on a ring: one cell leaves neighbours agreeing in sign 61% of the
# time (white noise), radius 3 takes it to 92%.
MALLET = 3

# Drive one mesh update in DRIVE_EVERY rather than all of them. Refreshing the
# excitation less often lets the membrane settle between kicks: with a radius 3
# mallet, every update gives 92% and every eighth 96%.
DRIVE_EVERY = 8

# How much of the scanned value fills the 8-bit display trace.
WAVE_SHIFT = 6

# A mallet covers ~29 cells, so the same per-cell amplitude injects far more
# energy than a single-cell strike. Scaled down to leave headroom.
PLUCK_AMP = 0.15

# How much of the 16-bit snapshot reaches the output.
OUT_SHIFT = 0

# Drive amplitude, as a shift on the 12-bit level from in0. Random signs make
# the membrane a random walk, so the equilibrium amplitude is roughly the
# injection times 2**((LOSS_SHIFT-1)/2) -- about 23x here. 3 puts a full-scale
# drive near a tenth of full scale in the mesh, which leaves room for a pluck
# on top without saturating.
DRIVE_SHIFT = 3


def lam2_for_presets(presets):
    """lam2 that puts each preset's fundamental at F_EVOLVE.

    Both terms are compile-time constants -- the target frequency and each
    preset's own 1/-mu -- so this is a table, not a multiplier. Normalising by
    mu matters for the same reason it does in LACUNA: without it the thin ring
    morphs a couple of octaves faster than the drum head and changing preset
    would change how alive the sound is.
    """
    k = 2.0 * (1.0 - math.cos(2.0 * math.pi * F_EVOLVE / UPDATE_RATE))
    k_q = int(k * (1 << K_FRAC))
    out = []
    for (_o, _i, _sq, _sl, inv_mu) in presets:
        lam = (k_q * inv_mu) >> (K_FRAC + INV_MU_FRAC - LAM_FRAC)
        out.append(min(lam, LAM_MAX - 1))
    return out


def nco_table():
    """Phase increment per audio sample, 1 V/oct over OCTAVES from F_LO."""
    out = []
    for i in range(1 << CV_BITS):
        f = F_LO * 2.0 ** (OCTAVES * i / (1 << CV_BITS))
        out.append(int(round(f * (1 << PHASE_BITS) / FS)))
    return out


def circle_table():
    """Unit circle as (cos, sin) packed into 16 bits, cos in the low byte."""
    out = []
    for k in range(1 << N_POINTS):
        a = 2.0 * math.pi * k / (1 << N_POINTS)
        c = int(round(math.cos(a) * (1 << CIRC_SCALE)))
        s = int(round(math.sin(a) * (1 << CIRC_SCALE)))
        out.append((c & 0xFF) | ((s & 0xFF) << 8))
    return out


class Orbita(wiring.Component):

    bitstream_help = BitstreamHelp(
        brief="Orbita: the membrane scanned as a wavetable",
        io_left=['drive', 'pitch', 'radius', 'geometry',
                 'scan', '', '', ''],
        io_right=['preset', '', 'video (fixed)', '', '', '']
    )

    def __init__(self, n=32, presets=PRESETS, video=False,
                 update_div=UPDATE_DIV):
        self.n = n
        self.update_div = update_div
        self.video = video
        self.presets = presets
        # Exposed for testbenches: the raw scanned value before output scaling.
        self.scan_dbg = Signal(signed(16))
        # Exposed so the top level can draw the scan circle over the mesh.
        self.radius_dbg = Signal(4)
        super().__init__({
            "i": In(stream.Signature(data.ArrayLayout(ASQ, 4))),
            "o": Out(stream.Signature(data.ArrayLayout(ASQ, 4))),
            "button": In(1),
            "disp_addr": In(range(n * n)),
            "disp_data": Out(8),
            # The circle unrolled: one bin per scan point, so the display can
            # draw the waveform as it is actually read, phase-locked to the
            # ring above it rather than free-running.
            "wave_addr": In(N_POINTS),
            "wave_data": Out(8),
        })

    def elaborate(self, platform):
        m = Module()
        n = self.n
        cx = cy = n // 2

        m.submodules.mesh = mesh = Mesh(
            n=n, presets=self.presets, video=self.video, snapshot=True,
            mallet=MALLET)
        m.d.comb += [
            mesh.disp_addr.eq(self.disp_addr),
            self.disp_data.eq(mesh.disp_data),
            mesh.loss_shift.eq(LOSS_SHIFT),
        ]

        m.submodules.nco_mem = nco_mem = Memory(
            shape=unsigned(PHASE_BITS), depth=1 << CV_BITS, init=nco_table())
        nco_rd = nco_mem.read_port()

        m.submodules.circ_mem = circ_mem = Memory(
            shape=unsigned(16), depth=1 << N_POINTS, init=circle_table())
        circ_rd = circ_mem.read_port()

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

        # lam2 is a per-preset constant here: pitch comes from the scan rate,
        # so the only thing lam2 sets is how fast the membrane itself moves.
        lam2 = Signal(LAM_FRAC)
        lam_tab = lam2_for_presets(self.presets)
        with m.Switch(preset_i):
            for p, v in enumerate(lam_tab):
                with m.Case(p):
                    m.d.comb += lam2.eq(v)
            with m.Default():
                m.d.comb += lam2.eq(lam_tab[0])
        m.d.comb += mesh.lam2.eq(lam2)

        # --- controls ---------------------------------------------------------
        cv_index = Signal(CV_BITS)
        pitch_q = Signal(signed(16))
        radius = Signal(unsigned(4))
        drive = Signal(unsigned(12))
        pluck_pending = Signal()
        gate_prev = Signal()
        m.d.comb += nco_rd.addr.eq(cv_index)

        # --- noise, for the drone ---------------------------------------------
        # A constant injection would build one standing wave and sit there; a
        # random sign keeps exciting every mode, which is what makes a held note
        # keep moving.
        lfsr = Signal(16, init=0xACE1)
        m.d.sync += lfsr.eq(Cat(lfsr[1:], lfsr[0] ^ lfsr[2] ^ lfsr[3] ^ lfsr[5]))

        # --- the scan ---------------------------------------------------------
        phase = Signal(PHASE_BITS)
        k_idx = Signal(N_POINTS)
        frac = Signal(10)
        m.d.comb += [
            k_idx.eq(phase[PHASE_BITS - N_POINTS:]),
            frac.eq(phase[PHASE_BITS - N_POINTS - 10:PHASE_BITS - N_POINTS]),
        ]

        # One multiplier pair, shared between the two points, by driving the
        # address straight off the circle ROM output rather than registering it.
        cos_v = Signal(signed(8))
        sin_v = Signal(signed(8))
        ox = Signal(signed(8))
        oy = Signal(signed(8))
        # The address is registered rather than fed straight to the snapshot:
        # circle ROM out (5.6 ns) -> multiply (3.9 ns) -> snapshot address in one
        # cycle took the sync domain to 49.7 MHz. Costs one state below.
        addr_r = Signal(range(n * n))
        m.d.comb += [
            cos_v.eq(circ_rd.data[:8].as_signed()),
            sin_v.eq(circ_rd.data[8:].as_signed()),
            # Round rather than floor: a plain shift biases every negative
            # offset a cell inwards, which would make the circle an egg.
            ox.eq((cos_v * radius + (1 << (CIRC_SCALE - 1))) >> CIRC_SCALE),
            oy.eq((sin_v * radius + (1 << (CIRC_SCALE - 1))) >> CIRC_SCALE),
            mesh.snap_addr.eq(addr_r),
        ]
        addr_c = Signal(range(n * n))
        m.d.comb += addr_c.eq(((cy + oy) << (n - 1).bit_length()) + (cx + ox))

        # Held, not pulsed: the mesh does not reach the strike node until
        # hundreds of cycles into its scan, so an amplitude driven only on the
        # cycle that pulses `step` has long since gone back to zero by the time
        # it is sampled. LACUNA gets away with a combinational constant.
        strike_amp = Signal(signed(WIDTH + 4))
        m.d.comb += mesh.strike_amp.eq(strike_amp)

        m.d.comb += self.radius_dbg.eq(radius)

        v0 = Signal(signed(16))
        v1 = Signal(signed(16))
        # Registered: the interpolation multiply, the DC blocker and the output
        # saturation in one cycle off v1 took the sync domain to 49.6 MHz.
        scanned = Signal(signed(16))
        scanned_c = Signal(signed(16))
        m.d.comb += scanned_c.eq(v0 + (((v1 - v0) * frac) >> 10))

        # --- waveform, for the display ----------------------------------------
        m.submodules.wave_mem = wave_mem = Memory(
            shape=unsigned(8), depth=1 << N_POINTS, init=[128] * (1 << N_POINTS))
        wave_wr = wave_mem.write_port()
        wave_rd = wave_mem.read_port(domain="dvi")
        wv = Signal(signed(16))
        wq = Signal(unsigned(8))
        m.d.comb += wv.eq(scanned >> WAVE_SHIFT)
        with m.If(wv > 127):
            m.d.comb += wq.eq(255)
        with m.Elif(wv < -127):
            m.d.comb += wq.eq(1)
        with m.Else():
            m.d.comb += wq.eq((wv + 128)[:8])
        m.d.comb += [
            wave_wr.addr.eq(k_idx),
            wave_wr.data.eq(wq),
            wave_rd.addr.eq(self.wave_addr),
            self.wave_data.eq(wave_rd.data),
        ]

        # --- DC blocker -------------------------------------------------------
        # The membrane carries a DC component and the scan inherits it; a
        # wavetable with an offset wastes headroom and thumps when the radius
        # moves. One pole at about 5 Hz.
        dc_x1 = Signal(signed(16))
        dc_y1 = Signal(signed(24))
        dc_y = Signal(signed(24))
        m.d.comb += dc_y.eq(scanned - dc_x1 + dc_y1 - (dc_y1 >> 10))

        out_payload = Signal(signed(16))
        dc_s = Signal(signed(24))
        m.d.comb += dc_s.eq(dc_y >> OUT_SHIFT)
        with m.If(dc_s > 32767):
            m.d.comb += out_payload.eq(32767)
        with m.Elif(dc_s < -32768):
            m.d.comb += out_payload.eq(-32768)
        with m.Else():
            m.d.comb += out_payload.eq(dc_s)

        # --- per-sample FSM ---------------------------------------------------
        div_count = Signal(range(max(2, self.update_div)))
        drive_count = Signal(range(DRIVE_EVERY))

        with m.FSM():
            with m.State("WAIT"):
                m.d.comb += self.i.ready.eq(1)
                with m.If(self.i.valid):
                    gate = Signal(signed(16))
                    pitch = Signal(signed(16))
                    pos = Signal(signed(16))
                    m.d.comb += [
                        gate.eq(_raw(self.i.payload[0])),
                        pitch.eq(_raw(self.i.payload[1])),
                        pos.eq(_raw(self.i.payload[2])),
                    ]
                    m.d.sync += gate_prev.eq(gate > 4000)
                    with m.If((gate > 4000) & ~gate_prev):
                        m.d.sync += pluck_pending.eq(1)
                    # Drive is the held level, not the edge: above the gate
                    # threshold it also keeps feeding the membrane.
                    m.d.sync += drive.eq(Mux(gate < 0, 0, gate[4:16]))
                    # Same clamps as LACUNA's position CV: a bare bit-slice of a
                    # signed value reads full scale for an idle jack one count
                    # below zero, and folds back around above 4 V.
                    m.d.sync += [
                        radius.eq(Mux(pos < 0, 0,
                                  Mux(pos > 16383, 15, pos[10:14]))),
                        mesh.fm.eq(_raw(self.i.payload[3])),
                    ]
                    m.d.sync += pitch_q.eq(pitch)
                    m.next = "IDX"

            with m.State("IDX"):
                # Its own state for the same reason LACUNA needs one: pitch
                # arrives from the CODEC calibrator, and calibrator -> multiply
                # -> clamp -> cv_index in a single cycle misses 60 MHz.
                idx = Signal(signed(20))
                m.d.comb += idx.eq((pitch_q * VOCT_Q16 + (1 << 15)) >> 16)
                with m.If(idx < 0):
                    m.d.sync += cv_index.eq(0)
                with m.Elif(idx > (1 << CV_BITS) - 1):
                    m.d.sync += cv_index.eq((1 << CV_BITS) - 1)
                with m.Else():
                    m.d.sync += cv_index.eq(idx)
                m.next = "P0"

            with m.State("P0"):
                # Address the first circle point; its data lands next cycle.
                m.d.comb += circ_rd.addr.eq(k_idx)
                m.next = "P1"

            with m.State("P1"):
                # circ_rd.data is point k: latch its mesh address, and ask for
                # the next point at the same time.
                m.d.sync += addr_r.eq(addr_c)
                m.d.comb += circ_rd.addr.eq(k_idx + 1)
                m.next = "P2"

            with m.State("P2"):
                # addr_r holds point k, so the snapshot read is in flight;
                # circ_rd.data is now point k+1, so latch that address too.
                m.d.sync += addr_r.eq(addr_c)
                m.next = "P3"

            with m.State("P3"):
                m.d.sync += v0.eq(mesh.snap_data)
                m.next = "P4"

            with m.State("P4"):
                m.d.sync += v1.eq(mesh.snap_data)
                m.next = "MIX"

            with m.State("MIX"):
                m.d.sync += scanned.eq(scanned_c)
                m.next = "EMIT"

            with m.State("EMIT"):
                m.d.comb += self.o.valid.eq(1)
                for k in range(4):
                    m.d.comb += _raw(self.o.payload[k]).eq(
                        out_payload if k == 0 else 0)
                with m.If(self.o.ready):
                    m.d.comb += wave_wr.en.eq(1)
                    m.d.sync += [
                        phase.eq(phase + nco_rd.data),
                        dc_x1.eq(scanned),
                        dc_y1.eq(dc_y),
                        self.scan_dbg.eq(scanned),
                    ]
                    # Advance the membrane every UPDATE_DIV samples. The mesh
                    # runs its own scan in the background; nothing here waits on
                    # it, and its snapshot is a separate memory from the one the
                    # scan above reads.
                    with m.If(div_count == self.update_div - 1):
                        m.d.sync += div_count.eq(0)
                        m.d.comb += mesh.step.eq(1)
                        with m.If(pluck_pending):
                            m.d.comb += mesh.strike.eq(1)
                            m.d.sync += [
                                strike_amp.eq(
                                    C(int(PLUCK_AMP * (1 << FRAC)),
                                      signed(WIDTH + 4))),
                                pluck_pending.eq(0),
                            ]
                        with m.Elif((drive != 0) & (drive_count == 0)):
                            m.d.comb += mesh.strike.eq(1)
                            m.d.sync += strike_amp.eq(
                                Mux(lfsr[0], drive, -drive) << DRIVE_SHIFT)
                        m.d.sync += drive_count.eq(drive_count + 1)
                    with m.Else():
                        m.d.sync += div_count.eq(div_count + 1)
                    m.next = "WAIT"

        return m
