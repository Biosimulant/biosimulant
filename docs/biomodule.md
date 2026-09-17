# API: `BioModule`

`BioModule` is the single runnable unit in the communication-step kernel. New
modules use one canonical execution method for temporal state or finite
input-to-output computation. BioWorld continues to commit outputs atomically.

Invocation policies require `biosimulant>=0.0.26`. They do not add a manifest
field or schema version.

## Runtime contract

```python
class BioModule:
    execution_policy = ExecutionPolicy.EACH_WINDOW

    def setup(self, config: dict[str, Any] | None = None) -> None: ...
    def reset(self) -> None: ...
    def set_inputs(self, signals: dict[str, BioSignal]) -> None: ...
    def execute(
        self,
        inputs: Mapping[str, BioSignal],
        *,
        context: ExecutionContext,
    ) -> Mapping[str, Any | BioSignal]: ...
    def advance_window(self, start: float, end: float) -> None: ...
    def get_outputs(self) -> dict[str, BioSignal]: ...
    def inputs(self) -> Mapping[str, SignalSpec]: ...
    def outputs(self) -> Mapping[str, SignalSpec]: ...
    def snapshot(self) -> dict[str, Any]: ...
    def restore(self, snapshot: Mapping[str, Any]) -> None: ...
    def visualize(self) -> VisualSpec | list[VisualSpec] | None: ...
```

## Rules

- New modules override `execute(inputs, *, context)` and return outputs directly.
- Existing temporal modules may retain `set_inputs()`, `advance_window(start,
  end)`, and `get_outputs()` as a supported compatibility contract.
- Do not override both computation hooks.
- `ExecutionPolicy.EACH_WINDOW` is the compatibility default.
- Declare the policy explicitly on new modules. Canonical modules that inherit
  `EACH_WINDOW` receive a registration warning.
- `ONCE_BEFORE_RUN` is for finite preprocessing, inference, and pure one-shot
  pipelines.
- `ONCE_AFTER_RUN` is for final analysis, ranking, reporting, or export.
- Canonical `EACH_WINDOW` is for temporal advancement or finite computation
  against evolving state; use `context.window_start` and `context.window_end`
  only when simulated time is scientifically meaningful.
- `inputs()` and `outputs()` declare port contracts as `port -> SignalSpec`.
- `execute()` and compatibility `get_outputs()` must only emit declared ports.
- Signals should be emitted as `ScalarSignal`, `ArraySignal`, `RecordSignal`, or `EventSignal`.
- `snapshot()` / `restore()` should round-trip the full module state needed for deterministic continuation from a communication boundary.

BioWorld runs connected once-policy modules only after all non-optional connected
inputs exist. It drains complete before-run and after-run dependency chains without
requiring additional settle steps. Each-window modules retain existing
next-window visibility and feedback behavior.

## Minimal example

```python
import biosimulant as biosim


class Counter(biosim.BioModule):
    execution_policy = biosim.ExecutionPolicy.EACH_WINDOW

    def __init__(self) -> None:
        self.count = 0

    def outputs(self):
        return {"count": biosim.SignalSpec.scalar(dtype="int64")}

    def execute(self, inputs, *, context: biosim.ExecutionContext):
        self.count += 1
        return {"count": self.count}

    def snapshot(self):
        return {"count": self.count}

    def restore(self, snapshot):
        self.count = int(snapshot["count"])
```

## Finite-computation example

```python
class Classifier(biosim.BioModule):
    execution_policy = biosim.ExecutionPolicy.ONCE_BEFORE_RUN

    def inputs(self):
        return {"features": biosim.SignalSpec.array(dtype="float32", shape=(4,))}

    def outputs(self):
        return {"score": biosim.SignalSpec.scalar(dtype="float64")}

    def execute(self, inputs, *, context: biosim.ExecutionContext):
        features = inputs["features"].value
        return {"score": float(predict(features))}
```

Raw return values are wrapped using the declared output spec and stamped at the
applicable BioWorld boundary. Before-run outputs use the run start, each-window
outputs use the window end, and after-run outputs use the final run time. Completed
once modules are not polled or retimestamped in later windows.

Phase edges must move forward: before-run modules may feed any later phase,
each-window modules may feed each-window or after-run modules, and after-run
modules may feed only after-run modules. Cycles are valid only among each-window
modules.

`ExecutionContext` is immutable. It contains `policy`, `run_start`, `run_end`,
optional positive `window_start`/`window_end`, and a derived `simulated_time`.
Calling inherited `advance_window()` directly on a canonical module is rejected;
invoke it through BioWorld. After a successful commit, `module.get_outputs()`
returns the latest normalized canonical result. A zero-duration `run()` performs
setup only. Canonical modules do not run during `settle()` in 0.0.26. A world with
no each-window modules crosses the run in a single step instead of iterating
empty communication windows.

## Declaring the policy in model.yaml

`model.yaml` may repeat the class's policy as `biosim.execution_policy`
(`once_before_run`, `each_window` or `once_after_run`). The Python attribute stays
the only thing that decides when BioWorld invokes the module; the manifest copy
lets tools that can't import model code decide whether a Lab's time settings
apply.

The policy the code resolves to:

- a class attribute resolves to that value;
- an instance attribute set in `__init__` resolves to that value for the
  constructed module's parameters;
- an `execute()` module that sets nothing resolves to `each_window` and warns,
  unless `model.yaml` declares `each_window`;
- a module that overrides `advance_window()` always resolves to `each_window`.

A declaration that disagrees with the constructed module is a load-time
`PackageError` naming both values. `biosimulant labs validate` reads model source
without importing it: it fails when the source provably disagrees, warns when the
policy can't be verified (for example when `__init__` picks it from a parameter),
and suggests the field for undeclared models whose source resolves. Leave the
field out when the policy depends on parameters. A Lab can't override a model's
policy.

## Opt-in convenience bases

`BioModule` stays intentionally small. Use it directly when a model needs full
control over timing, solver state, or emitted signal construction.

For existing temporal compatibility patterns, `biosimulant.modules` also provides:

- `SignalEmitterBioModule`: owns `_outputs`, resolves the emitted `source` from
  `_world_name`, wraps raw values into typed signals with `make_signal()`, and
  implements `get_outputs()`.
- `StatefulBioModule`: extends `SignalEmitterBioModule` with `_time`,
  `_input_overrides`, bounded `_history`, fixed-step `advance_window()`, and
  hooks for `apply_overrides()`, `step()`, `record_state()`, and
  `output_payload()`.

These classes are convenience layers, not a replacement contract. External
modules can continue to subclass `BioModule` directly.

```python
class Counter(biosim.StatefulBioModule):
    def __init__(self) -> None:
        super().__init__(integration_step=0.1, record_initial_state=True)
        self.count = 0

    def outputs(self):
        return {"count": biosim.SignalSpec.scalar(dtype="int64")}

    def step(self, h: float) -> None:
        self.count += 1

    def record_state(self, t: float) -> None:
        self._history.append({"t": t, "count": self.count})

    def output_payload(self, t: float):
        return {"count": self.count}
```

## Notes

- The world binds `_world_name` on registered modules so emitted signals can use the registered module name as their source.
- `reset()` is useful for UI-driven reruns, but `snapshot()` / `restore()` is the durability contract for branching and replay.
- Visualization remains optional and should return JSON-serializable specs.
