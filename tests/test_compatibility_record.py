from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import pytest
import yaml

from biosim import BioModule, BioWorld, ExecutionContext, SignalSpec
from biosim import managed_runtime
from biosim import pack as pack_module
from biosim.__main__ import main
from biosim.compatibility import (
    MAX_VIOLATIONS,
    CompatibilityError,
    CompatibilityIssue,
    CompatibilityRecorder,
    CompatibilityResult,
    check_compatibility,
    wire_mode,
)

FIXTURE_LAB = Path(__file__).parent / "fixtures" / "compat_lab"
SEQUENCE = {"profile": "protein.sequence/v1"}
HUMAN_SEQUENCE = {"profile": "protein.sequence/v1", "species": "NCBITaxon:9606"}
MOUSE_SEQUENCE = {"profile": "protein.sequence/v1", "species": "NCBITaxon:10090"}
SMILES = {"profile": "chemical.smiles/v1"}
A3M = {"profile": "protein.multiple-sequence-alignment/v1"}


def _sequence(contract: dict | None = None) -> SignalSpec:
    return SignalSpec.scalar(dtype="str", format="sequence", contract=contract)


def _copy_fixture(tmp_path: Path) -> Path:
    lab = tmp_path / "compat_lab"
    shutil.copytree(FIXTURE_LAB, lab, ignore=shutil.ignore_patterns("__pycache__"))
    return lab


def _edit_yaml(path: Path, change) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _wire(record: dict, source: str, target: str) -> dict:
    for wire in record["wires"]:
        if (
            f"{wire['source']['module']}.{wire['source']['port']}" == source
            and f"{wire['target']['module']}.{wire['target']['port']}" == target
        ):
            return wire
    raise AssertionError(f"wire {source} -> {target} not recorded")














class _Emitter(BioModule):
    execution_policy = "each_window"

    def __init__(self, spec: SignalSpec, value) -> None:
        self.spec = spec
        self.value = value

    def outputs(self):
        return {"value": self.spec}

    def execute(self, inputs, *, context: ExecutionContext):
        return {"value": self.value}


class _Receiver(BioModule):
    execution_policy = "each_window"

    def __init__(self, spec: SignalSpec) -> None:
        self.spec = spec

    def inputs(self):
        return {"value": self.spec}

    def execute(self, inputs, *, context: ExecutionContext):
        return {}












def test_model_pins_on_the_runtime_are_not_installed() -> None:
    from biosim.dependency_runtime import collect_requirements

    manifest = {"runtime": {"dependencies": {"packages": ["biosimulant==0.0.1"]}}}
    assert collect_requirements([("model", manifest)]) == []
    manifest["runtime"]["dependencies"]["packages"] = ["Biosimulant[onnx]==0.0.1", "pyyaml==6.0.2"]
    assert collect_requirements([("model", manifest)]) == ["pyyaml==6.0.2"]


def test_managed_child_compatibility_error_is_rebuilt(monkeypatch, tmp_path: Path) -> None:
    child_error = {
        "biosimulant_child_error": CompatibilityError(
            "incompatible ports: bad", compatibility={"summary": {"blocked": 1}}
        ).to_dict()
    }

    class _Process:
        def __init__(self, command, **_kwargs) -> None:
            self.command = command
            self.stdout = io.StringIO(json.dumps(child_error) + "\n")
            self.stderr = io.StringIO("")

        def wait(self) -> int:
            return 2

    monkeypatch.setattr(managed_runtime.subprocess, "Popen", _Process)

    with pytest.raises(CompatibilityError, match="incompatible ports: bad") as raised:
        managed_runtime.run_child_package(tmp_path / "python", tmp_path / "lab.bsilab", install_deps=False)
    assert raised.value.compatibility == {"summary": {"blocked": 1}}

def test_legacy_lab_runs_without_profile_grading(tmp_path,capsys):
    result=tmp_path/'results.json'
    main(['labs','run',str(FIXTURE_LAB),'--no-install-deps','--json','--results-file',str(result)],prog='biosimulant')
    capsys.readouterr()
    document=json.loads(result.read_text())
    record=document['compatibility']
    assert record['scope']=='structural'
    assert record['summary']['blocked']==0
    assert record['summary']['verified']==0
    assert record['summary']['value_checks']==0
    assert all(w['mode']=='structural' for w in record['wires'])
    assert document['execution']['outcome']['state']=='completed'
