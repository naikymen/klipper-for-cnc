# Code for handling printer nozzle extruders
#
# Copyright (C) 2016-2025  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, logging
import stepper, chelper
from extras import homing, force_move

class ExtruderStepper:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name().split()[-1]
        self.pressure_advance = self.pressure_advance_smooth_time = 0.
        self.config_pa = config.getfloat('pressure_advance', 0., minval=0.)
        self.config_smooth_time = config.getfloat(
                'pressure_advance_smooth_time', 0.040, above=0., maxval=.200)
        # Setup stepper. When an endstop is configured the stepper is built
        # from a rail so that it can be homed (see ExtruderHoming below).
        self.can_home = config.get('endstop_pin', None) is not None
        if self.can_home:
            self.rail = stepper.LookupRail(config)
            self.steppers = self.rail.get_steppers()
        else:
            self.rail = stepper.PrinterStepper(config)
            self.steppers = [self.rail]
        self.stepper = self.steppers[0]
        # Track homing limits as (min, max); an inverted range means not homed
        self.limits = [(1.0, -1.0)]
        ffi_main, ffi_lib = chelper.get_ffi()
        self.sk_extruder = ffi_main.gc(ffi_lib.extruder_stepper_alloc(),
                                       ffi_lib.extruder_stepper_free)
        self.stepper.set_stepper_kinematics(self.sk_extruder)
        self.motion_queue = None
        # Register commands
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        gcode = self.printer.lookup_object('gcode')
        if self.name == 'extruder':
            gcode.register_mux_command("SET_PRESSURE_ADVANCE", "EXTRUDER", None,
                                       self.cmd_default_SET_PRESSURE_ADVANCE,
                                       desc=self.cmd_SET_PRESSURE_ADVANCE_help)
        gcode.register_mux_command("SET_PRESSURE_ADVANCE", "EXTRUDER",
                                   self.name, self.cmd_SET_PRESSURE_ADVANCE,
                                   desc=self.cmd_SET_PRESSURE_ADVANCE_help)
        gcode.register_mux_command("SET_EXTRUDER_ROTATION_DISTANCE", "EXTRUDER",
                                   self.name, self.cmd_SET_E_ROTATION_DISTANCE,
                                   desc=self.cmd_SET_E_ROTATION_DISTANCE_help)
        gcode.register_mux_command("SYNC_EXTRUDER_MOTION", "EXTRUDER",
                                   self.name, self.cmd_SYNC_EXTRUDER_MOTION,
                                   desc=self.cmd_SYNC_EXTRUDER_MOTION_help)
    def _handle_connect(self):
        self._set_pressure_advance(self.config_pa, self.config_smooth_time)
    def get_status(self, eventtime):
        homed_axes = ""
        if self.can_home:
            homed_axes = "".join(a for a, (l, h) in zip("e", self.limits)
                                 if l <= h)
        return {'pressure_advance': self.pressure_advance,
                'smooth_time': self.pressure_advance_smooth_time,
                'motion_queue': self.motion_queue,
                'homed_axes': homed_axes}
    def set_position(self, newpos_e, homing_e=False):
        self.rail.set_position([newpos_e, 0., 0.])
        if homing_e and self.can_home:
            self.limits[0] = self.rail.get_range()
    def clear_homing_state(self, clear_axes):
        if self.can_home and 'e' in clear_axes.lower():
            self.limits = [(1.0, -1.0)]
    def check_move_limits(self, move, ea_index):
        if not self.can_home:
            return
        epos = move.end_pos[ea_index]
        l, h = self.limits[0]
        if l <= epos <= h:
            return
        if l > h:
            raise move.move_error("Must home extruder axis first")
        if move.toolhead.are_limits_enabled():
            raise move.move_error()
    def find_past_position(self, print_time):
        mcu_pos = self.stepper.get_past_mcu_position(print_time)
        return self.stepper.mcu_to_commanded_position(mcu_pos)
    def sync_to_extruder(self, extruder_name):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        motion_queuing = self.printer.lookup_object('motion_queuing')
        if not extruder_name:
            self.stepper.set_trapq(None)
            self.motion_queue = None
            motion_queuing.check_step_generation_scan_windows()
            return
        extruder = self.printer.lookup_object(extruder_name, None)
        if extruder is None or not isinstance(extruder, PrinterExtruder):
            raise self.printer.command_error("'%s' is not a valid extruder."
                                             % (extruder_name,))
        self.stepper.set_position([extruder.last_position, 0., 0.])
        self.stepper.set_trapq(extruder.get_trapq())
        self.motion_queue = extruder_name
        motion_queuing.check_step_generation_scan_windows()
    def _set_pressure_advance(self, pressure_advance, smooth_time):
        old_smooth_time = self.pressure_advance_smooth_time
        if not self.pressure_advance:
            old_smooth_time = 0.
        new_smooth_time = smooth_time
        if not pressure_advance:
            new_smooth_time = 0.
        toolhead = self.printer.lookup_object("toolhead")
        ffi_main, ffi_lib = chelper.get_ffi()
        espa = ffi_lib.extruder_set_pressure_advance
        if new_smooth_time != old_smooth_time:
            # Need full kinematic flush to change the smooth time
            toolhead.flush_step_generation()
            espa(self.sk_extruder, 0., pressure_advance, new_smooth_time)
            motion_queuing = self.printer.lookup_object('motion_queuing')
            motion_queuing.check_step_generation_scan_windows()
        else:
            toolhead.register_lookahead_callback(
                lambda print_time: espa(self.sk_extruder, print_time,
                                        pressure_advance, new_smooth_time))
        self.pressure_advance = pressure_advance
        self.pressure_advance_smooth_time = smooth_time
    cmd_SET_PRESSURE_ADVANCE_help = "Set pressure advance parameters"
    def cmd_default_SET_PRESSURE_ADVANCE(self, gcmd):
        extruder = self.printer.lookup_object('toolhead').get_extruder()
        if extruder.extruder_stepper is None:
            raise gcmd.error("Active extruder does not have a stepper")
        strapq = extruder.extruder_stepper.stepper.get_trapq()
        if strapq is not extruder.get_trapq():
            raise gcmd.error("Unable to infer active extruder stepper")
        extruder.extruder_stepper.cmd_SET_PRESSURE_ADVANCE(gcmd)
    def cmd_SET_PRESSURE_ADVANCE(self, gcmd):
        pressure_advance = gcmd.get_float('ADVANCE', self.pressure_advance,
                                          minval=0.)
        smooth_time = gcmd.get_float('SMOOTH_TIME',
                                     self.pressure_advance_smooth_time,
                                     minval=0., maxval=.200)
        self._set_pressure_advance(pressure_advance, smooth_time)
        msg = ("pressure_advance: %.6f\n"
               "pressure_advance_smooth_time: %.6f"
               % (pressure_advance, smooth_time))
        self.printer.set_rollover_info(self.name, "%s: %s" % (self.name, msg))
        gcmd.respond_info(msg, log=False)
    cmd_SET_E_ROTATION_DISTANCE_help = "Set extruder rotation distance"
    def cmd_SET_E_ROTATION_DISTANCE(self, gcmd):
        rotation_dist = gcmd.get_float('DISTANCE', None)
        if rotation_dist is not None:
            if not rotation_dist:
                raise gcmd.error("Rotation distance can not be zero")
            invert_dir, orig_invert_dir = self.stepper.get_dir_inverted()
            next_invert_dir = orig_invert_dir
            if rotation_dist < 0.:
                next_invert_dir = not orig_invert_dir
                rotation_dist = -rotation_dist
            toolhead = self.printer.lookup_object('toolhead')
            toolhead.flush_step_generation()
            self.stepper.set_rotation_distance(rotation_dist)
            self.stepper.set_dir_inverted(next_invert_dir)
        else:
            rotation_dist, spr = self.stepper.get_rotation_distance()
        invert_dir, orig_invert_dir = self.stepper.get_dir_inverted()
        if invert_dir != orig_invert_dir:
            rotation_dist = -rotation_dist
        gcmd.respond_info("Extruder '%s' rotation distance set to %0.6f"
                          % (self.name, rotation_dist))
    cmd_SYNC_EXTRUDER_MOTION_help = "Set extruder stepper motion queue"
    def cmd_SYNC_EXTRUDER_MOTION(self, gcmd):
        ename = gcmd.get('MOTION_QUEUE')
        self.sync_to_extruder(ename)
        gcmd.respond_info("Extruder '%s' now syncing with '%s'"
                          % (self.name, ename))

# Tracking for hotend heater, extrusion motion queue, and extruder stepper
class PrinterExtruder:
    def __init__(self, config, extruder_num):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.last_position = 0.
        # Setup hotend heater
        pheaters = self.printer.load_object(config, 'heaters')
        gcode_id = 'T%d' % (extruder_num,)
        self.heater = pheaters.setup_heater(config, gcode_id)
        # Setup kinematic checks
        self.nozzle_diameter = config.getfloat('nozzle_diameter', above=0.)
        filament_diameter = config.getfloat(
            'filament_diameter', minval=self.nozzle_diameter)
        self.filament_area = math.pi * (filament_diameter * .5)**2
        def_max_cross_section = 4. * self.nozzle_diameter**2
        def_max_extrude_ratio = def_max_cross_section / self.filament_area
        max_cross_section = config.getfloat(
            'max_extrude_cross_section', def_max_cross_section, above=0.)
        self.max_extrude_ratio = max_cross_section / self.filament_area
        logging.info("Extruder max_extrude_ratio=%.6f", self.max_extrude_ratio)
        toolhead = self.printer.lookup_object('toolhead')
        max_velocity, max_accel = toolhead.get_max_velocity()
        self.max_e_velocity = config.getfloat(
            'max_extrude_only_velocity', max_velocity * def_max_extrude_ratio
            , above=0.)
        self.max_e_accel = config.getfloat(
            'max_extrude_only_accel', max_accel * def_max_extrude_ratio
            , above=0.)
        self.max_e_dist = config.getfloat(
            'max_extrude_only_distance', 50., minval=0.)
        self.instant_corner_v = config.getfloat(
            'instantaneous_corner_velocity', 1., minval=0.)
        # Setup extruder trapq (trapezoidal motion queue)
        self.motion_queuing = self.printer.load_object(config, 'motion_queuing')
        self.trapq = self.motion_queuing.allocate_trapq()
        self.trapq_append = self.motion_queuing.lookup_trapq_append()
        # Setup extruder stepper
        self.extruder_stepper = None
        self.extruder_homing = None
        self.can_home = False
        if (config.get('step_pin', None) is not None
            or config.get('dir_pin', None) is not None
            or config.get('rotation_distance', None) is not None):
            self.extruder_stepper = ExtruderStepper(config)
            self.extruder_stepper.stepper.set_trapq(self.trapq)
            if self.extruder_stepper.can_home:
                self.extruder_homing = ExtruderHoming(self)
                self.can_home = True
        # Register commands
        gcode = self.printer.lookup_object('gcode')
        if self.name == 'extruder':
            toolhead.set_extruder(self, 0.)
            gcode.register_command("M104", self.cmd_M104)
            gcode.register_command("M109", self.cmd_M109)
        gcode.register_mux_command("ACTIVATE_EXTRUDER", "EXTRUDER",
                                   self.name, self.cmd_ACTIVATE_EXTRUDER,
                                   desc=self.cmd_ACTIVATE_EXTRUDER_help)
    def get_status(self, eventtime):
        sts = self.heater.get_status(eventtime)
        sts['can_extrude'] = self.heater.can_extrude
        if self.extruder_stepper is not None:
            sts.update(self.extruder_stepper.get_status(eventtime))
        return sts
    def get_name(self):
        return self.name
    def get_heater(self):
        return self.heater
    def get_trapq(self):
        return self.trapq
    def get_axis_gcode_id(self):
        return 'E'
    # Homing support (only meaningful when the extruder stepper can home)
    def home(self, homing_state):
        if self.extruder_homing is None:
            raise self.printer.command_error(
                "No endstop for extruder '%s'" % (self.name,))
        self.extruder_homing.home(homing_state)
    def get_steppers(self):
        if self.extruder_stepper is None:
            return []
        return self.extruder_stepper.steppers
    def calc_position(self, stepper_positions):
        return [stepper_positions[self.extruder_stepper.rail.get_name()],
                0., 0.]
    def get_range(self):
        return self.extruder_stepper.rail.get_range()
    def get_homing_info(self):
        return self.extruder_stepper.rail.get_homing_info()
    def get_endstops(self):
        return self.extruder_stepper.rail.get_endstops()
    def set_position(self, newpos_e, homing_axes="", print_time=None):
        toolhead = self.printer.lookup_object('toolhead')
        if print_time is None:
            toolhead.flush_step_generation()
            print_time = toolhead.get_last_move_time()
        ffi_main, ffi_lib = chelper.get_ffi()
        ffi_lib.trapq_set_position(self.trapq, print_time, newpos_e, 0., 0.)
        self.last_position = newpos_e
        if self.extruder_stepper is not None:
            self.extruder_stepper.set_position(
                newpos_e, 'e' in homing_axes.lower())
        toolhead.set_extra_axis_position(self, newpos_e)
    def clear_homing_state(self, clear_axes):
        if self.extruder_stepper is not None:
            self.extruder_stepper.clear_homing_state(clear_axes)
    def stats(self, eventtime):
        return self.heater.stats(eventtime)
    def check_move(self, move, ea_index):
        if self.extruder_stepper is not None:
            self.extruder_stepper.check_move_limits(move, ea_index)
        if not self.heater.can_extrude:
            raise self.printer.command_error(
                "Extrude below minimum temp\n"
                "See the 'min_extrude_temp' config option for details")
        axis_r = move.axes_r[ea_index]
        axis_d = move.axes_d[ea_index]
        if (not move.axes_d[0] and not move.axes_d[1]) or axis_r < 0.:
            # Extrude only move (or retraction move) - limit accel and velocity
            if abs(axis_d) > self.max_e_dist:
                raise self.printer.command_error(
                    "Extrude only move too long (%.3fmm vs %.3fmm)\n"
                    "See the 'max_extrude_only_distance' config"
                    " option for details" % (axis_d, self.max_e_dist))
            inv_extrude_r = 1. / abs(axis_r)
            move.limit_speed(self.max_e_velocity * inv_extrude_r,
                             self.max_e_accel * inv_extrude_r)
        elif axis_r > self.max_extrude_ratio:
            if axis_d <= self.nozzle_diameter * self.max_extrude_ratio:
                # Permit extrusion if amount extruded is tiny
                return
            area = axis_r * self.filament_area
            logging.debug("Overextrude: %s vs %s (area=%.3f dist=%.3f)",
                          axis_r, self.max_extrude_ratio, area, move.move_d)
            raise self.printer.command_error(
                "Move exceeds maximum extrusion (%.3fmm^2 vs %.3fmm^2)\n"
                "See the 'max_extrude_cross_section' config option for details"
                % (area, self.max_extrude_ratio * self.filament_area))
    def calc_junction(self, prev_move, move, ea_index):
        diff_r = move.axes_r[ea_index] - prev_move.axes_r[ea_index]
        if diff_r:
            return (self.instant_corner_v / abs(diff_r))**2
        return move.max_cruise_v2
    def process_move(self, print_time, move, ea_index):
        axis_r = move.axes_r[ea_index]
        accel = move.accel * axis_r
        start_v = move.start_v * axis_r
        cruise_v = move.cruise_v * axis_r
        can_pressure_advance = False
        if axis_r > 0. and (move.axes_d[0] or move.axes_d[1]):
            can_pressure_advance = True
        # Queue movement (x is extruder movement, y is pressure advance flag)
        self.trapq_append(self.trapq, print_time,
                          move.accel_t, move.cruise_t, move.decel_t,
                          move.start_pos[ea_index], 0., 0.,
                          1., can_pressure_advance, 0.,
                          start_v, cruise_v, accel)
        self.last_position = move.end_pos[ea_index]
    def find_past_position(self, print_time):
        if self.extruder_stepper is None:
            return 0.
        return self.extruder_stepper.find_past_position(print_time)
    def cmd_M104(self, gcmd, wait=False):
        # Set Extruder Temperature
        temp = gcmd.get_float('S', 0.)
        index = gcmd.get_int('T', None, minval=0)
        if index is not None:
            section = 'extruder'
            if index:
                section = 'extruder%d' % (index,)
            extruder = self.printer.lookup_object(section, None)
            if extruder is None:
                if temp <= 0.:
                    return
                raise gcmd.error("Extruder not configured")
        else:
            extruder = self.printer.lookup_object('toolhead').get_extruder()
        pheaters = self.printer.lookup_object('heaters')
        pheaters.set_temperature(extruder.get_heater(), temp, wait)
    def cmd_M109(self, gcmd):
        # Set Extruder Temperature and Wait
        self.cmd_M104(gcmd, wait=True)
    cmd_ACTIVATE_EXTRUDER_help = "Change the active extruder"
    def cmd_ACTIVATE_EXTRUDER(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        if toolhead.get_extruder() is self:
            gcmd.respond_info("Extruder %s already active" % (self.name,))
            return
        gcmd.respond_info("Activating extruder %s" % (self.name,))
        toolhead.flush_step_generation()
        toolhead.set_extruder(self, self.last_position)
        self.printer.send_event("extruder:activate_extruder")

# Dummy extruder class used when a printer has no extruder at all
class DummyExtruder:
    def __init__(self, printer):
        self.printer = printer
    def check_move(self, move, ea_index):
        raise move.move_error("Extrude when no extruder present")
    def find_past_position(self, print_time):
        return 0.
    def calc_junction(self, prev_move, move, ea_index):
        return move.max_cruise_v2
    def get_name(self):
        return ""
    def get_heater(self):
        raise self.printer.command_error("Extruder not configured")
    def get_trapq(self):
        return None
    def get_axis_gcode_id(self):
        return 'E'

# Adapter that drives an extruder's own trapq/timeline during homing, so that
# HomingMove can home the extruder against its endstop without disturbing the
# active toolhead's XYZ motion queue.
class ExtruderHoming:
    def __init__(self, extruder):
        self.printer = extruder.printer
        self.extruder = extruder
        toolhead = self.printer.lookup_object('toolhead')
        max_velocity, max_accel = toolhead.get_max_velocity()
        self.homing_accel = max_accel
        # Register commands
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command("HOME_EXTRUDER", "EXTRUDER",
                                   extruder.name, self.cmd_HOME_EXTRUDER,
                                   desc=self.cmd_HOME_EXTRUDER_help)
        if "HOME_ACTIVE_EXTRUDER" not in gcode.ready_gcode_handlers:
            gcode.register_command("HOME_ACTIVE_EXTRUDER",
                                   self.cmd_HOME_ACTIVE_EXTRUDER,
                                   desc=self.cmd_HOME_ACTIVE_EXTRUDER_help)
    def _submit_move(self, movetime, movepos, speed, accel):
        extruder = self.extruder
        cp = extruder.last_position
        axis_r, accel_t, cruise_t, cruise_v = force_move.calc_move_time(
            movepos - cp, speed, accel)
        # Use the extruder trapq convention: direction is encoded in the
        # signed velocity/accel, with axes_r_x fixed at 1.0. Each homing
        # move starts from rest, so start_v is zero.
        extruder.trapq_append(extruder.trapq, movetime,
                              accel_t, cruise_t, accel_t,
                              cp, 0., 0.,
                              1., 0., 0.,
                              0., cruise_v * axis_r, accel * axis_r)
        extruder.last_position = movepos
        return movetime + accel_t + cruise_t + accel_t
    def do_move(self, movepos, speed, accel):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        start_time = toolhead.get_last_move_time()
        end_time = self._submit_move(start_time, movepos, speed, accel)
        self.extruder.motion_queuing.note_mcu_movequeue_activity(end_time)
        toolhead.dwell(end_time - start_time)
    def do_set_position(self, setpos):
        self.extruder.set_position(setpos)
    def _do_homing_move(self, endstops, homepos, speed):
        hmove = homing.HomingMove(self.printer, endstops, self)
        hmove.homing_move([homepos, 0., 0., 0.], speed)
        return hmove
    def _retract_move(self, homing_info, forcepos, homepos):
        axes_d = homepos - forcepos
        move_d = abs(axes_d)
        retract_r = min(1., homing_info.retract_dist / move_d)
        retractpos = homepos - axes_d * retract_r
        self.do_move(retractpos, homing_info.retract_speed, self.homing_accel)
        startpos = retractpos - axes_d * retract_r
        self.do_set_position(startpos)
        return homepos
    def home(self, homing_state):
        extruder = self.extruder
        rail = extruder.extruder_stepper.rail
        position_min, position_max = rail.get_range()
        hi = rail.get_homing_info()
        homepos = hi.position_endstop
        if hi.positive_dir:
            forcepos = homepos - 1.5 * (homepos - position_min)
        else:
            forcepos = homepos + 1.5 * (position_max - homepos)
        # Notify of upcoming homing operation
        self.printer.send_event("homing:home_rails_begin", homing_state,
                                [rail])
        # Set the axis position to the start of the homing sweep
        self.do_set_position(forcepos)
        endstops = rail.get_endstops()
        # Perform first home
        hmove = self._do_homing_move(endstops, homepos, hi.speed)
        # Perform second home after retracting
        if hi.retract_dist:
            self._retract_move(hi, forcepos, homepos)
            hmove = self._do_homing_move(endstops, homepos,
                                         hi.second_homing_speed)
            if hmove.check_no_movement() is not None:
                raise self.printer.command_error(
                    "Endstop %s still triggered after retract"
                    % (hmove.check_no_movement(),))
        # Mark the extruder as homed at the final position left by the
        # homing move (which already accounts for any endstop overshoot)
        extruder.set_position(extruder.last_position, "e")
        homing_state.trigger_mcu_pos = {sp.stepper_name: sp.trig_pos
                                        for sp in hmove.stepper_positions}
        homing_state.adjust_pos = {}
        self.printer.send_event("homing:home_rails_end", homing_state,
                                [rail])
    cmd_HOME_EXTRUDER_help = "Home the extruder against its endstop"
    def cmd_HOME_EXTRUDER(self, gcmd):
        homing_state = homing.Homing(self.printer)
        # The extruder always occupies toolhead axis 3, whether or not it is
        # the active extruder (inactive extruders are absent from extra_axes).
        homing_state.set_axes([3])
        try:
            self.home(homing_state)
        except self.printer.command_error:
            if self.printer.is_shutdown():
                raise
            self.extruder.clear_homing_state("e")
            raise
    cmd_HOME_ACTIVE_EXTRUDER_help = ("Home the active extruder against its"
                                     " endstop")
    def cmd_HOME_ACTIVE_EXTRUDER(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        extruder = toolhead.get_extruder()
        if (not isinstance(extruder, PrinterExtruder)
                or extruder.extruder_homing is None):
            raise gcmd.error("Active extruder cannot be homed")
        extruder.extruder_homing.cmd_HOME_EXTRUDER(gcmd)
    # Toolhead wrappers to support HomingMove
    def flush_step_generation(self):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
    def get_position(self):
        return [self.extruder.last_position, 0., 0., 0.]
    def set_position(self, newpos, homing_axes=""):
        self.extruder.set_position(newpos[0], homing_axes)
    def get_last_move_time(self):
        toolhead = self.printer.lookup_object('toolhead')
        return toolhead.get_last_move_time()
    def dwell(self, delay):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.dwell(delay)
    def drip_move(self, newpos, speed, drip_completion):
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        start_time = toolhead.get_last_move_time()
        end_time = self._submit_move(start_time, newpos[0],
                                     speed, self.homing_accel)
        self.extruder.motion_queuing.drip_update_time(start_time, end_time,
                                                      drip_completion)
        self.extruder.motion_queuing.wipe_trapq(self.extruder.trapq)
    def get_kinematics(self):
        return self.extruder
    def get_steppers(self):
        return self.extruder.get_steppers()
    def calc_position(self, stepper_positions):
        return self.extruder.calc_position(stepper_positions)

def add_printer_objects(config):
    printer = config.get_printer()
    for i in range(99):
        section = 'extruder'
        if i:
            section = 'extruder%d' % (i,)
        if not config.has_section(section):
            break
        pe = PrinterExtruder(config.getsection(section), i)
        printer.add_object(section, pe)
