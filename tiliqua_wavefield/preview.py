"""
Render frames of the `Wavefield` beamracing core to PNG, using the Amaranth
simulator only -- no Tiliqua tree, no yosys, no nextpnr, no hardware.

The point is the iteration loop: this takes seconds, a bitstream takes minutes.
Get the picture right here, then build once.

    python preview.py --freq-a 220 --freq-b 331 --out frame.png

Timing note: the beam is simulated at half of 720p (640x360 active) to keep the
run quick, but the sample-and-hold still advances once per scanline, which is
the relationship the effect depends on. Test frequencies are scaled by 2 so the
preview shows the same number of waveform cycles the real 720p screen would.
"""

import argparse
import math

from amaranth.sim import Simulator
from wavefield import Wavefield

H_ACTIVE, H_TOTAL = 640, 660
V_ACTIVE, V_TOTAL = 360, 370
H_SYNC_START, H_SYNC_END = 648, 656

FULL_SCALE = 32767
LINE_RATE_HZ = 45000.0  # 720p60 line rate; what one sample-per-line implies
PREVIEW_SCALE = 720 // V_ACTIVE


def volts(v):
    return int((v / 8.192) * FULL_SCALE)


def render(freq_a, freq_b, amp_a, amp_b, zoom_v, palette_v, out_path):
    dut = Wavefield(center_x=H_ACTIVE // 2)
    sim = Simulator(dut)
    sim.add_clock(1e-6)

    pixels = [[(0, 0, 0)] * H_ACTIVE for _ in range(V_ACTIVE)]

    async def testbench(ctx):
        phase_a = 0.0
        phase_b = 0.0
        step_a = 2 * math.pi * (freq_a * PREVIEW_SCALE) / LINE_RATE_HZ
        step_b = 2 * math.pi * (freq_b * PREVIEW_SCALE) / LINE_RATE_HZ

        ctx.set(dut.i.audio_in2, volts(zoom_v))
        ctx.set(dut.i.audio_in3, volts(palette_v))

        for y in range(V_TOTAL):
            # One fresh audio sample per scanline, as on hardware.
            sample_a = int(amp_a * FULL_SCALE * math.sin(phase_a))
            sample_b = int(amp_b * FULL_SCALE * math.sin(phase_b))
            phase_a += step_a
            phase_b += step_b
            ctx.set(dut.i.audio_in0, sample_a)
            ctx.set(dut.i.audio_in1, sample_b)

            for x in range(-(H_TOTAL - H_ACTIVE), H_ACTIVE):
                ctx.set(dut.i.x, x)
                ctx.set(dut.i.y, y if y < V_ACTIVE else y - V_TOTAL)
                ctx.set(dut.i.de, 1 if (0 <= x < H_ACTIVE and y < V_ACTIVE) else 0)
                hsync = (H_SYNC_START - H_TOTAL) <= x < (H_SYNC_END - H_TOTAL)
                ctx.set(dut.i.hsync, 1 if hsync else 0)
                ctx.set(dut.i.vsync, 1 if y >= V_ACTIVE else 0)
                await ctx.tick()

                # Output is two pipeline stages behind the beam.
                px = x - 2
                if y < V_ACTIVE and 0 <= px < H_ACTIVE:
                    pixels[y][px] = (ctx.get(dut.o.r),
                                     ctx.get(dut.o.g),
                                     ctx.get(dut.o.b))

    sim.add_testbench(testbench)
    sim.run()

    from PIL import Image
    img = Image.new("RGB", (H_ACTIVE, V_ACTIVE))
    img.putdata([px for row in pixels for px in row])
    img.save(out_path)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--freq-a", type=float, default=220.0)
    p.add_argument("--freq-b", type=float, default=330.0)
    p.add_argument("--amp-a", type=float, default=0.8)
    p.add_argument("--amp-b", type=float, default=0.5)
    p.add_argument("--zoom", type=float, default=0.0, help="in2 CV, volts")
    p.add_argument("--palette", type=float, default=0.0, help="in3 CV, volts")
    p.add_argument("--out", default="frame.png")
    a = p.parse_args()
    render(a.freq_a, a.freq_b, a.amp_a, a.amp_b, a.zoom, a.palette, a.out)
