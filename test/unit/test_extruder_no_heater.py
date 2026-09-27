#!/usr/bin/env python3
# Unit tests for heater-less extruders (kinematics/extruder.py)
#
# The klippy test harness only inspects the exit status of a klippy run, so it
# cannot assert *which* error a bad configuration produces, and it cannot reach
# DummyHeater without a full printer.  DummyHeater is instantiated here against
# stubs instead, which is what makes the stray-option messages, the
# require_heater truth table and the agreement with the real Heater interface
# testable at all.
#
# Run with:  python3 test/unit/test_extruder_no_heater.py
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import ast, os, sys, threading, unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, 'klippy'))

from extras import heaters
from kinematics import extruder

HEATERS_PY = os.path.join(REPO_ROOT, 'klippy', 'extras', 'heaters.py')
# Read by the temperature sensor modules instead of by heaters.py, so the
# source scan below cannot find them.
SENSOR_OPTIONS = {'sensor_pin', 'pullup_resistor'}


class ConfigError(Exception):
    pass


def read_file(path):
    with open(path) as f:
        return f.read()


class CommandError(Exception):
    pass


class StubPrinter:
    command_error = CommandError
    def is_shutdown(self):
        return False


class StubMcu:
    def estimated_print_time(self, eventtime):
        return 0.


class StubPwm:
    def get_mcu(self):
        return StubMcu()


class StubConfig:
    def __init__(self, name='extruder', **values):
        self.name = name
        self.values = values
    def get_printer(self):
        return StubPrinter()
    def get_name(self):
        return self.name
    def get(self, option, default=None, **kwargs):
        return self.values.get(option, default)
    def error(self, msg):
        raise ConfigError(msg)


def make_dummy(require_heater=True, **values):
    return extruder.DummyHeater(StubConfig(**values), require_heater)


def make_real_heater(name='extruder'):
    """Build a real Heater without running its __init__.

    Only the attributes its status methods read are needed, which keeps this
    test independent of the pin, sensor and mcu setup a full Heater needs.
    """
    heater = heaters.Heater.__new__(heaters.Heater)
    heater.printer = StubPrinter()
    heater.mcu_pwm = StubPwm()
    heater.short_name = name
    heater.lock = threading.Lock()
    heater.verify_mainthread_time = 0.
    heater.target_temp = 0.
    heater.smoothed_temp = 0.
    heater.last_temp = 0.
    heater.last_pwm_value = 0.
    return heater


def heater_config_options():
    """The config options heaters.py itself reads, via its own source."""
    tree = ast.parse(read_file(HEATERS_PY))
    found = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for fn in node.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            for sub in ast.walk(fn):
                if not isinstance(sub, ast.Call):
                    continue
                func = sub.func
                if (not isinstance(func, ast.Attribute)
                        or not func.attr.startswith('get')
                        or not isinstance(func.value, ast.Name)
                        or func.value.id != 'config'):
                    continue
                if sub.args and isinstance(sub.args[0], ast.Constant):
                    found.add(sub.args[0].value)
    return found


class TestStrayHeatingOptions(unittest.TestCase):
    def test_no_heating_options_is_accepted(self):
        dummy = make_dummy()
        self.assertEqual(dummy.get_name(), 'extruder')
        self.assertTrue(dummy.can_extrude)
        self.assertEqual(dummy.get_temp(0.), (0., 0.))
        self.assertFalse(dummy.check_busy(0.))

    def test_each_heating_option_is_rejected_on_its_own(self):
        for option in extruder.DummyHeater.heating_options:
            try:
                make_dummy(**{option: '1'})
            except ConfigError as e:
                msg = str(e)
                self.assertIn(option, msg)
                self.assertIn('heater_pin', msg)
                self.assertIn('extruder', msg)
            else:
                self.fail("'%s' without heater_pin was accepted" % (option,))

    def test_every_stray_option_is_named_in_order(self):
        try:
            make_dummy(sensor_type='EPCOS 100K B57560G104F', sensor_pin='PK5',
                       control='pid', pid_Kp='10', min_temp='0')
        except ConfigError as e:
            msg = str(e)
        else:
            self.fail("stray heating options were accepted")
        self.assertIn("section 'extruder'", msg)
        listed = msg.split('set: ')[1].split(', ')
        self.assertEqual(listed, ['sensor_type', 'sensor_pin', 'control',
                                  'min_temp', 'pid_Kp'])

    def test_guard_covers_every_option_the_heater_path_reads(self):
        # A heating option missing from the guard would be reported by
        # check_unused() as an invalid option instead of being explained as a
        # missing heater_pin, so the guard has to track heaters.py.
        expected = heater_config_options() - {'heater_pin', 'gcode_id'}
        expected |= SENSOR_OPTIONS
        self.assertEqual(set(extruder.DummyHeater.heating_options), expected)


class TestRequireHeater(unittest.TestCase):
    def test_positive_request_is_an_error_by_default(self):
        dummy = make_dummy()
        self.assertRaises(CommandError, dummy.set_temp, 200.)

    def test_zero_request_is_always_accepted(self):
        for require_heater in (True, False):
            make_dummy(require_heater).set_temp(0.)

    def test_requests_are_ignored_when_not_required(self):
        dummy = make_dummy(require_heater=False)
        for degrees in (1., 200., 250.):
            dummy.set_temp(degrees)


class TestStatusAgreement(unittest.TestCase):
    def test_status_matches_the_real_heater_when_idle(self):
        real = make_real_heater().get_status(0.)
        self.assertEqual(make_dummy().get_status(0.), real)
        self.assertEqual(set(real), {'temperature', 'target', 'power'})

    def test_stats_matches_the_real_heater_format(self):
        real = make_real_heater().stats(0.)
        self.assertEqual(make_dummy().stats(0.), real)
        # statistics.py feeds the first field to max(), so it must be a bool
        self.assertIs(make_dummy().stats(0.)[0], False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
