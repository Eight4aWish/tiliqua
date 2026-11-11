#![no_std]
#![no_main]

use critical_section::Mutex;
use log::{info, error};
use riscv_rt::entry;
use irq::handler;
use core::cell::RefCell;
use embassy_sync::blocking_mutex::{Mutex as FlashMutex, raw::NoopRawMutex};

use midi_types::*;
use midi_convert::parse::MidiTryParseSlice;

use tiliqua_fw::*;
use tiliqua_lib::*;
use tiliqua_lib::dsp::OnePoleSmoother;
use pac::constants::*;
use tiliqua_lib::calibration::*;

use tiliqua_hal::embedded_graphics::prelude::*;

use options::*;
use opts::persistence::*;
use opts::{Options, OptionTrait};
use opts::cc_map::{MidiCcMapper, CcMapMode};
use hal::pca9635::Pca9635Driver;
use tiliqua_hal::dma_framebuffer::Rotate;
use tiliqua_hal::tusb322::{TUSB322Driver, TUSB322Mode, AttachedState};
use tiliqua_hal::i2c::{I2cBus, I2cDevice};
use static_cell::StaticCell;
use tiliqua_hal::persist::Persist;

pub const TICK_PERIOD_MS: u32 = 5;

/// Auto trigger: how often it re-reads the ch0 meter (ticks), how many of
/// those windows the level tracks (3 s, long enough for slow LFOs), the
/// smallest swing it will trigger on (counts, 4 per mV), and how many
/// updates in a row must agree before the timebase changes.
const AUTO_UPDATE_TICKS: u32 = 100 / TICK_PERIOD_MS;
const AUTO_WINDOWS: usize = 30;
const AUTO_MIN_SWING: i32 = 40;
const AUTO_TB_AGREE: u8 = 3;

/// Fastest first, so the first one whose sweep holds two periods is the
/// tightest view: 2 to 5 cycles on screen with 1-2-5 steps.
const TIMEBASES_FAST_FIRST: [Timebase; 13] = [
    Timebase::Timebase50us, Timebase::Timebase100us, Timebase::Timebase200us,
    Timebase::Timebase500us, Timebase::Timebase1ms, Timebase::Timebase2ms,
    Timebase::Timebase5ms, Timebase::Timebase10ms, Timebase::Timebase20ms,
    Timebase::Timebase50ms, Timebase::Timebase100ms, Timebase::Timebase200ms,
    Timebase::Timebase500ms,
];

/// Trigger intervals within this many percent of each other count as one
/// trigger per cycle.
const AUTO_LOCK_PCT: u64 = 6;

/// Holdoff search state for `TriggerMode::Auto`.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Lock {
    /// Ignore the next update (its intervals straddle a holdoff change).
    Settle,
    /// Intervals differ: raise the holdoff each update until they agree.
    Climb,
    /// One trigger per cycle. `probed` is set once a shorter holdoff has
    /// been tried at this period.
    Locked { interval: u32, probed: bool },
    /// Trying 70% of the holdoff; `prev` is restored if the lock breaks.
    Probe { prev: u32 },
}

/// State for `TriggerMode::Auto`. The gateware measures ch0: min and max,
/// and the shortest and longest interval between triggers since the last
/// clear. This turns those into a trigger level halfway up the swing, a
/// hysteresis band of half the swing, a holdoff and a timebase.
///
/// The holdoff is what makes a wavefolded or harmonic-rich wave sit still.
/// Such a wave crosses the level several times per cycle, so without it the
/// sweep starts on whichever crossing comes first. Starting from 0, the
/// holdoff climbs (to 95% of the longest interval, at least +15% a step)
/// until the intervals agree: one trigger per cycle, always on the same
/// crossing. Once locked it tries 70% once, in case it locked on every other
/// cycle; if a lock breaks for two updates (a new note) it starts again from
/// 0. Modelled on folded waves at 80 Hz-2 kHz before it was built: locks in
/// 0.3-0.7 s.
///
/// The timebase only moves on a locked period, so the search never makes it
/// flick between steps.
struct AutoTrigger {
    win_min: [i32; AUTO_WINDOWS],
    win_max: [i32; AUTO_WINDOWS],
    win_idx: usize,
    last_update: u32,
    last_count: u16,
    last_trig_tick: u32,
    lock: Lock,
    unlocked_n: u8,
    period_us: Option<u64>,
    holdoff: u32,
    pending_tb: Option<Timebase>,
    pending_n: u8,
    hyst: i16,
    free_run: bool,
}

impl AutoTrigger {
    fn new() -> Self {
        Self {
            win_min: [i32::MAX; AUTO_WINDOWS],
            win_max: [i32::MIN; AUTO_WINDOWS],
            win_idx: 0,
            last_update: 0,
            last_count: 0,
            last_trig_tick: 0,
            lock: Lock::Settle,
            unlocked_n: 0,
            period_us: None,
            holdoff: 0,
            pending_tb: None,
            pending_n: 0,
            hyst: 0,
            free_run: true,
        }
    }

    fn set_holdoff(&mut self, scope: &mut Scope0, samples: u32) {
        self.holdoff = samples;
        scope.set_holdoff(samples);
    }

    fn reacquire(&mut self, scope: &mut Scope0) {
        self.set_holdoff(scope, 0);
        self.lock = Lock::Settle;
        self.unlocked_n = 0;
    }

    /// One step of the holdoff search, from this window's intervals.
    fn step_holdoff(&mut self, scope: &mut Scope0, imin: u32, imax: u32) {
        let agree = u64::from(imax) * 100 <= u64::from(imin) * (100 + AUTO_LOCK_PCT);
        self.lock = match self.lock {
            Lock::Settle => Lock::Climb,
            Lock::Probe { prev } => {
                if !agree {
                    self.set_holdoff(scope, prev);
                }
                Lock::Locked { interval: imax, probed: true }
            }
            _ if agree => {
                self.unlocked_n = 0;
                let probed = match self.lock {
                    Lock::Locked { interval, probed } =>
                        probed && imax.abs_diff(interval) * 10 <= interval,
                    _ => false,
                };
                if !probed && self.holdoff > 0 {
                    let prev = self.holdoff;
                    self.set_holdoff(scope, prev / 10 * 7);
                    Lock::Probe { prev }
                } else {
                    Lock::Locked { interval: imax, probed }
                }
            }
            Lock::Climb => {
                let up = (self.holdoff / 100 * 115).max(imax / 100 * 95);
                self.set_holdoff(scope, up);
                Lock::Climb
            }
            Lock::Locked { .. } => {
                self.unlocked_n += 1;
                if self.unlocked_n >= 2 {
                    self.reacquire(scope);
                    Lock::Settle
                } else {
                    self.lock
                }
            }
        };
        if let Lock::Locked { interval, .. } = self.lock {
            self.period_us = Some(u64::from(interval) * 1_000_000 / u64::from(scope.fs_up()));
        }
    }

    /// Returns the (trigger level, timebase) to show and use, if it has one.
    fn update(&mut self, now: u32, scope: &mut Scope0, current_tb: Timebase)
        -> Option<(i16, Timebase)>
    {
        if now.wrapping_sub(self.last_update) < AUTO_UPDATE_TICKS {
            return None;
        }
        self.last_update = now;

        let (mn, mx) = scope.meter_min_max();
        let (imin, imax) = scope.meter_intervals();
        let (_, cnt) = scope.meter_period();
        scope.meter_clear();
        self.win_min[self.win_idx] = mn;
        self.win_max[self.win_idx] = mx;
        self.win_idx = (self.win_idx + 1) % AUTO_WINDOWS;

        if cnt != self.last_count {
            self.last_count = cnt;
            self.last_trig_tick = now;
        }
        if imax > 0 && imin <= imax {
            self.step_holdoff(scope, imin, imax);
        }

        let lo = *self.win_min.iter().min().unwrap_or(&i32::MAX);
        let hi = *self.win_max.iter().max().unwrap_or(&i32::MIN);
        if lo > hi || hi - lo < AUTO_MIN_SWING {
            // Nothing there to trigger on: free-run, like a bench scope's auto.
            self.free_run = true;
            self.hyst = 0;
            self.reacquire(scope);
            return None;
        }

        let level = ((lo + hi) / 2).clamp(i32::from(i16::MIN), i32::from(i16::MAX)) as i16;
        self.hyst = ((hi - lo) / 4).clamp(1, i32::from(i16::MAX)) as i16;

        let mut tb = current_tb;
        if let Some(p) = self.period_us {
            // Keep the current timebase while it shows roughly 2-6 cycles,
            // so a period near a step boundary doesn't flick between steps.
            let cur = scope.sweep_us(current_tb);
            let want = if cur * 10 >= p * 18 && cur <= p * 6 {
                current_tb
            } else {
                TIMEBASES_FAST_FIRST.iter()
                    .copied()
                    .find(|t| scope.sweep_us(*t) >= 2 * p)
                    .unwrap_or(Timebase::Timebase500ms)
            };
            if want == current_tb {
                self.pending_tb = None;
                self.pending_n = 0;
            } else if self.pending_tb == Some(want) {
                self.pending_n += 1;
                if self.pending_n >= AUTO_TB_AGREE {
                    tb = want;
                    self.pending_tb = None;
                    self.pending_n = 0;
                }
            } else {
                self.pending_tb = Some(want);
                self.pending_n = 1;
            }
        }

        // No trigger for twice the longer of a sweep and a period (and at
        // least 300 ms): free-run and search again.
        let wait_us = 2 * scope.sweep_us(tb).max(self.period_us.unwrap_or(0)).max(150_000);
        let waited_us = u64::from(now.wrapping_sub(self.last_trig_tick)) * u64::from(TICK_PERIOD_MS) * 1000;
        self.free_run = waited_us > wait_us;
        if self.free_run {
            self.reacquire(scope);
        }

        Some((level, tb))
    }
}

fn global_index(opts: &Opts, opt: &dyn OptionTrait) -> usize {
    let key = opt.key().value();
    opts.all().enumerate()
        .find(|(_, o)| o.key().value() == key)
        .expect("cc_map: option key not found").0
}

fn apply_cc_action(opts: &mut Opts, action: &opts::cc_map::CcAction) {
    if let Some(opt) = opts.all_mut().nth(action.global_index) {
        match action.mode {
            CcMapMode::Absolute => { opt.set_from_cc(action.cc_value); }
        }
    }
}

fn build_cc_mapper(opts: &Opts) -> MidiCcMapper {
    let mut m = MidiCcMapper::new();
    // Vector page (CC 20-27)
    m.add(20, global_index(opts, &opts.vector.x_offset),  CcMapMode::Absolute);
    m.add(21, global_index(opts, &opts.vector.x_scale),   CcMapMode::Absolute);
    m.add(22, global_index(opts, &opts.vector.y_offset),  CcMapMode::Absolute);
    m.add(23, global_index(opts, &opts.vector.y_scale),   CcMapMode::Absolute);
    m.add(24, global_index(opts, &opts.vector.i_offset),  CcMapMode::Absolute);
    m.add(25, global_index(opts, &opts.vector.i_scale),   CcMapMode::Absolute);
    m.add(26, global_index(opts, &opts.vector.c_offset),  CcMapMode::Absolute);
    m.add(27, global_index(opts, &opts.vector.c_scale),   CcMapMode::Absolute);
    // Delay page (CC 30-33)
    m.add(30, global_index(opts, &opts.delay.delay_x),    CcMapMode::Absolute);
    m.add(31, global_index(opts, &opts.delay.delay_y),    CcMapMode::Absolute);
    m.add(32, global_index(opts, &opts.delay.delay_i),    CcMapMode::Absolute);
    m.add(33, global_index(opts, &opts.delay.delay_c),    CcMapMode::Absolute);
    // Beam page (CC 40-45, CC 41 formerly decay now unused)
    m.add(40, global_index(opts, &opts.beam.persist),     CcMapMode::Absolute);
    m.add(42, global_index(opts, &opts.beam.ui_hue),      CcMapMode::Absolute);
    m.add(43, global_index(opts, &opts.beam.palette),     CcMapMode::Absolute);
    m.add(44, global_index(opts, &opts.beam.grid),        CcMapMode::Absolute);
    m.add(45, global_index(opts, &opts.beam.grid_i),      CcMapMode::Absolute);
    // Misc page (CC 50-52)
    m.add(50, global_index(opts, &opts.misc.plot_type),   CcMapMode::Absolute);
    m.add(51, global_index(opts, &opts.misc.plot_src),    CcMapMode::Absolute);
    m.add(52, global_index(opts, &opts.misc.rotation),    CcMapMode::Absolute);
    // Scope1 page (CC 60-64)
    m.add(60, global_index(opts, &opts.scope1.ypos0),     CcMapMode::Absolute);
    m.add(61, global_index(opts, &opts.scope1.ypos1),     CcMapMode::Absolute);
    m.add(62, global_index(opts, &opts.scope1.ypos2),     CcMapMode::Absolute);
    m.add(63, global_index(opts, &opts.scope1.ypos3),     CcMapMode::Absolute);
    m.add(64, global_index(opts, &opts.scope1.n_channels), CcMapMode::Absolute);
    // Scope2 page (CC 70-76)
    m.add(70, global_index(opts, &opts.scope2.yscale),    CcMapMode::Absolute);
    m.add(71, global_index(opts, &opts.scope2.timebase),  CcMapMode::Absolute);
    m.add(72, global_index(opts, &opts.scope2.xzoom),     CcMapMode::Absolute);
    m.add(73, global_index(opts, &opts.scope2.trig_mode), CcMapMode::Absolute);
    m.add(74, global_index(opts, &opts.scope2.trig_lvl),  CcMapMode::Absolute);
    m.add(75, global_index(opts, &opts.scope2.intensity), CcMapMode::Absolute);
    m.add(76, global_index(opts, &opts.scope2.hue),       CcMapMode::Absolute);
    m
}

static I2C0_BUS: StaticCell<I2cBus<I2c0>> = StaticCell::new();

type Ui = ui::UI<Encoder0, EurorackPmod0, I2cDevice<'static, I2c0>, Opts>;

struct App {
    ui: Ui,
    cc_mapper: MidiCcMapper,
    xbeam: pac::XBEAM_PERIPH,
    ticks: u32,
}

impl App {
    pub fn new(ui: Ui, xbeam: pac::XBEAM_PERIPH) -> Self {
        let cc_mapper = build_cc_mapper(&ui.opts);
        Self {
            ui,
            cc_mapper,
            xbeam,
            ticks: 0,
        }
    }
}

fn tick_handler(app: &Mutex<RefCell<App>>) {
    critical_section::with(|cs| {
        let mut app = app.borrow_ref_mut(cs);
        app.ticks = app.ticks.wrapping_add(1);
        app.ui.update();

        // Check for TRS MIDI CC traffic
        let midi_word = app.xbeam.midi_read().read().bits();
        if midi_word != 0 {
            app.ui.midi_activity();
            let bytes = [
                (midi_word & 0xFF) as u8,
                ((midi_word >> 8) & 0xFF) as u8,
                ((midi_word >> 16) & 0xFF) as u8,
            ];
            if let Ok(MidiMessage::ControlChange(_, cc, val)) = MidiMessage::try_parse_slice(&bytes) {
                if let Some(action) = app.cc_mapper.process(cc.into(), val.into()) {
                    if app.ui.opts.misc.cc_highlight.value == CcHighlight::On {
                        app.ui.opts.select_global(action.global_index);
                        app.ui.external_modify();
                    }
                    apply_cc_action(&mut app.ui.opts, &action);
                }
            }
        }

        if app.ui.opts.misc.help.value == HelpPage::Off
            && app.ui.opts.tracker.page.value == Page::Help {
            app.ui.opts.tracker.page.value = Page::Vector;
        }
        app.ui.opts.misc.plot_type.value = match app.ui.opts.tracker.page.value {
            Page::Vector => PlotType::Vector,
            Page::Scope1 => PlotType::Scope,
            Page::Scope2 => PlotType::Scope,
            _ => app.ui.opts.misc.plot_type.value
        };
    });
}

#[entry]
fn main() -> ! {
    let peripherals = pac::Peripherals::take().expect("unable to take peripherals");
    let serial = Serial0::new(peripherals.UART0);
    let mut persist = Persist0::new(peripherals.PERSIST_PERIPH);
    let spiflash = SPIFlash0::new(
        peripherals.SPIFLASH_CTRL,
        SPIFLASH_BASE,
        SPIFLASH_SZ_BYTES
    );

    tiliqua_fw::handlers::logger_init(serial);

    info!("Hello from Tiliqua XBEAM!");

    let bootinfo = unsafe { bootinfo::BootInfo::from_addr(BOOTINFO_BASE) }.expect("invalid bootinfo");
    let modeline = bootinfo.modeline.maybe_override_fixed(
        FIXED_MODELINE, CLOCK_DVI_HZ);
    let mut display = hal::new_framebuffer!(peripherals, modeline.clone());

    let mut i2c1 = I2c1::new(peripherals.I2C1);
    let mut pmod = EurorackPmod0::new(peripherals.PMOD0_PERIPH);
    CalibrationConstants::load_or_default(&mut i2c1, &mut pmod);

    //
    // Start up TUSB322 in UFP/Device mode
    //

    let i2c0 = I2C0_BUS.init(tiliqua_hal::i2c::bus(I2c0::new(peripherals.I2C0)));
    let mut tusb322 = TUSB322Driver::new(I2cDevice::new(i2c0));
    tusb322.soft_reset().ok();
    tusb322.set_mode(TUSB322Mode::Ufp).ok();

    //
    // Create options and maybe load from persistent storage
    //

    let mut opts = Opts::default();
    opts.misc.rotation.value = modeline.rotate;
    let flash = FlashMutex::<NoopRawMutex, _>::new(RefCell::new(spiflash));
    let mut flash_persist_opt = FlashOptionsPersistence::open_partition(
        &flash, bootinfo.manifest.get_option_storage_window(), &mut opts);

    //
    // Create App instance
    //

    let mut last_palette = opts.beam.palette.value;
    let ui = ui::UI::new(opts, TICK_PERIOD_MS, Encoder0::new(peripherals.ENCODER0),
                         Pca9635Driver::new(I2cDevice::new(i2c0)), pmod);
    let app = Mutex::new(RefCell::new(App::new(ui, peripherals.XBEAM_PERIPH)));

    handler!(tick_isr = || tick_handler(&app));

    let mut delay_smoothers = [OnePoleSmoother::new(0.05f32); 4];

    irq::scope(|s| {

        s.register(tick::Interrupt::TICK, tick_isr);

        tick::enable(TICK_PERIOD_MS);

        let mut vscope = Vector0::new(peripherals.VECTOR_PERIPH);
        let mut scope = Scope0::new(peripherals.SCOPE_PERIPH, 6);
        let overlay_periph = peripherals.OVERLAY_PERIPH;
        let mut first = true;
        let mut auto_trig = AutoTrigger::new();

        let mut usb_cc_attached = false;

        // Grid overlay operates in DVI pixel space (pre-rotation)
        let dvi_w = u32::from(modeline.h_active);
        let dvi_h = u32::from(modeline.v_active);
        overlay_periph.grid_offset().write(|w| unsafe {
            w.offset_x().bits((dvi_w / 2) as u16);
            w.offset_y().bits((dvi_h / 2) as u16)
        });

        loop {

            let h_active = display.size().width;
            let v_active = display.size().height;

            let (opts, draw_options, save_opts, wipe_opts, now) = critical_section::with(|cs| {
                let mut app = app.borrow_ref_mut(cs);
                let save_opts = app.ui.opts.misc.save_opts.poll();
                let wipe_opts = app.ui.opts.misc.wipe_opts.poll();
                (app.ui.opts.clone(), app.ui.draw(), save_opts, wipe_opts, app.ticks)
            });

            let on_help_page = opts.tracker.page.value == Page::Help;

            if opts.beam.palette.value != last_palette || first {
                opts.beam.palette.value.write_to_hardware(&mut display);
                last_palette = opts.beam.palette.value;
            }

            if draw_options || on_help_page {
                let (x, y) = if on_help_page {
                    (h_active/2-30, v_active-100)
                } else {
                    (h_active-200, v_active/2)
                };
                draw::draw_options(&mut display, &opts, x, y, opts.beam.ui_hue.value).ok();
                draw::draw_name(&mut display, h_active/2, v_active-50, opts.beam.ui_hue.value,
                                &bootinfo.manifest.name, &bootinfo.manifest.tag, &modeline).ok();
            }

            if on_help_page {
                draw::draw_help_page(&mut display,
                    MODULE_DOCSTRING,
                    bootinfo.manifest.help.as_ref(),
                    h_active,
                    v_active,
                    opts.help.scroll.value,
                    opts.beam.ui_hue.value).ok();
                persist.set_persistence(64);
            } else {
                persist.set_persistence(opts.beam.persist.value);
            }


            if save_opts {
                if let Some(ref mut flash_persist) = flash_persist_opt {
                    if let Err(e) = flash_persist.save_options(&opts) {
                        error!("failed to save options: {e:?}");
                    }
                }
            }

            if wipe_opts {
                critical_section::with(|cs| {
                    let mut app = app.borrow_ref_mut(cs);
                    app.ui.opts = Opts::default();
                    app.ui.opts.misc.rotation.value = modeline.rotate;
                    if let Some(ref mut flash_persist) = flash_persist_opt {
                        if let Err(e) = flash_persist.erase_all() {
                            error!("failed to erase options: {e:?}");
                        }
                    }
                });
            }

            let (ppd_x, ppd_y) = vscope.pixels_per_div();
            vscope.set_xoffset_px(opts.vector.x_offset.value * (ppd_x / 4) as i16);
            vscope.set_yoffset_px(opts.vector.y_offset.value * (ppd_y / 4) as i16);
            vscope.set_xscale(opts.vector.x_scale.value);
            vscope.set_yscale(opts.vector.y_scale.value);
            vscope.set_pscale(opts.vector.i_scale.value);
            vscope.set_intensity(opts.vector.i_offset.value);
            vscope.set_cscale(opts.vector.c_scale.value);
            vscope.set_hue(opts.vector.c_offset.value);

            scope.set_hue(opts.scope2.hue.value);
            scope.set_intensity(opts.scope2.intensity.value);
            scope.set_trigger_level(opts.scope2.trig_lvl.value);
            scope.set_yscale(opts.scope2.yscale.value);
            let xscale_bits: u8 = match opts.scope2.xzoom.value {
                XZoom::Half   => 7,
                XZoom::Normal => 6,
                XZoom::Double => 5,
            };
            scope.set_xscale(xscale_bits);
            scope.set_timebase(opts.scope2.timebase.value);

            // Auto: follow ch0, and write the level and timebase it picks
            // back into the menu, so they show and become the starting point
            // if you switch to `rising`.
            let auto = opts.scope2.trig_mode.value == TriggerMode::Auto;
            if auto {
                if let Some((level, tb)) = auto_trig.update(now, &mut scope, opts.scope2.timebase.value) {
                    critical_section::with(|cs| {
                        let mut app = app.borrow_ref_mut(cs);
                        app.ui.opts.scope2.trig_lvl.value = level;
                        app.ui.opts.scope2.timebase.value = tb;
                    });
                }
                scope.set_trigger_hyst(auto_trig.hyst);
            } else {
                scope.set_trigger_hyst(0);
                auto_trig.reacquire(&mut scope);
            }
            let (sppd_x, sppd) = scope.pixels_per_div();
            let n_ch = opts.scope1.n_channels.value;
            let ypos = [opts.scope1.ypos0.value, opts.scope1.ypos1.value,
                         opts.scope1.ypos2.value, opts.scope1.ypos3.value];
            for ch in 0..4u8 {
                let pos = if ch < n_ch {
                    ypos[ch as usize] * (sppd / 4) as i16
                } else {
                    750 // hide inactive channels off-screen
                };
                scope.set_ypos_px(ch.into(), pos);
            }

            // Only connect USB PHY if the TUSB322 Type-C controller says we are attached.
            // This fixes enumeration issues on some machines when using typec <-> typec cables.
            if let Ok(status) = tusb322.read_connection_status_control() {
                // Only update on valid reads to reduce risk of unintended toggling mid-stream
                let new_state = status.attached_state == AttachedState::AttachedSnk;
                if new_state != usb_cc_attached {
                    info!("USB CC hotplug: {status:?}");
                    usb_cc_attached = new_state;
                }
            }

            critical_section::with(|cs| {
                app.borrow_ref(cs).xbeam.flags().write(
                    |w| { w.usb_en().bit(opts.misc.usb_mode.value == USBMode::Enable);
                          w.show_outputs().bit(opts.misc.plot_src.value == PlotSrc::Outputs);
                          w.usb_connect().bit(usb_cc_attached)
                    } );
            });

            // Update grid spacing. In scope mode use the scope's ppd_x for the
            // time axis. In portrait rotation the time/signal axes are swapped
            // relative to DVI x/y.
            let (scope_ppd_time, scope_ppd_sig) = if opts.misc.plot_type.value == PlotType::Scope {
                (sppd_x, ppd_y)
            } else {
                (ppd_x, ppd_y)
            };
            let is_portrait = matches!(opts.misc.rotation.value, Rotate::Left | Rotate::Right);
            let (dvi_ppd_x, dvi_ppd_y) = if is_portrait {
                (scope_ppd_sig, scope_ppd_time)
            } else {
                (scope_ppd_time, scope_ppd_sig)
            };
            overlay_periph.grid_spacing().write(|w| unsafe {
                w.spacing_x().bits(dvi_ppd_x as u8);
                w.spacing_y().bits(dvi_ppd_y as u8)
            });
            overlay_periph.grid_start().write(|w| unsafe {
                w.start_x().bits(((dvi_w / 2) % dvi_ppd_x) as u8);
                w.start_y().bits((((dvi_h / 2) + 1) % dvi_ppd_y) as u8)
            });

            // Grid overlay style/pixel (changes with options)
            let grid_style: u8 = if on_help_page { 0 } else {
                match opts.beam.grid.value {
                    GridOverlay::Off => 0,
                    GridOverlay::Grid => 1,
                    GridOverlay::Cross => 2,
                }
            };
            overlay_periph.flags().write(|w| unsafe {
                w.grid_style().bits(grid_style);
                w.grid_pixel().bits((opts.beam.grid_i.value << 4) | opts.beam.ui_hue.value)
            });

            critical_section::with(|cs| {
                let xbeam = &app.borrow_ref(cs).xbeam;
                xbeam.delay0().write(|w| unsafe { w.value().bits(
                        delay_smoothers[0].proc_u16(opts.delay.delay_x.value)) });
                xbeam.delay1().write(|w| unsafe { w.value().bits(
                        delay_smoothers[1].proc_u16(opts.delay.delay_y.value)) });
                xbeam.delay2().write(|w| unsafe { w.value().bits(
                        delay_smoothers[2].proc_u16(opts.delay.delay_i.value)) });
                xbeam.delay3().write(|w| unsafe { w.value().bits(
                        delay_smoothers[3].proc_u16(opts.delay.delay_c.value)) });
            });

            display.rotate(&opts.misc.rotation.value);


            if opts.tracker.page.value == Page::Help {
                scope.set_enabled(false, false);
                vscope.set_enabled(false);
            } else if opts.misc.plot_type.value == PlotType::Vector {
                scope.set_enabled(false, false);
                vscope.set_enabled(true);
            } else {
                scope.set_enabled(true, opts.scope2.trig_mode.value == TriggerMode::Always
                                        || (auto && auto_trig.free_run));
                vscope.set_enabled(false);
            }

            first = false;
        }
    })
}
