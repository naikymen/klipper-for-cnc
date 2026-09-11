# Directional G38 probe support
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
from .homing import HomingMove


class ProbeG38:
    def __init__(self, config, endstop_name='probe'):
        self.printer = config.get_printer()
        self.recovery_time = config.getfloat('recovery_time', 0.4, minval=0.)
        # Create the probe endstop from the configured pin.
        self.mcu_endstop = self.printer.lookup_object('pins').setup_pin(
            'endstop', config.get('pin'))
        # Expose the probe through QUERY_ENDSTOPS / M119.
        self.endstop_name = endstop_name
        query_endstops = self.printer.load_object(config, 'query_endstops')
        query_endstops.register_endstop(self.mcu_endstop, self.endstop_name)
        # Register g-code commands.
        self.gcode = self.printer.lookup_object('gcode')
        self.toolhead = None
        self.gcode_move = None
        self.register_commands()
        self.printer.register_event_handler('klippy:mcu_identify',
                                            self._handle_mcu_identify)

    def register_commands(self):
        gcode = self.gcode
        gcode.register_command('G38.2', self.cmd_G38_2,
                               when_not_ready=False, desc=self.cmd_G38_2_help)
        gcode.register_command('G38.3', self.cmd_G38_3,
                               when_not_ready=False, desc=self.cmd_G38_3_help)
        gcode.register_command('G38.4', self.cmd_G38_4,
                               when_not_ready=False, desc=self.cmd_G38_4_help)
        gcode.register_command('G38.5', self.cmd_G38_5,
                               when_not_ready=False, desc=self.cmd_G38_5_help)
        gcode.register_command('QUERY_PROBE', self.cmd_QUERY_PROBE,
                               desc=self.cmd_QUERY_PROBE_help)

    def _handle_mcu_identify(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.gcode_move = self.printer.lookup_object('gcode_move')
        # Associate the X, Y and Z steppers with the probe endstop so that
        # HomingMove can stop the move when the probe triggers.
        kin = self.toolhead.get_kinematics()
        for stepper in kin.get_steppers():
            if (stepper.is_active_axis('x')
                    or stepper.is_active_axis('y')
                    or stepper.is_active_axis('z')):
                self.mcu_endstop.add_stepper(stepper)

    cmd_QUERY_PROBE_help = "Return the status of the G38 probe"
    def cmd_QUERY_PROBE(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        res = self.mcu_endstop.query_endstop(print_time)
        gcmd.respond_info("%s: %s" % (self.endstop_name,
                                      ["open", "TRIGGERED"][not not res],))

    def _get_probe_move(self, gcmd):
        # Parse the target position and feedrate for a G38 move, mirroring
        # the coordinate handling of G1 (absolute/relative and base position).
        params = gcmd.get_command_parameters()
        pos = list(self.toolhead.get_position())
        base_position = self.gcode_move.base_position
        absolute_coord = self.gcode_move.absolute_coord
        probe_axes = []
        try:
            for axis, index in self.gcode_move.axis_map.items():
                if axis not in 'XYZ':
                    continue
                if axis in params:
                    v = float(params[axis])
                    if not absolute_coord:
                        pos[index] += v
                    else:
                        pos[index] = v + base_position[index]
                    probe_axes.append(axis.lower())
            speed = self.gcode_move.speed
            if 'F' in params:
                gcode_speed = float(params['F'])
                if gcode_speed <= 0.:
                    raise gcmd.error("Invalid speed in '%s'"
                                     % (gcmd.get_commandline(),))
                speed = gcode_speed * self.gcode_move.speed_factor
        except ValueError:
            raise gcmd.error("Unable to parse probe move '%s'"
                             % (gcmd.get_commandline(),))
        if not probe_axes:
            raise gcmd.error("G38 requires at least one of X, Y, or Z")
        return pos, speed, probe_axes

    def _check_no_movement(self, hmove, probe_axes):
        if self.printer.get_start_args().get('debuginput') is not None:
            return None
        for sp in hmove.stepper_positions:
            if sp.start_pos != sp.trig_pos:
                continue
            sp_name = sp.stepper_name
            if any(axis in sp_name.lower() for axis in probe_axes):
                return sp.endstop_name
        return None

    def _probe(self, gcmd, error_out, trigger_invert):
        pos, speed, probe_axes = self._get_probe_move(gcmd)
        if self.recovery_time:
            self.toolhead.dwell(self.recovery_time)
        hmove = HomingMove(self.printer, [(self.mcu_endstop,
                                           self.endstop_name)])
        try:
            epos = hmove.homing_move(pos, speed, probe_pos=True,
                                     triggered=trigger_invert,
                                     check_triggered=error_out)
        except self.printer.command_error:
            if self.printer.is_shutdown():
                raise self.printer.command_error(
                    "Probing failed due to printer shutdown")
            raise
        if self._check_no_movement(hmove, probe_axes) is not None:
            raise self.printer.command_error("Probe triggered prior to movement")
        # Report the trigger position.
        haltpos = self.toolhead.get_position()
        status = "probe trigger"
        if haltpos == pos:
            status = "probe ended without trigger"
        msg = " ".join(["%s=%.3f" % (axis.lower(), epos[index])
                        for axis, index in self.gcode_move.axis_map.items()])
        self.gcode.respond_info("%s at %s" % (status, msg))
        return epos

    cmd_G38_2_help = "Probe toward workpiece, stop on contact, error on failure"
    def cmd_G38_2(self, gcmd):
        self._probe(gcmd, error_out=True, trigger_invert=True)

    cmd_G38_3_help = "Probe toward workpiece, stop on contact"
    def cmd_G38_3(self, gcmd):
        self._probe(gcmd, error_out=False, trigger_invert=True)

    cmd_G38_4_help = "Probe away from workpiece, stop on loss of contact, error on failure"
    def cmd_G38_4(self, gcmd):
        self._probe(gcmd, error_out=True, trigger_invert=False)

    cmd_G38_5_help = "Probe away from workpiece, stop on loss of contact"
    def cmd_G38_5(self, gcmd):
        self._probe(gcmd, error_out=False, trigger_invert=False)


def load_config(config):
    return ProbeG38(config)
