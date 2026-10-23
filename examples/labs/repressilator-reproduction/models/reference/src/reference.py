"""RoadRunner-only adapter for checksum-pinned, unmodified BioModels SBML."""
import copy
import hashlib
import math
from pathlib import Path

import numpy as np
from biosimulant import BioModule, ExecutionPolicy, SignalSpec

SOURCE_SHA256 = "d535c84bc5fe68b01728cac5c53f8fed971176e3f0f2367a9ba1e9c043b11a2d"
SPECIES = ["X", "Y", "Z", "PX", "PY", "PZ"]


class RepressilatorReference(BioModule):
    execution_policy = ExecutionPolicy.EACH_WINDOW

    def __init__(self, model_path="artifacts/model.xml", integration_step=0.5):
        if not math.isfinite(integration_step) or integration_step <= 0:
            raise ValueError("Sampling step must be finite and positive")
        self._model_path = Path(__file__).resolve().parents[1] / model_path
        self.integration_step = float(integration_step)
        self._runner = None
        self._time = 0.0
        self._history = []
        self._initial_values = {}

    def inputs(self):
        return {}

    def outputs(self):
        return {
            "trajectory": SignalSpec.record(schema={"payload": "json"}, emitted_unit="copies/cell"),
            "state": SignalSpec.record(schema={s: "float" for s in SPECIES}, emitted_unit="copies/cell"),
            "summary": SignalSpec.record(schema={
                "duration_simulated": "float", "observable_count": "int",
                "largest_change_observable": "str", "largest_change_magnitude": "float",
                "peak_observable": "str", "peak_value": "float",
            }, emitted_unit="1"),
        }

    def setup(self, config=None):
        import roadrunner

        source = self._model_path.read_bytes()
        if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
            raise ValueError("Authoritative SBML checksum mismatch")
        # RoadRunner loads the original file directly: no XML edits or translated equations.
        self._runner = roadrunner.RoadRunner(str(self._model_path))
        self._runner.setIntegrator("cvode")
        self._runner.integrator.relative_tolerance = 1e-9
        self._runner.integrator.absolute_tolerance = 1e-11
        self._runner.integrator.maximum_time_step = 0.5
        self._initial_values = {s: float(self._runner[s]) for s in SPECIES}
        self._history = [{"t": 0.0, **self._initial_values}]
        self._time = 0.0

    def simulate_to(self, end):
        if self._runner is None:
            self.setup()
        end = float(end)
        if not math.isfinite(end) or end < self._time:
            raise ValueError("Native target time must be finite and nondecreasing")
        if end == self._time:
            return
        steps = (end - self._time) / self.integration_step
        if not math.isclose(steps, round(steps), rel_tol=0, abs_tol=1e-9):
            raise ValueError("Target must fall on the frozen sampling grid")
        result = np.asarray(self._runner.simulate(
            self._time, end, int(round(steps)) + 1,
            selections=["time", *SPECIES],
        ), dtype=float)
        if result.shape != (int(round(steps)) + 1, 7) or not np.isfinite(result).all():
            raise RuntimeError("Invalid native SBML trajectory")
        for row in result:
            if abs(float(row[0]) - self._history[-1]["t"]) <= 1e-10:
                continue
            self._history.append({"t": float(row[0]), **{
                s: float(row[j + 1]) for j, s in enumerate(SPECIES)
            }})
        self._time = end

    def trajectory(self):
        return {"time_unit": "min", "series": [
            {"name": s, "source": s, "points": [[r["t"], r[s]] for r in self._history]}
            for s in SPECIES
        ]}

    def execute(self, inputs, *, context):
        self.simulate_to(context.window_end)
        latest = self._history[-1]
        changes = {s: abs(latest[s] - self._initial_values[s]) for s in SPECIES}
        peak = max(SPECIES, key=lambda s: max(r[s] for r in self._history))
        changed = max(SPECIES, key=changes.get)
        return {
            "trajectory": {"payload": self.trajectory()},
            "state": {s: latest[s] for s in SPECIES},
            "summary": {
                "duration_simulated": self._time, "observable_count": len(SPECIES),
                "largest_change_observable": changed,
                "largest_change_magnitude": changes[changed],
                "peak_observable": peak,
                "peak_value": max(r[peak] for r in self._history),
            },
        }

    def snapshot(self):
        return {
            "initialized": self._runner is not None,
            "time": self._time,
            "amounts": None if self._runner is None else {
                s: float(self._runner[s]) for s in SPECIES
            },
            "history": copy.deepcopy(self._history),
            "initial_values": copy.deepcopy(self._initial_values),
        }

    def restore(self, snapshot):
        if not snapshot["initialized"]:
            self._runner = None
            self._time = 0.0
            self._history = []
            self._initial_values = {}
            return
        if self._runner is None:
            self.setup()
        for s in SPECIES:
            self._runner[s] = float(snapshot["amounts"][s])
        self._runner.model.setTime(float(snapshot["time"]))
        self._time = float(snapshot["time"])
        self._history = copy.deepcopy(snapshot["history"])
        self._initial_values = copy.deepcopy(snapshot["initial_values"])
