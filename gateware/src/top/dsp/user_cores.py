# Copyright (c) 2026 David Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""
Out-of-the-way module for locally-authored DSP cores.

Keeping cores here rather than in ``top.py`` keeps the diff against upstream
down to two lines (an import and a ``CORES`` entry), so rebasing onto new
upstream revisions stays trivial.

Build with::

    pdm dsp build --dsp-core template
"""

from amaranth import *
from amaranth.lib import data, stream, wiring
from amaranth.lib.wiring import In, Out

from tiliqua.dsp import ASQ
from tiliqua.build.types import BitstreamHelp


class Template(wiring.Component):

    """
    Starting point for a locally-authored core.

    Four audio channels in, four out, with a trivial transformation so that
    the build is verifiably doing something. Channel 1 is inverted; the rest
    pass through untouched.

    in0-in3: audio in
    out0: in0
    out1: -in1
    out2: in2
    out3: in3
    """

    i: In(stream.Signature(data.ArrayLayout(ASQ, 4)))
    o: Out(stream.Signature(data.ArrayLayout(ASQ, 4)))

    bitstream_help = BitstreamHelp(
        brief="Local core template",
        io_left=['in0', 'in1', 'in2', 'in3',
                 'in0', '-in1', 'in2', 'in3'],
        io_right=['', '', '', '', '', '']
    )

    def elaborate(self, platform):
        m = Module()

        # Stream handshake: this stage is purely combinational, so valid
        # passes straight downstream and ready passes straight upstream.
        # A pipelined stage would register the payload and need its own
        # skid buffer here instead.
        m.d.comb += [
            self.o.valid.eq(self.i.valid),
            self.i.ready.eq(self.o.ready),
        ]

        # Payload transformation. Replace this with real processing.
        m.d.comb += [
            self.o.payload[0].eq(self.i.payload[0]),
            self.o.payload[1].eq(-self.i.payload[1]),
            self.o.payload[2].eq(self.i.payload[2]),
            self.o.payload[3].eq(self.i.payload[3]),
        ]

        return m
