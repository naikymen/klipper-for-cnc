#!/usr/bin/env python3
# Unit tests for GET_STATUS_MSG in extras/status_msg.py
#
# The klippy test harness only inspects the exit status of a klippy run and it
# cannot read the console output, so test/klippy/status_msg.test can only show
# that the command is registered and does not raise.  What the command actually
# reports - a multi-line pretty-printed dict, the current status, and the
# reactor time it is asked for - is checked here against stubs.
#
# Run with:  python3 test/unit/test_status_msg.py
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import os, sys, unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, 'klippy'))

from extras import status_msg

# A status shaped like toolhead's own, including the extra axis this fork adds,
# and with the keys deliberately out of order.
STATUS = {'homed_axes': 'xyz', 'position': (1.5, 2.0, 0.0, 0.0),
          'extra_axes': {'manual_stepper a_stepper': 4}, 'stalls': 0}
STATUS_PFORMAT = ("{'extra_axes': {'manual_stepper a_stepper': 4},\n"
                  " 'homed_axes': 'xyz',\n"
                  " 'position': (1.5, 2.0, 0.0, 0.0),\n"
                  " 'stalls': 0}")


class StubReactor:
    def __init__(self, monotonic):
        self.monotonic_value = monotonic
    def monotonic(self):
        return self.monotonic_value


class StubGCode:
    def __init__(self):
        self.commands = {}
    def register_command(self, name, handler, when_not_ready=False, desc=None):
        self.commands[name] = (handler, when_not_ready, desc)


class StubToolHead:
    def __init__(self, status):
        self.status = status
        self.status_events = []
    def get_status(self, eventtime):
        self.status_events.append(eventtime)
        return self.status


class StubPrinter:
    def __init__(self, reactor):
        self.reactor = reactor
        self.gcode = StubGCode()
        self.toolhead = None
        self.lookups = []
    def get_reactor(self):
        return self.reactor
    def lookup_object(self, name):
        self.lookups.append(name)
        if name == 'gcode':
            return self.gcode
        if name == 'toolhead':
            if self.toolhead is None:
                raise KeyError('toolhead is not created yet')
            return self.toolhead
        raise KeyError(name)


class StubConfig:
    def __init__(self, printer):
        self.printer = printer
    def get_printer(self):
        return self.printer


class StubCommand:
    """Stands in for klippy.gcode.GCodeCommand."""
    def __init__(self):
        self.messages = []
    def respond_info(self, msg, log=True):
        self.messages.append(msg)


class TestStatusMsg(unittest.TestCase):
    def setUp(self):
        self.reactor = StubReactor(1234.5)
        self.printer = StubPrinter(self.reactor)
        self.toolhead = StubToolHead(dict(STATUS))
        self.printer.toolhead = self.toolhead
        self.status_msg = status_msg.StatusMsg(StubConfig(self.printer))
        handler, when_not_ready, desc = self.printer.gcode.commands[
            'GET_STATUS_MSG']
        self.handler = handler
        self.when_not_ready = when_not_ready
        self.desc = desc

    def run_command(self):
        gcmd = StubCommand()
        self.handler(gcmd)
        return gcmd.messages

    def test_load_config_returns_extra(self):
        self.assertIsInstance(status_msg.load_config(StubConfig(self.printer)),
                              status_msg.StatusMsg)

    def test_command_registered_with_help(self):
        self.assertIn('GET_STATUS_MSG', self.printer.gcode.commands)
        self.assertTrue(self.desc)

    def test_command_is_not_available_before_ready(self):
        # toolhead.get_status() needs the mcu, so the command must stay out of
        # the always-available set.
        self.assertFalse(self.when_not_ready)

    def test_reports_pretty_printed_status(self):
        self.assertEqual(self.run_command(), [STATUS_PFORMAT])

    def test_reports_the_current_status(self):
        self.toolhead.status['homed_axes'] = ''
        self.toolhead.status['position'] = (0.0, 0.0, 0.0, 0.0)
        messages = self.run_command()
        self.assertIn("'homed_axes': ''", messages[0])
        self.assertIn("'position': (0.0, 0.0, 0.0, 0.0)", messages[0])

    def test_queries_toolhead_status_at_reactor_time(self):
        self.run_command()
        self.assertEqual(self.toolhead.status_events,
                         [self.reactor.monotonic_value])

    def test_looks_up_the_toolhead_per_command(self):
        # A toolhead that only exists after the extras are loaded must still be
        # found, so the lookup cannot happen during __init__.
        self.printer.toolhead = None
        with self.assertRaises(KeyError):
            self.run_command()
        self.printer.toolhead = self.toolhead
        self.assertEqual(self.run_command(), [STATUS_PFORMAT])

    def test_reports_once_per_command(self):
        self.run_command()
        self.run_command()
        self.assertEqual(self.toolhead.status_events, [1234.5, 1234.5])

    def test_status_failures_are_not_swallowed(self):
        def broken(eventtime):
            raise AttributeError("'NoneType' object has no attribute 'mcu'")
        self.toolhead.get_status = broken
        with self.assertRaises(AttributeError):
            self.run_command()


if __name__ == '__main__':
    unittest.main()
