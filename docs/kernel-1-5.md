# Communication-Step Kernel

The communication-step kernel moves Biosimulant from a per-module due-time scheduler to a communication-step co-simulation model.

## What changed

- typed port contracts via `SignalSpec`
- typed runtime signals instead of schema-less `value: Any`
- `execute(inputs, *, context)` is the canonical new computation hook
- `advance_window(start, end)` remains the supported temporal compatibility hook
- explicit before-run, each-window, and after-run invocation policies
- atomic output commit at communication boundaries
- explicit staleness handling on consuming ports
- world snapshots and in-memory branching

## Communication-step semantics

For each window `[t, t + communication_step]`:

1. the world reads the committed signal store at `t`
2. the world delivers those inputs to each ready `EACH_WINDOW` module
3. each such module advances or executes across the same positive window
4. the world commits all module outputs atomically at `t + communication_step`

This means closed loops are modeled as sampled-data coupling at communication boundaries. Rollback, algebraic-loop solving, and FMI negotiation are explicit non-goals for this version.

## Final settle turns

`BioWorld.run(duration)` stops at the requested simulation time. Outputs produced
at the final boundary are committed, but downstream modules only observe them on
a later communication turn. `BioWorld.settle(steps)` provides those turns without
advancing simulation time:

1. start from the outputs published at the last committed boundary
2. schedule only modules downstream of those outputs
3. deliver their current committed inputs
4. call `advance_window(current_time, current_time)`
5. commit any new outputs as the next propagation frontier

Use this for temporal compatibility modules that require zero-time propagation.
Canonical modules use `ONCE_AFTER_RUN` for finite postprocessing and are excluded
from zero-time settling under every policy. The default runtime behavior remains
unchanged for existing temporal modules.

## Typed signals

The communication-step kernel defines a closed signal family:

- `ScalarSignal`
- `ArraySignal`
- `RecordSignal`
- `EventSignal`

Each signal binds a `SignalSpec` carrying:

- `signal_type`
- `kind`
- `dtype`
- `shape`
- `emitted_unit` on outputs
- `accepted_units` on simple inputs
- `accepted_profiles` on inputs
- `format`
- optional semantic `contract`
- `interpolation`
- `max_age`
- `stale_policy`
- optional record/event schema

The kernel validates compatibility but does not convert units or reinterpret
payload types. Outputs declare an emitted unit; inputs list units they accept
directly. Inputs may still use multiple accepted profiles when they genuinely
accept different structural representations. Small semantic contracts and
registered Python checkers add species-, identifier- and value-aware checks
without a separate profile catalogue.

## Snapshot and branch

`BioWorld.snapshot()` captures world time, committed signals, connection delivery state, module snapshots, and setup config. `branch()` deep-copies the current world and restores that snapshot into a new instance so both worlds can diverge independently from the same communication boundary.
