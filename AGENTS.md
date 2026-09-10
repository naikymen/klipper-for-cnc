# Klipper for CNC agent guide

## Mission

Reimplement the CNC and lab-automation capabilities from `develop` on top of
current upstream Klipper. The working integration branch is
`cnc-reimplementation`, which was created from `master`. Treat `develop` as a
read-only behavioral reference and history archive.

The goal is a maintainable implementation shaped around current Klipper APIs.
Do not merge `develop`, rebase onto it, or replay its commits. Do not copy a
large old patch into current code. A small self-contained module may be reused
only after checking every dependency against `master` and adapting it to the
current architecture.

Read [docs/CNC_Reimplementation.md](docs/CNC_Reimplementation.md) before
planning or changing a feature. Update its status table and decision log when a
feature's scope, design, compatibility, or verification status changes.

## Sources of truth

Use these in descending order:

1. The user's current request and stated machine behavior.
2. Current `master` code, documentation, and tests.
3. Observable behavior and user-facing contracts documented on `develop`.
4. Implementation details and commit history on `develop`.

The old implementation is evidence, not the target design. If its README and
code disagree, record the discrepancy and ask only when it changes a public
contract or a motion-safety decision.

## Branch and repository safety

- Preserve `master` as the upstream mirror and preserve `develop` as the legacy
  reference. Never commit to either branch.
- Work on `cnc-reimplementation` or on a narrowly named topic branch based on
  it. Before edits, inspect `git status --short --branch` and do not overwrite
  unrelated user changes.
- Keep each feature slice reviewable and independently testable. Avoid drive-by
  formatting, renamed files, or unrelated upstream cleanup.
- Do not fetch, merge, rebase, push, or change remotes unless the user asks.
- Use `scripts/cnc-reference.sh` for common read-only comparisons. Direct
  `git show`, `git log`, and `git diff` commands are also appropriate.

## Workflow for each feature slice

1. Pick one row from the roadmap and write its behavioral acceptance criteria.
2. Audit current `master` first. Determine what upstream now provides and what
   gap remains. Prefer composing current mechanisms over adding parallel ones.
3. Inspect the smallest useful legacy surface: README section, focused file
   diff, tests, and relevant commits. Do not begin with the full branch diff.
4. Add or adapt a regression test that demonstrates the gap when practical.
   Include negative cases for invalid configuration, limits, and homing state.
5. Implement the smallest coherent change using current APIs.
6. Update all affected reference documentation and config examples in the same
   slice.
7. Run focused tests, the import test, and whitespace checks. Expand to the
   complete regression suite when shared motion, homing, probing, G-Code state,
   or MCU protocol code changes.
8. Record results and remaining hardware validation in the roadmap.

## Current architectural direction

Current `master` already has dynamic extra-axis support in `ToolHead`,
`GCodeMove`, and `manual_stepper`. In particular, `MANUAL_STEPPER ...
GCODE_AXIS=<letter>` synchronizes an extra axis with `G1`, and manual steppers
have endstop-assisted move modes. Evaluate these facilities before designing
ABC support.

`generic_cartesian` generalizes the relationship between steppers and the
three spatial XYZ carriages; it does not by itself add ABC coordinates. The
legacy `cartesian_abc.py`, second-kinematics arrangement, and expanded fixed
position vectors are therefore not assumed to be the new foundation.

Keep these concepts distinct:

- XYZ tool-space motion and its Euclidean path/feedrate semantics.
- Additional synchronized axes and their own velocity, acceleration, corner,
  homing, and limit policies.
- Extrusion semantics, which have temperature, flow, and relative-coordinate
  behavior that a general CNC axis should not inherit accidentally.

Do not silently change feedrate or acceleration semantics. Specify and test how
mixed XYZ/ABC moves, ABC-only moves, junctions, and arcs are timed before
altering motion planning.

## Motion and hardware safety

- Treat homing direction, endstop polarity, soft limits, coordinate resets,
  probe trigger sense, and move timing as safety-critical behavior.
- Regression tests and batch-mode output do not prove behavior on hardware.
  Label hardware-dependent work as unverified until the user reports a test on
  a protected machine.
- Never weaken limit or homing checks merely to make a test pass. Features such
  as `M211` require conspicuous state reporting, conservative defaults, and
  explicit tests.
- Preserve normal 3D-printer behavior when CNC features are not configured.
  Backward compatibility is an acceptance criterion for every shared-core edit.

## Klipper conventions and verification

Follow `docs/CONTRIBUTING.md`, `docs/Code_Overview.md`, and nearby code style.
New commands and parameters must be documented in `docs/G-Codes.md` and
`docs/Config_Reference.md`; status fields and incompatible changes require the
corresponding reference updates.

Useful checks, from cheapest to broadest:

```shell
python3 klippy/klippy.py --import-test
./scripts/check_whitespace.sh
python3 scripts/test_klippy.py -d <dictionary-directory> test/klippy/<test>.test
python3 scripts/test_klippy.py -d <dictionary-directory> test/klippy/*.test
./scripts/ci-build.sh
```

The full CI script expects dependencies and toolchains installed by
`scripts/ci-install.sh`. Do not install them or download data dictionaries
without the user's approval. Report exactly which checks ran and which could
not run.

## Completion standard

A feature is complete only when its contract is documented, automated coverage
passes, normal Klipper configurations remain compatible, and the roadmap links
the evidence. Track physical-machine testing separately; never imply it was
performed by an agent.
