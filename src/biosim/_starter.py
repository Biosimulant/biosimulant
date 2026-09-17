from __future__ import annotations

from pathlib import Path


_STARTER_MODEL_MANIFEST = """schema_version: "2.0"
title: "Hello Model"
description: "Starter local Biosimulant model"
standard: other
tags: [starter]
authors: ["Biosimulant"]
package: local/hello
version: 0.1.0
biosim:
  entrypoint: "src.hello:HelloModule"
  communication_step: 1.0
  execution_policy: each_window
"""

_STARTER_MODEL_SOURCE = '''from biosimulant import BioModule, ExecutionContext, ExecutionPolicy, SignalSpec


class HelloModule(BioModule):
    execution_policy = ExecutionPolicy.EACH_WINDOW

    def __init__(self):
        self.time = 0.0

    def outputs(self):
        return {"time": SignalSpec.scalar(dtype="float64")}

    def execute(self, inputs, *, context: ExecutionContext):
        assert context.window_end is not None
        self.time = float(context.window_end)
        return {"time": self.time}

    def snapshot(self):
        return {"time": self.time}
'''


def write_starter_model(path: Path) -> None:
    """Write the canonical starter model shared by all Lab creation commands."""

    path.mkdir(parents=True, exist_ok=True)
    (path / "model.yaml").write_text(_STARTER_MODEL_MANIFEST, encoding="utf-8")
    src_dir = path / "src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "hello.py").write_text(_STARTER_MODEL_SOURCE, encoding="utf-8")
