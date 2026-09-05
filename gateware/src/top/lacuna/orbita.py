# ORBITA -- the membrane as a wavetable.
#
# Copyright (c) 2026 D. Baghurst
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
# Scan rate. Eight octaves at an exact 256 steps each.
#
# 0 V is 55 Hz, which is where it started and where the bass this instrument is
# good at actually lives. The offset briefly moved it to 220 Hz so an unpatched
# input landed somewhere musical, which put the low end out of reach of an
# ordinary 0-5 V sequencer -- a bad trade for an instrument whose best register
# is the bottom. The octaves were the useful half of that change: the ceiling
# was 880 Hz and is now 7040 Hz, so the high end is reachable without giving up
# the low one. Below 55 Hz needs negative CV, down to 27.5 Hz at -1 V.
F_LO, OCTAVES = 27.5, 8
CV_BITS = 11                  # 2048 steps, 256 to the octave
PITCH_OFFSET = 1 * 256        # 0 V lands on 55 Hz, where the bass lives
VOCT_Q16 = 4194               # 256 steps per 4000 counts (1 V), in Q16
PHASE_BITS = 32

N_POINTS = 6                  # 64 points on the circle
CIRC_SCALE = 6                # unit vectors stored as cos*64
RAD_FRAC = 4                  # sub-cell precision of the scan position

# One mesh update every UPDATE_DIV audio samples. This, not lam2, is what makes
# the membrane sub-audio: at 48 kHz / 64 the mesh advances at 750 Hz, so a mode
# that would sit at 880 Hz in LACUNA lands near 14 Hz here.
UPDATE_DIV = 64
UPDATE_RATE = FS / UPDATE_DIV

# Target fundamental for the membrane's own motion, in Hz, at UPDATE_RATE, and
# the rate at which the wavetable morphs.
#
# This has to put the *whole* membrane below hearing, not just its fundamental,
# or its high modes ring audibly in their own right and are heard as noise on
# top of the scanned tone. The highest mode is about 17x the fundamental, so:
#
#     F_EVOLVE   fundamental   10th mode   checkerboard
#          8.0        8.0 Hz     23.2 Hz       143.4 Hz   <- audible membrane
#          2.0        2.0 Hz      5.8 Hz        33.8 Hz
#          1.0        1.0 Hz      2.9 Hz        16.9 Hz   <- all sub-audio
#
# 1 Hz means a held note morphs over about a second, which is what scanned
# synthesis is for. Faster than this and you are listening to the membrane
# rather than to the shape it makes.
F_EVOLVE = 1.0

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
MALLET_MAX = 3

# Drive one mesh update in DRIVE_EVERY. Every eighth measured slightly smoother
# spatially than every one (96% against 92%), but at a 750 Hz update rate that
# is a kick every 93.75 Hz -- a periodic step in the wavetable, squarely in the
# audio band and audible as a buzz. Driving every update puts the repetition at
# 750 Hz and makes the excitation continuous rather than impulsive; the mallet
# already does the spatial smoothing that this was standing in for.
DRIVE_EVERY = 1

# How much of the scanned value fills the 8-bit display trace.
WAVE_SHIFT = 6

# A mallet covers ~29 cells, so the same per-cell amplitude injects far more
# energy than a single-cell strike. Scaled down to leave headroom.
PLUCK_AMP = 0.15

# How much of the 16-bit snapshot reaches the output.
OUT_SHIFT = 0

# Drive amplitude, as a shift on the 12-bit level from in0. Random signs make
# the membrane a random walk, so the equilibrium amplitude is roughly the
# injection times 2**((LOSS_SHIFT-1)/2) -- about 23x here. Driving every update
# rather than every eighth is sqrt(8) louder at equilibrium, so this comes down
# by two shifts to match.
DRIVE_SHIFT = 1


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
                 'scan L', 'scan R', '', ''],
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
        self.radius_dbg = Signal(6)
        self.radius2_dbg = Signal(6)
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
            mallet=MALLET_MAX)
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
        # in2 sweeps the scan circle between the inner and outer edges of the
        # membrane as it currently is, rather than over absolute cell radii.
        # Taken absolutely, most of the range fell off the drum -- on the wide
        # ring only 6 of the 16 steps landed on it at all, and the rest were
        # silence inside the hole or past the rim. Same fix as LACUNA's strike
        # position: scale by the span, not add to the edge.
        radius_cv = Signal(unsigned(8))
        radius = Signal(unsigned(5 + RAD_FRAC))     # cells, Q4
        radius_r = Signal(unsigned(5 + RAD_FRAC))
        rad_span = Signal(unsigned(6))
        rad_r_raw = Signal(unsigned(5 + RAD_FRAC))
        rad_max = Signal(unsigned(5 + RAD_FRAC))
        m.d.sync += [
            # Cells between the two edges the scan may sit on.
            rad_span.eq(mesh.geo_outer - mesh.geo_inner - 2),
            # (inner+1) .. (outer-1) as radius_cv runs 0..255, in Q4. The +1 on
            # radius_cv makes the top of the CV land exactly on the outer edge
            # rather than one step short.
            radius.eq(((mesh.geo_inner + 1) << RAD_FRAC)
                      + (((radius_cv + 1) * rad_span) >> (8 - RAD_FRAC))),
            # The right channel scans a circle a quarter of the annulus further
            # out, clamped at the rim. Two circles at different radii are two
            # genuinely different wavetables; two points on the *same* circle
            # would only be a phase offset, which gives width but combs in mono.
            # The fixed offset means there is always spread -- a scheme that
            # crossed the two would have a mono null in the middle of in2.
            rad_r_raw.eq(radius + (rad_span << 2)),
            rad_max.eq((mesh.geo_outer - 1) << RAD_FRAC),
            radius_r.eq(Mux(rad_r_raw > rad_max, rad_max, rad_r_raw)),
        ]
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
        # The circle is sampled *between* cells, not snapped to them. Nearest
        # cell measured 0.14 roughness on a perfectly smooth field against 0.039
        # for bilinear -- audible as roughness on the tone, and it also pinned
        # the radius to whole cells. Both go away by carrying the position in Q4
        # and blending the four cells around it.
        phase = Signal(PHASE_BITS)
        k_idx = Signal(N_POINTS)
        frac = Signal(10)
        m.d.comb += [
            k_idx.eq(phase[PHASE_BITS - N_POINTS:]),
            frac.eq(phase[PHASE_BITS - N_POINTS - 10:PHASE_BITS - N_POINTS]),
        ]

        # Angle, interpolated between adjacent ROM entries so the scan position
        # is continuous rather than stepping 64 times a cycle.
        c0 = Signal(signed(8)); s0 = Signal(signed(8))
        c1 = Signal(signed(8)); s1 = Signal(signed(8))
        ci = Signal(signed(9)); si = Signal(signed(9))

        # Position in Q4 cells. cos is Q6 and radius Q4, so their product is Q10.
        fx = Signal(unsigned(10)); fy = Signal(unsigned(10))
        base = Signal(range(n * n))
        tx = Signal(unsigned(RAD_FRAC)); ty = Signal(unsigned(RAD_FRAC))

        v00 = Signal(signed(16)); v10 = Signal(signed(16))
        v01 = Signal(signed(16)); v11 = Signal(signed(16))
        va = Signal(signed(17)); vb = Signal(signed(17))

        # Stereo without a second datapath. The FSM walks the position-and-blend
        # sequence twice, once per channel, and `ch` muxes which radius goes
        # into the shared multipliers -- so two channels cost states, of which
        # there are 1250 per sample and we use about thirty, rather than
        # multipliers, of which nine remain. Duplicating the datapath instead
        # would have needed seven more and almost certainly not placed.
        #
        # The angle is computed once and reused: only the radius differs between
        # the channels, so P0..P3 run once and P4 onward twice.
        ch = Signal()
        rad_sel = Signal(unsigned(5 + RAD_FRAC))
        m.d.comb += rad_sel.eq(Mux(ch, radius_r, radius))
        mix = Signal(signed(17))
        m.d.comb += mix.eq(va + (((vb - va) * ty) >> RAD_FRAC))

        # Held, not pulsed: the mesh does not reach the strike node until
        # hundreds of cycles into its scan, so an amplitude driven only on the
        # cycle that pulses `step` has long since gone back to zero by the time
        # it is sampled. LACUNA gets away with a combinational constant.
        strike_amp = Signal(signed(WIDTH + 4))
        m.d.comb += mesh.strike_amp.eq(strike_amp)

        # The overlay draws whole cells, so hand it the rounded radii.
        m.d.comb += [self.radius_dbg.eq(radius >> RAD_FRAC),
                     self.radius2_dbg.eq(radius_r >> RAD_FRAC)]

        scanned = Signal(signed(16))
        scanned_r = Signal(signed(16))

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
        # One per channel: they are independent signals with independent
        # offsets, and sharing a blocker between them would put each channel's
        # DC into the other.
        def blocked(src, name):
            x1 = Signal(signed(16), name=f"dc_x1_{name}")
            y1 = Signal(signed(24), name=f"dc_y1_{name}")
            y  = Signal(signed(24), name=f"dc_y_{name}")
            sc = Signal(signed(24), name=f"dc_s_{name}")
            out = Signal(signed(16), name=f"out_{name}")
            m.d.comb += [y.eq(src - x1 + y1 - (y1 >> 10)),
                         sc.eq(y >> OUT_SHIFT)]
            with m.If(sc > 32767):
                m.d.comb += out.eq(32767)
            with m.Elif(sc < -32768):
                m.d.comb += out.eq(-32768)
            with m.Else():
                m.d.comb += out.eq(sc)
            return out, x1, y1, y

        out_payload, dc_x1, dc_y1, dc_y = blocked(scanned, "l")
        out_payload2, dc_x1r, dc_y1r, dc_yr = blocked(scanned_r, "r")

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
                    # Hit it harder and it gets brighter: drive level shortens
                    # the mallet, so in0 is velocity as well as amplitude. A
                    # 1 V gate is a soft mallet, 6 V a hard stick.
                    hard = Signal(range(MALLET_MAX + 2))
                    m.d.comb += hard.eq(Mux(gate < 0, 0,
                                        Mux(gate[13:16] > MALLET_MAX,
                                            MALLET_MAX, gate[13:16])))
                    m.d.sync += mesh.mallet_r.eq(MALLET_MAX - hard)
                    # Same clamps as LACUNA's position CV: a bare bit-slice of a
                    # signed value reads full scale for an idle jack one count
                    # below zero, and folds back around above 4 V.
                    m.d.sync += [
                        # 8 bits, not 4: with the scan interpolating between
                        # cells the radius no longer has to land on one, so the
                        # CV gets 256 steps across the membrane instead of 16.
                        radius_cv.eq(Mux(pos < 0, 0,
                                      Mux(pos > 16383, 255, pos[6:14]))),
                        mesh.fm.eq(_raw(self.i.payload[3])),
                    ]
                    m.d.sync += pitch_q.eq(pitch)
                    m.next = "IDX"

            with m.State("IDX"):
                # Its own state for the same reason LACUNA needs one: pitch
                # arrives from the CODEC calibrator, and calibrator -> multiply
                # -> clamp -> cv_index in a single cycle misses 60 MHz.
                idx = Signal(signed(20))
                m.d.comb += idx.eq(((pitch_q * VOCT_Q16 + (1 << 15)) >> 16)
                                   + PITCH_OFFSET)
                with m.If(idx < 0):
                    m.d.sync += cv_index.eq(0)
                with m.Elif(idx > (1 << CV_BITS) - 1):
                    m.d.sync += cv_index.eq((1 << CV_BITS) - 1)
                with m.Else():
                    m.d.sync += cv_index.eq(idx)
                m.next = "P0"

            with m.State("P0"):
                m.d.comb += circ_rd.addr.eq(k_idx)
                m.next = "P1"

            with m.State("P1"):
                m.d.sync += [c0.eq(circ_rd.data[:8].as_signed()),
                             s0.eq(circ_rd.data[8:].as_signed())]
                m.d.comb += circ_rd.addr.eq(k_idx + 1)
                m.next = "P2"

            with m.State("P2"):
                m.d.sync += [c1.eq(circ_rd.data[:8].as_signed()),
                             s1.eq(circ_rd.data[8:].as_signed())]
                m.next = "P3"

            with m.State("P3"):
                # Continuous angle between the two ROM entries.
                m.d.sync += [ci.eq(c0 + (((c1 - c0) * frac) >> 10)),
                             si.eq(s0 + (((s1 - s0) * frac) >> 10))]
                m.next = "P4"

            with m.State("P4"):
                # Position in Q4 cells. cos is Q6 and radius Q4, so the product
                # is Q10 and needs six shifts to land back in Q4.
                m.d.sync += [
                    fx.eq((cx << RAD_FRAC) + ((ci * rad_sel) >> CIRC_SCALE)),
                    fy.eq((cy << RAD_FRAC) + ((si * rad_sel) >> CIRC_SCALE)),
                ]
                m.next = "P5"

            with m.State("P5"):
                # Split into the cell to sample from and the blend weights.
                m.d.sync += [
                    base.eq(((fy >> RAD_FRAC) << (n - 1).bit_length())
                            + (fx >> RAD_FRAC)),
                    tx.eq(fx[:RAD_FRAC]),
                    ty.eq(fy[:RAD_FRAC]),
                ]
                m.next = "A0"

            # Four reads: the cell the position falls in and its right, lower
            # and lower-right neighbours. The scan never gets within a cell of
            # the array edge -- radius is capped at outer-1 and every preset
            # keeps two cells clear -- so base+n+1 is always in range.
            with m.State("A0"):
                m.d.comb += mesh.snap_addr.eq(base)
                m.next = "A1"

            with m.State("A1"):
                m.d.sync += v00.eq(mesh.snap_data)
                m.d.comb += mesh.snap_addr.eq(base + 1)
                m.next = "A2"

            with m.State("A2"):
                m.d.sync += v10.eq(mesh.snap_data)
                m.d.comb += mesh.snap_addr.eq(base + n)
                m.next = "A3"

            with m.State("A3"):
                m.d.sync += v01.eq(mesh.snap_data)
                m.d.comb += mesh.snap_addr.eq(base + n + 1)
                m.next = "A4"

            with m.State("A4"):
                m.d.sync += v11.eq(mesh.snap_data)
                m.next = "B0"

            with m.State("B0"):
                # Blend along x on both rows.
                m.d.sync += [
                    va.eq(v00 + (((v10 - v00) * tx) >> RAD_FRAC)),
                    vb.eq(v01 + (((v11 - v01) * tx) >> RAD_FRAC)),
                ]
                m.next = "B1"

            with m.State("B1"):
                # ...then between the rows. `mix` is a single signal rather than
                # the expression written twice, so the blend is one multiplier
                # shared by both channels rather than two.
                with m.If(ch == 0):
                    m.d.sync += [scanned.eq(mix), ch.eq(1)]
                    m.next = "P4"          # same angle, the other radius
                with m.Else():
                    m.d.sync += [scanned_r.eq(mix), ch.eq(0)]
                    m.next = "EMIT"

            with m.State("EMIT"):
                m.d.comb += self.o.valid.eq(1)
                for k in range(4):
                    m.d.comb += _raw(self.o.payload[k]).eq(
                        {0: out_payload, 1: out_payload2}.get(k, 0))
                with m.If(self.o.ready):
                    m.d.comb += wave_wr.en.eq(1)
                    m.d.sync += [
                        phase.eq(phase + nco_rd.data),
                        dc_x1.eq(scanned), dc_y1.eq(dc_y),
                        dc_x1r.eq(scanned_r), dc_y1r.eq(dc_yr),
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
