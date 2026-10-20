"""Source-faithful canonical adapter for the immutable BioModels reference."""
import copy
import hashlib
from dataclasses import replace
from pathlib import Path
from biosim.contrib.sbml import TelluriumSBMLBioModule

SOURCE_SHA256 = "d535c84bc5fe68b01728cac5c53f8fed971176e3f0f2367a9ba1e9c043b11a2d"
SPECIES = ["X", "Y", "Z", "PX", "PY", "PZ"]

class RepressilatorReference(TelluriumSBMLBioModule):
    _SBML_ID = "BIOMD0000000012"
    _TITLE = "Elowitz2000 - Repressilator"
    _TIME_UNIT = "min"
    _OBSERVABLES = SPECIES
    _EXPOSE_INTEGRATION_STEP_INPUT = False

    def __init__(self, model_path="artifacts/model.xml", integration_step=0.5):
        super().__init__(model_path=model_path, integration_step=integration_step)

    def setup(self, config=None):
        if hashlib.sha256(self._model_path.read_bytes()).hexdigest() != SOURCE_SHA256:
            raise ValueError("Authoritative SBML checksum mismatch")
        super().setup(config)
        if self._patches_applied:
            raise ValueError("Unmodified source required; native adapter proposed a repair")
        self._runner.integrator.relative_tolerance = 1e-9
        self._runner.integrator.absolute_tolerance = 1e-11
        self._runner.integrator.maximum_time_step = 0.5

    def outputs(self):
        specs = dict(super().outputs())
        specs["trajectory"] = replace(specs["trajectory"], emitted_unit="copies/cell")
        specs["state"] = replace(specs["state"], emitted_unit="copies/cell")
        specs["summary"] = replace(specs["summary"], emitted_unit="1")
        return specs

    def snapshot(self):
        result = dict(super().snapshot())
        result.update({
            "time": self._time,
            "amounts": {s: float(self._runner[s]) for s in SPECIES},
            "history": copy.deepcopy(self._history),
            "initial_values": copy.deepcopy(self._initial_values),
        })
        return result

    def restore(self, snapshot):
        super().restore(snapshot)
        if self._runner is None:
            self.setup()
        for s in SPECIES:
            self._runner[s] = float(snapshot["amounts"][s])
        self._runner.model.setTime(float(snapshot["time"]))
        self._time = float(snapshot["time"])
        self._history = copy.deepcopy(snapshot["history"])
        self._initial_values = copy.deepcopy(snapshot["initial_values"])
        self.publish_outputs(self._time)
