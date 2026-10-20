// Handling of stepper drivers.
//
// Copyright (C) 2016-2025  Kevin O'Connor <kevin@koconnor.net>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "autoconf.h" // CONFIG_*
#include "basecmd.h" // oid_alloc
#include "board/gpio.h" // gpio_out_write
#include "board/irq.h" // irq_disable
#include "board/misc.h" // timer_is_before
#include "command.h" // DECL_COMMAND
#include "sched.h" // struct timer
#include "stepper.h" // stepper_event
#include "trsync.h" // trsync_add_signal

DECL_CONSTANT("STEPPER_STEP_BOTH_EDGE", 1);

#if CONFIG_INLINE_STEPPER_HACK && CONFIG_WANT_STEPPER_OPTIMIZED_BOTH_EDGE
 #define HAVE_EDGE_OPTIMIZATION 1
 #define HAVE_AVR_OPTIMIZATION 0
#elif CONFIG_INLINE_STEPPER_HACK && CONFIG_MACH_AVR
 #define HAVE_EDGE_OPTIMIZATION 0
 #define HAVE_AVR_OPTIMIZATION 1
#else
 #define HAVE_EDGE_OPTIMIZATION 0
 #define HAVE_AVR_OPTIMIZATION 0
#endif

struct stepper_move {
    struct move_node node;
    uint32_t interval;
    int16_t add;
    uint16_t count;
    uint8_t flags;
};

enum { MF_DIR=1<<0 };

struct stepper {
    struct timer time;
    uint32_t interval;
    int16_t add;
    uint32_t count;
    uint32_t next_step_time, step_pulse_ticks;
    uint32_t motion_time; // next step in motion clock time (while scaled)
    uint16_t mc_f;        // motion clock 1/k its pending step was timed with
    struct gpio_out step_pin, dir_pin;
    uint32_t position;
    struct move_queue_head mq;
    struct trsync_signal stop_signal;
    // gcc (pre v6) does better optimization when uint8_t are bitfields
    uint8_t flags : 8;
};

enum { POSITION_BIAS=0x40000000 };

enum {
    SF_LAST_DIR=1<<0, SF_NEXT_DIR=1<<1, SF_INVERT_STEP=1<<2, SF_NEED_RESET=1<<3,
    SF_SINGLE_SCHED=1<<4, SF_OPTIMIZED_PATH=1<<5, SF_HAVE_ADD=1<<6,
    SF_HELD=1<<7
};

static __always_inline uint32_t mc_motion_to_real(uint32_t m);
// Real time of a stepper's next step (s->motion_time) while scaled.  A step
// is never scheduled before the stepper's previous step: if it fell behind
// the mapping it catches up from its last step instead of being scheduled
// in the past (which the timer code treats as an overrun).
#define AVR_STEP_TICKS 40 // minimum instructions between step gpio pulses
#define mc_step_waketime(s) ({                                          \
    (s)->mc_f = mc_current_f();                                         \
    uint32_t _w = mc_motion_to_real((s)->motion_time);                  \
    uint32_t _min = (s)->time.waketime + AVR_STEP_TICKS;                \
    timer_is_before(_w, _min) ? _min : _w; })
static uint8_t mc_scaled; // motion clock scaled or held (steps use motion time)
static __always_inline uint16_t mc_current_f(void);
static uint32_t mc_offset(void);
static uint_fast8_t mc_is_held(void);
static void mc_reset(void);

// Setup a stepper for the next move in its queue
static uint_fast8_t
stepper_load_next(struct stepper *s)
{
    if (move_queue_empty(&s->mq)) {
        // There is no next move - the queue is empty
        s->count = 0;
        return SF_DONE;
    }

    // Read next 'struct stepper_move'
    struct move_node *mn = move_queue_pop(&s->mq);
    struct stepper_move *m = container_of(mn, struct stepper_move, node);
    uint32_t move_interval = m->interval;
    uint_fast16_t move_count = m->count;
    int_fast16_t move_add = m->add;
    uint_fast8_t need_dir_change = m->flags & MF_DIR;
    move_free(m);

    // Add all steps to s->position (stepper_get_position() can calc mid-move)
    s->position = (need_dir_change ? -s->position : s->position) + move_count;

    // Load next move into 'struct stepper'
    s->add = move_add;
    s->interval = move_interval + move_add;
    if (HAVE_EDGE_OPTIMIZATION && s->flags & SF_OPTIMIZED_PATH) {
        // Using optimized stepper_event_edge()
        s->time.waketime += move_interval;
        s->count = move_count;
    } else if (HAVE_AVR_OPTIMIZATION && s->flags & SF_OPTIMIZED_PATH) {
        // Using optimized stepper_event_avr() or stepper_event_scaled()
        if (unlikely(mc_scaled)) {
            // Motion clock is scaled - track motion time, convert to real
            s->motion_time += move_interval;
            s->time.waketime = mc_step_waketime(s);
        } else {
            s->time.waketime += move_interval;
        }
        s->count = move_count;
        s->flags = move_add ? s->flags | SF_HAVE_ADD : s->flags & ~SF_HAVE_ADD;
    } else {
        // Using fully scheduled stepper_event_full() code (the scheduler
        // may be called twice for each step)
        uint_fast8_t was_active = !!s->count;
        uint32_t min_next_time = s->time.waketime;
        s->next_step_time += move_interval;
        s->time.waketime = s->next_step_time;
        s->count = (s->flags & SF_SINGLE_SCHED ? move_count
                    : (uint32_t)move_count * 2);
        if (was_active && timer_is_before(s->next_step_time, min_next_time)) {
            // Actively stepping and next step event close to the last unstep
            int32_t diff = s->next_step_time - min_next_time;
            if (diff < (int32_t)-timer_from_us(1000))
                shutdown("Stepper too far in past");
            s->time.waketime = min_next_time;
        }
        if (was_active && need_dir_change) {
            // Must ensure minimum time between step change and dir change
            if (s->flags & SF_SINGLE_SCHED)
                while (timer_is_before(timer_read_time(), min_next_time))
                    ;
            gpio_out_toggle_noirq(s->dir_pin);
            uint32_t curtime = timer_read_time();
            min_next_time = curtime + s->step_pulse_ticks;
            if (timer_is_before(s->time.waketime, min_next_time))
                s->time.waketime = min_next_time;
            return SF_RESCHEDULE;
        }
    }

    // Set new direction (if needed)
    if (need_dir_change)
        gpio_out_toggle_noirq(s->dir_pin);
    return SF_RESCHEDULE;
}

// Edge optimization only enabled when fastest rate notably slower than 100ns
#define EDGE_STEP_TICKS DIV_ROUND_UP(CONFIG_CLOCK_FREQ, 8000000)
#if HAVE_EDGE_OPTIMIZATION
 DECL_CONSTANT("STEPPER_OPTIMIZED_EDGE", EDGE_STEP_TICKS);
#endif

// Optimized step function to step on each step pin edge
static uint_fast8_t
stepper_event_edge(struct timer *t)
{
    struct stepper *s = container_of(t, struct stepper, time);
    gpio_out_toggle_noirq(s->step_pin);
    uint32_t count = s->count - 1;
    if (likely(count)) {
        s->count = count;
        s->time.waketime += s->interval;
        s->interval += s->add;
        return SF_RESCHEDULE;
    }
    return stepper_load_next(s);
}

#if HAVE_AVR_OPTIMIZATION
 DECL_CONSTANT("STEPPER_OPTIMIZED_UNSTEP", AVR_STEP_TICKS);
#endif

#if HAVE_AVR_OPTIMIZATION
// AVR step function while the motion clock is scaled or held: like
// stepper_event_avr(), but steps are scheduled in motion time and
// converted to real time through the shared motion clock mapping.
// Inlined into stepper_event_avr() to avoid an indirect call per step.
static __always_inline uint_fast8_t
stepper_event_scaled(struct stepper *s)
{
    gpio_out_toggle_noirq(s->step_pin);
    uint16_t *pcount = (void*)&s->count, count = *pcount - 1;
    if (likely(count)) {
        *pcount = count;
        s->motion_time += s->interval;
        if (s->flags & SF_HAVE_ADD)
            s->interval += s->add;
        s->time.waketime = mc_step_waketime(s);
        gpio_out_toggle_noirq(s->step_pin);
        return SF_RESCHEDULE;
    }
    uint_fast8_t ret = stepper_load_next(s);
    gpio_out_toggle_noirq(s->step_pin);
    return ret;
}
#endif

// AVR optimized step function
static uint_fast8_t
stepper_event_avr(struct timer *t)
{
    struct stepper *s = container_of(t, struct stepper, time);
#if HAVE_AVR_OPTIMIZATION
    if (unlikely(mc_scaled))
        return stepper_event_scaled(s);
#endif
    gpio_out_toggle_noirq(s->step_pin);
    uint16_t *pcount = (void*)&s->count, count = *pcount - 1;
    if (likely(count)) {
        *pcount = count;
        s->time.waketime += s->interval;
        gpio_out_toggle_noirq(s->step_pin);
        if (s->flags & SF_HAVE_ADD)
            s->interval += s->add;
        return SF_RESCHEDULE;
    }
    uint_fast8_t ret = stepper_load_next(s);
    gpio_out_toggle_noirq(s->step_pin);
    return ret;
}


// Regular "fully scheduled" step function
static uint_fast8_t
stepper_event_full(struct timer *t)
{
    struct stepper *s = container_of(t, struct stepper, time);
    gpio_out_toggle_noirq(s->step_pin);
    uint32_t curtime = timer_read_time();
    uint32_t min_next_time = curtime + s->step_pulse_ticks;
    uint32_t count = s->count - 1;
    if (likely(count & 1 && !(s->flags & SF_SINGLE_SCHED)))
        // Schedule unstep event
        goto reschedule_min;
    if (likely(count)) {
        s->next_step_time += s->interval;
        s->interval += s->add;
        if (unlikely(timer_is_before(s->next_step_time, min_next_time)))
            // The next step event is too close - push it back
            goto reschedule_min;
        s->count = count;
        s->time.waketime = s->next_step_time;
        return SF_RESCHEDULE;
    }
    s->time.waketime = min_next_time;
    return stepper_load_next(s);
reschedule_min:
    s->count = count;
    s->time.waketime = min_next_time;
    return SF_RESCHEDULE;
}

// Optimized entry point for step function (may be inlined into sched.c code)
uint_fast8_t
stepper_event(struct timer *t)
{
    if (HAVE_EDGE_OPTIMIZATION)
        return stepper_event_edge(t);
    if (HAVE_AVR_OPTIMIZATION)
        return stepper_event_avr(t);
    return stepper_event_full(t);
}

void
command_config_stepper(uint32_t *args)
{
    struct stepper *s = oid_alloc(args[0], command_config_stepper, sizeof(*s));
    int_fast8_t invert_step = args[3];
    if (invert_step > 0)
        s->flags = SF_INVERT_STEP;
    else if (invert_step < 0)
        s->flags = SF_SINGLE_SCHED;
    s->step_pin = gpio_out_setup(args[1], s->flags & SF_INVERT_STEP);
    s->dir_pin = gpio_out_setup(args[2], 0);
    s->position = -POSITION_BIAS;
    s->step_pulse_ticks = args[4];
    move_queue_setup(&s->mq, sizeof(struct stepper_move));
    if (HAVE_EDGE_OPTIMIZATION) {
        if (invert_step < 0 && s->step_pulse_ticks <= EDGE_STEP_TICKS)
            s->flags |= SF_OPTIMIZED_PATH;
        else
            s->time.func = stepper_event_full;
    } else if (HAVE_AVR_OPTIMIZATION) {
        if (invert_step >= 0 && s->step_pulse_ticks <= AVR_STEP_TICKS)
            s->flags |= SF_SINGLE_SCHED | SF_OPTIMIZED_PATH;
        else
            s->time.func = stepper_event_full;
    } else if (!CONFIG_INLINE_STEPPER_HACK) {
        s->time.func = stepper_event_full;
    }
}
DECL_COMMAND(command_config_stepper, "config_stepper oid=%c step_pin=%c"
             " dir_pin=%c invert_step=%c step_pulse_ticks=%u");

// Return the 'struct stepper' for a given stepper oid
static struct stepper *
stepper_oid_lookup(uint8_t oid)
{
    return oid_lookup(oid, command_config_stepper);
}

// Schedule a set of steps with a given timing
void
command_queue_step(uint32_t *args)
{
    struct stepper *s = stepper_oid_lookup(args[0]);
    struct stepper_move *m = move_alloc();
    m->interval = args[1];
    m->count = args[2];
    if (!m->count)
        shutdown("Invalid count parameter");
    m->add = args[3];
    m->flags = 0;

    irq_disable();
    uint8_t flags = s->flags;
    if (!!(flags & SF_LAST_DIR) != !!(flags & SF_NEXT_DIR)) {
        flags ^= SF_LAST_DIR;
        m->flags |= MF_DIR;
    }
    if (s->count) {
        s->flags = flags;
        move_queue_push(&m->node, &s->mq);
    } else if (flags & SF_NEED_RESET) {
        move_free(m);
    } else {
        s->flags = flags;
        move_queue_push(&m->node, &s->mq);
        stepper_load_next(s);
        if (unlikely(mc_is_held()))
            // Motion clock is held - schedule on resume
            s->flags |= SF_HELD;
        else
            sched_add_timer(&s->time);
    }
    irq_enable();
}
DECL_COMMAND(command_queue_step,
             "queue_step oid=%c interval=%u count=%hu add=%hi");

// Set the direction of the next queued step
void
command_set_next_step_dir(uint32_t *args)
{
    struct stepper *s = stepper_oid_lookup(args[0]);
    uint8_t nextdir = args[1] ? SF_NEXT_DIR : 0;
    irq_disable();
    s->flags = (s->flags & ~SF_NEXT_DIR) | nextdir;
    irq_enable();
}
DECL_COMMAND(command_set_next_step_dir, "set_next_step_dir oid=%c dir=%c");

// Set an absolute time that the next step will be relative to
void
command_reset_step_clock(uint32_t *args)
{
    struct stepper *s = stepper_oid_lookup(args[0]);
    uint32_t waketime = args[1];
    irq_disable();
    if (s->count)
        shutdown("Can't reset time when stepper active");
    // The clock is in motion time (equal to real time unless a hold or
    // rate change has made the motion clock fall behind)
    s->motion_time = waketime;
    s->next_step_time = s->time.waketime = waketime + mc_offset();
    s->flags &= ~SF_NEED_RESET;
    irq_enable();
}
DECL_COMMAND(command_reset_step_clock, "reset_step_clock oid=%c clock=%u");

// Return the current stepper position.  Caller must disable irqs.
static uint32_t
stepper_get_position(struct stepper *s)
{
    uint32_t position = s->position;
    // If stepper is mid-move, subtract out steps not yet taken
    if (s->flags & SF_SINGLE_SCHED)
        position -= s->count;
    else
        position -= s->count / 2;
    // The top bit of s->position is an optimized reverse direction flag
    if (position & 0x80000000)
        return -position;
    return position;
}

// Report the current position of the stepper
void
command_stepper_get_position(uint32_t *args)
{
    uint8_t oid = args[0];
    struct stepper *s = stepper_oid_lookup(oid);
    irq_disable();
    uint32_t position = stepper_get_position(s);
    irq_enable();
    sendf("stepper_position oid=%c pos=%i", oid, position - POSITION_BIAS);
}
DECL_COMMAND(command_stepper_get_position, "stepper_get_position oid=%c");

// Stop all moves for a given stepper (caller must disable IRQs)
static void
stepper_stop(struct trsync_signal *tss, uint8_t reason)
{
    struct stepper *s = container_of(tss, struct stepper, stop_signal);
    sched_del_timer(&s->time);
    s->next_step_time = s->time.waketime = 0;
    s->position = -stepper_get_position(s);
    s->count = 0;
    s->flags = ((s->flags & (SF_INVERT_STEP|SF_SINGLE_SCHED|SF_OPTIMIZED_PATH))
                | SF_NEED_RESET);
    gpio_out_write(s->dir_pin, 0);
    if (!(s->flags & SF_SINGLE_SCHED)
        || (HAVE_AVR_OPTIMIZATION && s->flags & SF_OPTIMIZED_PATH))
        // Must return step pin to "unstep" state
        gpio_out_write(s->step_pin, s->flags & SF_INVERT_STEP);
    while (!move_queue_empty(&s->mq)) {
        struct move_node *mn = move_queue_pop(&s->mq);
        struct stepper_move *m = container_of(mn, struct stepper_move, node);
        move_free(m);
    }
}

// Set the stepper to stop on a "trigger event" (used in homing)
void
command_stepper_stop_on_trigger(uint32_t *args)
{
    struct stepper *s = stepper_oid_lookup(args[0]);
    struct trsync *ts = trsync_oid_lookup(args[1]);
    trsync_add_signal(ts, &s->stop_signal, stepper_stop);
}
DECL_COMMAND(command_stepper_stop_on_trigger,
             "stepper_stop_on_trigger oid=%c trsync_oid=%c");

void
stepper_shutdown(void)
{
    uint8_t i;
    struct stepper *s;
    foreach_oid(i, s, command_config_stepper) {
        move_queue_clear(&s->mq);
        stepper_stop(&s->stop_signal, 0);
    }
    mc_reset();
}
DECL_SHUTDOWN(stepper_shutdown);


/****************************************************************
 * Motion clock (time scaling)
 ****************************************************************/

// Stepper timetables are kept in "motion time" M.  Normally the motion
// clock runs with the hardware timer, M = R - offset, and the regular
// step path is used unchanged.  To slow down, hold or resume motion along
// the planned path, the motion clock advances at a rate 0 <= k <= 1:
//     M = M0 + k * (R - R0)    so    R = R0 + (M - M0) / k
// While k < 1 every stepper keeps the exact motion time of its next step
// and converts it through this one shared mapping, so axes stay in sync
// and rounding errors don't accumulate.  k changes in steps along a
// linear ramp, one step per MC_TICK, re-anchoring (R0, M0) each time.

#if HAVE_AVR_OPTIMIZATION

#define MC_K_ONE (1 << 14)          // k is fixed point Q14
#define MC_K_MIN (MC_K_ONE / 128)   // smallest non-zero k (keeps 1/k in 16 bits)
#define MC_TICK_US 1000             // ramp step period
#define MC_STEADY_US 100000         // re-anchor period while k is constant
#define MC_MIN_LEAD 40              // min ticks ahead when rescheduling

DECL_CONSTANT("MOTION_CLOCK_K_ONE", MC_K_ONE);

enum { MC_NORMAL, MC_SCALED, MC_HELD };

static struct {
    struct timer timer;
    uint32_t offset;            // R - M while normal
    uint32_t r0, m0;            // anchor of the scaled mapping
    uint16_t k, f;              // rate (Q14) and 1/k (Q8)
    uint16_t k_to, ramp_steps, ramp_pos;
    struct timer deadline_timer; // hold on underrun (host lease)
    uint32_t deadline;          // motion time up to which steps are queued
    uint16_t deadline_ramp_ms;
    uint8_t deadline_on, event;
    int16_t k_step, k_rem_step; // per-tick change of k, and its remainder
    uint16_t k_rem;             // remainder accumulator (Bresenham)
    uint8_t mode, active;
} MC;

static __always_inline uint16_t
mc_current_f(void)
{
    return MC.f;
}

static uint32_t
mc_offset(void)
{
    return MC.offset;
}

static uint_fast8_t
mc_is_held(void)
{
    return MC.mode == MC_HELD;
}

// Convert a motion time to a real time using the current mapping
static __always_inline uint32_t
mc_motion_to_real(uint32_t m)
{
    uint32_t dm = m - MC.m0;
    if (likely(!(dm >> 16)))
        // Common case: step within 2^16 ticks of the anchor - one multiply
        return MC.r0 + (((uint32_t)(uint16_t)dm * MC.f) >> 8);
    if ((int32_t)dm < 0)
        // Already due - don't stretch lateness
        return MC.r0 + dm;
    uint32_t hi = (uint32_t)(uint16_t)(dm >> 16) * MC.f;
    if (hi >= (1UL << 23))
        // Further away than can be represented
        return MC.r0 + 0x7fffffff;
    uint32_t lo = (uint32_t)(uint16_t)dm * MC.f;
    return MC.r0 + (hi << 8) + (lo >> 8);
}

// Scale a real-time duration by k (dt < 2^30)
static uint32_t
mc_scale(uint32_t dt, uint16_t k)
{
    uint32_t hi = (uint32_t)(uint16_t)(dt >> 16) * k;
    uint32_t lo = (uint32_t)(uint16_t)dt * k;
    return (hi << 2) + (lo >> 14);
}

// Refine f ~= 2^22 / k by Newton's method (f' = f * (2 - k*f)), valid
// when f is already close; multiplies only, no division
static uint16_t
mc_refine_inverse(uint16_t k, uint16_t f)
{
    uint_fast8_t i;
    for (i = 0; i < 2; i++) {
        int32_t e = (int32_t)(1UL << 22) - (int32_t)((uint32_t)k * f);
        f += ((int32_t)f * (e >> 8)) >> 14;
    }
    return f;
}

// Current motion time (caller must disable irqs)
static uint32_t
mc_motion_now(uint32_t now)
{
    if (MC.mode == MC_NORMAL)
        return now - MC.offset;
    if (MC.mode == MC_HELD)
        return MC.m0;
    return MC.m0 + mc_scale(now - MC.r0, MC.k);
}

// Reschedule a stepper's pending step after the mapping changed
static void
mc_reschedule(struct stepper *s)
{
    uint32_t w = mc_motion_to_real(s->motion_time);
    uint32_t min_w = MC.r0 + MC_MIN_LEAD;
    if (timer_is_before(w, min_w))
        w = min_w;
    s->time.waketime = w;
    s->mc_f = MC.f;
    sched_add_timer(&s->time);
}

// Move the pending steps whose timing has drifted: 1/k changed by more than
// 1/8 since they were timed.  A pending step can wait through many ticks,
// so the drift is measured from its own scheduling.  Steppers that step
// often re-time at every step and rarely need this.
static void
mc_retime_drifted(void)
{
    uint8_t i;
    struct stepper *s;
    foreach_oid(i, s, command_config_stepper) {
        if (!s->count || !(s->flags & SF_OPTIMIZED_PATH))
            continue;
        uint16_t sf = s->mc_f;
        uint16_t df = MC.f > sf ? MC.f - sf : sf - MC.f;
        if (df > (sf >> 3)) {
            sched_del_timer(&s->time);
            mc_reschedule(s);
        }
    }
}

// Apply a new mode / mapping to all steppers.  Pending steps are only
// moved when 'resched' is set: the mapping is continuous at each re-anchor,
// so a small change of k shifts a pending step by a fraction of one step
// interval, and the steps after it are computed from exact motion times.
static void
mc_update_steppers(uint8_t old_mode, uint8_t new_mode, uint8_t resched)
{
    uint8_t i;
    struct stepper *s;
    foreach_oid(i, s, command_config_stepper) {
        if (!(s->flags & SF_OPTIMIZED_PATH))
            continue;
        if (old_mode == MC_NORMAL) {
            // Start tracking motion time explicitly
            s->motion_time = s->time.waketime - MC.offset;
        }
        if (!s->count) {
            // Idle stepper - keep its last step time consistent
            if (new_mode == MC_NORMAL)
                s->time.waketime = s->motion_time + MC.offset;
            continue;
        }
        if (!resched && old_mode == MC_SCALED && new_mode == MC_SCALED)
            continue;
        if (s->flags & SF_HELD)
            s->flags &= ~SF_HELD;
        else
            sched_del_timer(&s->time);
        if (new_mode == MC_HELD) {
            s->flags |= SF_HELD;
        } else if (new_mode == MC_NORMAL) {
            uint32_t w = s->motion_time + MC.offset;
            uint32_t min_w = MC.r0 + MC_MIN_LEAD;
            s->time.waketime = timer_is_before(w, min_w) ? min_w : w;
            sched_add_timer(&s->time);
        } else {
            mc_reschedule(s);
        }
    }
}

static uint_fast8_t
mc_event(struct timer *t)
{
    uint32_t now = MC.timer.waketime;
    // Re-anchor the mapping at this tick
    uint8_t old_mode = MC.mode;
    if (old_mode == MC_NORMAL)
        MC.m0 = now - MC.offset;
    else if (old_mode == MC_SCALED)
        MC.m0 += mc_scale(now - MC.r0, MC.k);
    MC.r0 = now;
    if (MC.ramp_pos >= MC.ramp_steps) {
        // Steady at an intermediate rate - nothing else changes
        MC.timer.waketime = now + timer_from_us(MC_STEADY_US);
        return SF_RESCHEDULE;
    }
    // Next rate on the linear ramp (k_step per tick plus a Bresenham
    // remainder, so no division is needed here)
    MC.ramp_pos++;
    uint16_t k_old = old_mode == MC_NORMAL ? MC_K_ONE : MC.k;
    int32_t k_new = (int32_t)k_old + MC.k_step;
    MC.k_rem += MC.k_rem_step < 0 ? -MC.k_rem_step : MC.k_rem_step;
    if (MC.k_rem >= MC.ramp_steps) {
        MC.k_rem -= MC.ramp_steps;
        k_new += MC.k_rem_step < 0 ? -1 : 1;
    }
    if (MC.ramp_pos >= MC.ramp_steps || k_new < 0 || k_new > MC_K_ONE)
        k_new = MC.k_to;
    uint16_t k = k_new;
    if (k == 0) {
        MC.mode = MC_HELD;
        mc_scaled = 1;
        MC.k = 0;
        mc_update_steppers(old_mode, MC_HELD, 1);
        MC.active = 0;
        return SF_DONE;
    }
    if (k >= MC_K_ONE) {
        MC.mode = MC_NORMAL;
        mc_scaled = 0;
        MC.k = MC_K_ONE;
        MC.offset = MC.r0 - MC.m0;
        mc_update_steppers(old_mode, MC_NORMAL, 1);
        MC.active = 0;
        return SF_DONE;
    }
    if (k < MC_K_MIN)
        k = MC_K_MIN;
    // All pending steps are re-timed on mode changes, large steps of k and
    // at the end of a ramp; otherwise only those that drifted (bounding a
    // pending step's error to 1/8 of its wait)
    uint16_t dk = k > k_old ? k - k_old : k_old - k;
    uint8_t big_step = old_mode != MC_SCALED || dk > (k >> 3);
    MC.k = k;
    uint8_t ramp_end = MC.ramp_pos >= MC.ramp_steps;
    // Exact 1/k on big steps and at the end of a ramp (a steady override
    // must run at exactly its rate); Newton refinement otherwise
    MC.f = big_step || ramp_end ? (1UL << 22) / k : mc_refine_inverse(k, MC.f);
    MC.mode = MC_SCALED;
    mc_scaled = 1;
    if (big_step || ramp_end)
        mc_update_steppers(old_mode, MC_SCALED, 1);
    else
        mc_retime_drifted();
    MC.timer.waketime = now + timer_from_us(MC_TICK_US);
    return SF_RESCHEDULE;
}

static void mc_start_ramp(uint16_t k_to, uint16_t ramp_ms);

// Ramp the motion clock rate to k over ramp_ms milliseconds
// (k=0 holds, k=MOTION_CLOCK_K_ONE resumes normal motion)
void
command_motion_clock_set_rate(uint32_t *args)
{
    uint16_t k_to = args[0], ramp_ms = args[1];
    if (k_to > MC_K_ONE)
        shutdown("Invalid motion clock rate");
    uint8_t i;
    struct stepper *s;
    foreach_oid(i, s, command_config_stepper) {
        if (!(s->flags & SF_OPTIMIZED_PATH))
            shutdown("Motion clock needs optimized steppers");
    }
    irq_disable();
    mc_start_ramp(k_to, ramp_ms);
    irq_enable();
}
DECL_COMMAND(command_motion_clock_set_rate,
             "motion_clock_set_rate k=%hu ramp_ms=%hu");

// Start a linear ramp of the rate to k_to (caller must disable irqs)
static void
mc_start_ramp(uint16_t k_to, uint16_t ramp_ms)
{
    uint16_t k_now = MC.mode == MC_NORMAL ? MC_K_ONE : MC.k;
    MC.k_to = k_to;
    MC.ramp_steps = ramp_ms ? ramp_ms : 1;
    MC.ramp_pos = 0;
    int16_t span = (int16_t)k_to - (int16_t)k_now;
    MC.k_step = span / (int16_t)MC.ramp_steps;
    MC.k_rem_step = span % (int16_t)MC.ramp_steps;
    MC.k_rem = 0;
    if (k_to == k_now && MC.mode != MC_SCALED)
        // Already there
        return;
    if (MC.active)
        sched_del_timer(&MC.timer);
    MC.timer.func = mc_event;
    MC.timer.waketime = timer_read_time() + timer_from_us(100);
    MC.active = 1;
    sched_add_timer(&MC.timer);
}

/****************************************************************
 * Hold on underrun
 ****************************************************************/

// The host keeps renewing a deadline: "steps are queued up to motion time
// T".  If the motion clock reaches T - ramp without a newer deadline, the
// host has stopped feeding steps (lag, crash, lost link), and the MCU
// ramps the rate to zero by itself.  A linear ramp from full speed uses
// half the ramp time in motion time, so motion stops on the planned path
// before the queued steps run out - instead of "Timer too close" and a
// shutdown.  Motion stays held (position kept) until the host resumes.

static struct task_wake mc_wake;
enum { MC_EVENT_UNDERRUN_HOLD = 1 };

static uint32_t
mc_deadline_trigger(void)
{
    return MC.deadline - timer_from_us((uint32_t)MC.deadline_ramp_ms * 1000);
}

static uint_fast8_t
mc_deadline_event(struct timer *t)
{
    if (!MC.deadline_on || MC.mode == MC_HELD)
        return SF_DONE;
    uint32_t now = t->waketime;
    uint32_t trigger = mc_deadline_trigger();
    if ((int32_t)(mc_motion_now(now) - trigger) < 0) {
        // Not there yet (the motion clock is slowed) - check again when
        // the current mapping says the trigger is reached (at most one
        // ramp tick later, since the mapping may change)
        uint32_t w = (MC.mode == MC_NORMAL ? trigger + MC.offset
                      : mc_motion_to_real(trigger));
        uint32_t min_w = now + timer_from_us(MC_TICK_US);
        uint8_t ramping = MC.active && MC.ramp_pos < MC.ramp_steps;
        if (ramping && timer_is_before(min_w, w))
            w = min_w;      // the mapping changes every tick
        else if (timer_is_before(w, now + MC_MIN_LEAD))
            w = now + MC_MIN_LEAD;
        t->waketime = w;
        return SF_RESCHEDULE;
    }
    // Underrun: no newer deadline arrived in time - hold
    MC.deadline_on = 0;
    mc_start_ramp(0, MC.deadline_ramp_ms);
    MC.event = MC_EVENT_UNDERRUN_HOLD;
    sched_wake_task(&mc_wake);
    return SF_DONE;
}

// Renew (or with ramp_ms=0 disable) the underrun deadline
void
command_motion_clock_set_deadline(uint32_t *args)
{
    irq_disable();
    sched_del_timer(&MC.deadline_timer);
    MC.deadline = args[0];
    MC.deadline_ramp_ms = args[1];
    MC.deadline_on = !!MC.deadline_ramp_ms;
    if (MC.deadline_on) {
        uint32_t now = timer_read_time(), min_w = now + timer_from_us(100);
        uint32_t w = (MC.mode == MC_NORMAL
                      ? mc_deadline_trigger() + MC.offset : min_w);
        MC.deadline_timer.func = mc_deadline_event;
        MC.deadline_timer.waketime = timer_is_before(w, min_w) ? min_w : w;
        sched_add_timer(&MC.deadline_timer);
    }
    irq_enable();
}
DECL_COMMAND(command_motion_clock_set_deadline,
             "motion_clock_set_deadline mclock=%u ramp_ms=%hu");

// Report events raised in irq context
void
motion_clock_task(void)
{
    if (!sched_check_wake(&mc_wake))
        return;
    irq_disable();
    uint8_t event = MC.event;
    MC.event = 0;
    uint32_t mnow = mc_motion_now(timer_read_time());
    irq_enable();
    if (event)
        sendf("motion_clock_event reason=%c mclock=%u", event, mnow);
}
DECL_TASK(motion_clock_task);

void
command_motion_clock_query(uint32_t *args)
{
    irq_disable();
    uint32_t now = timer_read_time();
    uint32_t mnow = mc_motion_now(now);
    uint8_t mode = MC.mode;
    uint16_t k = mode == MC_NORMAL ? MC_K_ONE : MC.k;
    irq_enable();
    sendf("motion_clock_state mode=%c k=%hu clock=%u mclock=%u"
          , mode, k, now, mnow);
}
DECL_COMMAND_FLAGS(command_motion_clock_query, HF_IN_SHUTDOWN,
                   "motion_clock_query");

static void
mc_reset(void)
{
    MC.mode = MC_NORMAL;
    mc_scaled = 0;
    MC.k = MC_K_ONE;
    MC.offset = 0;
    MC.deadline_on = 0;
    MC.active = 0;
}

#else // !HAVE_AVR_OPTIMIZATION

static uint32_t mc_motion_to_real(uint32_t m) { return m; }
// mc_scaled stays 0 without the motion clock
static uint32_t mc_offset(void) { return 0; }
static uint_fast8_t mc_is_held(void) { return 0; }
static void mc_reset(void) { }

#endif
