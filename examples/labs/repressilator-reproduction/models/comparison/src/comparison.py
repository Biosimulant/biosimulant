"""After-run comparison using actual typed canonical-reference outputs."""
import importlib.metadata
import platform
import subprocess
import sys
from pathlib import Path
from biosimulant import AcceptedSignalProfile, BioModule, ExecutionPolicy, SignalSpec
from .core import SPECIES, PROTEINS, evaluate

class RepressilatorComparison(BioModule):
    execution_policy = ExecutionPolicy.ONCE_AFTER_RUN

    def __init__(self):
        self.traces = None
        self.report = None

    def inputs(self):
        return {"reference": SignalSpec.record(schema={"payload":"json"}, accepted_profiles=(
            AcceptedSignalProfile(signal_type="record", schema={"payload":"json"},
                                  accepted_units=("copies/cell",)),))}

    def outputs(self):
        return {"trajectories": SignalSpec.record(schema={"payload":"json"}, emitted_unit="copies/cell"),
                "report": SignalSpec.record(schema={"payload":"json"}, emitted_unit="1")}

    def execute(self, inputs, *, context):
        if "reference" not in inputs:
            raise ValueError("Completed native reference trajectory is required")
        payload = inputs["reference"].value["payload"]
        if payload["time_unit"] != "min":
            raise ValueError("Native source time must be minutes")
        series = {s["source"]:s["points"] for s in payload["series"]}
        time = [row[0] for row in series[SPECIES[0]]]
        if any([row[0] for row in series[s]] != time for s in SPECIES):
            raise ValueError("Native source series have inconsistent timestamps")
        states = [[series[s][i][1] for s in SPECIES] for i in range(len(time))]
        self.traces, self.report = evaluate(time, states)
        root = Path(__file__).resolve().parents[3]
        completed = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s",
                                    str(root/"tests"), "-v"], cwd=root,
                                   capture_output=True, text=True, timeout=180)
        self.report["acceptance_suite"] = {"returncode":completed.returncode,
                                          "passed":completed.returncode == 0,
                                          "output":(completed.stdout+completed.stderr)[-24000:]}
        self.report["environment"] = {"python":sys.version, "platform":platform.platform(),
            "packages":dict(sorted((d.metadata["Name"],d.version) for d in importlib.metadata.distributions()
                                   if d.metadata.get("Name")))}
        self.report["authoritative_sbml_sha256"] = "d535c84bc5fe68b01728cac5c53f8fed971176e3f0f2367a9ba1e9c043b11a2d"
        if completed.returncode:
            self.report["classification"] = "reproduction not established"
            self.report["classification_reason"] = "Acceptance tests failed; retain diagnostics and investigate."
        return {"trajectories":{"payload":self.traces},"report":{"payload":self.report}}

    def visualize(self):
        if self.traces is None:
            return []
        series = []
        # Same retained values, every fourth sample, first 1000 min: <=3003 points.
        for j, name in enumerate(PROTEINS):
            series.append({"name":name,"points":[[self.traces["time_min"][i],
                            self.traces["primary"][i][j+3]] for i in range(0,2001,4)]})
        return {"schema_version":"1","render":"timeseries",
                "data":{"x_label":"Time","x_unit":"min","y_label":"Protein","y_unit":"copies/cell","series":series},
                "description":"Independent six-ODE trajectories, 0–1000 min. Curated initial state; exact paper initialization is unavailable."}
