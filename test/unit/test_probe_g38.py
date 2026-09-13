#!/usr/bin/env python3
# Unit tests for extras/probe_G38.py and extras/probe_G38_multi.py
#
# The klippy test harness only inspects the exit status of a klippy run and
# always feeds the printer with a pre-recorded g-code file, so it cannot
# assert which object owns the plain G38 commands, cannot observe the probe
# selection rules and can never reach _check_no_movement (debuginput is always
# set for file input).  The classes are instantiated here directly against
# stubs instead, which is what makes the rules below testable at all.
#
# Run with:  python3 test/unit/test_probe_g38.py
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, sys, unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, 'klippy'))

from extras.probe_G38 import ProbeG38
from extras.probe_G38_multi import ProbeG38Multi

PLAIN_COMMANDS = ("G38.2", "G38.3", "G38.4", "G38.5", "QUERY_PROBE")


class StubConfigError(Exception):
    pass


class StubGCode:
    """Command registry with the duplicate rules of gcode.GCode."""
    def __init__(self):
        self.ready_gcode_handlers = {}
        self.mux_commands = {}
    def register_command(self, cmd, func, when_not_ready=False, desc=None):
        if cmd in self.ready_gcode_handlers:
            raise StubConfigError("gcode command %s already registered" % cmd)
        self.ready_gcode_handlers[cmd] = func
    def register_mux_command(self, cmd, key, value, func, desc=None):
        key, values = self.mux_commands.setdefault(cmd, (key, {}))
        if value in values:
            raise StubConfigError("mux command %s %s already registered"
                                  % (cmd, value))
        values[value] = func
        if cmd not in self.ready_gcode_handlers:
            self.register_command(cmd, lambda gcmd: None, desc=desc)
    def owner(self, cmd):
        return getattr(self.ready_gcode_handlers[cmd], '__self__', None)


class StubPins:
    def setup_pin(self, pin_type, pin_desc):
        return StubEndstop(pin_desc)


class StubEndstop:
    def __init__(self, desc):
        self.desc = desc
    def register_endstop(self, endstop, name):
        pass


class StubQueryEndstops:
    def __init__(self):
        self.endstops = []
    def register_endstop(self, endstop, name):
        self.endstops.append((endstop, name))


class StubExtruder:
    def __init__(self, name):
        self.name = name
    def get_name(self):
        return self.name


class StubToolhead:
    def __init__(self, extruder_name):
        self.extruder = StubExtruder(extruder_name)
    def get_extruder(self):
        return self.extruder


class StubPrinter:
    def __init__(self):
        self.objects = {'gcode': StubGCode(), 'pins': StubPins(),
                        'query_endstops': StubQueryEndstops(),
                        'toolhead': StubToolhead('')}
        self.handlers = {}
        self.start_args = {}
    def lookup_object(self, name):
        return self.objects[name]
    def load_object(self, config, name):
        return self.objects[name]
    def lookup_objects(self, module=None):
        return [(name, obj) for name, obj in self.objects.items()
                if obj.__class__.__module__.endswith(module)]
    def register_event_handler(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)
    def send_event(self, event, *args):
        for callback in self.handlers.get(event, []):
            callback(*args)
    def get_start_args(self):
        return self.start_args
    def config_error(self, msg):
        raise StubConfigError(msg)


class StubConfig:
    def __init__(self, printer, name, pin='^PJ2'):
        self.printer = printer
        self.name = name
        self.values = {'pin': pin, 'recovery_time': 0.4}
    def get_printer(self):
        return self.printer
    def get_name(self):
        return self.name
    def get(self, name, default=None):
        return self.values.get(name, default)
    def getfloat(self, name, default=None, minval=None, **kwargs):
        return self.values.get(name, default)
    def error(self, msg):
        raise StubConfigError(msg)


def load_sections(printer, section_names):
    """Instantiate probe sections in config file order."""
    loaded = {}
    for section in section_names:
        if section == 'probe_G38':
            obj = ProbeG38(StubConfig(printer, section))
        else:
            name = section.split(None, 1)[1]
            obj = ProbeG38Multi(StubConfig(printer, section))
            printer.objects[section] = obj
        loaded[section] = obj
    return loaded


class TestPlainCommandOwnership(unittest.TestCase):
    def check_ownership(self, sections, expected):
        printer = StubPrinter()
        loaded = load_sections(printer, sections)
        gcode = printer.lookup_object('gcode')
        # The plain commands are only claimed once every section is loaded.
        printer.send_event('klippy:connect')
        for cmd in PLAIN_COMMANDS:
            self.assertIs(gcode.owner(cmd), loaded[expected], cmd)
        return gcode

    def test_plain_section_first(self):
        self.check_ownership(['probe_G38', 'probe_G38_multi left',
                              'probe_G38_multi right'], 'probe_G38')

    def test_plain_section_last(self):
        # Regression: listing [probe_G38_multi] before [probe_G38] used to
        # raise "gcode command G38.2 already registered" at config load.
        self.check_ownership(['probe_G38_multi left',
                              'probe_G38_multi right', 'probe_G38'],
                             'probe_G38')

    def test_multi_fallback_without_plain_section(self):
        # Without a [probe_G38] section the first multi probe still owns the
        # plain commands, so a bare QUERY_PROBE keeps working.
        gcode = self.check_ownership(['probe_G38_multi left',
                                      'probe_G38_multi right'],
                                     'probe_G38_multi left')
        for cmd in ('SET_PROBE_G38', 'QUERY_PROBE_MUX', 'MULTIPROBE_TOWARD'):
            self.assertIn(cmd, gcode.ready_gcode_handlers)

    def test_mux_commands_survive_plain_section(self):
        printer = StubPrinter()
        load_sections(printer, ['probe_G38', 'probe_G38_multi left'])
        gcode = printer.lookup_object('gcode')
        values = gcode.mux_commands['SET_PROBE_G38'][1]
        self.assertEqual(list(values), ['left'])


class TestProbeSelection(unittest.TestCase):
    class FakeMulti:
        _all_probes = ProbeG38Multi._all_probes
        _selected_probe = ProbeG38Multi._selected_probe
        def __init__(self, printer):
            self.printer = printer

    def make(self, probe_names, extruder_name, selected=None):
        printer = StubPrinter()
        printer.objects['toolhead'] = StubToolhead(extruder_name)
        probes = []
        for name in probe_names:
            probe = ProbeG38Multi.__new__(ProbeG38Multi)
            probe.probe_name = name
            probe.selected = name == selected
            printer.objects['probe_G38_multi ' + name] = probe
            probes.append(probe)
        return self.FakeMulti(printer), probes

    def test_extruder_name_wins_over_selection(self):
        multi, probes = self.make(['left', 'right'], 'right', selected='left')
        self.assertIs(multi._selected_probe(), probes[1])

    def test_selection_wins_without_extruder_match(self):
        multi, probes = self.make(['left', 'right'], 'extruder',
                                  selected='right')
        self.assertIs(multi._selected_probe(), probes[1])

    def test_first_probe_is_the_default(self):
        multi, probes = self.make(['left', 'right'], 'extruder')
        self.assertIs(multi._selected_probe(), probes[0])

    def test_unnamed_extruder_falls_through_to_selection(self):
        # Toolheads without a named extruder must not break the fallback.
        multi, probes = self.make(['left', 'right'], '', selected='right')
        self.assertIs(multi._selected_probe(), probes[1])


class StubHomingMove:
    def __init__(self, stepper_positions):
        self.stepper_positions = stepper_positions


class TestCheckNoMovement(unittest.TestCase):
    class FakeProbe:
        _check_no_movement = ProbeG38._check_no_movement
        def __init__(self, start_args):
            self.printer = StubPrinter()
            self.printer.start_args = start_args

    class StepperPosition:
        def __init__(self, name, moved, endstop_name='probe'):
            self.stepper_name = name
            self.endstop_name = endstop_name
            self.start_pos = 0.
            self.trig_pos = 1. if moved else 0.

    def test_skipped_when_input_is_a_file(self):
        # The harness always runs klippy with -i, so this branch means the
        # "Probe triggered prior to movement" error can never be tested from a
        # .test file.
        probe = self.FakeProbe({'debuginput': '_test_.gcode'})
        hmove = StubHomingMove([self.StepperPosition('stepper_z', False)])
        self.assertIsNone(probe._check_no_movement(hmove, ['z']))

    def test_reports_stepper_that_never_moved(self):
        probe = self.FakeProbe({})
        hmove = StubHomingMove([self.StepperPosition('stepper_z', False)])
        self.assertEqual(probe._check_no_movement(hmove, ['z']), 'probe')

    def test_ignores_moved_steppers_and_other_axes(self):
        probe = self.FakeProbe({})
        hmove = StubHomingMove([self.StepperPosition('stepper_z', True),
                                self.StepperPosition('stepper_x', False)])
        self.assertIsNone(probe._check_no_movement(hmove, ['z']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
