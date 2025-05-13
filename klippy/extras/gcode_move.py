# G-Code G1 movement commands (and associated coordinate manipulation)
#
# Copyright (C) 2016-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, klippy
from gcode import GCodeDispatch, GCodeCommand
from extras.homing import Homing
from copy import copy

class GCodeMove:
    """Main GCodeMove class.

    Example config:

    [printer]
    kinematics: cartesian
    axis: XYZ  # Optional: XYZ or XYZABC
    kinematics_abc: cartesian_abc # Optional
    max_velocity: 5000
    max_z_velocity: 250
    max_accel: 1000

    TODO:
      - The "checks" still have the XYZ logic.
      - Homing is not implemented for ABC.
    """
    def __init__(self, config):
        # NOTE: amount of non-extruder axes: XYZ=3, XYZABC=6.
        # TODO: cmd_M114 only supports 3 or 6 for now.
        # TODO: find a way to get the axis value from the config, this does not work.
        # self.axis_names = config.get('axis', 'XYZABC')  # "XYZ" / "XYZABC"
        # self.axis_names = kwargs.get("axis", "XYZ")  # "XYZ" / "XYZABC"
        main_config = config.getsection("printer")
        self.axis_names = main_config.get('axis', 'XYZ')
        self.axis_count = len(self.axis_names)

        # Skip relative E offset on GCODE restore if requested.
        self.relative_e_restore = main_config.getboolean('relative_e_restore', True)

        # Axis sets and names for them are partially hardcoded all around.
        self.axis_triplets = ["XYZ", "ABC", "UVW"]
        self.axis_letters = "".join(self.axis_triplets)
        # Find the minimum amount of axes needed for the requested axis triplets.
        # For example, 1 triplet would be required for "XYZ" or "ABC", but 2
        # triplets are needed for any mixing of those (e.g. "XYZAB").
        self.min_axes = 3 * sum([ 1 for axset in self.axis_triplets if set(self.axis_names).intersection(axset) ])

        # Length for the position vector, matching the required axis,
        # plus 1 for the extruder axis (even if it is a dummy one).
        self.pos_length = self.min_axes + 1
        # self.pos_length = self.axis_count + 1
        # NOTE: The value of this attriute must match the one at "toolhead.py".

        # Dictionary to map axes to their indexes in the position vector.
        # Examples:
        #   {'X': 0, 'Y': 1, 'Z': 2, 'A': 3, 'B': 4, 'C': 5, 'E': 6}
        #   {'X': 0, 'Y': 1, 'Z': 2, 'E': 3}
        self.axis_map = {a: i for i, a in enumerate(list(self.axis_letters)[:self.min_axes] + ["E"])}

        logging.info(f"GCodeMove: starting setup with axis_names='{self.axis_names}' and axis_map: '{self.axis_map}'")

        printer = config.get_printer()
        self.printer: klippy.Printer = printer
        # NOTE: Event prefixes are not neeeded here, because the init class
        #       in the "extra toolhead" version of GcodeMove overrides this one.
        #       This one will only be used by the main "klippy.py pipeline".
        printer.register_event_handler("klippy:ready", self._handle_ready)
        printer.register_event_handler("klippy:shutdown", self._handle_shutdown)
        printer.register_event_handler("toolhead:set_position",
                                       self.reset_last_position)
        printer.register_event_handler("toolhead:manual_move",
                                       self.reset_last_position)
        printer.register_event_handler("gcode:command_error",
                                       self.reset_last_position)
        printer.register_event_handler("extruder:activate_extruder",
                                       self._handle_activate_extruder)
        printer.register_event_handler("homing:home_rails_end",
                                       self._handle_home_rails_end)
        self.is_printer_ready = False

        # Register g-code commands
        gcode: GCodeDispatch = printer.lookup_object('gcode')
        handlers = [
            'G1', 'G20', 'G21',
            'M82', 'M83', 'G90', 'G91', 'G92', 'M220', 'M221',
            'SET_GCODE_OFFSET', 'SAVE_GCODE_STATE', 'RESTORE_GCODE_STATE'
        ]
        # NOTE: this iterates over the commands above and finds the functions
        #       and description strings by their names (as they appear in "handlers").
        for cmd in handlers:
            func = getattr(self, 'cmd_' + cmd)
            desc = getattr(self, 'cmd_' + cmd + '_help', None)
            gcode.register_command(cmd, func, when_not_ready=False, desc=desc)

        # Register G0 as an alias for G1.
        # TODO: Re-implement G0 as a proper "fast/non-contact move".
        gcode.register_command('G0', self.cmd_G1, when_not_ready=False, desc=self.cmd_G0_help)

        # NOTE: These commands require `when_not_ready=True`.
        gcode.register_command('M114', self.cmd_M114, when_not_ready=True)
        gcode.register_command('GET_POSITION', self.cmd_GET_POSITION, when_not_ready=True,
                               desc=self.cmd_GET_POSITION_help)

        self.Coord = gcode.Coord

        # G-Code coordinate manipulation
        self.absolute_coord = self.absolute_extrude = True
        # NOTE: The length of these vectors must match the
        #       "commanded_pos" attribute in "toolhead.py".
        self.base_position = [0.0 for i in range(self.pos_length)]
        self.last_position = self.base_position.copy()
        self.homing_position = self.base_position.copy()
        self.speed = 25.
        # TODO: This 1/60 by default, because "feedrates"
        #       provided by the "F" GCODE are in "mm/min",
        #       which contrasts with the usual "mm/sec" unit
        #       used throughout Klipper.
        self.speed_factor = 1. / 60.
        self.extrude_factor = 1.

        # G-Code state
        self.saved_states = {}
        self.move_transform = self.move_with_transform = None
        # NOTE: Default function for "position_with_transform",
        #       overriden later on by "_handle_ready" (which sets
        #       toolhead.get_position) or "set_move_transform".
        self.position_with_transform = (lambda: [0.0 for i in range(self.pos_length)])

    def _handle_ready(self):
        self.is_printer_ready = True
        if self.move_transform is None:
            toolhead = self.printer.lookup_object('toolhead')
            self.move_with_transform = toolhead.move
            self.position_with_transform = toolhead.get_position
        self.reset_last_position()

    def _handle_shutdown(self):
        if not self.is_printer_ready:
            return
        self.is_printer_ready = False
        logging.info("gcode state: absolute_coord=%s absolute_extrude=%s"
                     " base_position=%s last_position=%s homing_position=%s"
                     " speed_factor=%s extrude_factor=%s speed=%s",
                     self.absolute_coord, self.absolute_extrude,
                     self.base_position, self.last_position,
                     self.homing_position, self.speed_factor,
                     self.extrude_factor, self.speed)

    def _handle_activate_extruder(self):
        # NOTE: the "reset_last_position" method overwrites "last_position"
        #       with the position returned by "position_with_transform",
        #       which is apparently "toolhead.get_position", returning the
        #       toolhead's "commanded_pos".
        #       This seems reasonable because the fourth coordinate of "commanded_pos"
        #       would have just been set to the "last position" of the new extruder
        #       (by the cmd_ACTIVATE_EXTRUDER method in "extruder.py").
        # TODO: find out if this can fail when the printer is "not ready".
        self.reset_last_position()

        # TODO: why would the factor be set to 1 here?
        self.extrude_factor = 1.

        # TODO: why would the base position be set to the last position of
        #       the new extruder?
        # NOTE: Commented the following line, which was effectively like
        #       running "G92 E0". It was meant to "support main slicers",
        #       but no checking was done.
        #       See discussion at: https://klipper.discourse.group/t/6558
        # self.base_position[3] = self.last_position[3]

    def _handle_home_rails_end(self, homing_state: Homing, rails):
        """Triggered by the home_rails method in homing.py after the homing moves complete"""
        self.reset_last_position()
        for axis in homing_state.get_axes():
            self.base_position[axis] = self.homing_position[axis]

    def set_move_transform(self, transform, force=False):
        # NOTE: This method is called by bed_mesh, bed_tilt,
        #       skewcorrection, etc. to set a special move
        #       transformation function. By default the
        #       "move_with_transform" function is "toolhead.move".
        if self.move_transform is not None and not force:
            raise self.printer.config_error(
                "G-Code move transform already specified")
        old_transform = self.move_transform
        if old_transform is None:
            old_transform = self.printer.lookup_object('toolhead', None)
        self.move_transform = transform
        self.move_with_transform = transform.move
        self.position_with_transform = transform.get_position
        return old_transform

    def _get_gcode_position(self):
        p = [lp - bp for lp, bp in zip(self.last_position, self.base_position)]
        p[-1] /= self.extrude_factor
        return p

    def _get_gcode_speed(self):
        return self.speed / self.speed_factor

    def _get_gcode_speed_override(self):
        return self.speed_factor * 60.

    def get_status(self, eventtime=None):
        move_position = self._get_gcode_position()
        return {
            'speed_factor': self._get_gcode_speed_override(),
            'speed': self._get_gcode_speed(),
            'extrude_factor': self.extrude_factor,
            'absolute_coordinates': self.absolute_coord,
            'absolute_extrude': self.absolute_extrude,
            # NOTE: Ensure that the extruder coordinate is passed properly.
            'homing_origin': self.Coord(*self.homing_position[:-1], e=self.homing_position[-1]),
            'position': self.Coord(*self.last_position[:-1], e=self.last_position[-1]),
            'gcode_position': self.Coord(*move_position[:-1], e=move_position[-1]),
        }

    def reset_last_position(self):
        # NOTE: Handler for "toolhead:set_position" and other events.
        #       Also called by "_handle_activate_extruder" (and other methods).
        logging.info(f"gcode_move.reset_last_position: triggered.")
        if self.is_printer_ready:
            # NOTE: The "position_with_transform" method is actually either "transform.get_position",
            #       "toolhead.get_position", or a default function returning "0.0" for each axis.
            self.last_position = self.position_with_transform()
            logging.info(f"gcode_move.reset_last_position: set self.last_position={self.last_position}")
        else:
            logging.info(f"gcode_move.reset_last_position: printer not ready self.last_position={self.last_position} not updated.")

    # G-Code movement commands
    cmd_G1_help = "Linear move to a specified position with a controlled feedrate."
    cmd_G0_help = "Command alias for G1."
    def cmd_G1(self, gcmd):

        # Move
        params = gcmd.get_command_parameters()
        logging.info(f"GCodeMove: G1 starting setup with params={params}")
        logging.info(f"GCodeMove: current G1 modes are absolute_coord={self.absolute_coord} and absolute_extrude={self.absolute_extrude}")
        try:
            # NOTE: XYZ(ABC) move coordinates.
            for pos, axis in enumerate(list(self.axis_map)[:-1]):
                if axis in params:
                    if axis not in self.axis_names:
                        raise self.printer.command_error(f"G1 error: you must configure the {axis} axis in order to use it.")
                    v = float(params[axis])
                    logging.info(f"GCodeMove: parsed axis={axis} with value={v}")
                    if not self.absolute_coord:
                        # Relative move, with value relative to position of last move.
                        self.last_position[pos] += v
                    else:
                        # Absolute move, with value relative to base coordinate position.
                        self.last_position[pos] = v + self.base_position[pos]
            # NOTE: extruder move coordinates.
            if 'E' in params:
                v = float(params['E']) * self.extrude_factor
                logging.info(f"GCodeMove: parsed axis=E with value={v}")
                if not self.absolute_coord or not self.absolute_extrude:
                    # Relative move, with value relative to position of last move.
                    self.last_position[-1] += v
                else:
                    # Absolute move, with value relative to base coordinate position.
                    self.last_position[-1] = v + self.base_position[-1]
            # NOTE: move feedrate.
            if 'F' in params:
                gcode_speed = float(params['F'])
                if gcode_speed <= 0.:
                    raise gcmd.error("Invalid speed in '%s'"
                                     % (gcmd.get_commandline(),))
                self.speed = gcode_speed * self.speed_factor

        except ValueError as e:
            raise gcmd.error("Unable to parse move '%s'"
                             % (gcmd.get_commandline(),))

        # NOTE: send event to handlers, like "extra_toolhead.py"
        self.printer.send_event("gcode_move:parsing_move_command", gcmd, params)

        # NOTE: This is just a call to "toolhead.move", unless a
        #       move "transform" is in between (e.g. a bed mesh).
        logging.info(f"GCodeMove: G1 moving to '{self.last_position}' at speed: {self.speed}")
        self.move_with_transform(self.last_position, self.speed)

    # G-Code coordinate manipulation
    cmd_G20_help = "Set units to inches."
    def cmd_G20(self, gcmd):
        # Set units to inches
        raise gcmd.error('Machine does not support G20 (inches) command')
    cmd_G21_help = "Set units to millimeters."
    def cmd_G21(self, gcmd):
        # Set units to millimeters
        pass
    cmd_M82_help = "Use absolute distances for extrusion."
    def cmd_M82(self, gcmd):
        # Use absolute distances for extrusion
        self.absolute_extrude = True
    cmd_M83_help = "Use relative distances for extrusion."
    def cmd_M83(self, gcmd):
        # Use relative distances for extrusion
        self.absolute_extrude = False
    cmd_G90_help = "Use absolute coordinates."
    def cmd_G90(self, gcmd):
        # Use absolute coordinates
        self.absolute_coord = True
    cmd_G91_help = "Use relative coordinates."
    def cmd_G91(self, gcmd):
        # Use relative coordinates
        self.absolute_coord = False
    cmd_G92_help = "Set position of the toolhead (i.e. set the gcode_base offsets)."
    def cmd_G92(self, gcmd):
        # Set position
        ax_names = list(self.axis_map)  # e.g.: ["X", "Y", "Z", "A", "E"]
        offsets = [ gcmd.get_float(a, None) for a in ax_names ]
        for i, offset in enumerate(offsets):
            if offset is not None:
                if i == len(offsets) - 1:
                    # NOTE: The last item holds info from the extruder.
                    offset *= self.extrude_factor
                # NOTE: Use the axis mapping to know what position element
                #       corresponds to particular given axis.
                pos_idx = self.axis_map[ax_names[i]]
                self.base_position[pos_idx] = self.last_position[pos_idx] - offset
        if all([v is None for v in offsets]):
            self.base_position = list(self.last_position)

    cmd_M114_help = "Get current position."
    def cmd_M114(self, gcmd):
        # Get Current Position
        pos = self._get_gcode_position()
        msg = " ".join([k.upper() + ":" + "%.3f" % pos[v] for k, v in self.axis_map.items() ])
        gcmd.respond_raw(copy(msg))

    cmd_M220_help = "Set speed factor override percentage."
    def cmd_M220(self, gcmd):
        # Set speed factor override percentage
        # NOTE: a value between "0" and "1/60".
        value = (gcmd.get_float('S', 100.0, above=0.0) / 100.0) / 60.0
        # NOTE: This is the same as:
        #           (self.speed / self.speed_factor) * value
        #       Since "self.speed_factor" has not yet been updated, it contains
        #       the older value. Dividing by the old factor must then remove its
        #       effect, and multiplying by the new one applies it.
        self.speed = self._get_gcode_speed() * value
        self.speed_factor = value

    cmd_M221_help = "Set extrude factor override percentage."
    def cmd_M221(self, gcmd):
        # Set extrude factor override percentage
        new_extrude_factor = gcmd.get_float('S', 100., above=0.) / 100.
        last_e_pos = self.last_position[-1]
        e_value = (last_e_pos - self.base_position[-1]) / self.extrude_factor
        self.base_position[-1] = last_e_pos - e_value * new_extrude_factor
        self.extrude_factor = new_extrude_factor

    cmd_SET_GCODE_OFFSET_help = "Set a virtual offset to g-code positions"
    def cmd_SET_GCODE_OFFSET(self, gcmd):
        move_delta = [0.0 for i in range(self.pos_length)]
        for pos, axis in enumerate(list(self.axis_map)):
            # Enumerate all axis names (e.g. X, Y, Z, A, B, C, E).
            offset = gcmd.get_float(axis, None)
            if offset is None:
                offset = gcmd.get_float(axis + '_ADJUST', None)
                if offset is None:
                    continue
                offset += self.homing_position[pos]
            delta = offset - self.homing_position[pos]
            move_delta[pos] = delta
            self.base_position[pos] += delta
            self.homing_position[pos] = offset
        # Move the toolhead the given offset if requested
        if gcmd.get_int('MOVE', 0):
            speed = gcmd.get_float('MOVE_SPEED', self.speed, above=0.)
            for pos, delta in enumerate(move_delta):
                self.last_position[pos] += delta
            self.move_with_transform(self.last_position, speed)

    cmd_SAVE_GCODE_STATE_help = "Save G-Code coordinate state"
    def cmd_SAVE_GCODE_STATE(self, gcmd):
        state_name = gcmd.get('NAME', 'default')
        self.saved_states[state_name] = {
            'absolute_coord': self.absolute_coord,
            'absolute_extrude': self.absolute_extrude,
            'base_position': list(self.base_position),
            'last_position': list(self.last_position),
            'homing_position': list(self.homing_position),
            'speed': self.speed, 'speed_factor': self.speed_factor,
            'extrude_factor': self.extrude_factor,
        }

    cmd_RESTORE_GCODE_STATE_help = "Restore a previously saved G-Code state"
    def cmd_RESTORE_GCODE_STATE(self, gcmd):
        state_name = gcmd.get('NAME', 'default')
        state = self.saved_states.get(state_name)
        if state is None:
            raise gcmd.error("Unknown g-code state: %s" % (state_name,))
        # Restore state
        self.absolute_coord = state['absolute_coord']
        self.absolute_extrude = state['absolute_extrude']
        self.base_position = list(state['base_position'])
        self.homing_position = list(state['homing_position'])
        self.speed = state['speed']
        self.speed_factor = state['speed_factor']
        self.extrude_factor = state['extrude_factor']
        # Restore the relative E position ().
        # NOTE: The default behaviour is equivalent to a G92 using the saved position of the extruder.
        #       The purpose of it is unclear (added in commit c54b8da530dc724b129066d1f3a825226926c5e6).
        # TODO: This behaviour causes issues with axis limits on home-able extruders,
        #       as revealed by moves done by Mainsail with the "_CLIENT" macros.
        #       It is now optional through a new parameter in the "[printer]" config section.
        e_diff = self.last_position[-1] - state['last_position'][-1]
        self.base_position[-1] += e_diff if self.relative_e_restore else 0
        # Move the toolhead back if requested
        if gcmd.get_int('MOVE', 0):
            speed = gcmd.get_float('MOVE_SPEED', self.speed, above=0.)
            self.last_position[:-1] = state['last_position'][:-1]
            self.move_with_transform(self.last_position, speed)

    cmd_GET_POSITION_help = (
        "Return information on the current location of the toolhead")
    def cmd_GET_POSITION(self, gcmd: GCodeCommand):

        # TODO: add ABC steppers to GET_POSITION.
        # TODO: add manual steppers to GET_POSITION.
        if self.axis_names != 'XYZ':
            gcmd.respond_info('cmd_GET_POSITION: Partial support for extruder position information. Only XYZABC is complete.')

        toolhead = self.printer.lookup_object('toolhead', None)

        if toolhead is None:
            raise gcmd.error("Printer not ready")

        # TODO: Add information from the extruder kinematic, if any.
        mcu_pos_list = []
        stepper_pos_list = []
        kin_pos_list = []
        for kin_name, kin in toolhead.kinematics.items():
            steppers = kin.get_steppers()
            # MCU
            mcu_pos = " ".join(["%s:%d" % (s.get_name(), s.get_mcu_position())
                                for s in steppers])
            mcu_pos_list.append(mcu_pos)
            # Stepper
            cinfo = [(s.get_name(), s.get_commanded_position()) for s in steppers]
            stepper_pos = " ".join(["%s:%.6f" % (a, v) for a, v in cinfo])
            stepper_pos_list.append(stepper_pos)
            # Kinematic
            kinfo = zip(kin.axis_names, kin.calc_position(dict(cinfo)))
            kin_pos = " ".join(["%s:%.6f" % (a, v) for a, v in kinfo])
            kin_pos_list.append(kin_pos)

        # Toolhead
        toolhead_coords = toolhead.get_position()
        toolhead_pos = " ".join(["%s:%.6f" % (a, toolhead_coords[self.axis_map[a]])
                                for a in self.axis_names + "E"])

        gcode_pos = " ".join(["%s:%.6f"  % (a, self.last_position[self.axis_map[a]])
                              for a in self.axis_names + "E"])

        base_pos = " ".join(["%s:%.6f"  % (a, self.base_position[self.axis_map[a]])
                             for a in self.axis_names + "E"])

        homing_pos = " ".join(["%s:%.6f"  % (a, self.homing_position[self.axis_map[a]])
                               for a in self.axis_names + "E"])

        gcmd.respond_info("mcu: %s\n"
                          "stepper: %s\n"
                          "kinematic: %s\n"
                          "toolhead: %s\n"
                          "gcode: %s\n"
                          "gcode base: %s\n"
                          "gcode homing: %s"
                          % (" ".join(mcu_pos_list),
                             " ".join(stepper_pos_list),
                             " ".join(kin_pos_list),
                             toolhead_pos,
                             gcode_pos, base_pos, homing_pos))

def load_config(config):
    return GCodeMove(config)
