# biosimulant

[![PyPI - Version](https://img.shields.io/pypi/v/biosimulant.svg)](https://pypi.org/project/biosimulant)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/biosimulant.svg)](https://pypi.org/project/biosimulant)

Composable simulation runtime + UI layer for orchestrating runnable biomodules.

`biosimulant` is the primary package, import namespace, and CLI name. The
existing `biosim` Python import path and `python -m biosim` command remain
supported for existing model packages during the migration.

---

## Executive Summary & System Goals

### Vision

Provide a small, stable composition layer for simulations: wire reusable components ("biomodules") into a `BioWorld`, run them with a single orchestration contract, and visualize/debug labs via the local `labs serve` web UI. Biomodules are self-contained Python packages that can wrap external simulators internally (SBML/NeuroML/CellML/etc.) without a separate adapter layer.

### Core Mission

- Compose simulations from reusable, interoperable biomodules.
- Make "run + visualize + share a config" the default local workflow, with an
  explicit client for durable hosted execution when requested.
- Keep the runtime small and predictable while letting biomodules embed their own simulator/tooling.

### Primary Users

- Developers and researchers who need composable simulation workflows and fast iteration.
- Near-term beachhead: neuroscience demos (single neuron + small E/I microcircuits) with strong visuals and reproducible configs.

---

## Installation

Preferred (pinned GitHub ref):

```console
pip install "biosimulant @ git+https://github.com/Biosimulant/biosimulant.git@<ref>"
```

Alternative (package index):

```console
pip install biosimulant
```

The default install includes the local web runtime used by
`biosimulant labs serve`.

## Managed Developer API

`BioWorld.run()` remains local and free. Use `Client` or `AsyncClient` when you
explicitly want to run an accessible, versioned Hub lab on managed compute:

```python
from biosimulant import Client

with Client() as client:  # reads BIOSIMULANT_API_KEY
    result = client.run(
        "demi/microbiology-hello-world-growth@1.0.0",
        inputs={"initial_cells": 10, "available_food": 80},
        timeout=300,
    )
    print(result.outputs)
```

For a durable handle that returns immediately, use
`client.runs.create(...).wait()`. See the
[Developer API documentation](https://docs.biosimulant.com/developer-api) for
async execution, cancellation, timeout recovery, webhooks, and billing.
The [supported Python API surface](https://docs.biosimulant.com/runtime/python-api)
lists every supported root import, including `Client`, `AsyncClient`, result
types, and public errors.

For the shared ONNX biomodule helpers:

```console
pip install "biosimulant[ml]"
```

### Compatibility and command ownership

Use `biosimulant` for new Python examples:

```python
import biosimulant as biosim
```

The package still ships the legacy `biosim` Python import path so existing model
packages keep working:

```python
import biosim
```

Use `biosimulant` for new CLI examples:

```bash
biosimulant --help
python -m biosimulant --help
```

`python -m biosim` remains available as a compatibility command. The PyPI
package is the canonical headless CLI used by Desktop, Studio, local terminals,
CI, and servers. Desktop-specific windows and application state remain private
application code; Biosimulant operations use this same CLI everywhere.

### Shell completion

`biosimulant` supports bash and zsh completion for commands, options, and file
paths. Add the completion hook once to your shell startup file:

```bash
# ~/.zshrc or ~/.bashrc
eval "$(register-python-argcomplete biosimulant)"
```

Then restart the shell, or run `source ~/.zshrc` / `source ~/.bashrc`.

## Publishing to PyPI

See the release guide: [`docs/releasing.md`](docs/releasing.md).

## Local Labs

The open-source Python CLI can create, manage, validate, run, and serve local
lab source trees without Desktop or Hub:

```bash
biosimulant labs create ./my-lab --name "My Lab"
biosimulant labs list .
biosimulant labs get ./my-lab
biosimulant labs package ./my-lab --out dist
biosimulant labs validate ./my-lab
biosimulant labs run ./my-lab --no-install-deps
biosimulant labs serve ./my-lab
```

`labs init` creates a runnable starter lab by default. Use `--empty` when you
want only a bare `lab.yaml` scaffold.
Managed local lab identity lives in `.biosimulant/lab.json`; exported labs use
the portable `lab.yaml` source manifest.

## Compose Hub Labs Locally

Use a local `BioModule` beside a version-pinned Hub Lab in one `BioWorld`.
The parent Lab owns the dependency state and lockfile; resolving a Hub Lab is
explicit and is never triggered by importing Python code.

```python
from biosimulant import BioWorld
from biosimulant.hub import HubComposition

world = BioWorld(communication_step=0.1)
world.add_biomodule("controller", DoseController())

# ./lab/biosimulant.lock pins the exact archive checksum for this reference.
composition = HubComposition(world, lab_root="./lab")
composition.add("medium", "your-namespace/nutrient-medium@1.1.0")
composition.apply()
composition.setup()
world.run(duration=24.0)
```

`biosimulant.lock` must contain an exact SHA-256 for every package-backed child
Lab. Dependencies resolve into `./lab/.biosimulant/dependencies/`; delete that
directory to force a clean, lockfile-verified resolution. See
[`docs/hub-composition.md`](docs/hub-composition.md) for the lockfile format,
vendored archival packages, and read-only archive state overrides.
The hosted [HubComposition API reference](https://docs.biosimulant.com/runtime/hub-composition-api)
documents its constructor, `.add()`, `.connect()`, `.apply()`, and `.setup()` methods.

## Packaging Models And Labs

`biosimulant` packages labs through the `labs` command group. Standalone model
package helpers remain Python APIs, but the public CLI object is the lab.

Common commands:

```bash
# Validate and build a package repository manifest
biosimulant labs release validate biosimulant-packages.yaml
biosimulant labs release build biosimulant-packages.yaml --out dist/biosimulant-packages

# Build a self-contained lab package (.bsilab)
biosimulant labs package path/to/lab --out dist

# Discover and pull public registry labs
biosimulant labs search immune
biosimulant labs info owner/lab-name@1.0.0
biosimulant labs pull owner/lab-name@1.0.0 --target ./labs/lab-name

# Validate or run a lab source tree or .bsilab
biosimulant labs validate path/to/lab
biosimulant labs run dist/local__source-lab-1.0.0.bsilab --no-install-deps
```

Notes:
- local `labs validate`, `labs run`, and `labs serve` can use source labs without `package:` or `version:`; the CLI assigns a transient `local/<lab-directory-slug>` identity for local execution.
- standalone `labs package` builds require `package:` and `version:` in `lab.yaml`, or explicit `--package` and `--version` values. Release manifests can supply identity through `biosimulant-packages.yaml`.
- model dependencies in manifests must use exact `==` pins.
- ordinary lab builds preserve version-pinned Hub child references and their lockfile; use `--vendor-dependencies` when an archival `.bsilab` must embed every child Lab.
- nested lab dependencies may use a relative `path`, or an exact `package` + `version` with a matching `biosimulant.lock` checksum.
- `validate` prints human-readable success or failure output by default; add `--json` for machine-readable output.

See [`docs/packaging.md`](docs/packaging.md) for the full package layout, recommended authoring flow, and CLI examples.

## Provisional Runtime Helpers

`biosimulant.runtime` is the provisional public home for package interpretation helpers shared by the open-source CLI and Biosimulant platform executors. It owns entrypoint loading, typed run input coercion, communication-step resolution, and source-neutral lab flattening. Import these helpers from `biosimulant.runtime`; the legacy `biosim.runtime` path remains available for compatibility.

## BioModule Convenience Layers

Invocation policies are available in `biosimulant>=0.0.26`; manifests remain
unchanged.

`BioModule` remains the single runtime contract. New finite and temporal models
implement `execute(inputs, *, context)` and explicitly select an invocation
policy. Existing temporal models can keep `advance_window(start, end)` unchanged:

```python
class Predictor(biosim.BioModule):
    execution_policy = biosim.ExecutionPolicy.ONCE_BEFORE_RUN

    def outputs(self):
        return {"score": biosim.SignalSpec.scalar(dtype="float64")}

    def execute(self, inputs, *, context: biosim.ExecutionContext):
        return {"score": predict(inputs)}
```

The available policies are `ONCE_BEFORE_RUN`, `EACH_WINDOW` (the compatibility
default), and `ONCE_AFTER_RUN`. BioWorld automatically propagates chains of
before-run and after-run modules in dependency order. Use `EACH_WINDOW` when an
AI or analytical model must consume evolving simulation state. Canonical modules
that inherit `EACH_WINDOW` without declaring it receive a registration warning.
The context supplies run bounds and, for positive windows, `window_start` and
`window_end`. Canonical modules are deliberately excluded from zero-time
`settle()` calls in this release.

For common temporal adapters, `biosimulant` also exports opt-in helpers:

- `SignalEmitterBioModule`: output storage, source-name resolution, and raw
  value to typed `BioSignal` wrapping.
- `StatefulBioModule`: fixed-step window advancement, input override storage,
  bounded history, and output publishing hooks.

Signal helper functions are available from `biosimulant.signals` and top-level
`biosimulant`: `unwrap_payload`, `coerce_float`, `scalar_or_record_input`, and
`make_signal`.

## Examples

- See `examples/` for quick-start scripts. Try:

```bash
pip install -e .
python examples/basic_usage.py
```

For advanced curated demos (neuro/ecology), wiring configs, and model-pack templates, see the companion repo:

- https://github.com/Biosimulant/models

### Quick Start: BioWorld

Minimal usage:

```python
import biosimulant as biosim
from biosimulant import ExecutionContext, ExecutionPolicy, SignalSpec


class Counter(biosim.BioModule):
    execution_policy = ExecutionPolicy.EACH_WINDOW

    def __init__(self):
        self.value = 0
        self._t = 0.0

    def outputs(self):
        return {"count": SignalSpec.scalar(dtype="int64", emitted_unit="1")}

    def execute(self, inputs, *, context: ExecutionContext):
        assert context.window_end is not None
        self.value += 1
        self._t = context.window_end
        return {"count": self.value}

    def snapshot(self) -> dict:
        return {"value": self.value, "t": self._t}

    def restore(self, snapshot: dict) -> None:
        self.value = int(snapshot.get("value", 0))
        self._t = float(snapshot.get("t", 0.0))


world = biosim.BioWorld(communication_step=0.1)
world.add_biomodule("counter", Counter())
world.run(duration=1.0)
```

Outputs produced during a communication window are committed at the end of that
window and become visible to downstream modules on a later communication turn.
Canonical report, export, or visualisation modules can use
`ONCE_AFTER_RUN`; BioWorld drains their dependency chain after the final window.
For legacy temporal visualisation modules, `world.settle(steps=1)` remains
available to propagate final outputs without advancing simulated time.

### Visuals from Modules

Modules may optionally expose visuals via `visualize()`, returning a dict or list of dicts with keys `render` and `data`. The world can collect them without any transport layer:

```python
class MyModule(biosim.BioModule):
    execution_policy = biosim.ExecutionPolicy.EACH_WINDOW

    def execute(self, inputs, *, context: biosim.ExecutionContext):
        return {}

    def snapshot(self) -> dict:
        return {}

    def restore(self, snapshot: dict) -> None:
        _ = snapshot

    def visualize(self):
        return {
            "render": "timeseries",
            "data": {"series": [{"name": "s", "points": [[0.0, 1.0]]}]},
        }

world = biosim.BioWorld(communication_step=0.1)
world.add_biomodule("module", MyModule())
world.run(duration=0.1)
print(world.collect_visuals())  # [{"module": "module", "visuals": [...]}]
```

If visuals are generated by finite downstream analysis, implement
`execute(inputs, *, context)`
with `ExecutionPolicy.ONCE_AFTER_RUN`; BioWorld drains it automatically before
visuals are collected. Keep explicit settle turns only for legacy temporal
modules that deliberately need zero-time propagation.

See `examples/visuals_demo.py` for a minimal end-to-end example.

### ONNX Modules

`biosimulant` can host ONNX-backed modules without changing the core runtime. Install
the ML extras and wrap the ONNX model behind the standard `BioModule`
interface:

```python
from biosimulant import (
    ArraySignal,
    ExecutionContext,
    ExecutionPolicy,
    OnnxClassifierModule,
    SignalSpec,
)


class OneShotClassifier(OnnxClassifierModule):
    execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN

classifier = OneShotClassifier(
    model_path="artifacts/model.onnx",
    class_labels=["quiescent", "subthreshold", "spiking"],
    input_port="state_vector",
    probabilities_port="state_probabilities",
    predicted_port="predicted_state",
    input_vector_length=4,
)

context = ExecutionContext(
    policy=ExecutionPolicy.ONCE_BEFORE_RUN,
    run_start=0.0,
    run_end=0.001,
)
outputs = classifier.execute(
    {
        "state_vector": ArraySignal(
            source="adapter",
            name="state_vector",
            value=[-64.0, 0.1, 0.6, 0.3],
            emitted_at=0.0,
            spec=SignalSpec.array(dtype="float32", shape=(4,)),
        )
    },
    context=context,
)
print(outputs["predicted_state"]["label"])
```

Model packs can subclass `OnnxClassifierModule` to set model-relative
`model_path`, port names, and label sets while keeping the inference logic in
the shared library. The shared class keeps the backward-compatible
`EACH_WINDOW` default; finite subclasses should explicitly select a once policy.

## Local Lab UI

`biosimulant labs serve` starts the bundled local lab UI from any runnable lab
source tree, `.bsilab` package, or registry reference.

```console
biosimulant labs serve ./my-lab
```

The command opens the browser by default and serves the UI at the root URL, for
example `http://127.0.0.1:8765/`. Use `--no-open` to suppress the browser
launch and `--port` to choose a different port.

The UI stores lab edits in local files:

- `lab.yaml` for model, world, runtime, and wiring changes.
- `wiring-layout.json` for canvas positions.
- Run history is in memory for the active server process.

Maintainer flow for the bundled frontend:

- Edit the private React/Vite app under `packages/labs-serve-ui`.
- Build with `bash scripts/build_labs_serve_ui.sh`.
- Packaging includes `src/biosim/labs_serve/static/**`, so end users never need npm.

### Visual Notes
- VisualSpec types supported now:
  - `timeseries`: `data = { "series": [{ "name": str, "points": [[x, y], ...] }, ...] }`
  - `bar`: `data = { "items": [{ "label": str, "value": number }, ...] }`
  - `scatter`: `data = { "points": [{ "x": number, "y": number, "label"?: str, "series"?: str }, ...] }`
  - `heatmap`: `data = { "values": [[number, ...], ...], "x_labels"?: [str, ...], "y_labels"?: [str, ...] }`
  - `table`: `data = { "columns": [..], "rows": [[..], ...] }` or `data = { "items": [{...}, ...] }`
  - `image`: `data = { "src": str, "alt"?: str, "width"?: number, "height"?: number }`
  - `graph`: simple node-edge graph renderer
  - `structure3d`: `data = { "title"?: str, "source": { "kind": "url", "url": str } | { "kind": "artifact", "artifact_id": str }, "format": "mmcif" | "pdb", "annotations"?: [{ "label": str, "value": str|number|bool }], "initial_view"?: {...} }`
- VisualSpec may also include an optional `description` (string) for hover text or captions.

## Terminology

Understanding the core concepts is essential for working with Biosimulant effectively.

| Term | Description |
|------|-------------|
| **BioWorld** | Runtime container that orchestrates multi-rate biomodules, routes signals, and publishes lifecycle events. |
| **BioModule** | Pluggable unit of behavior with typed ports and a canonical `execute(inputs, *, context)` hook. |
| **BioSignal** | Typed, versioned data payload exchanged between modules via named ports. |
| **WorldEvent** | Runtime events emitted by the BioWorld (`STARTED`, `TICK`, `FINISHED`, etc.). |
| **Wiring** | Module connection graph. Defined programmatically, via `WiringBuilder`, or loaded from YAML/TOML configs. |
| **VisualSpec** | JSON structure returned by `module.visualize()` with `render` type and `data` payload. |

### Event Lifecycle

Every simulation follows this sequence:
```
STARTED -> TICK (xN) -> FINISHED
```

`PAUSED`, `RESUMED`, `STOPPED`, and `ERROR` may also be emitted depending on runtime control flow.

## License

MIT. See `LICENSE.txt`.
