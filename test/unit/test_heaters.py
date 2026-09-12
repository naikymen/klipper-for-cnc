#!/usr/bin/env python3
# Unit tests for the PID sample window in extras/heaters.py
#
# The klippy test harness only inspects the exit status of a klippy run, so it
# cannot assert anything about the PID arithmetic.  ControlPID is instantiated
# here directly against stubs instead, which is what makes the noisy-signal
# behavior below testable at all.
#
# Run with:  python3 test/unit/test_heaters.py
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import math, os, random, sys, unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, 'klippy'))

from extras import heaters

PID_KP = 22.2
PID_KI = 1.08
PID_KD = 114.
MAX_POWER = 1.0
SMOOTH_TIME = 1.0


class StubHeater:
    def __init__(self):
        self.pwm = []
    def get_max_power(self):
        return MAX_POWER
    def get_smooth_time(self):
        return SMOOTH_TIME
    def set_pwm(self, read_time, value):
        self.pwm.append((read_time, value))


class StubConfig:
    def __init__(self, samples=None, Kp=PID_KP, Ki=PID_KI, Kd=PID_KD):
        self.samples = samples
        self.values = {'pid_Kp': Kp, 'pid_Ki': Ki, 'pid_Kd': Kd}
    def getfloat(self, name, default=None, minval=None, **kwargs):
        return self.values[name]
    def getint(self, name, default=None, minval=None, **kwargs):
        assert name == 'samples'
        return self.samples


class ReferencePID:
    """The PID arithmetic exactly as it stood before the sample window."""
    def __init__(self, Kp=PID_KP, Ki=PID_KI, Kd=PID_KD):
        self.Kp = Kp / heaters.PID_PARAM_BASE
        self.Ki = Ki / heaters.PID_PARAM_BASE
        self.Kd = Kd / heaters.PID_PARAM_BASE
        self.min_deriv_time = SMOOTH_TIME
        self.temp_integ_max = MAX_POWER / self.Ki
        self.prev_temp = heaters.AMBIENT_TEMP
        self.prev_temp_time = 0.
        self.prev_temp_deriv = 0.
        self.prev_temp_integ = 0.
    def temperature_update(self, read_time, temp, target_temp):
        time_diff = read_time - self.prev_temp_time
        # Calculate change of temperature
        temp_diff = temp - self.prev_temp
        if time_diff >= self.min_deriv_time:
            temp_deriv = temp_diff / time_diff
        else:
            temp_deriv = (self.prev_temp_deriv
                          * (self.min_deriv_time-time_diff)
                          + temp_diff) / self.min_deriv_time
        # Calculate accumulated temperature "error"
        temp_err = target_temp - temp
        temp_integ = self.prev_temp_integ + temp_err * time_diff
        temp_integ = max(0., min(self.temp_integ_max, temp_integ))
        # Calculate output
        co = self.Kp*temp_err + self.Ki*temp_integ - self.Kd*temp_deriv
        bounded_co = max(0., min(MAX_POWER, co))
        # Store state for next measurement
        self.prev_temp = temp
        self.prev_temp_time = read_time
        self.prev_temp_deriv = temp_deriv
        if co == bounded_co:
            self.prev_temp_integ = temp_integ
        return bounded_co


def make_pid(samples=None, **kwargs):
    heater = StubHeater()
    return heaters.ControlPID(heater, StubConfig(samples, **kwargs)), heater


def naive_slope(times, temps):
    """Slope via the raw moment formula, without centering the timestamps."""
    num_samples = len(times)
    sum_x, sum_y = sum(times), sum(temps)
    sum_xy = sum(times[i] * temps[i] for i in range(num_samples))
    sum_x_sq = sum(times[i] ** 2 for i in range(num_samples))
    return ((num_samples * sum_xy - sum_x * sum_y)
            / (num_samples * sum_x_sq - sum_x ** 2))


class TestDefaultBehavior(unittest.TestCase):
    def test_without_samples_the_previous_arithmetic_is_unchanged(self):
        # A config that does not set "samples" must behave exactly as it did
        # before, to the last bit.
        pid, heater = make_pid(None)
        reference = ReferencePID()
        rng = random.Random(4242)
        read_time = 0.
        for _ in range(400):
            read_time += 0.3
            temp = (25. + 120. * (1. - math.exp(-read_time / 15.))
                    + rng.gauss(0., 1.2))
            pid.temperature_update(read_time, temp, 200.)
            expected = reference.temperature_update(read_time, temp, 200.)
            self.assertEqual(heater.pwm[-1], (read_time, expected))
            self.assertEqual(pid.prev_temp_deriv, reference.prev_temp_deriv)
            self.assertEqual(pid.prev_temp_integ, reference.prev_temp_integ)


class TestSampleWindow(unittest.TestCase):
    def test_two_samples_give_a_plain_secant(self):
        pid, heater = make_pid(2)
        pid.temperature_update(10., 20., 100.)
        pid.temperature_update(10.3, 20.15, 100.)
        self.assertAlmostEqual(pid.prev_temp_deriv, 0.5, places=9)

    def test_slope_is_exact_on_a_ramp(self):
        pid, heater = make_pid(5)
        for i in range(12):
            pid.temperature_update(1. + i * 0.3, 25. + 0.5 * i * 0.3, 200.)
        self.assertAlmostEqual(pid.prev_temp_deriv, 0.5, places=9)

    def test_proportional_term_uses_the_window_mean(self):
        # With Ki and Kd zeroed the output is purely the proportional term, so
        # the temperature the P term saw is readable straight off the PWM.
        gcode_kp = 25.5
        pid, heater = make_pid(3, Kp=gcode_kp, Ki=0., Kd=0.)
        for temp in (20., 40., 40.):
            pid.temperature_update(1., temp, 40.)
        mean = (20. + 40. + 40.) / 3.
        expected = (gcode_kp / heaters.PID_PARAM_BASE) * (40. - mean)
        self.assertAlmostEqual(heater.pwm[-1][1], expected, places=9)
        # The same input through the default path sees the latest sample only.
        pid, heater = make_pid(None, Kp=gcode_kp, Ki=0., Kd=0.)
        for temp in (20., 40., 40.):
            pid.temperature_update(1., temp, 40.)
        self.assertEqual(heater.pwm[-1][1], 0.)

    def test_check_busy_reports_the_window_slope(self):
        pid, heater = make_pid(5)
        for i in range(8):
            pid.temperature_update(100. + i * 0.3, 199. + 0.5 * i * 0.3, 200.)
        self.assertGreater(pid.prev_temp_deriv, heaters.PID_SETTLE_SLOPE)
        self.assertTrue(pid.check_busy(0., 200., 200.))

    def test_repeated_timestamps_do_not_divide_by_zero(self):
        # A sensor that reports twice against the same read_time makes the
        # slope denominator exactly zero.
        pid, heater = make_pid(3)
        for temp in (30., 31., 32., 33.):
            pid.temperature_update(500., temp, 100.)
        self.assertEqual(pid.prev_temp_deriv, 0.)

    def test_mixed_repeated_timestamps_do_not_divide_by_zero(self):
        pid, heater = make_pid(4)
        for read_time, temp in [(500., 30.), (500., 31.), (500.2, 32.),
                                (500.2, 33.), (500.4, 34.)]:
            pid.temperature_update(read_time, temp, 100.)
        self.assertTrue(math.isfinite(pid.prev_temp_deriv))

    def test_slope_stays_accurate_after_long_uptimes(self):
        # read_time is an absolute process time, so on a host that has been up
        # for a while the raw moment formula cancels away most of its
        # precision and silently returns a slope that is off by an order of
        # magnitude.  Centering the timestamps keeps the result exact.
        first_time, samples = 1e8, 2
        times = [first_time + i * 0.3 for i in range(samples)]
        temps = [25. + 0.5 * i * 0.3 for i in range(samples)]
        naive_error = abs(naive_slope(times, temps) - 0.5) / 0.5
        self.assertGreater(naive_error, 0.5)
        pid, heater = make_pid(samples)
        for read_time, temp in zip(times, temps):
            pid.temperature_update(read_time, temp, 200.)
        # read_time itself is only representable to ulp(1e8), which caps the
        # accuracy of any slope taken over these timestamps.
        self.assertAlmostEqual(pid.prev_temp_deriv, 0.5, places=6)

    def test_slope_does_not_overflow_after_long_uptimes(self):
        # With a wider window the same formula underflows to exactly zero and
        # raises ZeroDivisionError outright.
        first_time, samples = 1e9, 10
        times = [first_time + i * 0.3 for i in range(samples)]
        temps = [25. + 0.5 * i * 0.3 for i in range(samples)]
        with self.assertRaises(ZeroDivisionError):
            naive_slope(times, temps)
        pid, heater = make_pid(samples)
        for read_time, temp in zip(times, temps):
            pid.temperature_update(read_time, temp, 200.)
        self.assertAlmostEqual(pid.prev_temp_deriv, 0.5, places=6)

    def test_window_rejects_noise_better_than_the_default(self):
        # The whole point of the option: a regression through several samples
        # estimates the rate of change far better than a single difference.
        def rms_error(samples):
            pid, heater = make_pid(samples)
            rng = random.Random(20260912)
            read_time, errors = 0., []
            while read_time < 60.:
                read_time += 0.3
                true_rate = 120. / 15. * math.exp(-read_time / 15.)
                temp = (25. + 120. * (1. - math.exp(-read_time / 15.))
                        + rng.gauss(0., 1.5))
                pid.temperature_update(read_time, temp, 200.)
                if read_time > 30.:
                    errors.append(pid.prev_temp_deriv - true_rate)
            return math.sqrt(sum(e * e for e in errors) / len(errors))
        default_error = rms_error(None)
        self.assertLess(rms_error(10), default_error / 2.)


if __name__ == '__main__':
    unittest.main(verbosity=2)
