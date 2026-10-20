# Host support for the MCU motion clock: feed hold / resume and hold on
# underrun (see src/stepper.c "Motion clock")
#
# Copyright (C) 2026  Nicolás Méndez <nmendez@fbmc.fcen.uba.ar>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# The main MCU schedules steps in "motion time", which normally runs with
# its hardware clock.  A hold (or a slowed rate) makes the motion clock fall
# behind; print_time stays real time for everything else (heaters, sensors).
# While the MCU is held or ramping, the toolhead and step flushing follow the
# frozen/slowed motion (via mcu.estimated_motion_time()), so they stop
# planning ahead.  Once the MCU is back at normal rate, the accumulated lag
# is folded in by shifting all motion state (toolhead, step generation,
# trapq moves, step time base) by the same amount, which keeps the step
# clocks sent to the MCU continuous.
#
# Limitations: single MCU only; outputs synchronized with moves (fans,
# lasers via SET_PIN) that were already sent before a hold are not delayed
# by it.
import logging
import chelper

MODE_NORMAL, MODE_SCALED, MODE_HELD = 0, 1, 2
MODE_NAMES = {MODE_NORMAL: "normal", MODE_SCALED: "scaled", MODE_HELD: "held"}
EVENT_UNDERRUN_HOLD = 1
POLL_TIME = 0.050
POLL_STEADY_TIME = 0.500
# Extra lead (beyond the hold ramp) required before arming the deadline
ARM_MARGIN = 0.100

class MotionClock:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self.reactor = printer.get_reactor()
        self.mcu = printer.lookup_object('mcu')
        self.ramp_ms = config.getint('ramp_ms', 250, minval=1, maxval=60000)
        # Underrun protection: ramp length of the automatic hold (0 disables)
        self.deadline_ramp_ms = config.getint('deadline_ramp_ms', 250,
                                              minval=0, maxval=60000)
        self.auto_resume = config.getboolean('underrun_auto_resume', True)
        # Hold must be able to start from any planned speed, and the scaled
        # step path is slower than the regular one: optionally refuse
        # configurations whose worst-case step rate exceeds it
        self.max_step_rate = config.getfloat('max_step_rate', 0., minval=0.)
        self.debug = config.getboolean('debug', False)
        # Steps must be queued further ahead than the hold ramp: raise the
        # step flush lead time accordingly
        # Flushed lead must exceed the transmit lead (ramp + 0.15 s, below)
        min_lead = self.deadline_ramp_ms / 1000. + 0.250
        self.flush_lead = config.getfloat('flush_lead', min_lead,
                                          minval=min_lead)
        self.k_one = None
        self.state = None   # (mode, k, clock64, offset64) from the MCU
        self.applied_offset = 0  # step clock offset folded into print_time
        self.deadline_armed = False
        self.want_resume = False
        self.resume_ramp_ms = self.ramp_ms
        self.override_k = None  # active override rate (set on connect)
        # Modes a requested change will show up as (poll until seen)
        self.expect = None
        self.poll_timer = self.reactor.register_timer(self._poll)
        self.query_cmd = self.set_rate_cmd = self.set_deadline_cmd = None
        self.mcu.register_config_callback(self._build_config)
        mq = printer.load_object(config, 'motion_queuing')
        self.sync_se = mq.allocate_syncemitter(self.mcu, "motion_clock",
                                               alloc_stepcompress=False)
        printer.register_event_handler("klippy:connect", self._handle_connect)
        # Commands
        gcode = printer.lookup_object('gcode')
        gcode.register_command('MOTION_HOLD', self.cmd_MOTION_HOLD,
                               desc=self.cmd_MOTION_HOLD_help)
        gcode.register_command('MOTION_RESUME', self.cmd_MOTION_RESUME,
                               desc=self.cmd_MOTION_RESUME_help)
        gcode.register_command('MOTION_RATE', self.cmd_MOTION_RATE,
                               desc=self.cmd_MOTION_RATE_help)
        gcode.register_realtime_command('MOTION_RATE', self._realtime_rate)
        gcode.register_command('MOTION_CLOCK_STATUS',
                               self.cmd_MOTION_CLOCK_STATUS,
                               desc=self.cmd_MOTION_CLOCK_STATUS_help)
        # Hold/resume also work while other commands are blocked (a hold
        # stops the toolhead from accepting moves)
        gcode.register_realtime_command('MOTION_HOLD', self._realtime_hold)
        gcode.register_realtime_command('MOTION_RESUME',
                                        self._realtime_resume)
        webhooks = printer.lookup_object('webhooks')
        webhooks.register_endpoint("motion_clock/hold", self._handle_web_hold)
        webhooks.register_endpoint("motion_clock/resume",
                                   self._handle_web_resume)
        webhooks.register_endpoint("motion_clock/rate", self._handle_web_rate)
    # Setup
    def _build_config(self):
        consts = self.mcu.get_constants()
        if 'MOTION_CLOCK_K_ONE' not in consts:
            raise self.printer.config_error(
                "[motion_clock] needs MCU firmware with the motion clock")
        self.k_one = int(consts['MOTION_CLOCK_K_ONE'])
        if self.override_k is None:
            self.override_k = self.k_one
        self.query_cmd = self.mcu.lookup_command("motion_clock_query")
        self.set_rate_cmd = self.mcu.lookup_command(
            "motion_clock_set_rate k=%hu ramp_ms=%hu")
        self.set_deadline_cmd = self.mcu.lookup_command(
            "motion_clock_set_deadline mclock=%u ramp_ms=%hu")
        self.mcu.register_serial_response(
            self._handle_state, "motion_clock_state mode=%c k=%hu clock=%u"
            " mclock=%u")
        self.mcu.register_serial_response(
            self._handle_event, "motion_clock_event reason=%c mclock=%u")
    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.motion_queuing = mq = self.printer.lookup_object('motion_queuing')
        self.clocksync = cs = self.mcu.get_clocksync()
        self.freq = self.mcu.seconds_to_clock(1.)
        cs.motion_lag = self._motion_lag
        # Lead time of the step flushing must exceed the hold ramp
        import extras.motion_queuing as mqmod
        extra = max(0., self.flush_lead - mqmod.BGFLUSH_LOW_TIME)
        for name in ('BGFLUSH_LOW_TIME', 'BGFLUSH_HIGH_TIME',
                     'BGFLUSH_SG_LOW_TIME', 'BGFLUSH_SG_HIGH_TIME'):
            setattr(mqmod, name, getattr(mqmod, name) + extra)
        if self.deadline_ramp_ms:
            mq.register_flush_callback(self._handle_flush)
        self._check_step_rate()
        # The serial queue sends a message only ~100ms before its req_clock,
        # so the MCU normally holds just that much of steps (and a deadline
        # would arrive after its own trigger).  Transmit steps - and the
        # deadlines queued among them - earlier than the hold ramp plus a
        # margin, so the steps a deadline promises are on the MCU in time.
        if self.deadline_ramp_ms:
            ffi_main, ffi_lib = chelper.get_ffi()
            sq = self.mcu._serial.serialqueue
            self.reqtime_delta = self.deadline_ramp_ms / 1000. + ARM_MARGIN + .050
            ffi_lib.serialqueue_set_reqtime_delta(sq, self.reqtime_delta)
            if self.flush_lead < self.reqtime_delta + .100:
                logging.info("motion_clock: flush_lead %.3f below transmit"
                             " lead %.3f", self.flush_lead, self.reqtime_delta)
        self.query_cmd.send()
    def _check_step_rate(self):
        kin = self.toolhead.get_kinematics()
        max_v = self.toolhead.get_max_velocity()[0]
        max_z_v = getattr(kin, 'max_z_velocity', max_v)
        total = 0.
        for s in kin.get_steppers():
            v = max_z_v if s.is_active_axis('z') else max_v
            total += v / s.get_step_dist()
        logging.info("motion_clock: worst-case kinematic step rate %.0f"
                     " steps/s (max_step_rate %s)", total,
                     self.max_step_rate or "not set")
        if self.max_step_rate and total > self.max_step_rate:
            raise self.printer.config_error(
                "motion_clock: worst-case step rate %.0f steps/s exceeds"
                " max_step_rate %.0f - a hold could overload the MCU; lower"
                " max_velocity / max_z_velocity" % (total, self.max_step_rate))
    # MCU state tracking.  Serial response handlers run in the serial
    # background thread: hand them to the reactor before touching any
    # toolhead / step generation state.
    def _handle_state(self, params):
        self.reactor.register_async_callback(
            lambda e, p=params: self._process_state(p))
    def _handle_event(self, params):
        self.reactor.register_async_callback(
            lambda e, p=params: self._process_event(p))
    def _process_state(self, params):
        clock = self.mcu.clock32_to_clock64(params['clock'])
        offset32 = (params['clock'] - params['mclock']) & 0xffffffff
        prev = self.state[3] if self.state is not None else 0
        # Extend to 64 bits from the last value (the offset mostly grows,
        # but can step back by rounding while the rate is ramping)
        diff = (offset32 - prev) & 0xffffffff
        if diff >= 0x80000000:
            diff -= 0x100000000
        offset = prev + diff
        if self.state is None:
            # First report after connect: adopt the MCU's current offset
            self._set_applied_offset(offset)
        self.state = (params['mode'], params['k'], clock, offset)
        if self.expect is not None and params['mode'] in self.expect:
            self.expect = None
        if params['mode'] == MODE_NORMAL:
            if offset != self.applied_offset:
                self._fold_in_lag(offset)
            self.want_resume = False
        elif params['mode'] == MODE_HELD and self.want_resume:
            self._do_resume()
    def _process_event(self, params):
        if params['reason'] == EVENT_UNDERRUN_HOLD:
            logging.info("motion_clock: underrun hold (host stopped feeding"
                         " steps) at mclock %d", params['mclock'])
            self.printer.lookup_object('gcode').respond_info(
                "motion_clock: underrun - motion held on the path")
            self.deadline_armed = False
            self.expect = (MODE_HELD,)
            if self.auto_resume:
                # The host is alive again; resume once the steps are topped
                # up (flushing continues against the held motion clock)
                self.want_resume = True
                self.resume_ramp_ms = self.ramp_ms
        self._start_polling()
    def _start_polling(self):
        self.reactor.update_timer(self.poll_timer, self.reactor.NOW)
    def _poll(self, eventtime):
        self.query_cmd.send()
        if self.expect is not None or self.want_resume or self.state is None:
            return eventtime + POLL_TIME
        mode, k = self.state[0], self.state[1]
        if mode == MODE_NORMAL and self.state[3] == self.applied_offset:
            return self.reactor.NEVER
        if mode == MODE_SCALED and k == self.override_k:
            # Steady override: the lag estimate extrapolates between polls
            return eventtime + POLL_STEADY_TIME
        return eventtime + POLL_TIME
    def _current_offset(self, eventtime):
        mode, k, clock, offset = self.state
        if mode == MODE_NORMAL:
            return offset
        rclock = self.clocksync.get_clock(eventtime)
        if mode == MODE_HELD:
            return offset + (rclock - clock)
        return offset + (rclock - clock) * (1. - float(k) / self.k_one)
    def _motion_lag(self, eventtime):
        if self.state is None:
            return 0.
        return (self._current_offset(eventtime) - self.applied_offset) / self.freq
    # Folding the lag into print_time
    def _set_applied_offset(self, offset):
        self.applied_offset = offset
        self.clocksync.motion_offset_time = offset / self.freq
        ffi_main, ffi_lib = chelper.get_ffi()
        ss = self.motion_queuing._lookup_steppersync(self.mcu)
        ffi_lib.steppersync_set_time(ss, offset / self.freq, self.freq)
        ffi_lib.steppersync_set_movequeue_clock_offset(ss, offset)
    def _fold_in_lag(self, offset):
        delta_clock = offset - self.applied_offset
        dt = delta_clock / self.freq
        ffi_main, ffi_lib = chelper.get_ffi()
        th, mq = self.toolhead, self.motion_queuing
        with self.reactor.assert_no_pause():
            th.print_time += dt
            if 0. < th.need_check_pause < self.reactor.NEVER:
                th.need_check_pause += dt
            if th.check_stall_time:
                th.check_stall_time += dt
            mq.last_flush_time += dt
            mq.last_step_gen_time += dt
            mq.need_flush_time += dt
            mq.need_step_gen_time += dt
            mq.drip_start_times = [t + dt for t in mq.drip_start_times]
            for tq in mq.trapqs:
                ffi_lib.trapq_shift_time(tq, dt)
            for se in mq.syncemitters:
                sk = ffi_lib.syncemitter_get_stepper_kinematics(se)
                if sk != ffi_main.NULL:
                    ffi_lib.itersolve_shift_time(sk, dt)
            self._set_applied_offset(offset)
        logging.info("motion_clock: folded in %.6f s of motion lag (total"
                     " offset %.6f s)", dt, offset / self.freq)
        self.printer.send_event("motion_clock:shift", dt)
        self.reactor.update_timer(mq.flush_timer, self.reactor.NOW)
    # Underrun deadline
    def _mclock(self, print_time):
        return int(print_time * self.freq) - self.applied_offset
    def _mclock32(self, print_time):
        return self._mclock(print_time) & 0xffffffff
    def _queue_synced(self, print_time, cmd, args):
        # Queue an MCU command in the step stream: it is transmitted after
        # all step commands that start before 'print_time' (a deadline must
        # never reach the MCU ahead of the steps it promises)
        ffi_main, ffi_lib = chelper.get_ffi()
        data = [cmd.get_command_tag()] + list(args)
        cdata = ffi_main.new('uint32_t[%d]' % len(data), data)
        ffi_lib.syncemitter_queue_msg(self.sync_se, self._mclock(print_time),
                                      cdata, len(data))
    def _handle_flush(self, flush_time, step_gen_time):
        mq = self.motion_queuing
        if flush_time >= mq.need_flush_time:
            # Everything queued is flushed: idle, nothing to protect
            if self.deadline_armed:
                self._queue_synced(flush_time, self.set_deadline_cmd, [0, 0])
                self.deadline_armed = False
            return
        est = self.mcu.estimated_motion_time(self.reactor.monotonic())
        if self.debug:
            logging.info("motion_clock: flush %.4f est %.4f lead %.4f %s",
                         flush_time, est, flush_time - est,
                         "renew" if self.deadline_armed else "arm?")
        if not self.deadline_armed:
            # Starting from idle, motion is primed only BUFFER_TIME_START
            # ahead: arming now would put the trigger (deadline - ramp) in
            # the past and hold at once.  Arm once the lead covers the ramp.
            if flush_time - est < self.deadline_ramp_ms / 1000. + ARM_MARGIN:
                return
        self._queue_synced(flush_time, self.set_deadline_cmd,
                           [self._mclock32(flush_time), self.deadline_ramp_ms])
        self.deadline_armed = True

    # Hold / resume
    def hold(self, ramp_ms=None):
        self.want_resume = False
        # Until actually held, a resume must not be dropped as "not held"
        self.expect = (MODE_HELD,)
        self.set_rate_cmd.send([0, ramp_ms or self.ramp_ms])
        self._start_polling()
    def resume(self, ramp_ms=None):
        self.resume_ramp_ms = ramp_ms or self.ramp_ms
        self.want_resume = True
        if self.state is not None and self.state[0] != MODE_HELD \
           and self.expect is None:
            # Not held: nothing to resume
            self.want_resume = False
            return
        self._do_resume()
    def set_override(self, rate, ramp_ms=None):
        k = max(1, min(self.k_one, int(round(rate * self.k_one))))
        self.override_k = k
        if self.state is not None and self.state[0] == MODE_HELD:
            # Takes effect on resume
            return
        self.resume_ramp_ms = ramp_ms or self.ramp_ms
        self._do_resume()
    def _do_resume(self):
        if self.state is None:
            # No state yet: retry once it arrives
            self._start_polling()
            return
        self.want_resume = False
        # Re-arm the deadline from what is already flushed, so it doesn't
        # trigger again straight away
        mq = self.motion_queuing
        t = mq.last_flush_time
        if self.deadline_ramp_ms and t < mq.need_flush_time:
            self._queue_synced(t, self.set_deadline_cmd,
                               [self._mclock32(t), self.deadline_ramp_ms])
            self.deadline_armed = True
        k = self.override_k
        self.expect = (MODE_NORMAL,) if k >= self.k_one else (MODE_SCALED,)
        # Behind all steps already flushed, so the MCU never resumes ahead
        # of steps still in the host's queues
        self._queue_synced(t, self.set_rate_cmd, [k, self.resume_ramp_ms])
        # Flush at the current horizon (no new motion) to transmit them
        mq._advance_flush_time(t)
        self._start_polling()
    def _realtime_hold(self, line):
        self.hold()
    def _realtime_resume(self, line):
        self.resume()
    def _realtime_rate(self, line):
        import re
        m = re.search(r'RATE=([0-9.]+)', line.upper())
        r = re.search(r'RAMP_MS=([0-9]+)', line.upper())
        if m:
            self.set_override(float(m.group(1)), int(r.group(1)) if r else None)
    def _handle_web_rate(self, web_request):
        self.set_override(web_request.get_float('rate'),
                          web_request.get_int('ramp_ms', None))
    def _handle_web_hold(self, web_request):
        self.hold(web_request.get_int('ramp_ms', None))
    def _handle_web_resume(self, web_request):
        self.resume(web_request.get_int('ramp_ms', None))
    cmd_MOTION_HOLD_help = "Ramp motion to a stop on the planned path"
    def cmd_MOTION_HOLD(self, gcmd):
        self.hold(gcmd.get_int('RAMP_MS', None, minval=1, maxval=60000))
    cmd_MOTION_RESUME_help = "Resume held motion"
    def cmd_MOTION_RESUME(self, gcmd):
        self.resume(gcmd.get_int('RAMP_MS', None, minval=1, maxval=60000))
    cmd_MOTION_RATE_help = "Set the motion rate override (0 < RATE <= 1)"
    def cmd_MOTION_RATE(self, gcmd):
        self.set_override(gcmd.get_float('RATE', above=0., maxval=1.),
                          gcmd.get_int('RAMP_MS', None, minval=1, maxval=60000))
    cmd_MOTION_CLOCK_STATUS_help = "Report the MCU motion clock state"
    def cmd_MOTION_CLOCK_STATUS(self, gcmd):
        eventtime = self.reactor.monotonic()
        if self.state is None:
            raise gcmd.error("motion_clock: no state from the MCU yet")
        mode, k, clock, offset = self.state
        gcmd.respond_info(
            "motion_clock: mode=%s k=%.4f override=%.4f offset=%.6f s"
            " applied=%.6f s"
            " lag=%.6f s deadline=%s" % (
                MODE_NAMES.get(mode, mode), float(k) / self.k_one,
                float(self.override_k) / self.k_one, offset / self.freq, self.applied_offset / self.freq,
                self._motion_lag(eventtime),
                "armed" if self.deadline_armed else "off"))
    def get_status(self, eventtime):
        if self.state is None:
            return {'mode': 'unknown'}
        mode, k, clock, offset = self.state
        return {'mode': MODE_NAMES.get(mode, mode),
                'rate': float(k) / self.k_one,
                'override': float(self.override_k) / self.k_one,
                'lag': self._motion_lag(eventtime),
                'offset': offset / self.freq}

def load_config(config):
    return MotionClock(config)
