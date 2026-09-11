# CNC feature reimplementation

This document is the durable plan for rebuilding the `develop` branch's CNC
and lab-automation features on current Klipper. It is both a human roadmap and
the first project context an AI coding agent should read after `AGENTS.md`.

## Repository roles

| Ref | Role | Snapshot inspected on 2026-09-10 |
| --- | --- | --- |
| `master` | Upstream-matching baseline; do not develop here | `2d7717e3` (2026-09-09) |
| `develop` | Legacy behavior and implementation reference; read only | `3222de26` (2026-05-18) |
| `cnc-reimplementation` | Integration branch based on `master` | `2d7717e3` at creation |

At the inspected snapshot, `master` and `develop` have 723 and 900 unique
commits respectively, with merge base `413ff19e` (2025-04-17). A direct tip
comparison spans hundreds of files because it mixes fork features with
upstream evolution. It is unsuitable as an implementation patch.

Run `scripts/cnc-reference.sh summary` to refresh these facts. Use
`feature-diff` to study the legacy feature delta from the merge base and
`current-diff` only when checking how an old file differs from today's code.

## Findings from the initial audit

The legacy fork expanded fixed position vectors and introduced a second ABC
kinematics instance. Its largest changes are in `klippy/toolhead.py`,
`klippy/extras/homing.py`, `klippy/extras/gcode_move.py`, and
`klippy/kinematics/extruder.py`. It also added dedicated ABC kinematics modules,
G38 probe modules, extruder homing, a continuously moving manual stepper, and a
small batch regression fixture.

Current `master` has since acquired mechanisms that materially change the
design space:

- `ToolHead` owns a dynamic `extra_axes` collection.
- `GCodeMove` builds its G-Code axis map from those registered axes.
- `manual_stepper` can register an uppercase G-Code axis and synchronize it
  with XYZ moves, with per-axis velocity, acceleration, and junction limits.
- Manual stepper endstop moves support probe, home, inverted, and non-failing
  variants.
- `generic_cartesian` models flexible stepper-to-XYZ carriage relationships,
  while the main spatial planner and homing code still center on XYZ.

The current motion path is:

```text
G0/G1 parameters
      |
      v
GCodeMove dynamic axis map
      |
      v
ToolHead.Move and lookahead
      |-------------------------------|
      v                               v
XYZ kinematics checks/trapq     each registered extra axis
                                checks + its own trapq
```

Manual-stepper homing deliberately uses the generic homing machinery with the
manual stepper acting as a small toolhead adapter. By contrast, the main
`ToolHead.drip_move()` retains only XYZ from its requested destination. A new
ABC design should therefore reuse or generalize the manual-stepper path rather
than assuming normal XYZ homing already supports dynamic extra axes.

These upstream facilities should be proven with tests before adding core
motion abstractions. The likely first implementation is a declarative wrapper
around extra-axis/manual-stepper behavior plus the missing homing, limits,
status, and G-Code contracts. This is a hypothesis, not a settled design.

### Critical motion-model difference

The two branches currently compute move geometry differently. This must be an
explicit product decision, because it changes real move duration:

| Move | Legacy `develop` | Current `master` extra-axis behavior |
| --- | --- | --- |
| Mixed XYZ+A | Uses the Euclidean length of all configured non-extruder axes, so A contributes to path length | Uses XYZ Euclidean length; A is synchronized to that duration and may lower the common speed/acceleration through its configured limits |
| A-only | Uses absolute A displacement as the move length | Uses the greatest absolute extra-axis displacement as the move length |
| Junction | Includes configured ABC direction components in the toolhead corner calculation | XYZ controls the toolhead corner calculation; each extra axis adds its own instantaneous corner limit |
| Acceleration | A configurable `accel_limited_axes` set scales a shared acceleration pool | Each registered extra axis can limit the common move through its own acceleration bound |

Current upstream behavior is closer to the legacy arc code's stated intent:
G2/G3 feedrate describes the XYZ arc while extra coordinates finish in the
same time. It differs from legacy straight G1 behavior. F0 must turn both into
measured regression cases before F1 chooses compatibility or a new consistent
contract.

## Compatibility target

The intended user-visible baseline comes from `develop`'s README and tests:

- Configurable partial XYZABC machines, with synchronized ABC motion in `G1`.
- Homing of configured ABC axes and extruder steppers.
- Directional probing through `G38.2`, `G38.3`, `G38.4`, and `G38.5`, including
  multiple named probe inputs.
- `SET_KINEMATIC_POSITION` support for applicable non-XYZ coordinates.
- Explicit soft-limit control (`M211`) with safe default behavior.
- Predictable absolute extruder coordinates across tool changes and optional
  extruder-coordinate behavior during `RESTORE_GCODE_STATE`.
- Optional symmetric extruder motion limits.
- Correct mixed-axis G2/G3 behavior.
- Heater PID sample averaging and regression-based derivative estimation.

Compatibility does not require preserving legacy internal class names, config
layout, debug logging, or implementation quirks. Any intentional public syntax
change must have a migration note and user approval.

## Feature roadmap

Statuses are `audit`, `design`, `implement`, `automated`, `hardware`, `done`,
or `defer`. Only one or two rows should normally be in `implement` at once.

| ID | Capability | Status | Primary legacy evidence | Current-master starting point | Exit evidence |
| --- | --- | --- | --- | --- | --- |
| F0 | Baseline extra-axis spike | done | `README.md`, `test/klippy/k4cnc.*` | `manual_stepper.py`, dynamic `extra_axes` | Tests document current G1, limit, junction, unregister, and endstop behavior |
| F1 | Declarative ABC axes and partial axis sets | done | `cartesian_abc.py`, `corexy_abc.py`, `toolhead.py` | F0 plus `generic_cartesian.py` | XYZA and XYZABC configs; mixed and extra-only moves |
| F2 | ABC homing, state, limits, and position reporting | done | `homing.py`, ABC kinematics, `M211` commits | Manual-stepper endstop modes; XYZ homing APIs | G28/position/status contracts, failure paths, motor-off state, soft-limit tests |
| F3 | Directional G38 single probe | done | `probe_G38.py` | Current `probe.py` and `homing.py` | All four trigger modes, absolute/relative coordinates, feedrate, error cases |
| F4 | Multiple named directional probes | audit | `probe_G38_multi.py` | F3; current pin/endstop registration | Named selection, active-tool selection, query/status, conflict tests |
| F5 | Home-able extruder steppers | audit | `extruder_home.py`, `extruder.py` | Extra-axis and current homing primitives | Multiple extruders, both directions, retract/second home, error recovery |
| F6 | Extruder coordinate and limit semantics | audit | `gcode_move.py`, `extruder.py` | Current active-extruder and saved-state code | Tool-change absolute E, restore policy, kinematic reset, symmetric limits |
| F7 | Mixed-axis G2/G3 arcs | audit | `gcode_arcs.py` | Current dynamic G-Code axis map | Endpoint, segmentation, feedrate, acceleration, absolute/relative mode tests |
| F8 | Heater PID sampling/regression | audit | `heaters.py`, README PID section | Current heater control classes | Noisy-signal unit tests, compatibility defaults, documented tuning |
| F9 | CNC configuration and migration docs | audit | CNC shield config and fork README | Current config conventions | Tested example, config reference, G-Code docs, migration guide |

### Secondary changes requiring scope decisions

The initial audit also found fork-specific work that is not central to the
README's main compatibility promise. Do not lose it, but confirm each item with
the user before spending substantial implementation effort.

| ID | Change found on `develop` | Initial disposition |
| --- | --- | --- |
| S1 | `manual_spinner.py` for continuous manual-stepper motion | Audit after F0; may fit current manual-stepper APIs |
| S2 | `pipettin.py` coordinate-file helper | Defer; application-specific file-writing behavior |
| S3 | Direct skew-factor commands and `use_lengths` option | Audit independently; likely a small isolated feature |
| S4 | `QUERY_HX71` convenience command | Audit against current load-cell/HX71 APIs |
| S5 | Extended G-Code help exposed through webhooks | Audit against current command/webhook help |
| S6 | `GET_STATUS_MSG` console helper and extra motion-report fields | Fold into status/reporting design where still useful |
| S7 | AVR compatibility fixes and requirement changes | Verify whether already present upstream; do not port blindly |
| S8 | Branding, funding, issue templates, install scripts, and old images | Defer until functional compatibility and release preparation |

## Feature integration strategy: extras first

The audit and F0–F2 confirm the fork's remaining behavior can be rebuilt as
Klipper *extras* (`klippy/extras/` modules) that alter core behavior through
existing extension points, with only `M211` (already landed with F2) requiring
a small core hook. Adopt this policy for F3–F9 and S1–S8:

1. **Extras first.** Implement each feature as a loadable extra rather than an
   edit to a core file (`toolhead.py`, `gcode.py`, `mcu.py`, `stepper.py`,
   `reactor.py`, kinematics, etc.).
2. **Core hook only when a check is embedded mid-method.** Reserve core edits
   for cases where one behavior is interleaved with unrelated logic inside a
   core method, making a wrapper unsafe. `M211` is the one instance found.
3. **Discard upstream drift.** Large `develop`↔`master` deltas in `mcu.py`,
   `stepper.py`, `reactor.py`, `mathutil.py`, and most of `toolhead.py` reflect
   `develop` predating upstream refactors, not fork features. Do not port them.

### Extension mechanisms available on `master`

- **Replace a G-Code command.** `gcode.register_command(cmd, None)` returns the
  previous handler; re-register a wrapper that delegates or replaces it
  (`safe_z_home.py`'s `G28` pattern).
- **Move-transform chain.** `gcode_move.set_move_transform(self)` composes
  per-move transforms (`skew_correction`, `bed_mesh`, `bed_tilt`,
  `exclude_object`).
- **Deferred connect hook.** `register_event_handler("klippy:connect", ...)`
  runs after `toolhead` exists, so an extra can register axes or wrap core
  singletons.
- **Singleton reach-in.** `printer.lookup_object("toolhead" | "gcode" |
  "gcode_move" | "homing" | "probe")`.
- **Motion primitives.** `homing.probing_move()`, `homing.manual_home()`, and
  `manual_stepper` endstop modes back the probing and homing slices.
- **Instance monkey-patching.** Wrap a looked-up object's method with a
  `functools.wraps` closure (e.g. `toolhead.get_status`) to extend behavior
  without editing core.

### Per-feature classification

| Feature | Legacy evidence | Strategy | Core edit? |
| --- | --- | --- | --- |
| F3/F4 | `probe_G38.py`, `probe_G38_multi.py` | New extra over `HomingMove` (imported directly) with a local axes-filtered no-movement check; register `G38.2`–`G38.5` handlers | no |
| F5 | `extruder_home.py`, `extruder.py` | New extra over `manual_stepper`/`homing` homing primitives | no |
| F6 | `gcode_move.py` `relative_e_restore`, `extruder.py` | Wrap `RESTORE_GCODE_STATE` and saved-state logic via `register_command(..., None)` | no (see note) |
| F7 | `gcode_arcs.py` | `gcode_arcs.py` is already an extra | no (extra file) |
| F8 | `heaters.py` | Edit `extras/heaters.py` (already an extra); re-derive numpy-free | no (extra file) |
| S1 | `manual_spinner.py` | Existing manual-stepper APIs | no |
| S3 | `skew_correction.py` `SET_SKEW_FACTORS` | Already an extra, self-contained | no (extra file) |
| S5 | `gcode.py`, `webhooks.py` | Wrap `gcode.get_command_help` plus the help webhook; a ~15-line `gcode.py` edit is the fallback | optional |
| S6 | `toolhead.py` status fields | Monkey-patch `toolhead.get_status()` | no |
| `M211` | `toolhead.py`, kinematics | Flag + accessor consulted in the `check_move` sites | **yes (minimal, landed with F2)** |
| `cartesian_abc.py` / `corexy_abc.py`, multi-toolhead experiments | — | Superseded by `generic_cartesian` + `extra_axes` | n/a (do not port) |

Notes: F6's tool-change absolute-E behavior may need a small `gcode_move.py`
touch if wrapping `RESTORE_GCODE_STATE` proves insufficient; decide during the
F6 slice and record it. S2 (`pipettin.py`), S4 (`QUERY_HX71`), S7 (AVR fixes),
and S8 (branding/docs) are covered by their existing defer/audit dispositions
and are omitted from this table.

## Recommended implementation order

1. F0 proves the current upstream primitives and creates reusable test fixtures.
2. F1 and F2 establish the axis model, homing state, limits, and reporting.
3. F3 and F4 build probing on the settled axis/homing abstractions.
4. F5 and F6 address extruders without entangling them with the ABC design.
5. F7 follows once mixed-axis timing semantics are stable.
6. F8 can proceed independently after its behavior and tuning contract are
   specified.
7. F9 evolves with every slice and finishes with migration and release docs.

This order may change after F0. Record the reason in the decision log rather
than silently rewriting dependencies.

## Acceptance questions to settle during F0/F1

- Are A/B/C linear distances, rotary angles, or opaque axis units? Can each axis
  declare its own unit in status and documentation?
- Does `F` describe XYZ path speed, full N-dimensional path speed, or move time
  when XYZ and ABC move together?
- How should an ABC-only move derive duration and acceleration?
- Are additional axes acceleration-limited by default? How are impossible
  machine settings rejected?
- Must the exact legacy config (`kinematics_abc`, `axis`, `[stepper_a]`) remain
  valid, or is a documented migration to current manual-stepper-style sections
  acceptable?
- Must `G28 A` be supported, and should multi-axis homing be simultaneous or
  ordered?
- Should M211 disable every software boundary or only selected CNC axes?
- Which behavior must remain compatible with Mainsail/Moonraker status models?

Until answered, preserve legacy syntax in tests as desired behavior, but do not
cement it into implementation merely for convenience.

## Test strategy

Every motion feature needs a batch regression fixture under `test/klippy/`.
Prefer extending upstream-style fixtures over restoring the legacy test README
or its stale dictionaries. Generated dictionaries are external test inputs;
keep them outside Git unless repository policy changes.

For each feature, cover:

- configuration success and configuration rejection;
- absolute and relative motion modes;
- mixed XYZ/extra-axis and extra-axis-only movement;
- homed, unhomed, limit-edge, and out-of-range behavior;
- command/status output needed by frontends;
- shutdown or motor-off state reset;
- unchanged behavior when the new feature is absent.

Inspect serialized batch output when timing or step generation matters. Reserve
physical tests for a protected machine with conservative speeds, reachable
emergency stop, validated endstop polarity, and one new axis or probe behavior
at a time.

## Decision log

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-09-10 | Create `cnc-reimplementation` directly from `master` | Keeps the new implementation on the current upstream architecture |
| 2026-09-10 | Keep `develop` read-only and prohibit wholesale replay | The branch mixes years of upstream merges, experiments, debug notes, and coupled core edits |
| 2026-09-10 | Begin with an upstream extra-axis capability spike | Current Klipper already implements dynamic G-Code axes and synchronized manual steppers |
| 2026-09-10 | F0 tests run against a CI-built current-master dictionary | `data-dict` artifact from build-test run #49 (`2d7717e`) supplied a valid `atmega2560.dict` without an AVR toolchain |
| 2026-09-10 | F0 spike complete; four fixtures pass | Success + three SHOULD_FAIL fixtures document G1, limit, unregister, and endstop behavior |
| 2026-09-10 | A G-Code-registered manual stepper cannot be commanded directly | `cmd_MANUAL_STEPPER` requires `GCODE_AXIS=` unregister first; tests must unregister before MOVE/STOP_ON_ENDSTOP |
| 2026-09-10 | Extra-axis move timing: mixed moves use XYZ Euclidean length, extra-only moves use max extra-axis displacement | Confirmed from `Move.move_d` and `ToolHead.move`; F0 fixtures exercise both |
| 2026-09-10 | F1 uses declarative ABC via current `extra_axes` (no dual-kinematics port) | User chose to auto-register config-declared A/B/C as extra axes rather than port `kinematics_abc`/dual-trapq |
| 2026-09-10 | Declarative ABC syntax is `[manual_stepper <name>]` + `gcode_axis: <A/B/C>` | Reuses the proven manual-stepper primitives; legacy `kinematics_abc`/`axis`/`[stepper_a]` gets a migration note (F9) |
| 2026-09-10 | Declarative registration deferred to a `klippy:connect` handler | `toolhead` is created last in `_read_config`, so `[manual_stepper]` `__init__` cannot register an axis; connect-time registration shares the runtime registration path |
| 2026-09-10 | Partial base XYZ ("XY" without Z) deferred out of F1 | Current `CartKinematics` is hardcoded to `xyz`; the chosen extra-axis approach keeps XYZ fixed, so partial XYZ is a separate, larger change |
| 2026-09-10 | Bare `G28` homes XYZ plus every homeable extra axis, ordered and sequential | Matches `develop`; extra axes home after the XYZ kinematic pass |
| 2026-09-10 | `G28 <A/B/C>` routes to the matching extra axis's `home()` | Reuses `HomingMove` with the manual stepper acting as a mini-toolhead, matching `develop`'s per-axis path |
| 2026-09-10 | "Must home axis first" applies only to registered, homeable axes; non-homeable and unregistered axes are exempt | `check_move` only runs for axes with non-zero `axes_d`, and `can_home=False` axes skip the guard |
| 2026-09-10 | `M211` gates only out-of-bounds checks, not "must home first" | Faithful to `develop` (`limit_checks_enabled` defaults true) |
| 2026-09-10 | `M84`/`motor_off` clears XYZ plus every extra-axis gcode id from homing state | `ToolHead.clear_homing_state` fans out to kinematics and each extra axis |
| 2026-09-10 | `toolhead.homed_axes` stays XYZ-only; extra-axis homing state is exposed per axis object | Mainsail's `ZoffsetControl` uses an exact `homed_axes === 'xyz'` check for babystepping `MOVE=1`, so appending A/B/C would silently break it; Moonraker only does substring membership checks but the exact match is decisive |
| 2026-09-10 | `M114`/`GET_POSITION` report extra axes after `E` (`X Y Z E A B C`) | Preserves the upstream `X Y Z E` token prefix that existing console/position parsers rely on; `develop` inserts A/B/C before E, which shifts the E token |
| 2026-09-10 | Adopt an extras-first policy for F3–F9 and S1–S8 | Every remaining feature fits an existing extension point; core edits are reserved for mid-method checks like `M211` (already landed with F2) |
| 2026-09-10 | Discard `develop` core deltas that are upstream drift | `mcu.py`, `stepper.py`, `reactor.py`, `mathutil.py`, and most of `toolhead.py` differ because `develop` predates upstream refactors, not because of fork features |
| 2026-09-10 | F3/F4/F5/F7/F8/S1/S3/S6 are pure extras; F6 and S5 use command/method wrapping | Reuse `homing.probing_move`, manual-stepper homing, `gcode_arcs.py`, `heaters.py`, and `get_status` wrapping instead of core edits |
| 2026-09-10 | F3 is a pure extra that imports `HomingMove` directly instead of `homing.probing_move` | `probing_move` hardcodes `probe_pos=True` and cannot forward `triggered`/`check_triggered`, and its `check_no_movement` has no axis filter; importing `HomingMove` and doing a local axes-filtered no-movement check avoids a core edit |
| 2026-09-10 | F3 maps G38.2/3/4/5 to `homing_move(triggered=, check_triggered=)` | `triggered`=trigger-invert, `check_triggered`=error-out; current `home_wait` returns `0.0` for a clean no-trigger so `check_triggered=False` reports "ended without trigger" without the legacy timeout-string mapping |
| 2026-09-10 | F3 scope is XYZ-only; E probing and named probes deferred | Matches `develop`'s working G38 (it left a "TODO: update G38 to work with ABC axis"); extruder probing belongs to F5/F6 and named probes to F4 |

## Per-slice handoff template

Add a short roadmap note or commit/PR description containing:

```text
Feature:
Behavioral contract:
Current upstream capability:
Legacy evidence inspected:
Design decision:
Tests run and result:
Hardware validation:
Known gaps / next slice:
```
