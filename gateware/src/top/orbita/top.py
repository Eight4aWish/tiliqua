# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""
ORBITA: the membrane scanned as a wavetable.

Sibling to LACUNA and sharing its membrane. LACUNA listens to the mesh through
a pickup; ORBITA evolves it slowly and reads a circular path through it at
audio rate, so the scan rate is the pitch and the membrane's shape is the
timbre. See research/scan/DESIGN.md.

.. code-block:: bash

   # from the `gateware` directory
   pdm orbita build --modeline 1280x720p60
   pdm flash archive build/orbita-r5/orbita-<tag>-r5.tar.gz --slot <n>

The display draws the membrane exactly as LACUNA does, and overlays the scan
circle on it, so you can see the path the waveform is being read from and where
it sits relative to the hole.
"""

import os
import sys

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.cdc import FFSynchronizer

from tiliqua.build import sim
from tiliqua.build.cli import top_level_cli
from tiliqua.periph import eurorack_pmod
from tiliqua.platform import RebootProvider
from tiliqua.video import dvi

# ORBITA and LACUNA share mesh.py, and each top level is run as a script with
# only its own directory on the path. Rather than duplicate the membrane or
# turn these into a package -- which would break the standalone tests that
# import them directly -- reach across to the sibling directory.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "lacuna"))

from orbita import Orbita          # noqa: E402


# The mesh is 32x32 and each cell is drawn as a CELL x CELL block, which is a
# shift rather than a divide. 32*16 is 512, centred in whatever the modeline
# gives us.
CELL_SHIFT = 4


class OrbitaTop(Elaboratable):

    def __init__(self, clock_settings):
        assert clock_settings.modeline is not None, (
            "orbita draws the mesh and races the beam to do it, so it needs a "
            "static modeline: pass e.g. --modeline 1280x720p60")
        self.core = Orbita(video=True)
        self.core.audio_clock = clock_settings.audio_clock
        self.clock_settings = clock_settings
        self.pmod0 = eurorack_pmod.EurorackPmod(clock_settings.audio_clock)
        self.dvi_tgen = dvi.DVITimingGen()
        self.bitstream_help = self.core.bitstream_help
        super().__init__()

    def elaborate(self, platform):
        m = Module()
        m.submodules.pmod0 = pmod0 = self.pmod0
        m.submodules.core = core = self.core

        assert sim.is_hw(platform), "orbita's video path has no sim harness"

        m.submodules.car = platform.clock_domain_generator(self.clock_settings)
        m.submodules.provider = provider = eurorack_pmod.FFCProvider()
        wiring.connect(m, pmod0.pins, provider.pins)
        m.submodules.reboot = reboot = RebootProvider(
                self.clock_settings.frequencies.sync)
        m.submodules.btn = FFSynchronizer(
                platform.request("encoder").s.i, reboot.button)
        m.d.comb += pmod0.codec_mute.eq(reboot.mute)
        m.d.comb += core.button.eq(reboot.button)

        wiring.connect(m, pmod0.o_cal, core.i)
        wiring.connect(m, core.o, pmod0.i_cal)

        # --- video ------------------------------------------------------------
        m.submodules.dvi_tgen = dvi_tgen = self.dvi_tgen
        for member in dvi_tgen.timings.signature.members:
            m.d.comb += (getattr(dvi_tgen.timings, member)
                         .eq(getattr(self.clock_settings.modeline, member)))

        n = self.core.n
        side = n << CELL_SHIFT
        x0 = (self.clock_settings.modeline.h_active - side) // 2
        y0 = (self.clock_settings.modeline.v_active - side) // 2
        assert x0 >= 0 and y0 >= 0, (
            f"a {n}x{n} mesh at {1 << CELL_SHIFT}x does not fit this modeline")

        x, y = dvi_tgen.x, dvi_tgen.y

        # Two pipeline stages, so look two pixels ahead: the cell address is
        # registered (x -> subtract -> shift -> BRAM address in one cycle only
        # reached 68 MHz against 74.25) and the snapshot read is synchronous on
        # top of that. Everything else is delayed to match, so the picture is
        # not shifted.
        xn = Signal(signed(12))
        m.d.comb += xn.eq(x + 2)
        cxc = Signal(range(n))
        cyc = Signal(range(n))
        # Fold the look-ahead into the offset: x + 2 then - x0 is two carry
        # chains in series on the pixel path, and one constant does both.
        m.d.comb += [
            cxc.eq((x + (2 - x0)) >> CELL_SHIFT),
            cyc.eq((y - y0) >> CELL_SHIFT),
        ]
        m.d.dvi += core.disp_addr.eq(Cat(cxc, cyc))

        on_mesh = Signal()
        on_mesh_1 = Signal()
        on_mesh_q = Signal()
        m.d.comb += on_mesh.eq((xn >= x0) & (xn < x0 + side) &
                               (y >= y0) & (y < y0 + side))
        m.d.dvi += [on_mesh_1.eq(on_mesh), on_mesh_q.eq(on_mesh_1)]

        # --- scan circle overlay ----------------------------------------------
        # The path the waveform is read from, drawn over the membrane. radius
        # crosses from the audio domain; it only changes at sample rate and a
        # torn value would show as one frame of a slightly wrong circle, so a
        # plain synchroniser is enough.
        radius_dvi = Signal(4)
        m.submodules.rad_cdc = FFSynchronizer(
                core.radius_dbg, radius_dvi, o_domain="dvi")
        ddx = Signal(signed(8))
        ddy = Signal(signed(8))
        dd2 = Signal(signed(16))
        rr2 = Signal(signed(16))
        on_circle = Signal()
        on_circle_q = Signal()
        # dd2 and rr2 are registered: the squares sat on the pixel path and
        # took the dvi domain to 51 MHz against 74.25. Registering them lands
        # the compare one cycle later, which is exactly when disp_data arrives,
        # so the overlay stays aligned with the mesh underneath it.
        # Squares of numbers this small are lookups, not multipliers: three
        # DSP blocks in the pixel path made placement tight enough that the
        # 371 MHz serialiser stopped closing, and it contains none of our logic.
        SQ = Array([C(v * v, unsigned(9)) for v in range(n // 2 + 1)])
        adx = Signal(range(n // 2 + 1))
        ady = Signal(range(n // 2 + 1))
        rad_q = Signal(4)
        m.d.comb += [
            ddx.eq(cxc - (n // 2)),
            ddy.eq(cyc - (n // 2)),
            adx.eq(Mux(ddx < 0, -ddx, ddx)),
            ady.eq(Mux(ddy < 0, -ddy, ddy)),
        ]
        m.d.dvi += [
            dd2.eq(SQ[adx] + SQ[ady]),
            rr2.eq(SQ[radius_dvi]),
            rad_q.eq(radius_dvi),
        ]
        # |d2 - r2| < r is about a one-cell-wide ring at any radius.
        m.d.comb += on_circle.eq((rad_q != 0)
                                 & (dd2 - rr2 < rad_q.as_signed())
                                 & (rr2 - dd2 < rad_q.as_signed()))
        m.d.dvi += on_circle_q.eq(on_circle)

        # Diverging palette, as LACUNA: 0 is outside the membrane, 128 a node at
        # rest, and the two signs of displacement go to blue and red. The scan
        # circle is added as a green wash so it reads over either sign without
        # hiding the mesh underneath it.
        CIRCLE_G = 90
        v = core.disp_data
        mag = Signal(unsigned(8))
        pos = Signal()
        r = Signal(unsigned(8))
        g = Signal(unsigned(8))
        b = Signal(unsigned(8))
        gm = Signal(unsigned(9))
        m.d.comb += [
            pos.eq(v >= 128),
            mag.eq(Mux(pos, (v - 128) << 1, (128 - v) << 1)),
            gm.eq((mag >> 2) + Mux(on_circle_q & on_mesh_q, CIRCLE_G, 0)),
        ]
        with m.If(~on_mesh_q):
            m.d.comb += [r.eq(0), g.eq(0), b.eq(0)]
        with m.Else():
            m.d.comb += [
                r.eq(Mux(pos, mag, 0)),
                b.eq(Mux(pos, 0, mag)),
                g.eq(Mux(gm > 255, 255, gm)),
            ]

        m.submodules.dvi_gen = dvi_gen = dvi.DVIPHY()
        m.d.dvi += [
            dvi_gen.i.de.eq(dvi_tgen.ctrl_phy.de),
            dvi_gen.i.r.eq(r),
            dvi_gen.i.g.eq(g),
            dvi_gen.i.b.eq(b),
            dvi_gen.i.hsync.eq(dvi_tgen.ctrl_phy.hsync),
            dvi_gen.i.vsync.eq(dvi_tgen.ctrl_phy.vsync),
        ]

        return m


if __name__ == "__main__":
    top_level_cli(OrbitaTop)
