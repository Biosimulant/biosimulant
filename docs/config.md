# Configuration Files

Biosimulant wiring configs are YAML or TOML mappings with three top-level sections:

- `runtime`
- `modules`
- `wiring`

## Required runtime block

```yaml
runtime:
  communication_step: 0.1
```

`communication_step` controls how often modules exchange committed signals. A
separate lab/package runtime may also declare `duration` and optional
`settle_steps`. Prefer `ExecutionPolicy.ONCE_AFTER_RUN` for finite downstream
report, export, or analysis modules; BioWorld drains those automatically.
`settle_steps` remains available for legacy temporal modules that deliberately
need zero-time propagation after the requested simulation duration.

When every model in a Lab declares `biosim.execution_policy` as `once_before_run`
or `once_after_run`, the Lab is *finite*: duration, communication step and settle
steps don't affect results, so Run dialogs hide them. The runtime block still
requires `communication_step`, and the run still needs a positive `duration`.

Run input files passed to `biosimulant labs run --run-input-file` may place runtime
overrides at `simulation_config.<key>` or `simulation_config.runtime.<key>` (the
nested form wins), initial inputs at `parameters.initial_inputs`,
`simulation_config.initial_inputs` or `simulation_config.runtime.initial_inputs`,
and model parameters at `parameters.per_model.<alias>` or `parameters.<alias>`.
Parameter overrides merge onto the Lab's parameters.

## Module declarations

Each module is either:

- a dotted class path string
- or an object with `class` and optional `args`

```yaml
modules:
  eye:
    class: examples.wiring_builder_demo.Eye
  lgn:
    class: examples.wiring_builder_demo.LGN
    args:
      gain: 2.0
```

## Wiring declarations

```yaml
wiring:
  - from: eye.visual_stream
    to: [lgn.retina]
  - from: lgn.thalamus
    to: [sc.vision]
```

References use `name.port`.

## Validation

- Source and destination ports must be declared by the participating modules.
- Port compatibility is checked from `SignalSpec`.
- Invalid configs fail fast at build/load time.
