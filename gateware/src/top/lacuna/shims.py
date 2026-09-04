# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
"""Stand-ins so mesh.py can be simulated without the Tiliqua tree."""

from amaranth.hdl import signed

ASQ = signed(16)


class BitstreamHelp:
    def __init__(self, brief="<none>", io_left=None, io_right=None):
        self.brief = brief
        self.io_left = io_left or [''] * 8
        self.io_right = io_right or [''] * 6
