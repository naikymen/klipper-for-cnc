# CNC motion recovery and reversible motion

This document records the failure-mode investigation that led to a proposed
architecture for **controlled deceleration, recovery, and reversible motion**
in this fork. It is a design proposal, not yet a settled plan; see
[CNC_Reimplementation.md](CNC_Reimplementation.md) for the durable roadmap and
the decision log where this work will be scheduled.

Related: [failure modes](#1-background-why-klippers-current-failures-are-unrecoverable),
[standard CNC behavior](#2-how-standard-cnc-machines-handle-this),
[the homing-pattern discovery](#3-the-key-discovery-homing-already-implements-the-recovery-loop),
[proposed architecture](#4-proposed-architecture),
[phased plan](#5-phased-implementation-plan),
[risks](#6-risks--open-questions).

## 1. Background: why Klipper's current failures are unrecoverable

Klipper does all trajectory planning on the host and streams *pre-timed* step
commands to the MCU. The host therefore owns three logically separate notions
of "where the tool is":

| Authority | Location | Survivability |
| --- | --- | --- |
| Host commanded position (`toolhead.commanded_pos`, `gcode_move.last_position`) | host RAM | lost on `RESTART` |
| Iterative-solver position (`sk->commanded_pos` in `itersolve.c`) | host RAM | lost on `RESTART` |
| Compressed step history (`stepcompress.history_list`) | host RAM | lost on `RESTART` |
| **True step count** (`stepper.position` in the MCU, via `POSITION_BIAS`) | MCU RAM | **survives** a motion fault; lost only on power loss |

The three failure modes studied all terminate the same way:
`sched_shutdown()` on the MCU runs every `DECL_SHUTDOWN` handler (steppers
stopped, heaters off), and `Printer.invoke_shutdown()` on the host sets
`in_shutdown_state = True`. The only exit is `FIRMWARE_RESTART`/`RESTART`,
which discards all four host-side authorities. The MCU's true step count
survives but is never re-read, so position is lost.

### 1.1 "Timer too close"

An MCU-side fault raised in [sched.c](../src/sched.c):

```c
if (timer_is_before(waketime, timer_read_time()))
    try_shutdown("Timer too close");
```

When the host schedules a step timer whose wake time is *already in the past*
relative to the MCU clock, the MCU shuts down immediately. The hint in
[error_mcu.py](../klippy/extras/error_mcu.py) confirms the intent: *"often
indicates the host computer is overloaded."*

Root cause: because every motion decision is made on a non-realtime host, a
host stall (CPU spike, GC, swap, competing process) makes the clock prediction
drift behind reality, so a step is scheduled in the past. The MCU treats this
as fatal rather than degrading gracefully.

### 1.2 Communication failures

Two variants:

- **Host-side timeout** — [mcu.py](../klippy/mcu.py) `check_active()` fires
  `invoke_shutdown("Lost communication with MCU '%s'")` when the MCU stops
  answering clock queries.
- **MCU-side "Missed scheduling of next …"** — e.g.
  [gpiocmds.c](../src/gpiocmds.c), [pwmcmds.c](../src/pwmcmds.c),
  [linux/hard_pwm.c](../src/linux/hard_pwm.c): a scheduled output event is
  already in the past because the command arrived too late over the serial
  link. [error_mcu.py](../klippy/extras/error_mcu.py) classifies this as
  *"intermittent communication failure."*

Both are fatal, and a single dropped or late byte kills the job.

### 1.3 Zero-interval step commands

A step command is `queue_step oid=%c interval=%u count=%hu add=%hi`. An
`interval=0` command means "two consecutive step pulses at the **same** clock
tick", i.e. the requested step rate exceeds the MCU's clock resolution or the
configured `step_pulse_duration`.

This fails in two places:

1. **Host side**, [stepcompress.c](../klippy/chelper/stepcompress.c)
   `check_line()`:

   ```c
   if (!move.count || (!move.interval && !move.add && move.count > 1) ...)
       errorf("stepcompress ... Invalid sequence");
   ```

   This raises `"Internal error in stepcompress"`, which becomes a host
   shutdown.

2. **MCU side**, [stepper.c](../src/stepper.c) `stepper_event_full()`. The
   firmware already tries to *soft-clip*: if `next_step_time` is too close it
   is pushed back to `min_next_time` (`reschedule_min`). But if it falls more
   than 1000 µs behind, it gives up:

   ```c
   if (diff < (int32_t)-timer_from_us(1000))
       shutdown("Stepper too far in past");
   ```

Note the compression heuristic in `compress_bisect_add()` actually *prefers*
`add = 0` sequences (`zerocount`), so a too-fast feed naturally collapses
toward `interval = 0` and trips this fault.

### 1.4 The common thread

All three converge on one path: MCU `sched_shutdown()` → `longjmp` →
`run_shutdown()`, which runs every `DECL_SHUTDOWN` handler. On the host,
`invoke_shutdown()` sets the shutdown state and the only escape is
`FIRMWARE_RESTART`/`RESTART`, which discards all motion state and job progress.

There is also a related "silent" fault: MCU `move_alloc()` returns
`"Move queue overflow"` ([basecmd.c](../src/basecmd.c)) when the host queues
more commands than the firmware's pre-allocated move pool can hold.

## 2. How standard CNC machines handle this

| Aspect | Klipper (as-is) | Standard CNC (LinuxCNC, Fanuc, Haas, GRBL) |
| --- | --- | --- |
| Motion planning location | Host (non-realtime Python) | Hard-realtime core (RTOS/fieldbus) |
| Position authority | Host's *predicted* clock + MCU step counter | Encoder/step count is authoritative and retained |
| On host lag | **Fatal** ("Timer too close") | **Feed hold** — decelerate to a stop, keep spindle |
| On link glitch | **Fatal** ("Lost communication") | Buffered/cycle-consistent bus; transient errors tolerated |
| On over-speed request | **Fatal** (interval=0 / "too far in past") | Clamped to max feed by the planner, never faults |
| Recovery after fault | Full restart; job state lost | Feed-hold → resume, or "run from here / line N" with re-acquire |

The key philosophical difference: CNC controllers treat the *trajectory* as
authoritative and degrade gracefully under load (feed-hold) rather than
dropping the machine into estop. A transient host hiccup never destroys the
machine's knowledge of where the tool is.

The design principle for the new branch is therefore:

> **The MCU step count is the single source of truth.** Every host-side
> position authority must be *re-derivable* from it at any time, and must be
> re-synchronized to it after any interruption. A motion fault becomes a
> "hold + resync + continue", not a shutdown.

## 3. The key discovery: homing already implements the recovery loop

`HomingMove.homing_move()` (in [homing.py](../klippy/extras/homing.py)) is a
complete "interruptible move with position read-back and resync" and is the
template to generalize. Its sequence:

1. `toolhead.drip_move(newpos, speed, drip_completion)` — commit motion in
   small bounded chunks (see `ToolHead._update_drip_move_time` and
   `DRIP_SEGMENT_TIME = 0.050` in [toolhead.py](../klippy/toolhead.py)).
2. On the `drip_completion` firing (endstop), `_update_drip_move_time` raises
   `DripModeEndSignal`; `drip_move` then:
   - `lookahead.reset()` — discard the un-executed host move queue,
   - `trapq_finalize_moves(trapq, NEVER, 0)` — flush the trapezoid queue,
   - `extruder.update_move_time(NEVER, 0)`.
3. `StepperPosition.note_home_end()` reads back the truth:
   - `halt_pos = stepper.get_mcu_position()` (MCU authoritative count),
   - `trig_pos = stepper.get_past_mcu_position(trigger_time)` (from
     `stepcompress_find_past_position`, interpolating the compressed history).
4. The toolhead position is re-anchored with `toolhead.set_position(haltpos)`.

The MCU side of this loop is `trsync` ([trsync.c](../src/trsync.c),
`MCU_trsync` in [mcu.py](../klippy/mcu.py)): `stepper_stop_on_trigger` →
`stepper_stop()` ([stepper.c](../src/stepper.c)), which cancels the stepper
timer, flushes its move queue, and **preserves** `stepper.position`; then
`trsync_state` reports the stop reason and clock.
`MCU_trsync.REASON_HOST_REQUEST` (= 2) already exists for exactly a
host-commanded stop.

`note_homing_end()` in [stepper.py](../klippy/stepper.py) is the resync
primitive: `stepcompress_reset` + `reset_step_clock oid=… clock=…` +
`_query_mcu_position` + `stepcompress_set_last_position`. This is the
"re-anchor the clock and position" step needed for recovery.

**Conclusion:** recovery does not need a new mechanism. It needs the homing
loop generalized from "stop on endstop" to "stop on any fault / hold / reverse
event".

## 4. Proposed architecture

### 4.1 A journal of segments (the new layer)

Introduce a **motion journal**: every move admitted by `ToolHead.move()` is
recorded as a `MotionSegment` with:

- its time/velocity envelope (already computed by `Move`): `accel_t`,
  `cruise_t`, `decel_t`, `start_v`, `cruise_v`, `end_v`,
- axis direction vector and `move_d`,
- a monotonic `segment_id`,
- the `print_time` range it occupies,
- the step-history span it maps to (`first_clock`/`last_clock` per stepper,
  via a new hook in `add_move()` in
  [stepcompress.c](../klippy/chelper/stepcompress.c)).

The journal is the **replayable/reversible source**. It retains segment
definitions for a configurable window (`reverse_window` seconds, default e.g.
5–30 s), mirroring the existing `MOVE_HISTORY_EXPIRE = 30.` step-history
window.

### 4.2 The commit-horizon / hold state machine (new class `MotionControl`)

Reuse `special_queuing_state` and generalize the drip pattern into a supervisor
with states:

```text
RUNNING ──(fault | FEED_HOLD | reverse request)──► HOLDING ──► HELD
   ▲                                                     │
   └────────────(RESUME | RECOVER)───────────────────────┘
```

- **HOLDING**: stop admitting new moves; inject a bounded deceleration ramp
  (using the `force_move.calc_move_time` + `trapq_append` pattern in
  [force_move.py](../klippy/extras/force_move.py)) at the current velocity;
  keep draining the already-committed stepcompress horizon.
- **HELD**: when the committed horizon has drained (or via a `trsync`
  host-request stop), send `stepper_stop_on_trigger`/host-request trigger, then
  read back every stepper's `get_mcu_position()`.
- **RECOVER**: run the `note_homing_end()` resync on every stepper, reconcile
  `toolhead.commanded_pos`, `gcode_move.last_position`, and the kinematics
  `commanded_pos` from the true step counts (the homing `calc_toolhead_pos`
  pattern generalized to all axes).
- **RESUME**: re-plan the un-executed tail of the journal from the reconciled
  position and re-admit it.

The crucial invariant, borrowed from drip mode: **the committed stepcompress
horizon is always small** (tens of ms), so a fault only ever invalidates a
tiny, trivially re-plannable window.

### 4.3 Fault → recovery bridge (replacing the fatal path)

Route the three motion faults into the hold state machine instead of
`invoke_shutdown`, while keeping genuine safety faults (thermal runaway,
heater, endstop wiring, power) on the fatal path:

- `Lost communication` (`MCU.check_active`) → HOLDING (stop generating; keep
  MCU state);
- `Timer too close` / `Rescheduled timer in the past` / `Stepper too far in
  past` ([sched.c](../src/sched.c), [stepper.c](../src/stepper.c),
  [linux/timer.c](../src/linux/timer.c)) → MCU clips to min step time and
  *reports* instead of `shutdown`; host enters HOLDING and re-anchors the
  clock.

MCU-side changes (small, guarded by a config flag to preserve stock behavior):

- [stepper.c](../src/stepper.c) `stepper_event_full()`: replace the fatal
  1000 µs give-up with "clip to `min_next_time` and report a
  status/`trsync_state`", so over-speed becomes a hold, not a crash.
- [sched.c](../src/sched.c) `sched_add_timer()` (`Timer too close`):
  downgrade to a reported reschedule when the recovery flag is set; the host
  then re-syncs via `reset_step_clock`.
- Add a `command_truncate_step_queue` (a `stepper_stop` without the trigger
  requirement) to stop cleanly at the next step boundary and return the exact
  count.

### 4.4 Reversible motion (optional/advanced, but designed-in)

This is what makes a pendant able to incrementally step through the program,
in either direction.

Because the journal stores the full segment envelope, a segment's reverse twin
is trivially constructible (a trapezoid is time-symmetric):

- swap start/end positions, negate the axis direction vector and all
  velocities, keep the time parameters.

Reversal then re-runs the normal `trapq_append → itersolve → stepcompress` path
on the negated segment — **no new MCU capability**. The iterative solver
already handles arbitrary direction (it tracks `sdir` and flips on direction
change; see [itersolve.c](../klippy/chelper/itersolve.c)). A pendant
"step back" becomes: pop the last N segments from the journal, emit their
reverse twins, advance the program pointer backwards.

IO semantics need a policy hook (spindle and coolant cannot "un-run"): the
default is to reverse *axis motion only* and re-issue IO as forward events,
configurable per tool function.

## 5. Phased implementation plan

1. **Journal + commit-horizon refactor** (biggest, pure host): add
   `MotionSegment` journaling and bound the commit horizon by default; expose
   `segment_id` and step-history span hooks. No behavioral change yet.
2. **Feed-hold supervisor**: implement HOLDING/HELD/RESUME using the existing
   drip + `force_move` + `trsync REASON_HOST_REQUEST` primitives; wire a
   `FEED_HOLD` / `RESUME` G-Code pair (and pendant events).
3. **Recovery bridge**: dispatch the three faults into the supervisor; add the
   MCU clip-and-report changes behind a flag; run the `note_homing_end` resync
   on recovery.
4. **Reversibility**: reverse-twin emission from the journal plus a pendant
   `MOTION_STEP_BACK` G-Code; IO reverse policy hook.

Phases 1–2 are host-only and follow the fork's extras-first policy
(see [CNC_Reimplementation.md](CNC_Reimplementation.md#feature-integration-strategy-extras-first)).
Phase 3 is the only one that needs `src/` edits, and it is the phase that
finally makes a motion fault recoverable.

## 6. Risks / open questions

- **MCU clip semantics**: clipping step timing silently changes feed rate; the
  host must treat the clip report as authoritative (feed-back rather than
  feed-forward). This is the real-time "clamp to max feed" that CNC
  controllers do natively.
- **Multi-MCU rails**: `TriggerDispatch` already rejects multi-MCU shared
  rails for homing; recovery has the same constraint and should be documented.
- **Clock re-anchor under `Lost communication`**: if the link is truly dead, no
  resync is possible until it returns; recovery must distinguish a link blip
  from a hard disconnect (the `trsync` `REASON_COMMS_TIMEOUT` path already
  models this).
- **Spindle engagement**: for CNC, the blanket shutdown (spindle off) is
  itself damaging; the hold path should decelerate and hold while keeping the
  spindle controllable, which the state machine supports but which needs
  explicit policy.
- **Interaction with existing extras**: `pause_resume.py` already captures and
  restores G-Code state, but only for the lifetime of the process. A resume
  across a fault should reuse its `SAVE_GCODE_STATE`/`RESTORE_GCODE_STATE`
  contract rather than invent a parallel one.
