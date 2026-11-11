#[macro_export]
macro_rules! impl_scope {
    ($( $SCOPEX:ident: $PACSCOPEX:ty, )+) => { $(
        pub struct $SCOPEX {
            registers: $PACSCOPEX,
            xscale: u8,
            px_div_x: u32,
            px_div_y: u32,
            fs_up: u32,
        }

        impl $SCOPEX {
            pub fn new(registers: $PACSCOPEX, xscale: u8) -> Self {
                let ppv = registers.pixels_per_volt().read().pixels_per_volt().bits() as u32;
                let fs_up = registers.fs().read().fs().bits();
                let px_div_x = ppv >> xscale;
                let px_div_y = ppv >> tiliqua_lib::scope::VScale::Scale1V.to_scale_bits();
                registers.xscale().write(|w| unsafe { w.xscale().bits(xscale) });
                Self { registers, xscale, px_div_x, px_div_y, fs_up }
            }

            pub fn pixels_per_div(&self) -> (u32, u32) {
                (self.px_div_x, self.px_div_y)
            }

            pub fn set_timebase(&mut self, tb: tiliqua_lib::scope::Timebase) {
                let numer: u64 = (self.px_div_x as u64) * (1u64 << (15 + self.xscale as u32));
                let raw = (numer * 1_000_000 / (self.fs_up as u64 * tb.t_div_us())) as u32;
                self.registers.timebase().write(|w| unsafe { w.timebase().bits(raw) });
            }

            pub fn set_yscale(&mut self, vs: tiliqua_lib::scope::VScale) {
                self.registers.yscale().write(|w| unsafe { w.yscale().bits(vs.to_scale_bits()) });
            }

            pub fn set_hue(&mut self, hue: u8) {
                self.registers.hue().write(|w| unsafe { w.hue().bits(hue) });
            }

            pub fn set_intensity(&mut self, intensity: u8) {
                self.registers.intensity().write(|w| unsafe { w.intensity().bits(intensity) });
            }

            pub fn set_trigger_level(&mut self, lvl: i16) {
                self.registers.trigger_lvl().write(|w| unsafe { w.trigger_level().bits(lvl as u16) });
            }

            pub fn set_ypos_px(&mut self, ch: usize, pos: i16) {
                match ch {
                    0 => self.registers.ypos0().write(|w| unsafe { w.ypos().bits(pos as u16) }),
                    1 => self.registers.ypos1().write(|w| unsafe { w.ypos().bits(pos as u16) }),
                    2 => self.registers.ypos2().write(|w| unsafe { w.ypos().bits(pos as u16) }),
                    3 => self.registers.ypos3().write(|w| unsafe { w.ypos().bits(pos as u16) }),
                    _ => return,
                };
            }

            pub fn set_xscale(&mut self, scale: u8) {
                self.xscale = scale;
                let ppv = self.registers.pixels_per_volt().read().pixels_per_volt().bits() as u32;
                self.px_div_x = ppv >> scale;
                self.registers.xscale().write(|w| unsafe { w.xscale().bits(scale) });
            }

            pub fn set_xpos_px(&mut self, pos: i16) {
                self.registers.xpos().write(|w| unsafe { w.xpos().bits(pos as u16) });
            }

            /// Trigger hysteresis, in `set_trigger_level` units. 0 is the original
            /// single-threshold trigger.
            pub fn set_trigger_hyst(&mut self, hyst: i16) {
                self.registers.trigger_hyst().write(|w| unsafe { w.trigger_hyst().bits(hyst as u16) });
            }

            /// Channel 0 lowest and highest sample since the last `meter_clear`,
            /// in `set_trigger_level` units. Returns (i32::MAX, i32::MIN)-ish
            /// sentinels if nothing arrived since the clear.
            pub fn meter_min_max(&self) -> (i32, i32) {
                (self.registers.meter_min().read().level().bits() as i32,
                 self.registers.meter_max().read().level().bits() as i32)
            }

            pub fn meter_clear(&mut self) {
                self.registers.meter_ctrl().write(|w| w.clear().bit(true));
            }

            /// (samples between the last two triggers, triggers so far wrapping at 2^16).
            /// Samples are at `fs_up()`.
            pub fn meter_period(&self) -> (u32, u16) {
                (self.registers.meter_period().read().samples().bits(),
                 self.registers.meter_count().read().count().bits())
            }

            /// Rate of the stream the scope plots (audio rate times the upsampling).
            pub fn fs_up(&self) -> u32 {
                self.fs_up
            }

            /// Length in microseconds of one full sweep at timebase `tb`,
            /// at the current x scale. Follows `dsp.Ramp`: the beam steps
            /// `timebase` per sample over a span of about 2^31 register units.
            pub fn sweep_us(&self, tb: tiliqua_lib::scope::Timebase) -> u64 {
                let numer: u64 = (self.px_div_x as u64) * (1u64 << (15 + self.xscale as u32));
                let raw = (numer * 1_000_000 / (self.fs_up as u64 * tb.t_div_us())).max(1);
                let samples = (1u64 << 31) / raw;
                samples * 1_000_000 / self.fs_up as u64
            }

            pub fn set_enabled(&mut self, enabled: bool, trigger_always: bool) {
                self.registers.flags().write(|w| {
                    w.enable().bit(enabled);
                    w.trigger_always().bit(trigger_always)
                });
            }
        }
    )+ };
}
