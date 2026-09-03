# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""
Lacuna: a 2D membrane mesh whose hole is the instrument.

See LACUNA.md for the design, and lacuna.py for the mesh itself. Build and
flash it as follows:

.. code-block:: bash

   # from the `gateware` directory
   pdm lacuna build --modeline 720x720p60r2
   pdm flash archive build/lacuna-r5/lacuna-<tag>-r5.tar.gz --slot <n>

The display shows the membrane itself rather than its output waveform: a
32x32 grid of node values, upscaled and drawn straight from a snapshot the
audio scan keeps up to date. Blue and red are the two signs of displacement,
so what you see is the mode pattern -- and mode beating, which is the mesh's
most characteristic behaviour, reads as that pattern precessing rather than
as a change in brightness.

There is no framebuffer and no PSRAM: every pixel is coloured from the mesh a
few microseconds before it goes down the cable, the way the `beamrace`
top-level does it. A static modeline is therefore required.
"""

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.cdc import FFSynchronizer

from tiliqua.build import sim
from tiliqua.build.cli import top_level_cli
from tiliqua.periph import eurorack_pmod
from tiliqua.platform import RebootProvider
from tiliqua.video import dvi

from lacuna import Lacuna


# The mesh is 32x32 and each cell is drawn as a CELL x CELL block, which is a
# shift rather than a divide and keeps the multipliers for the mesh. 32*16 is
# 512, centred inside whatever the modeline gives us.
CELL_SHIFT = 4


class LacunaTop(Elaboratable):

    def __init__(self, clock_settings):
        assert clock_settings.modeline is not None, (
            "lacuna draws the mesh and races the beam to do it, so it needs a "
            "static modeline: pass e.g. --modeline 720x720p60r2")
        self.core = Lacuna(video=True)
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

        assert sim.is_hw(platform), "lacuna's video path has no simulation harness"

        m.submodules.car = platform.clock_domain_generator(self.clock_settings)
        m.submodules.provider = provider = eurorack_pmod.FFCProvider()
        wiring.connect(m, pmod0.pins, provider.pins)
        m.submodules.reboot = reboot = RebootProvider(
                self.clock_settings.frequencies.sync)
        m.submodules.btn = FFSynchronizer(
                platform.request("encoder").s.i, reboot.button)
        m.d.comb += pmod0.codec_mute.eq(reboot.mute)
        # The core watches the same button. RebootProvider only acts on a
        # 3 second hold, so short presses are free for preset switching.
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

        # The snapshot read is synchronous, so address it with the *next* pixel
        # and its data arrives in time to colour this one.
        xn = Signal(signed(12))
        m.d.comb += xn.eq(x + 1)
        cx = Signal(range(n))
        cy = Signal(range(n))
        m.d.comb += [
            cx.eq((xn - x0) >> CELL_SHIFT),
            cy.eq((y - y0) >> CELL_SHIFT),
            core.disp_addr.eq(Cat(cx, cy)),
        ]

        # Whether the pixel being coloured now -- one behind the address -- is
        # inside the mesh square.
        on_mesh = Signal()
        on_mesh_q = Signal()
        m.d.comb += on_mesh.eq((xn >= x0) & (xn < x0 + side) &
                               (y >= y0) & (y < y0 + side))
        m.d.dvi += on_mesh_q.eq(on_mesh)

        # Diverging palette. 0 is a cell outside the membrane and is drawn as a
        # dim grey field, so the shape of the current preset -- disc, ring,
        # square hole, slit -- is on screen whether or not anything is ringing.
        # Inside, 128 is a node at rest and the two signs of displacement go to
        # blue and red, so what you see is the mode pattern rather than just a
        # brightness envelope. A little green either side lifts the peaks
        # towards white so a loud strike does not clip to a flat primary.
        OUTSIDE = 16
        v = core.disp_data
        mag = Signal(unsigned(8))
        pos = Signal()
        outside = Signal()
        r = Signal(unsigned(8))
        g = Signal(unsigned(8))
        b = Signal(unsigned(8))
        m.d.comb += [
            outside.eq(v == 0),
            pos.eq(v >= 128),
            mag.eq(Mux(pos, (v - 128) << 1, (128 - v) << 1)),
        ]
        with m.If(~on_mesh_q):
            m.d.comb += [r.eq(0), g.eq(0), b.eq(0)]
        with m.Elif(outside):
            m.d.comb += [r.eq(OUTSIDE), g.eq(OUTSIDE), b.eq(OUTSIDE)]
        with m.Else():
            m.d.comb += [
                r.eq(Mux(pos, mag, 0)),
                b.eq(Mux(pos, 0, mag)),
                g.eq(mag >> 2),
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
    top_level_cli(LacunaTop)
