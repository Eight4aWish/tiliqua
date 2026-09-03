# Beamracing 'Wavefield' core for apf.audio Tiliqua.
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# Drop-in pattern core for `gateware/src/top/beamrace/top.py` in the
# apfaudio/tiliqua tree. See README.md in this directory for how to install
# and build it.

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

try:
    from tiliqua.build.types import BitstreamHelp
except ImportError:
    # Standalone: `preview.py` renders frames without the Tiliqua tree present.
    from shims import BitstreamHelp


# These mirror the signatures in `top/beamrace/top.py`. They are defined here
# rather than imported from it because top.py imports this module: importing
# back would be circular. BeamRaceTop drives these members directly rather than
# through wiring.connect, so a structurally identical pair is all it needs.
# If upstream changes the beamrace signatures, update these to match.

class BeamRaceInputs(wiring.Signature):
    def __init__(self):
        super().__init__({
            "hsync": Out(1),
            "vsync": Out(1),
            "de": Out(1),
            "x": Out(signed(12)),
            "y": Out(signed(12)),
            "audio_in0": Out(signed(16)),
            "audio_in1": Out(signed(16)),
            "audio_in2": Out(signed(16)),
            "audio_in3": Out(signed(16)),
        })


class BeamRaceOutputs(wiring.Signature):
    def __init__(self):
        super().__init__({
            "r": Out(8),
            "g": Out(8),
            "b": Out(8),
        })


class Wavefield(wiring.Component):

    """
    Beamracing waveform raster. No framebuffer, no PSRAM, no CPU: the colour of
    every pixel is decided from the live audio inputs microseconds before that
    pixel is transmitted.

    At 720p60 the line rate is 45 kHz and the codec runs at 48 kHz, so one
    scanline carries very nearly one audio sample. Each line samples-and-holds
    the inputs at hsync, and the trace position for the whole line comes from
    that one sample. Time therefore runs *down* the screen at 45 kHz: a full
    frame is 720 consecutive samples, i.e. a 16 ms window of audio.

    The consequence worth patching for: pitch becomes visible geometry. An input
    at an exact multiple of the 60 Hz frame rate stands perfectly still; detune
    it slightly and the whole field creeps, exactly like an analogue scope with a
    free-running timebase. Sweep it and the pattern shears.

    in0: trace A (audio)
    in1: trace B (audio)
    in2: zoom CV, ~1 V per step over 0-7 V
    in3: palette CV, ~2 V per palette
    """

    i: In(BeamRaceInputs())
    o: Out(BeamRaceOutputs())

    bitstream_help = BitstreamHelp(
        brief="Beamracing waveform raster (no framebuffer)",
        io_left=['trace A', 'trace B', 'zoom', 'palette',
                 'in0 (copy)', 'in1 (copy)', 'in2 (copy)', 'in3 (copy)'],
        io_right=['', '', 'video (fixed)', '', '', '']
    )

    def __init__(self, center_x=640, falloff=5):
        # `center_x` is half the active width; `falloff` sets trace thickness
        # (brightness lost per pixel is 2**falloff, so the glow is 256>>falloff
        # pixels wide either side: falloff=5 gives an 8px trace).
        self.center_x = center_x
        self.falloff = falloff
        super().__init__()

    def elaborate(self, platform):

        m = Module()

        def add_sat(name, a, b):
            # 8-bit additive blend with saturation, as a scope trace would.
            wide = Signal(9, name=f"{name}_wide")
            out = Signal(8, name=name)
            m.d.comb += [
                wide.eq(a + b),
                out.eq(Mux(wide[8], 0xFF, wide[:8])),
            ]
            return out

        # --- one scanline, one sample ---------------------------------------

        l_hsync = Signal()
        m.d.sync += l_hsync.eq(self.i.hsync)

        s_a = Signal(signed(16))
        s_b = Signal(signed(16))
        zoom = Signal(3)
        palette = Signal(2)

        with m.If(self.i.hsync & ~l_hsync):
            # Full scale is +/-8.192 V, so ~4000 counts per volt. Bits 12:15 of
            # the zoom CV give one step per ~1.02 V; negative CV wraps.
            # Shift 7 puts a full-scale input at +/-256 px; each volt on in2
            # halves the deflection from there.
            m.d.sync += [
                s_a.eq(self.i.audio_in0),
                s_b.eq(self.i.audio_in1),
                zoom.eq(7 + self.i.audio_in2[12:15]),
                palette.eq(self.i.audio_in3[13:15]),
            ]

        # Horizontal position of each trace on this line. Settles during
        # blanking, long before the beam reaches active video.
        x_a = Signal(signed(13))
        x_b = Signal(signed(13))
        m.d.sync += [
            x_a.eq(self.center_x + (s_a >> zoom)),
            x_b.eq(self.center_x + (s_b >> zoom)),
        ]

        # --- stage 1: distance from this pixel to each trace -----------------

        d_a = Signal(13)
        d_b = Signal(13)
        on_center = Signal()
        de_1 = Signal()
        m.d.sync += [
            d_a.eq(abs(self.i.x - x_a)),
            d_b.eq(abs(self.i.x - x_b)),
            on_center.eq(self.i.x == self.center_x),
            de_1.eq(self.i.de),
        ]

        # --- stage 2: distance to intensity ----------------------------------

        span = 256 >> self.falloff

        i_a = Signal(8)
        i_b = Signal(8)
        graticule = Signal(8)
        de_2 = Signal()
        m.d.sync += [
            i_a.eq(Mux(d_a < span, 255 - (d_a << self.falloff), 0)),
            i_b.eq(Mux(d_b < span, 255 - (d_b << self.falloff), 0)),
            graticule.eq(Mux(on_center, 0x18, 0x00)),
            de_2.eq(de_1),
        ]

        # --- colour ----------------------------------------------------------

        # Trace A and B always take contrasting hues so they stay readable when
        # they cross. Shifts only, no multipliers.
        r_a = Signal(8); g_a = Signal(8); b_a = Signal(8)
        r_b = Signal(8); g_b = Signal(8); b_b = Signal(8)

        with m.Switch(palette):
            with m.Case(0):  # amber / cyan
                m.d.comb += [
                    r_a.eq(i_a), g_a.eq(i_a - (i_a >> 2)), b_a.eq(i_a >> 3),
                    r_b.eq(i_b >> 3), g_b.eq(i_b), b_b.eq(i_b),
                ]
            with m.Case(1):  # green / magenta
                m.d.comb += [
                    r_a.eq(i_a >> 3), g_a.eq(i_a), b_a.eq(i_a >> 2),
                    r_b.eq(i_b), g_b.eq(i_b >> 3), b_b.eq(i_b),
                ]
            with m.Case(2):  # ice / orange
                m.d.comb += [
                    r_a.eq(i_a >> 1), g_a.eq(i_a - (i_a >> 3)), b_a.eq(i_a),
                    r_b.eq(i_b), g_b.eq(i_b >> 1), b_b.eq(i_b >> 3),
                ]
            with m.Case(3):  # white / red
                m.d.comb += [
                    r_a.eq(i_a), g_a.eq(i_a), b_a.eq(i_a),
                    r_b.eq(i_b), g_b.eq(i_b >> 3), b_b.eq(i_b >> 3),
                ]

        red = add_sat("red", add_sat("red_ab", r_a, r_b), graticule)
        green = add_sat("green", add_sat("green_ab", g_a, g_b), graticule)
        blue = add_sat("blue", add_sat("blue_ab", b_a, b_b), graticule)

        # Two pipeline stages means the picture lands one pixel later than the
        # PHY's own sync delay. That one-pixel offset is not worth a stage to fix.
        with m.If(de_2):
            m.d.comb += [
                self.o.r.eq(red),
                self.o.g.eq(green),
                self.o.b.eq(blue),
            ]

        return m
