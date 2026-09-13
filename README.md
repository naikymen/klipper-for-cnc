# Klipper for CNC

Welcome to my fork of the [Klipper](https://www.klipper3d.org/) firmware, with
extra CNC axes, home-able extruder steppers, and CNC-style directional
probing!

This branch (`cnc-reimplementation`) reimplements the CNC features of the older
`develop` / `pipetting` branch on top of a clean, current upstream `master`
(upstream Klipper `0.13.0` plus the commits after it, up to `2d7717e3`). New
behavior lives in Klipper *extras* wherever possible, so upstream changes keep
merging cleanly. The roadmap, the design decisions and the details of the
migration are recorded in
[docs/CNC_Reimplementation.md](docs/CNC_Reimplementation.md).

[![Klipper](docs/img/klipper-logo-small.png)](https://www.klipper3d.org/)

**Use cases**

- General CNC machining: milling, laser, drawing.
- Syringe, paste and clay extruders.
- Pipetting / liquid handling / lab automation, where the "extruder" stepper is
  really a positioning axis.
- Machines with more axes than XYZ.

**What you can expect**

- Expect to spend some time tuning and asking questions; it should then work
  fine.
- Every feature is covered by the batch regression tests in
  [test/klippy](test/klippy), but most of them are not yet validated on real
  hardware. Bring up one axis or one probe at a time, on a machine you can
  stop.
- [Ask for help](https://klipper.discourse.group/t/12249) or
  [raise an issue](https://github.com/naikymen/klipper-for-cnc/issues) when
  something misbehaves.

**Critical limitations**

- Only the `cartesian` kinematic has been exercised with a full CNC
  configuration. The extra axes are registered with the toolhead rather than
  with a kinematic, so they do not get in the way of the other kinematics, but
  nothing else has been tested.
- XYZ acceleration is shared through `max_accel`, so a demanding extra axis
  slows down the XYZ part of the same move. Each extra axis has its own
  `velocity` and `accel`, and `limit_velocity` / `limit_accel` can cap the
  speed that a `G1` move gives it.
- Directional probing moves only X, Y and Z. The extruder and the extra axes
  cannot be probed.
- `[probe_G38]` and `[probe_G38_multi]` are separate from the upstream
  `[probe]` section. `bed_mesh`, `PROBE`, `PROBE_CALIBRATE` and the
  `probe:z_virtual_endstop` pin need the upstream `[probe]` section, which this
  fork does not change.
- Everything else in Klipper is unadapted: features that assume exactly three
  axes, a filament extruder, or a Z probe may be untested with these features
  enabled.
- Every option added by this fork defaults to the upstream behavior. An
  existing 3D printer is unaffected until it asks for a new feature.

**Disclaimers**

- This is an **EXPERIMENTAL** fork. Ask around
  [in the forum](https://klipper.discourse.group/t/12249) before giving it a
  try.
- Protect your machine (and yourself) first: validate endstop polarity and
  probe signals, keep the speeds conservative, and always have a reachable
  emergency stop.

Demo (recorded on the old branch, same feature set):

[![IMAGE ALT TEXT](http://img.youtube.com/vi/8IuYqU3vUo4/0.jpg)](http://www.youtube.com/watch?v=8IuYqU3vUo4 "Demo on YouTube")

## What this fork adds

Everything below is documented in [G-Codes.md](docs/G-Codes.md) and
[Config_Reference.md](docs/Config_Reference.md).

### Extra CNC axes (A, B, C, ...)

Any number of extra axes may be declared in the config file. Each one is a
`[manual_stepper]` section with a `gcode_axis` setting:

```yaml
[manual_stepper a_stepper]
step_pin: PA0
dir_pin: PA1
enable_pin: !PA2
microsteps: 16
rotation_distance: 40
velocity: 25
accel: 1000
gcode_axis: A
```

- The axis is registered when the printer starts, and from then on it follows
  the matching G-Code word: `G1 X10 A5`, `G28 A`, and so on.
- `M114`, `GET_POSITION` and the `toolhead` status report the extra axes after
  `E` (that is, `X Y Z E A B C`).
- Adding an `endstop_pin` turns the axis into a home-able axis. Add
  `position_min`, `position_max`, `position_endstop` and the usual `homing_*`
  options. Such an axis must be homed, or unlocked with
  `SET_KINEMATIC_POSITION`, before it will move, and `M18` / `M84` clears that
  state again.
- A bare `G28` homes XYZ and then every home-able extra axis. Axes without an
  endstop are skipped.
- `M211 S0` disables the software limits of all axes, including the extra
  axes, and `M211 S1` enables them again.
- `MANUAL_STEPPER STEPPER=a_stepper ...` is unavailable while the axis is
  registered. Use `MANUAL_STEPPER STEPPER=a_stepper GCODE_AXIS=` to unregister
  it at runtime if direct control is needed.
- Extra axes may share an `enable_pin` with the XYZ steppers.
- Extra axes take part in `G2` / `G3` arcs just like the extruder does: they
  are interpolated along the arc and finish exactly on the commanded target.

### Home-able extruder steppers

Add the homing options to `[extruder]` (or `[extruder1]`, ...) to turn its
stepper into an axis. No additional section is needed:

```yaml
[extruder]
# ... the usual extruder options ...
endstop_pin: ^PA0
position_endstop: -10.
position_min: -10.
position_max: 3000.
homing_speed: 50.
homing_positive_dir: False
```

- `HOME_EXTRUDER EXTRUDER=<name>` homes a named extruder, and
  `HOME_ACTIVE_EXTRUDER` homes the active one. `G28 E` and a bare `G28` home it
  as well.
- As with the extra axes, the extruder must be homed before it will move, and
  `M18` / `M84` clears that state.
- Without an `endstop_pin` the extruder stepper behaves exactly like upstream:
  it is not home-able, it has no limits, and it does not need to be homed.
- `SET_KINEMATIC_POSITION E=<pos>` unlocks the extruder axis without homing it.
  This requires `[force_move] enable_force_move: True`, and it does not accept
  the extra axes.

### Directional probing: `G38.2` - `G38.5`

The `[probe_G38]` section provides LinuxCNC-style directional probing with a
single probe pin:

```yaml
[probe_G38]
pin: ^PA4
recovery_time: 0.4
```

- `G38.2` probes toward the workpiece and errors out if the probe does not
  trigger; `G38.3` does the same without the error; `G38.4` and `G38.5` probe
  away from the workpiece and stop when the probe clears.
- The target is interpreted like a `G1` target (absolute or relative, and
  relative to the current G-Code offsets), and `F` sets the probing speed.
- `QUERY_PROBE` reports the state of the probe. The pin is also listed by
  `QUERY_ENDSTOPS` / `M119` as `probe`, and by the `query_endstops/status`
  webhook that frontends use for their endstop panel.
- `[probe_G38]` does not consume nozzle or bed offsets, and does not provide
  the `probe:z_virtual_endstop` pin, so it cannot be used by `bed_mesh`.

### Multiple named probes: `[probe_G38_multi <name>]`

Define any number of named probes, and reach each of them with the muxed
commands:

```yaml
[probe_G38_multi extruder]
pin: ^PA2

[probe_G38_multi left_touch]
pin: ^PA5
```

- `MULTIPROBE_TOWARD PROBE_NAME=left_touch Z=-20 F=1` and
  `MULTIPROBE_TOWARD_NOERROR` behave like `G38.2` and `G38.3`;
  `MULTIPROBE_AWAY` and `MULTIPROBE_AWAY_NOERROR` behave like `G38.4` and
  `G38.5`.
- `QUERY_PROBE_MUX PROBE_NAME=left_touch` reports the state of one probe, and
  every probe is listed by `QUERY_ENDSTOPS` / `M119` under its `probe_<name>`.
- When no `[probe_G38]` section is configured, the ordinary `G38.*` and
  `QUERY_PROBE` commands use one of the named probes: the probe named after the
  active extruder, if there is one, otherwise the probe selected with
  `SET_PROBE_G38 PROBE_NAME=<name>`, otherwise the first probe in the config
  file.
- Therefore, a probe named after an extruder (for example
  `[probe_G38_multi extruder]`) follows that extruder's tool changes
  automatically, and `SET_PROBE_G38` cannot override it while that extruder is
  active.

### Opt-in changes to the extruder coordinate

Three options change long-standing 3D-printer behavior. They all default to the
upstream behavior, and are meant for machines that treat the extruder as a
regular axis:

```yaml
[printer]
# Keep the extruder coordinate unchanged by a tool change.
#tool_change_e_reset: True   # set to False for CNC use
# Keep the extruder coordinate unchanged by RESTORE_GCODE_STATE.
#relative_e_restore: True    # set to False for CNC use

[extruder]
# Apply max_extrude_only_velocity/max_extrude_only_accel to every
# move of this extruder, in both directions.
#symmetric_speed_limits: False   # set to True for CNC use
```

### Heater PID sample averaging

The PID controller can average a window of ADC samples for its proportional
term and fit a regression to the window for its derivative term. This is a
large improvement on noisy ADCs, such as an Arduino:

```yaml
[extruder]
# ... inside the PID block ...
control: pid
pid_Kp: 22.2
pid_Ki: 1.08
pid_Kd: 114
# Window of ADC samples used by the P and D terms (2 or more).
samples: 10
```

`samples` is opt-in and accepts `2` or more. Upstream's arithmetic is used when
the option is absent. A value of `2` differentiates noise instead of rejecting
it, so `5` or `10` are better starting points; see the measurements in
[docs/CNC_Reimplementation.md](docs/CNC_Reimplementation.md).

## Configuration quick start

1. Configure XYZ as usual for a cartesian machine. The only difference is that
   you may add extra axes and a home-able extruder.
2. Declare one `[manual_stepper]` section per extra axis, with `gcode_axis: A`
   (and `B`, `C`, ...). Leave out `endstop_pin` for an axis that cannot be
   homed.
3. Add `endstop_pin` and the position/homing options to `[extruder]` if the
   extruder stepper should be a home-able axis.
4. Add `[probe_G38]` (single probe) or `[probe_G38_multi <name>]` (named
   probes).
5. Add `M211 S0` to a macro only when you really need to move an axis past its
   configured limits. Never leave a machine unattended with the limits
   disabled.

Two complete examples are shipped with the fork:

- [config/generic-cnc-shield-v3.0.cfg](config/generic-cnc-shield-v3.0.cfg) - an
  Arduino Uno with a Protoneer CNC-Shield v3.0, XYZA plus an extruder, and a
  commented-out template for an extra axis.
- [test/klippy/k4cnc.cfg](test/klippy/k4cnc.cfg) - a six-axis machine with
  three declarative extra axes, a home-able extruder and two named probes. Its
  [test](test/klippy/k4cnc.test) is run by the batch test suite.

## Migrating from the old `develop` / `pipetting` branch

The old branch put extra axes into the kinematics. This branch registers them
as toolhead axes instead, so most of the configuration syntax changed:

| Old (`develop` / `pipetting`) | New (`cnc-reimplementation`) |
| --- | --- |
| `[printer] kinematics: cartesian_abc` or `corexy_abc` | `[printer] kinematics: cartesian` (the old `*_abc` kinematics are not ported) |
| `[printer] axis: XYZABC` | Removed. Axes are declared by the sections that own them |
| `[stepper_a]`, `[stepper_b]`, `[stepper_c]` sections | `[manual_stepper <name>]` sections with `gcode_axis: A`, `B`, `C`. The stepper options (`step_pin`, `dir_pin`, `enable_pin`, `microsteps`, `rotation_distance`, the position/homing options) keep their names; `velocity` and `accel` replace the old shared limits |
| `[printer] accel_limited_axes` | Removed. Use each axis's `velocity` / `accel`, and `limit_velocity` / `limit_accel` to cap the speed that `G1` gives it |
| `[extruder_home <name>]` section per home-able extruder | Removed. Adding `endstop_pin` (plus `position_endstop` and `position_max`) to `[extruder]` is enough |
| `MULTIPROBE2 / 3 / 4 / 5 PROBE_NAME=...` | `MULTIPROBE_TOWARD`, `MULTIPROBE_TOWARD_NOERROR`, `MULTIPROBE_AWAY`, `MULTIPROBE_AWAY_NOERROR`, each with `PROBE_NAME=...` |
| `[probe_G38]` / `[probe_G38_multi <name>]` with `z_offset` | Same sections, without `z_offset`. `recovery_time` is the dwell before the probing move, not a slow approach |
| `GET_STATUS_MSG`, `manual_spinner`, `pipettin.py`, `QUERY_HX71`, `SET_SKEW_FACTORS`, extended G-Code help | Not ported yet. Each one is tracked as `S1` - `S8` in [docs/CNC_Reimplementation.md](docs/CNC_Reimplementation.md) |
| `min_extrude_temp: -273.15` (and negative `min_temp`) | Rejected. Heater minimums are clamped to `min_temp`, so use `min_temp: 0` with `min_extrude_temp: 0` to allow cold extrusion moves |
| Absolute extruder coordinates were hardcoded | Opt-in: `[printer] tool_change_e_reset: False` and `relative_e_restore: False`, plus `[extruder] symmetric_speed_limits: True` |
| `M114` / `GET_POSITION` reported `X Y Z A B C E` | Reports `X Y Z E A B C`, so the `X Y Z E` prefix keeps its upstream meaning |
| `samples: 2` was always on | `samples` is opt-in, and the upstream arithmetic is used when it is absent |
| Probing an ABC or E axis (`MULTIPROBE2 A=-20`) | Not supported: `G38.*` only moves X, Y and Z |

## Testing

The batch regression tests need a directory of MCU data dictionaries, which are
binary build outputs and therefore not committed. See
[the "Local MCU data dictionaries" section](docs/CNC_Reimplementation.md#local-mcu-data-dictionaries)
for how to obtain or refresh the cache in `ci_build/dict/`.

```shell
# All of the batch fixtures (configs, G-Code scripts and expected errors):
python3 scripts/test_klippy.py -d ci_build/dict test/klippy/*.test

# The heater PID arithmetic, including the samples window:
python3 test/unit/test_heaters.py
```

The fixtures for this fork's features are, among others, `extra_axis*.test`
(extra axes), `homeable_extruder*.test` (home-able extruders),
`probe_g38*.test` (probing), `extruder_coordinates.test` (extruder coordinate
options), `gcode_arcs_abc*.test` (arcs with extra axes) and
`heater_pid_samples*.test` (PID sampling).

## Contributing

Pull requests are very welcome over here, and will be merged quickly. For
anything that touches the extra-axis, homing, probing or extruder-coordinate
code, please add or extend a fixture in `test/klippy/` - the [per-slice
checklist](docs/CNC_Reimplementation.md) lists what each new motion feature
should cover.

Let's chat over here: <https://klipper.discourse.group/t/12249>

Show your love for this project through Liberapay:
<https://liberapay.com/naikymen> <3

Also consider donating to upstream Klipper and its appendages.

## Installation

The easiest way is to use a KIAUH "klipper_repos.txt" file. Details at:
<https://github.com/th33xitus/kiauh/blob/master/klipper_repos.txt.example>

1. SSH into the Pi.
2. Copy `klipper_repos.txt.example` to `klipper_repos.txt`:
   `cp kiauh/klipper_repos.txt.example kiauh/klipper_repos.txt`
3. Append `naikymen/klipper-for-cnc,cnc-reimplementation` to that file:
   `echo "naikymen/klipper-for-cnc,cnc-reimplementation" >> kiauh/klipper_repos.txt`
4. Start KIAUH (`./kiauh/kiauh.sh`).
5. Choose option `6) [Settings]`, then `1) Set custom Klipper repository`.
6. Choose the option corresponding to `naikymen/klipper-for-cnc -> cnc-reimplementation`.
7. Use KIAUH to uninstall and reinstall Klipper.

Thanks to some
[changes in upstream moonraker](https://github.com/Arksine/moonraker/issues/615),
a properly configured repository can be updated from Mainsail just like the
original Klipper.

## Not implemented here (yet)

- Controlled deceleration and recovery from motion faults (`Timer too close`,
  lost communication, ...). The investigation and a phased design are in
  [docs/CNC_Motion_Recovery.md](docs/CNC_Motion_Recovery.md).
- The secondary changes of the old branch (`S1` - `S8` in the roadmap):
  `manual_spinner`, the `pipettin.py` helper, `QUERY_HX71`, extended G-Code
  help, direct skew-factor commands, extra status fields, branding, and the
  old test README.

---

# Original Klipper documentation

Welcome to the Klipper project!

https://www.klipper3d.org/

The Klipper firmware controls 3d-Printers. It combines the power of a
general purpose computer with one or more micro-controllers. See the
[features document](https://www.klipper3d.org/Features.html) for more
information on why you should use the Klipper software.

Start by [installing Klipper software](https://www.klipper3d.org/Installation.html).

Klipper software is Free Software. See the [license](COPYING) or read
the [documentation](https://www.klipper3d.org/Overview.html). We
depend on the generous support from our
[sponsors](https://www.klipper3d.org/Sponsors.html).
