"""Real-engine qualification; never replace the original RoadRunner reproduction.

Set BIOSIMULANT_TELLURIUM_QUALIFICATION_REQUIRED=1 in the prepared Linux
environment to turn missing optional engines into failures rather than skips.
These tests exercise an already prepared environment; dependency installation and
``uv pip check`` must be qualified separately by the managed-runtime harness.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import sys
from pathlib import Path

import numpy as np
import pytest

import biosim
from biosim.contrib.sbml import TelluriumSBMLBioModule
from biosim.signals import RecordSignal, ScalarSignal

pytestmark = pytest.mark.integration
ROOT = Path(
    os.environ.get(
        "BIOSIMULANT_TELLURIUM_QUALIFICATION_SOURCE_ROOT",
        str(Path(__file__).resolve().parents[1]),
    )
).resolve()
REPRODUCTION = ROOT / "examples/labs/repressilator-reproduction"
SOURCE_SHA256 = "d535c84bc5fe68b01728cac5c53f8fed971176e3f0f2367a9ba1e9c043b11a2d"

# Authored, explicit SBML: x(t)=x0*exp(-k*t), y=2*x. At t=1 an event
# changes x to 10; it does not alter the ODE or the assignment rule.
DECAY_SBML = """<?xml version="1.0" encoding="UTF-8"?>
<sbml xmlns="http://www.sbml.org/sbml/level3/version2/core" level="3" version="2">
  <model id="qualification_decay">
    <listOfCompartments><compartment id="cell" size="1" constant="true"/></listOfCompartments>
    <listOfSpecies>
      <species id="x" compartment="cell" initialAmount="2" hasOnlySubstanceUnits="true"
               boundaryCondition="false" constant="false"/>
    </listOfSpecies>
    <listOfParameters>
      <parameter id="k" value="0.5" constant="true"/>
      <parameter id="y" value="4" constant="false"/>
    </listOfParameters>
    <listOfRules>
      <rateRule variable="x"><math xmlns="http://www.w3.org/1998/Math/MathML">
        <apply><minus/><apply><times/><ci>k</ci><ci>x</ci></apply></apply>
      </math></rateRule>
      <assignmentRule variable="y"><math xmlns="http://www.w3.org/1998/Math/MathML">
        <apply><times/><cn>2</cn><ci>x</ci></apply>
      </math></assignmentRule>
    </listOfRules>
    <listOfEvents><event id="pulse" useValuesFromTriggerTime="true">
      <trigger initialValue="false" persistent="true">
        <math xmlns="http://www.w3.org/1998/Math/MathML"><apply><geq/>
          <csymbol encoding="text" definitionURL="http://www.sbml.org/sbml/symbols/time">time</csymbol>
          <cn>1</cn>
        </apply></math>
      </trigger>
      <listOfEventAssignments><eventAssignment variable="x">
        <math xmlns="http://www.w3.org/1998/Math/MathML"><cn>10</cn></math>
      </eventAssignment></listOfEventAssignments>
    </event></listOfEvents>
  </model>
</sbml>
"""


@pytest.fixture(scope="module")
def real_engines():
    required = os.environ.get("BIOSIMULANT_TELLURIUM_QUALIFICATION_REQUIRED") == "1"
    for name in ("tellurium", "scipy"):
        if importlib.util.find_spec(name) is None:
            if required:
                pytest.fail(f"Required qualification dependency is missing: {name}")
            pytest.skip(f"Optional real-engine qualification dependency is missing: {name}")
    # Once installed, broken imports must fail; importorskip can conceal an ABI failure.
    import scipy
    import tellurium

    return tellurium, scipy


class DecayModule(TelluriumSBMLBioModule):
    _OBSERVABLES = ["x", "y"]
    _PARAMETER_INPUTS = {"decay_rate": ("k", 0.5, "1/s", "Decay constant.")}
    _INITIAL_CONDITION_INPUTS = {"initial_x": ("x", 2.0, "copies", "Initial amount.")}
    _ENABLE_PARAMETER_OVERRIDES = True
    _ENABLE_INITIAL_CONDITIONS = True


@pytest.fixture
def decay(tmp_path, real_engines):
    source = tmp_path / "decay.xml"
    source.write_text(DECAY_SBML, encoding="utf-8")
    return DecayModule(str(source), integration_step=0.05)


def _scalar(module, name, value):
    return ScalarSignal("qualification", name, value, 0.0, spec=module.inputs()[name])


def _record(module, name, value):
    return RecordSignal("qualification", name, {"payload": value}, 0.0, spec=module.inputs()[name])


def _accurate(module):
    module._runner.setIntegrator("cvode")
    module._runner.integrator.relative_tolerance = 1e-10
    module._runner.integrator.absolute_tolerance = 1e-12
    module._runner.integrator.maximum_time_step = 0.05


def _state(module):
    return module.get_outputs()["state"].value


def test_real_initial_conditions_named_overrides_and_reset(decay):
    decay.set_inputs(
        {
            "initial_x": _scalar(decay, "initial_x", 6.0),
            "decay_rate": _scalar(decay, "decay_rate", 0.25),
        }
    )
    decay.setup()
    _accurate(decay)
    assert decay._patches_applied == []
    assert _state(decay) == pytest.approx({"x": 6.0, "y": 12.0})
    decay.advance_window(0.0, 0.25)
    decay.advance_window(0.25, 0.5)
    expected = 6.0 * np.exp(-0.25 * 0.5)
    assert _state(decay) == pytest.approx({"x": expected, "y": 2 * expected}, rel=1e-7)
    before = decay.get_outputs()["trajectory"].value
    decay.reset()
    assert _state(decay) == pytest.approx({"x": 6.0, "y": 12.0})
    decay.advance_window(0.0, 0.5)
    after = decay.get_outputs()["trajectory"].value
    for old, new in zip(before["payload"]["series"], after["payload"]["series"]):
        np.testing.assert_allclose(old["points"], new["points"], rtol=1e-7, atol=1e-9)


def test_real_record_overrides(decay):
    decay.set_inputs(
        {
            "initial_conditions": _record(decay, "initial_conditions", {"x": 8.0}),
            "parameter_overrides": _record(decay, "parameter_overrides", {"k": 0.75}),
        }
    )
    decay.setup()
    _accurate(decay)
    assert _state(decay) == pytest.approx({"x": 8.0, "y": 16.0})
    decay.advance_window(0.0, 0.5)
    expected = 8.0 * np.exp(-0.75 * 0.5)
    assert _state(decay) == pytest.approx({"x": expected, "y": 2 * expected}, rel=1e-7)


def test_real_sbml_event_assignment_and_reset_replay(decay):
    decay.setup()
    _accurate(decay)
    decay.advance_window(0.0, 0.5)
    assert _state(decay)["x"] == pytest.approx(2 * np.exp(-0.25), rel=1e-7)
    decay.advance_window(0.5, 1.5)
    expected = 10.0 * np.exp(-0.5 * 0.5)
    assert _state(decay) == pytest.approx({"x": expected, "y": 2 * expected}, rel=1e-7)
    decay.reset()
    decay.advance_window(0.0, 1.5)
    assert _state(decay) == pytest.approx({"x": expected, "y": 2 * expected}, rel=1e-7)


def test_canonical_checkpoint_limitation_is_explicit(decay):
    decay.setup()
    decay.advance_window(0.0, 0.5)
    # Characterize the existing limitation, rather than claiming continuation works.
    checkpoint = json.loads(json.dumps(decay.snapshot()))
    assert checkpoint == {}
    restored = DecayModule(str(decay._model_path))
    restored.restore(checkpoint)
    assert restored._runner is None
    assert restored.get_outputs() == {}
    restored.setup()
    assert _state(restored)["x"] == pytest.approx(2.0)
    assert _state(decay)["x"] != pytest.approx(2.0)


def test_unchanged_repressilator_against_frozen_independent_protocol(real_engines):
    source = REPRODUCTION / "evidence/BIOMD0000000012_url.xml"
    original = source.read_bytes()
    assert hashlib.sha256(original).hexdigest() == SOURCE_SHA256
    protocol = REPRODUCTION / "models/comparison/src/core.py"
    spec = importlib.util.spec_from_file_location("tellurium_qualification_protocol", protocol)
    assert spec is not None and spec.loader is not None
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)

    class QualificationModule(TelluriumSBMLBioModule):
        _OBSERVABLES = list(core.SPECIES)
        _TIME_UNIT = "min"

    adapter = QualificationModule(str(source), integration_step=0.5)
    adapter.setup()
    assert adapter._patches_applied == []
    adapter._runner.setIntegrator("cvode")
    adapter._runner.integrator.relative_tolerance = 1e-9
    adapter._runner.integrator.absolute_tolerance = 1e-11
    adapter._runner.integrator.maximum_time_step = 0.5
    adapter.advance_window(0.0, 2000.0)
    trajectory = adapter.get_outputs()["trajectory"].value["payload"]
    assert trajectory["time_unit"] == "min"
    assert [series["name"] for series in trajectory["series"]] == core.SPECIES
    assert [series["source"] for series in trajectory["series"]] == core.SPECIES
    for series in trajectory["series"]:
        np.testing.assert_allclose(np.asarray(series["points"])[:, 0], core.TIME, rtol=0, atol=1e-9)
    states = np.column_stack([np.asarray(s["points"])[:, 1] for s in trajectory["series"]])
    np.testing.assert_allclose(states[0], core.INITIAL, rtol=0, atol=1e-10)
    _, report = core.evaluate(core.TIME, states)
    # Keep the figure conclusion and all pre-existing scientific tolerances.
    assert source.read_bytes() == original
    destination = os.environ.get("BIOSIMULANT_TELLURIUM_QUALIFICATION_REPORT")
    if destination:
        report["qualification"] = {
            "scope": "local scientific adapter qualification; not deployment evidence",
            "python": sys.version,
            "platform": platform.platform(),
            "runtime_import": biosim.__file__,
            "adapter_solver": {
                "method": "cvode",
                "relative_tolerance": 1e-9,
                "absolute_tolerance": 1e-11,
                "maximum_time_step_min": 0.5,
            },
            "reference_role": (
                "The unchanged SBML executed through Tellurium is supplied as the reference; "
                "the preserved protocol computes independent DOP853 and tighter Radau trajectories."
            ),
            "distributions": {
                d: importlib.metadata.version(d)
                for d in (
                    "tellurium",
                    "libroadrunner",
                    "antimony",
                    "phrasedml",
                    "numpy",
                    "scipy",
                )
            },
            "sha256": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (source, protocol, REPRODUCTION / "MRS.md", REPRODUCTION / "MTS.md")
            },
            "canonical_checkpoint_continuation_supported": False,
        }
        Path(destination).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    assert all(report["checks"].values()), json.dumps(report["checks"], sort_keys=True)
    assert report["classification"] == "partial reproduction"
