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
| F4 | Multiple named directional probes | done | `probe_G38_multi.py` | F3; current pin/endstop registration | Named selection, active-tool selection, query/status, conflict tests |
| F5 | Home-able extruder steppers | automated | `extruder_home.py`, `extruder.py` | Extra-axis and current homing primitives | Multiple extruders, both directions, retract/second home, error recovery |
| F6 | Extruder coordinate and limit semantics | automated | `gcode_move.py`, `extruder.py` | Current active-extruder and saved-state code | Tool-change absolute E, restore policy, kinematic reset, symmetric limits |
| F7 | Mixed-axis G2/G3 arcs | automated | `gcode_arcs.py` | Current dynamic G-Code axis map | Extra axes travel alongside the arc in all three planes and land exactly on the target; an un-homed extra axis is rejected; upstream `gcode_arcs.test` unchanged and passing |
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

## Motion fault recovery and reversibility (design proposal)

The audit above stops at feature parity. Separately, an investigation of
Klipper's failure modes found that the three motion faults a CNC machine is
most likely to hit — `Timer too close`, `Lost communication` / `Missed
scheduling of next …`, and zero-interval `queue_step` / `Stepper too far in
past` — are all fatal in current Klipper. Each ends in
`sched_shutdown()`/`invoke_shutdown()`, and the only exit is
`RESTART`/`FIRMWARE_RESTART`, which discards every host-side position authority.
A stock CNC controller instead feed-holds, retains the authoritative position,
and resumes.

[CNC_Motion_Recovery.md](CNC_Motion_Recovery.md) documents the full
investigation, the evidence (including the finding that homing already
implements the required interrupt → read-back → resync loop), and a phased
design for controlled deceleration, fault recovery, and reversible/pendant-step
motion. It is **not yet scheduled** and is outside the F0–F9 compatibility
promise; it is recorded here so the feature rows above are not mistaken for the
whole design space. Its phases 1–2 follow the extras-first policy below; only
phase 3 needs `src/` edits.

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
| F3/F4 | `probe_G38.py`, `probe_G38_multi.py` | New extras over `HomingMove` (imported directly) with a local axes-filtered no-movement check; F4 adds named mux commands and active-probe dispatch | no |
| F5 | `extruder_home.py`, `extruder.py` | Rail-backed `ExtruderStepper` plus an `ExtruderHoming` toolhead adapter over the `manual_stepper`/`homing` homing primitives, folded into `kinematics/extruder.py` | no |
| F6 | `gcode_move.py` `relative_e_restore`, `extruder.py` | Two new `[printer]` booleans gating existing rebases in `gcode_move.py`, one new `[extruder]` boolean in `kinematics/extruder.py`, plus an extruder branch in `force_move.py`'s `SET_KINEMATIC_POSITION` | no (existing extras and kinematics only) |
| F7 | `gcode_arcs.py` | `gcode_arcs.py` is already an extra; interpolate every registered extra axis linearly alongside the arc and snap the last segment to the exact commanded target | no (extra file) |
| F8 | `heaters.py` | Edit `extras/heaters.py` (already an extra); re-derive numpy-free | no (extra file) |
| S1 | `manual_spinner.py` | Existing manual-stepper APIs | no |
| S3 | `skew_correction.py` `SET_SKEW_FACTORS` | Already an extra, self-contained | no (extra file) |
| S5 | `gcode.py`, `webhooks.py` | Wrap `gcode.get_command_help` plus the help webhook; a ~15-line `gcode.py` edit is the fallback | optional |
| S6 | `toolhead.py` status fields | Monkey-patch `toolhead.get_status()` | no |
| `M211` | `toolhead.py`, kinematics | Flag + accessor consulted in the `check_move` sites | **yes (minimal, landed with F2)** |
| `cartesian_abc.py` / `corexy_abc.py`, multi-toolhead experiments | — | Superseded by `generic_cartesian` + `extra_axes` | n/a (do not port) |

Notes: F6 needed no wrapping after all — the three behaviors that change an
existing computation live in files that are already extras or kinematics
(`gcode_move.py` and `kinematics/extruder.py`), so the rebases and the
extrusion-limit branch were gated directly there and no command is
re-registered. S2 (`pipettin.py`), S4 (`QUERY_HX71`), S7 (AVR fixes),
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
  when XYZ and ABC move together? **Settled for arcs (F7):** the XYZ path rate,
  with ABC slaved to start and stop with XYZ (NIST RS274/NGC §2.1.2.5).
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

### Local MCU data dictionaries

`scripts/test_klippy.py` needs a directory of MCU data dictionaries (`-d`) and
the `.test` files reference them by name (for example
`DICTIONARY atmega2560.dict`). Those dictionaries are binary build outputs, not
source, so they are deliberately **not** committed. This machine has no
`avr-gcc`, so `./scripts/ci-build.sh` cannot regenerate them locally and a
cached copy is required to run any regression test at all.

The cache lives at the same path the CI build script populates and the
`build-test` workflow uploads as the `data-dict` artifact:

| Path | Contents |
| --- | --- |
| `ci_build/dict/` | 42 extracted `*.dict` files, ready for `-d ci_build/dict` |
| `ci_build/data-dict.zip` | The pristine `data-dict` CI artifact they were extracted from |

`/ci_build/` is git-ignored, so the cache never enters the repository and never
appears in `git status`.

Standard invocations:

```shell
python3 scripts/test_klippy.py -d ci_build/dict test/klippy/<test>.test
python3 scripts/test_klippy.py -d ci_build/dict test/klippy/*.test
```

To refresh or rebuild the cache:

- **Preferred, needs the toolchain:** run `./scripts/ci-install.sh` followed by
  `./scripts/ci-build.sh`, which repopulates `ci_build/dict/` in place without
  deleting anything.
- **Without the toolchain:** download the `data-dict` artifact from the
  `build-test` workflow run of this (or the upstream) repository and extract it
  into `ci_build/dict/`:

  ```shell
  gh run download --repo <owner>/<repo> --name data-dict --dir ci_build/dict
  ```

  or download the artifact zip from the Actions UI and run
  `unzip data-dict.zip -d ci_build/dict`.

The cached dictionaries are tied to the upstream MCU protocol they were built
from. Re-download them after pulling a large upstream change or after any
change to `src/`, otherwise batch tests can silently exercise an older
protocol than the host code expects. `test/klippy/load_cell.test` additionally
needs the `numpy` and `scipy` Python modules in the interpreter running klippy.

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
| 2026-09-10 | F4 uses `[probe_G38_multi <name>]` with muxed named commands | `MULTIPROBE_TOWARD`, `MULTIPROBE_TOWARD_NOERROR`, `MULTIPROBE_AWAY`, and `MULTIPROBE_AWAY_NOERROR` carry `PROBE_NAME=...`; numeric legacy names are not parseable by current G-Code command parsing |
| 2026-09-10 | F4 regular `G38.*` commands select by active extruder name, then explicit `SET_PROBE_G38`, then first configured probe | This keeps F4 independent of F5 while allowing extruder/probe naming to become automatic when home-able extruders are implemented |
| 2026-09-11 | F5 folds extruder homing into `kinematics/extruder.py` instead of porting the `[extruder_homing]` extra | The extruder stepper itself must become homeable (rail-backed) and own its homed state, so a separate module would have to reach into private state; a rail-backed `ExtruderStepper` plus a small `ExtruderHoming` toolhead adapter keeps the change local and needs no new config section |
| 2026-09-11 | A homeable extruder is enabled purely by adding `endstop_pin` to `[extruder]`/`[extruder1]` | Removes `develop`'s mandatory `[extruder_homing <name>]` section while keeping the same command set (`G28 E`, `HOME_EXTRUDER`, `HOME_ACTIVE_EXTRUDER`); `can_home` exposes whether the stepper has an endstop |
| 2026-09-11 | `HomingMove` drives the extruder via a `PrinterExtruder`-shaped adapter rather than the toolhead | Mirrors `manual_stepper`, the established pattern for homing a stepper that is not one of the XYZ rails; leaves the active toolhead's trapq and axis mapping untouched, so homing an inactive extruder cannot perturb the active axis |
| 2026-09-11 | Extended F5 with `homeable_extruder*.test` fixtures (min/max direction, must-home, bounds, `M84`, non-homeable `G28 E`) | `develop` carries no automated coverage for extruder homing; these lock in both directions, retract/second home, and error recovery |
| 2026-09-11 | Deviation from `develop`: `G28 E` on a non-homeable extruder raises `No endstop for extruder '<name>'` | Current master's `cmd_G28` routes an explicit axis letter straight to the extra axis's `home()`, while `develop` silently ignored `E` and homed XYZ; the explicit error is clearer and keeps master's semantics |
| 2026-09-11 | Deviation from `develop`: "Must home extruder axis first" is not gated by `M211` | Matches the house style for extra axes (`ManualStepper.check_move`, `CartKinematics._check_endstops`); `M211` still gates the out-of-bounds check |
| 2026-09-11 | Cache the MCU data dictionaries at `ci_build/dict/` (git-ignored), alongside the pristine `data-dict.zip` CI artifact | Dictionaries are binary build outputs and stay out of Git, but this machine has no `avr-gcc`, so a cached copy is required to run any regression test; `ci_build/dict` is the exact path `scripts/ci-build.sh` populates, so a toolchain-enabled refresh is a no-op copy. See "Local MCU data dictionaries" |
| 2026-09-11 | F6 keeps every changed behavior opt-in: the new `relative_e_restore` and `tool_change_e_reset` `[printer]` options default to **True** and `symmetric_speed_limits` defaults to **False**, so a machine that does not set them behaves exactly like upstream | `develop` hardcodes the tool-change change by deleting the rebase line, which would silently alter every existing printer's G-Code; the compatibility target requires "unchanged behavior when the new feature is absent", so the removal becomes an opt-out instead. `relative_e_restore` keeps `develop`'s name, section, and default |
| 2026-09-11 | F6 reads both new `[printer]` options via `config.getsection('printer')` inside `GCodeMove.__init__` | `gcode_move` is constructed with its own `[gcode_move]` section, and `printer` is an extra section that may be absent; `getboolean` on the `printer` wrapper still applies the default in that case |
| 2026-09-11 | F6 `SET_KINEMATIC_POSITION E=` marks the extruder axis homed when the value is given explicitly | An extruder may have no endstop to home against, so this is the only way to leave the un-homed state after `M84`; `SET_HOMED`/`CLEAR_HOMED` still win when they name (or omit) `e`, and `CLEAR`/`CLEAR_HOMED` take precedence over the explicit value, matching upstream's ordering |
| 2026-09-11 | F6 extends `SET_KINEMATIC_POSITION` to `E` only, not to the ABC axes | `G92` and `SET_GCODE_OFFSET` remain `XYZE`-only, so adding ABC here would make the three commands inconsistent; ABC axes can already be un-homed and re-homed with `G28 <axis>` (F2). The extruder is the one axis where upstream provides no homing path |
| 2026-09-11 | F6 edits `force_move.py`'s `cmd_SET_KINEMATIC_POSITION` in place rather than wrapping the command | The command is owned by an extra and its XYZ path is unchanged, so a wrapper would duplicate the axis parsing for no isolation benefit; the `[force_move] enable_force_move` gate is preserved, so the command still does not exist unless the user asks for it |
| 2026-09-11 | F6 reuses `homeable_extruder.cfg` for the stock-semantics `SHOULD_FAIL` fixtures | The stock behavior *is* the default configuration, so a second near-identical config would only add drift; `extruder_coordinates.cfg` carries the opt-in options and all fork-semantics fixtures. `test_klippy.py` needs one file per expected failure because a file aborts at its first failing case, and a single file cannot hold several inline-GCode `CONFIG` blocks (the shared `GCODE`-file form is the only supported multi-case shape) |
| 2026-09-11 | F7 slaves every registered extra axis to the arc, interpolating it linearly and snapping the final segment to the exact commanded target | Mirrors how the helical axis and the extruder already behave, so an arc is just a segmented move with the extra axes interpolated alongside it; the snap removes the accumulated float error `develop` leaves at the arc end |
| 2026-09-11 | F7 keeps `F` as the XYZ path rate and does **not** port `develop`'s extra-axis `feedrate_factor` | NIST RS274/NGC §2.1.2.5: while XYZ move, `F` is the XYZ cartesian rate and ABC only have to start and stop with them. `develop` needs its factor because its `toolhead.Move.move_d` sums `axes_d[:-1]` (XYZABC); our `move_d` sums `axes_d[:3]` (XYZ only) and bounds each extra axis independently in `manual_stepper.check_move`, so the factor would make arcs about 3x too fast here. Inverse-time feed (`G93`) is the standard way to make `F` govern a whole move |
| 2026-09-11 | F7 fixes `develop`'s extra-axis endpoint bug instead of reproducing it | `develop` advances each axis by `target / segments` from the current position, which lands on `currentPos + target` whenever the arc starts away from zero. The port interpolates `(target - currentPos)` and keeps the final-segment snap |
| 2026-09-11 | F7 does not port `develop`'s `cmd_G2/G3/G17/G18/G19_help` attributes or its per-segment `/tmp/gcode.log` logging | The help strings are inert on `develop` (its `register_command` calls pass no `desc=`, and `gcode.py` only records a description supplied at registration), so porting them would add dead code; surfacing command help is S5. The file logging is a debugging artifact |
| 2026-09-11 | F7 adds `gcode_arcs_abc.cfg`, `gcode_arcs_abc.test` and `gcode_arcs_abc_unhomed.test`, and leaves upstream's `gcode_arcs.cfg`/`.test` byte-identical | `test_klippy.py` only inspects exit status, so the `SHOULD_FAIL` fixture leaves the endstop-equipped B axis un-homed: an arc carrying `B` must be rejected, and it is that fixture (not the positive one) which discriminates — verified by running it against `master`'s `gcode_arcs.py`, where it fails with "Test failed to raise an error". The positive fixture instead uses a tight `position_max: 60` on axis A so that a wrong endpoint becomes a hard "Move out of range"; re-introducing `develop`'s endpoint arithmetic makes it fail, which is the regression guard |

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
