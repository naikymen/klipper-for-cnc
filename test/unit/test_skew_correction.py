#!/usr/bin/env python3
# Unit tests for SET_SKEW_FACTORS in extras/skew_correction.py
#
# The klippy test harness only inspects the exit status of a klippy run, so it
# cannot assert the error messages, the order in which the factors are
# assigned, or the arguments handed to gcode_move.  Those are exactly the
# places where the `develop` implementation went wrong (it stored the raw
# command string into the factor attribute, so the next move raised a
# TypeError, and it rebuilt the transform result as a four element list,
# dropping any extra axes), so PrinterSkew is instantiated here directly
# against stubs.
#
# Run with:  python3 test/unit/test_skew_correction.py
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, sys, unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, 'klippy'))

from extras import skew_correction


class StubGCode:
    def __init__(self):
        self.commands = {}
    def register_command(self, name, handler, when_not_ready=False, desc=None):
        self.commands[name] = handler


class StubGCodeMove:
    def __init__(self, skew):
        self.skew = skew
        self.reset_count = 0
        # The factors observed at the moment of the reset, which is the only
        # thing reset_last_position() can see.
        self.factors_at_reset = None
    def reset_last_position(self):
        self.reset_count += 1
        self.factors_at_reset = (self.skew.xy_factor, self.skew.xz_factor,
                                 self.skew.yz_factor)


class StubPrinter:
    def __init__(self):
        self.gcode = StubGCode()
        self.gcode_move = None
        self.event_handlers = {}
    def register_event_handler(self, event, handler):
        self.event_handlers[event] = handler
    def lookup_object(self, name):
        if name == 'gcode':
            return self.gcode
        if name == 'gcode_move':
            return self.gcode_move
        raise KeyError(name)


class StubConfig:
    def __init__(self, printer):
        self.printer = printer
    def get_printer(self):
        return self.printer
    def get_name(self):
        return 'skew_correction'
    def get_prefix_sections(self, prefix):
        return []


class CommandError(Exception):
    pass


class StubCommand:
    """Stands in for klippy.gcode.GCodeCommand."""
    def __init__(self, parameters, commandline="SET_SKEW_FACTORS"):
        self.parameters = parameters
        self.commandline = commandline
        self.responses = []
    def get_int(self, name, default=0, minval=None, maxval=None, **kwargs):
        for key, value in self.parameters.items():
            if key.upper() == name.upper():
                return int(float(value))
        return default
    def get(self, name, default=None, **kwargs):
        for key, value in self.parameters.items():
            if key.upper() == name.upper():
                return value
        return default
    def get_float(self, name, default=None, minval=None, maxval=None,
                  above=None, below=None, **kwargs):
        for key, value in self.parameters.items():
            if key.upper() == name.upper():
                try:
                    return float(value)
                except ValueError:
                    raise CommandError(
                        "Unable to parse parameter '%s'" % (key,))
        return default
    def get_commandline(self):
        return self.commandline
    def respond_info(self, msg):
        self.responses.append(msg)
    def error(self, msg):
        return CommandError(msg)


class SkewTestCase(unittest.TestCase):
    def setUp(self):
        self.printer = StubPrinter()
        self.skew = skew_correction.PrinterSkew(StubConfig(self.printer))
        self.gcode_move = StubGCodeMove(self.skew)
        self.printer.gcode_move = self.gcode_move

    def run_command(self, **parameters):
        gcmd = StubCommand(parameters)
        self.skew.cmd_SET_SKEW_FACTORS(gcmd)
        return gcmd

    def run_set_skew(self, **parameters):
        gcmd = StubCommand(parameters, commandline="SET_SKEW")
        self.skew.cmd_SET_SKEW(gcmd)
        return gcmd

    def assertFactors(self, xy, xz, yz):
        self.assertEqual((self.skew.xy_factor, self.skew.xz_factor,
                          self.skew.yz_factor), (xy, xz, yz))

    def test_registered(self):
        self.assertEqual(self.printer.gcode.commands['SET_SKEW_FACTORS'],
                         self.skew.cmd_SET_SKEW_FACTORS)

    def test_parses_float_not_string(self):
        # Regression: `develop` used gcmd.get(), which stores the raw string in
        # the factor, and the next move then raised a TypeError.
        self.run_command(XY='0.1')
        self.assertFactors(0.1, 0., 0.)
        for factor in (self.skew.xy_factor, self.skew.xz_factor,
                       self.skew.yz_factor):
            self.assertIsInstance(factor, float)

    def test_single_plane_leaves_others_alone(self):
        self.skew.xy_factor = 0.2
        self.skew.yz_factor = 0.3
        self.run_command(XZ='0.05')
        self.assertFactors(0.2, 0.05, 0.3)

    def test_all_planes_at_once(self):
        self.run_command(XY='0.1', XZ='0.2', YZ='0.3')
        self.assertFactors(0.1, 0.2, 0.3)
        self.assertEqual(self.gcode_move.reset_count, 1)

    def test_command_parameters_are_case_insensitive(self):
        self.run_command(xy='0.25')
        self.assertFactors(0.25, 0., 0.)

    def test_clear_resets_every_plane(self):
        self.run_command(XY='0.1', XZ='0.2', YZ='0.3')
        gcmd = self.run_command(CLEAR='1')
        self.assertFactors(0., 0., 0.)
        self.assertEqual(gcmd.responses, [])

    def test_factors_are_set_before_last_position_is_reset(self):
        # reset_last_position() records the new logical position through the
        # transform, so it must never observe a mixture of old and new factors.
        self.run_command(XY='0.1')
        self.assertEqual(self.gcode_move.factors_at_reset, (0.1, 0., 0.))

    def test_no_factor_is_an_error(self):
        with self.assertRaises(CommandError) as cm:
            self.run_command()
        self.assertIn("no skew factor given", str(cm.exception))
        self.assertEqual(self.gcode_move.reset_count, 0)

    def test_clear_only_is_not_an_error(self):
        self.run_command(CLEAR='1')

    def test_non_finite_factors_are_rejected(self):
        # gcmd.get_float() parses with float(), so nan/inf reach the command and
        # would poison every later transform.
        for plane in ('XY', 'XZ', 'YZ'):
            for value in ('nan', 'inf', '-inf'):
                with self.assertRaises(CommandError) as cm:
                    self.run_command(**{plane: value})
                self.assertIn("plane [%s] is not a finite number" % (plane,),
                              str(cm.exception))
        self.assertFactors(0., 0., 0.)
        self.assertEqual(self.gcode_move.reset_count, 0)

    def test_rejected_call_changes_nothing(self):
        self.skew.xy_factor = 0.4
        # A valid plane followed by a rejected one must not half-apply.
        with self.assertRaises(CommandError):
            self.run_command(XY='0.5', YZ='nan')
        self.assertFactors(0.4, 0., 0.)

    def test_non_numeric_factor_is_rejected(self):
        with self.assertRaises(CommandError):
            self.run_command(XY='not-a-number')
        self.assertFactors(0., 0., 0.)

    def test_set_skew_refreshes_last_position(self):
        # Regression: SET_SKEW assigned the factors plane by plane without
        # calling _update_skew(), so the transform changed underneath a stale
        # last_position and the next move was computed from the old factors.
        # The physical position is authoritative, so the refresh must see all
        # three new factors at once.
        self.run_set_skew(XY="140.4,142.8,99.8")
        self.assertEqual(self.gcode_move.reset_count, 1)
        factor = skew_correction.calc_skew_factor(140.4, 142.8, 99.8)
        self.assertAlmostEqual(self.skew.xy_factor, factor)
        self.assertEqual(self.gcode_move.factors_at_reset,
                         (self.skew.xy_factor, 0., 0.))

    def test_set_skew_leaves_unnamed_planes_alone(self):
        self.skew.xz_factor = 0.2
        self.skew.yz_factor = 0.3
        self.run_set_skew(XY="140.4,142.8,99.8")
        self.assertAlmostEqual(self.skew.xy_factor,
                               skew_correction.calc_skew_factor(
                                   140.4, 142.8, 99.8))
        self.assertEqual((self.skew.xz_factor, self.skew.yz_factor),
                         (0.2, 0.3))
        self.assertEqual(self.gcode_move.factors_at_reset,
                         (self.skew.xy_factor, 0.2, 0.3))

    def test_set_skew_clear_refreshes_last_position(self):
        self.skew.xy_factor = 0.1
        self.run_set_skew(CLEAR='1')
        self.assertFactors(0., 0., 0.)
        self.assertEqual(self.gcode_move.reset_count, 1)

    def test_set_skew_without_arguments_changes_nothing(self):
        self.skew.xy_factor = 0.1
        self.run_set_skew()
        self.assertFactors(0.1, 0., 0.)
        self.assertEqual(self.gcode_move.reset_count, 0)

    def test_set_skew_rejected_measurements_change_nothing(self):
        # Regression: the old per-plane setattr loop left the first plane
        # applied when the second one was malformed.
        self.skew.xy_factor = 0.4
        with self.assertRaises(CommandError) as cm:
            self.run_set_skew(XY="140.4,142.8")
        self.assertIn("improperly formatted entry for plane [XY]",
                      str(cm.exception))
        with self.assertRaises(CommandError):
            self.run_set_skew(XY="not-a-number")
        # A valid plane followed by a malformed one must not half-apply.
        with self.assertRaises(CommandError):
            self.run_set_skew(XZ="141.6,141.4,99.8", YZ="142.4,140.5")
        self.assertFactors(0.4, 0., 0.)
        self.assertEqual(self.gcode_move.reset_count, 0)

    def test_calc_skew_keeps_extra_axes(self):
        # Regression: `develop` returned a hardcoded four element list, which
        # dropped any axis beyond E.
        self.skew.xy_factor = 0.1
        self.skew.xz_factor = 0.2
        self.skew.yz_factor = 0.3
        position = [10., 20., 30., 40., 50., 60.]
        skewed = self.skew.calc_skew(position)
        self.assertEqual(len(skewed), len(position))
        self.assertEqual(skewed[3:], position[3:])
        self.assertAlmostEqual(skewed[0], 10. - 20. * 0.1
                               - 30. * (0.2 - 0.1 * 0.3))
        self.assertAlmostEqual(skewed[1], 20. - 30. * 0.3)
        self.assertEqual(self.skew.calc_unskew(position)[3:], position[3:])


if __name__ == '__main__':
    unittest.main()
