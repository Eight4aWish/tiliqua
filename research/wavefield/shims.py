# Copyright (c) 2026 D. Baghurst
#
# SPDX-License-Identifier: CERN-OHL-S-2.0
#
# Stand-in for `tiliqua.build.types.BitstreamHelp`, so `wavefield.py` can be
# simulated by `preview.py` without the apfaudio/tiliqua tree present.
# In-tree the real dataclass is imported instead.


class BitstreamHelp:
    def __init__(self, brief="<none>", io_left=None, io_right=None):
        self.brief = brief
        self.io_left = io_left or [''] * 8
        self.io_right = io_right or [''] * 6
