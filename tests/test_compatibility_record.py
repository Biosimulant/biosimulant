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


def test_wire_mode_classifies_each_connection() -> None:
    cases = [
        (_sequence(SEQUENCE), _sequence(SEQUENCE), "verified"),
        (_sequence(SEQUENCE), _sequence(), "partial"),
        (_sequence(), _sequence(SEQUENCE), "partial"),
        (_sequence(), _sequence(), "structural"),
        (_sequence(SEQUENCE), _sequence(SMILES), "blocked"),
        (_sequence(MOUSE_SEQUENCE), _sequence(HUMAN_SEQUENCE), "blocked"),
        (_sequence(SEQUENCE), _sequence(HUMAN_SEQUENCE), "blocked"),
        (SignalSpec.scalar(dtype="float64"), _sequence(), "blocked"),
    ]
    for source, target, expected in cases:
        assert wire_mode(source, target, check_compatibility(source, target)) == expected


def test_lab_run_writes_record_for_every_wire(tmp_path: Path, capsys) -> None:
    results_file = tmp_path / "results.json"
    main(
        [
            "labs",
            "run",
            str(FIXTURE_LAB),
            "--no-install-deps",
            "--json",
            "--results-file",
            str(results_file),
        ],
        prog="biosimulant",
    )
    capsys.readouterr()
    record = json.loads(results_file.read_text(encoding="utf-8"))["compatibility"]

    assert record["schema_version"] == "1"
    assert record["standard"] == "biosimulant.model-compatibility"
    assert record["runtime_version"] == pack_module.__version__
    assert [profile["ref"] for profile in record["profiles"]] == ["protein.sequence/v1"]
    assert record["profiles"][0]["sha256"].startswith("sha256:")
    summary = record["summary"]
    assert (summary["wires"], summary["verified"], summary["partial"], summary["structural"], summary["blocked"]) == (3, 1, 1, 1, 0)
    assert summary["value_checks"] >= 2
    assert summary["value_failures"] == 0
    assert record["violations"] == []
    assert record["truncated"] is False

    verified = _wire(record, "emitter.protein_sequence", "consumer.protein_sequence")
    assert verified["mode"] == "verified"
    assert verified["source"]["profile"] == verified["target"]["profile"] == "protein.sequence/v1"
    assert verified["value_checks"] == {"count": 1, "failures": 0}
    partial = _wire(record, "emitter.protein_sequence", "sink.protein_sequence")
    assert (partial["mode"], partial["status"]) == ("partial", "warning")
    assert [issue["code"] for issue in partial["issues"]] == ["PROFILE_PARTIAL"]
    structural = _wire(record, "emitter.note", "consumer.note")
    assert structural["mode"] == "structural"
    assert structural["value_checks"] == {"count": 0, "failures": 0}

    ports = {(port["module"], port["direction"], port["port"]): port for port in record["ports"]}
    assert set(ports) == {
        ("emitter", "output", "protein_sequence"),
        ("consumer", "input", "protein_sequence"),
    }
    assert ports[("emitter", "output", "protein_sequence")]["value_checks"]["count"] == 1


def test_lab_validate_reports_static_wire_modes(capsys) -> None:
    main(["labs", "validate", str(FIXTURE_LAB), "--json"], prog="biosimulant")
    payload = json.loads(capsys.readouterr().out)

    assert payload["valid"] is True
    record = payload["compatibility"]
    assert [
        _wire(record, source, target)["mode"]
        for source, target in (
            ("emitter.protein_sequence", "consumer.protein_sequence"),
            ("emitter.protein_sequence", "sink.protein_sequence"),
            ("emitter.note", "consumer.note"),
        )
    ] == ["verified", "partial", "structural"]
    assert record["summary"]["value_checks"] == 0


def test_lab_validate_fails_on_statically_blocked_wire(tmp_path: Path, capsys) -> None:
    lab = _copy_fixture(tmp_path)

    def use_human(data: dict) -> None:
        data["io"]["inputs"][0]["contract"] = dict(HUMAN_SEQUENCE)

    _edit_yaml(lab / "models" / "consumer" / "model.yaml", use_human)

    with pytest.raises(SystemExit) as exited:
        main(["labs", "validate", str(lab), "--json"], prog="biosimulant")
    assert exited.value.code == 1
    payload = json.loads(capsys.readouterr().err)
    assert "emitter.protein_sequence -> consumer.protein_sequence" in payload["errors"][0]


def test_blocked_emitted_value_exits_2_with_partial_record(tmp_path: Path, capsys) -> None:
    lab = _copy_fixture(tmp_path)

    def bad_sequence(data: dict) -> None:
        data["models"][0]["parameters"] = {"sequence": "12!"}

    _edit_yaml(lab / "lab.yaml", bad_sequence)
    results_file = tmp_path / "results.json"

    with pytest.raises(SystemExit) as exited:
        main(
            [
                "labs",
                "run",
                str(lab),
                "--no-install-deps",
                "--json",
                "--results-file",
                str(results_file),
            ],
            prog="biosimulant",
        )
    assert exited.value.code == 2

    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])["error"]
    assert error["code"] == "compatibility_blocked"
    assert "unsupported character" in error["message"]
    violations = error["compatibility"]["violations"]
    assert len(violations) == 1
    assert violations[0]["stage"] == "output_value"
    assert (violations[0]["module"], violations[0]["port"]) == ("emitter", "protein_sequence")
    assert violations[0]["code"] == "VALUE_INVALID"

    results = json.loads(results_file.read_text(encoding="utf-8"))
    assert results["status"] == "failed"
    assert results["error"]["code"] == "compatibility_blocked"
    assert results["compatibility"]["summary"]["wires"] == 3
    assert results["compatibility"]["summary"]["value_failures"] == 1


def test_initial_input_is_checked_against_manifest_only_contract(tmp_path: Path, capsys) -> None:
    lab = _copy_fixture(tmp_path)

    def consumer_only(data: dict) -> None:
        data["models"] = [model for model in data["models"] if model["alias"] == "consumer"]
        data["wiring"] = []
        data["runtime"]["initial_inputs"] = {"protein_sequence": "12!"}

    _edit_yaml(lab / "lab.yaml", consumer_only)

    with pytest.raises(SystemExit) as exited:
        main(["labs", "run", str(lab), "--no-install-deps", "--json"], prog="biosimulant")
    assert exited.value.code == 2
    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])["error"]
    (violation,) = error["compatibility"]["violations"]
    assert violation["stage"] == "initial_input"
    assert (violation["module"], violation["port"]) == ("consumer", "protein_sequence")


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


def test_wire_value_violation_names_both_ports() -> None:
    world = BioWorld(communication_step=1.0)
    world.add_biomodule("producer", _Emitter(_sequence(), "ACD1"))
    world.add_biomodule("consumer", _Receiver(_sequence(SEQUENCE)))
    world.connect("producer.value", "consumer.value")

    with pytest.raises(CompatibilityError, match="unsupported character") as raised:
        world.run(2.0)

    record = raised.value.compatibility
    (violation,) = record["violations"]
    assert violation["stage"] == "wire_value"
    assert (violation["module"], violation["port"], violation["peer"]) == ("consumer", "value", "producer.value")
    wire = _wire(record, "producer.value", "consumer.value")
    assert (wire["mode"], wire["status"]) == ("blocked", "blocked")
    assert wire["value_checks"] == {"count": 1, "failures": 1}


def test_profile_mismatch_blocks_and_records_at_connect() -> None:
    world = BioWorld(communication_step=1.0)
    world.add_biomodule("producer", _Emitter(_sequence(SEQUENCE), "ACDE"))
    world.add_biomodule("consumer", _Receiver(_sequence(SMILES)))

    with pytest.raises(CompatibilityError, match="expects 'chemical.smiles/v1'") as raised:
        world.connect("producer.value", "consumer.value")

    (violation,) = raised.value.compatibility["violations"]
    assert (violation["stage"], violation["code"], violation["sim_time"]) == ("connect", "PROFILE_MISMATCH", None)
    assert world.compatibility.to_dict()["summary"]["blocked"] == 1


def test_unconnected_profiled_output_is_checked_on_emission() -> None:
    world = BioWorld(communication_step=1.0)
    world.add_biomodule("producer", _Emitter(_sequence(SEQUENCE), "ACD1"))

    with pytest.raises(CompatibilityError) as raised:
        world.run(1.0)

    (violation,) = raised.value.compatibility["violations"]
    assert (violation["stage"], violation["module"], violation["port"]) == ("output_value", "producer", "value")


def test_unchanged_output_file_is_checked_once(tmp_path: Path) -> None:
    alignment = tmp_path / "query.a3m"
    alignment.write_text(">query\nACDE\n", encoding="utf-8")
    spec = SignalSpec.scalar(dtype="str", value_type="file", format="a3m", contract=A3M)
    world = BioWorld(communication_step=1.0)
    world.add_biomodule("producer", _Emitter(spec, str(alignment)))

    world.run(3.0)
    counts = world.compatibility.to_dict()["ports"][0]["value_checks"]
    assert counts == {"count": 1, "failures": 0}

    alignment.write_text(">query\nACDEF\n", encoding="utf-8")
    world.run(1.0)
    assert world.compatibility.to_dict()["ports"][0]["value_checks"]["count"] == 2


def test_violations_are_capped_and_flagged() -> None:
    recorder = CompatibilityRecorder()
    blocked = CompatibilityResult((CompatibilityIssue(level="blocked", code="VALUE_INVALID", message="bad"),))
    for index in range(150):
        recorder.record_value_check("output_value", "producer", "value", None, blocked, float(index), profile="protein.sequence/v1")

    record = recorder.to_dict()
    assert len(record["violations"]) == MAX_VIOLATIONS == 100
    assert record["truncated"] is True
    assert record["summary"]["violations"] == 150
    assert record["summary"]["value_failures"] == 150


def test_model_pins_on_the_runtime_are_not_installed(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return _Completed()

    monkeypatch.setattr(pack_module, "_uv_module_available", lambda: False)
    monkeypatch.setattr(pack_module.subprocess, "run", fake_run)

    manifest = {"runtime": {"dependencies": {"packages": ["biosimulant==0.0.1"]}}}
    pack_module._install_declared_dependencies(manifest)
    assert calls == []
    assert "not installing biosimulant==0.0.1" in capsys.readouterr().err

    manifest["runtime"]["dependencies"]["packages"] = ["Biosimulant[onnx]==0.0.1", "pyyaml==6.0.2"]
    pack_module._install_declared_dependencies(manifest)
    assert calls[-1][-1] == "pyyaml==6.0.2"
    assert not any("iosimulant" in part for part in calls[-1])


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
