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
   pdm lacuna build
   pdm flash archive build/lacuna-r5/lacuna-<tag>-r5.tar.gz

The mesh is a plain audio-domain core with no SoC, no video and no PSRAM, so
the top level here is only the CODEC, the clocks and the encoder.
"""

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.cdc import FFSynchronizer

from tiliqua.build import sim
from tiliqua.build.cli import top_level_cli
from tiliqua.periph import eurorack_pmod
from tiliqua.platform import RebootProvider

from lacuna import Lacuna


class LacunaTop(Elaboratable):

    def __init__(self, clock_settings):
        self.core = Lacuna()
        self.core.audio_clock = clock_settings.audio_clock
        self.clock_settings = clock_settings
        self.pmod0 = eurorack_pmod.EurorackPmod(clock_settings.audio_clock)
        self.bitstream_help = self.core.bitstream_help
        super().__init__()

    def elaborate(self, platform):
        m = Module()
        m.submodules.pmod0 = pmod0 = self.pmod0

        if sim.is_hw(platform):
            m.submodules.car = platform.clock_domain_generator(
                    self.clock_settings)
            m.submodules.provider = provider = eurorack_pmod.FFCProvider()
            wiring.connect(m, pmod0.pins, provider.pins)
            m.submodules.reboot = reboot = RebootProvider(
                    self.clock_settings.frequencies.sync)
            m.submodules.btn = FFSynchronizer(
                    platform.request("encoder").s.i, reboot.button)
            m.d.comb += pmod0.codec_mute.eq(reboot.mute)
            # The core watches the same button. RebootProvider only acts on a
            # 3 second hold, so short presses are free for preset switching.
            m.d.comb += self.core.button.eq(reboot.button)
        else:
            m.submodules.car = sim.FakeTiliquaDomainGenerator()

        m.submodules.core = self.core
        wiring.connect(m, pmod0.o_cal, self.core.i)
        wiring.connect(m, self.core.o, pmod0.i_cal)

        return m


def simulation_ports(fragment):
    return {
        "clk_audio":  (ClockSignal("audio"),              None),
        "rst_audio":  (ResetSignal("audio"),              None),
        "clk_sync":   (ClockSignal("sync"),               None),
        "rst_sync":   (ResetSignal("sync"),               None),
        "clk_fast":   (ClockSignal("fast"),               None),
        "rst_fast":   (ResetSignal("fast"),               None),
        "i2s_sdin1":  (fragment.pmod0.pins.i2s.sdin1,     None),
        "i2s_sdout1": (fragment.pmod0.pins.i2s.sdout1,    None),
        "i2s_lrck":   (fragment.pmod0.pins.i2s.lrck,      None),
        "i2s_bick":   (fragment.pmod0.pins.i2s.bick,      None),
    }


if __name__ == "__main__":
    top_level_cli(
        LacunaTop,
        video_core=False,
        sim_ports=simulation_ports,
        # Generic harness for a self-contained audio core; shared with `dsp`.
        sim_harness="../../src/top/dsp/sim_dsp_core.cpp",
    )
