# Quickstart

## Install

```bash
pip install biosimulant
```

The default install includes the local lab UI for `biosimulant labs serve`.

For bash or zsh Tab completion, register the installed CLI once:

```bash
# ~/.zshrc or ~/.bashrc
eval "$(register-python-argcomplete biosimulant)"
```

Restart the shell, or run `source ~/.zshrc` / `source ~/.bashrc`.

## Local Lab CLI

```bash
biosimulant labs create ./my-lab --name "My Lab"
biosimulant labs list .
biosimulant labs validate ./my-lab
biosimulant labs run ./my-lab --no-install-deps
biosimulant labs serve ./my-lab
```

`labs serve` opens `http://127.0.0.1:8765/` by default. Use `--no-open` to
start the server without opening a browser.

## Minimal example

```python
import biosimulant as biosim


class Eye(biosim.BioModule):
    execution_policy = biosim.ExecutionPolicy.EACH_WINDOW

    def outputs(self):
        return {"visual_stream": biosim.SignalSpec.scalar(dtype="float64")}

    def execute(self, inputs, *, context: biosim.ExecutionContext):
        assert context.window_end is not None
        return {"visual_stream": context.window_end}


class LGN(biosim.BioModule):
    execution_policy = biosim.ExecutionPolicy.EACH_WINDOW

    def inputs(self):
        return {"retina": biosim.SignalSpec.scalar(dtype="float64", max_age=0.2)}

    def outputs(self):
        return {"thalamus": biosim.SignalSpec.scalar(dtype="float64")}

    def execute(self, inputs, *, context: biosim.ExecutionContext):
        signal = inputs.get("retina")
        if signal is None:
            return {}
        return {"thalamus": signal.value}


world = biosim.BioWorld(communication_step=0.1)
builder = biosim.WiringBuilder(world)
builder.add("eye", Eye()).add("lgn", LGN())
builder.connect("eye.visual_stream", ["lgn.retina"]).apply()
world.run(duration=0.3)
```

## Choose an invocation policy

A finite input-to-output model remains a `BioModule` and uses the canonical
execution method:

```python
class Predictor(biosim.BioModule):
    execution_policy = biosim.ExecutionPolicy.ONCE_BEFORE_RUN

    def outputs(self):
        return {"score": biosim.SignalSpec.scalar(dtype="float64")}

    def execute(self, inputs, *, context: biosim.ExecutionContext):
        return {"score": 0.95}
```

Use `ONCE_AFTER_RUN` for final analysis of simulation outputs. Use `EACH_WINDOW`
when temporal or AI computation consumes evolving state; temporal code reads the
window bounds from `context`.

Repeat the policy in the model's `model.yaml` so Studio, Desktop and
`biosimulant labs serve` can tell when it runs without importing its code:

```yaml
biosim:
  entrypoint: "src.predictor:Predictor"
  communication_step: 0.01
  execution_policy: once_before_run
```

The class attribute still decides when the model runs; the runtime refuses to
load a model whose declaration disagrees with it. When every module in a Lab
declares a run-once policy, Run dialogs hide duration, communication step and
settle steps. Lab `runtime.communication_step` stays required, and a run still
needs a positive duration.

## Run the built-in examples

- `python examples/world_simulation.py`
- `python examples/wiring_builder_demo.py`
- `python examples/visuals_demo.py`

## Next reading

- `docs/biomodule.md`
- `docs/bioworld.md`
- `docs/wiring.md`
