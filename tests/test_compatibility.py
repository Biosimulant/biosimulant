from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from biosim import (
    AcceptedSignalProfile,
    BioModule,
    BioWorld,
    CompatibilityIssue,
    CompatibilityResult,
    ExecutionContext,
    SignalEnvelope,
    SignalSpec,
    check_compatibility,
    check_payload,
    compatibility_provenance,
    contract_digest,
)
from biosim.__main__ import main
from biosim import compatibility as compatibility_module
from biosim.compatibility import (
    bind_manifest_ports,
    enforce_result,
    load_yaml,
    manifest_has_compatibility_declarations,
    validate_contract,
    validate_manifest,
)

STANDARD = {
    "standard": "biosimulant.model-compatibility",
    "version": "0",
}
SEQUENCE = {"profile": "protein.sequence/v1"}
HUMAN_SEQUENCE = {"profile": "protein.sequence/v1", "species": "NCBITaxon:9606"}
SMILES = {"profile": "chemical.smiles/v1"}
PROBABILITY = {"profile": "boltz.binding-probability/v1"}
AFFINITY = {"profile": "boltz.log10-ic50-micromolar/v1"}


def _manifest(*, direction: str = "outputs", contract: dict | None = None) -> dict:
    port = {
        "name": "sequence",
        "signal_type": "scalar",
        "dtype": "str",
        "format": "sequence",
    }
    if contract is not None:
        port["contract"] = copy.deepcopy(contract)
    return {
        "schema_version": "2.0",
        "standard": "other",
        "title": "Compatibility example",
        "biosim": {"entrypoint": "src.model:Model"},
        "compatibility": copy.deepcopy(STANDARD),
        "io": {"inputs": [port] if direction == "inputs" else [], "outputs": [port] if direction == "outputs" else []},
    }


def test_contract_validation_is_closed_and_profile_based(monkeypatch) -> None:
    assert validate_contract(None).status == "ok"
    assert validate_contract("bad").issues[0].code == "INVALID_CONTRACT"
    assert validate_contract({}).issues[0].code == "PROFILE_UNKNOWN"
    assert validate_contract({"profile": "missing/v1"}).issues[0].code == "PROFILE_UNKNOWN"
    assert validate_contract({"profile": "chemical.smiles/v1", "species": "any"}).status == "blocked"
    assert validate_contract({"profile": "protein.sequence/v1", "species": "any"}).status == "ok"
    assert validate_contract({"profile": "protein.sequence/v1", "typo": "x"}).status == "blocked"

    monkeypatch.delitem(compatibility_module._CHECKERS, "protein_sequence")
    result = validate_contract(SEQUENCE)
    assert result.status == "blocked"
    assert result.issues[-1].code == "CHECKER_UNAVAILABLE"


def test_connection_rules_are_exact_and_context_is_not_guessed() -> None:
    plain = SignalSpec.scalar(dtype="str", format="sequence")
    sequence = SignalSpec.scalar(dtype="str", format="sequence", contract=SEQUENCE)
    human = SignalSpec.scalar(dtype="str", format="sequence", contract=HUMAN_SEQUENCE)
    any_species = SignalSpec.scalar(
        dtype="str", format="sequence", contract={"profile": "protein.sequence/v1", "species": "any"}
    )
    mouse = SignalSpec.scalar(
        dtype="str", format="sequence", contract={"profile": "protein.sequence/v1", "species": "NCBITaxon:10090"}
    )
    smiles = SignalSpec.scalar(dtype="str", format="sequence", contract=SMILES)

    assert check_compatibility(plain, plain).status == "ok"
    assert check_compatibility(plain, sequence).issues[-1].code == "STANDARD_REQUIRED"
    assert check_compatibility(sequence, plain).issues[-1].code == "STANDARD_REQUIRED"
    assert check_compatibility(sequence, smiles).issues[-1].code == "PROFILE_MISMATCH"
    assert check_compatibility(human, any_species).status == "ok"
    assert check_compatibility(sequence, human).issues[-1].code == "CONTEXT_MISSING"
    assert check_compatibility(any_species, human).issues[-1].code == "CONTEXT_MISSING"
    assert check_compatibility(mouse, human).issues[-1].code == "CONTEXT_MISMATCH"


def test_structural_mismatch_blocks_before_profile_comparison() -> None:
    source = SignalSpec.scalar(dtype="str", format="sequence", contract=SEQUENCE)
    wrong_format = SignalSpec.scalar(dtype="str", format="fasta", contract=SEQUENCE)
    wrong_dtype = SignalSpec.scalar(dtype="float64", contract=PROBABILITY)
    numeric_target = SignalSpec.scalar(dtype="float32", contract=PROBABILITY)
    assert check_compatibility(source, wrong_format).issues[0].code == "PROFILE_REPRESENTATION_MISMATCH"
    assert check_compatibility(wrong_dtype, numeric_target).issues[0].code == "PROFILE_REPRESENTATION_MISMATCH"


def test_value_checkers_cover_all_six_profiles(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(compatibility_module, "_CHECKERS", dict(compatibility_module._CHECKERS))
    assert check_payload(SEQUENCE, "ACDEFG").status == "ok"
    assert check_payload(SEQUENCE, "ACD1").status == "blocked"
    assert check_payload(SMILES, "CCO>>CC=O").status == "blocked"
    assert check_payload(PROBABILITY, 0.5).status == "ok"
    assert check_payload(PROBABILITY, 1.1).status == "blocked"
    assert check_payload(PROBABILITY, True).status == "blocked"
    assert check_payload(AFFINITY, -2.1).status == "ok"
    assert check_payload(AFFINITY, float("inf")).status == "blocked"

    a3m = tmp_path / "query.a3m"
    a3m.write_text(">query\nACDE\n>hit\nAC-E\n", encoding="utf-8")
    assert check_payload({"profile": "protein.multiple-sequence-alignment/v1"}, str(a3m)).status == "ok"
    a3m.write_text("ACDE\n", encoding="utf-8")
    assert check_payload({"profile": "protein.multiple-sequence-alignment/v1"}, str(a3m)).status == "blocked"

    cif = tmp_path / "model.cif"
    cif.write_text("data_model\n_entry.id model\n", encoding="utf-8")
    assert check_payload({"profile": "protein-ligand.complex-structure-mmcif/v1"}, str(cif)).status == "ok"
    cif.write_text("_entry.id model\n", encoding="utf-8")
    assert check_payload({"profile": "protein-ligand.complex-structure-mmcif/v1"}, str(cif)).status == "blocked"


def test_checker_exception_fails_closed(monkeypatch) -> None:
    def broken(value):
        raise RuntimeError("broken checker")

    monkeypatch.setitem(compatibility_module._CHECKERS, "finite_number", broken)
    result = check_payload(AFFINITY, 1.0)
    assert result.status == "blocked"
    assert result.issues[-1].code == "CHECKER_FAILED"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda m: m.pop("compatibility"), "STANDARD_REQUIRED"),
        (lambda m: m["compatibility"].update({"standard": "other"}), "STANDARD_MISMATCH"),
        (lambda m: m["compatibility"].update({"version": "1"}), "STANDARD_MISMATCH"),
        (lambda m: m["io"]["outputs"][0].update({"dtype": "float64"}), "PROFILE_REPRESENTATION_MISMATCH"),
        (lambda m: m["io"]["outputs"][0].update({"format": "fasta"}), "PROFILE_REPRESENTATION_MISMATCH"),
    ],
)
def test_manifest_validation_blocks_invalid_declarations(change, code: str) -> None:
    manifest = _manifest(contract=SEQUENCE)
    change(manifest)
    assert code in {item["code"] for item in validate_manifest(manifest)}


def test_manifest_without_profiles_remains_valid() -> None:
    manifest = _manifest(contract=None)
    manifest.pop("compatibility")
    assert validate_manifest(manifest) == []
    assert manifest_has_compatibility_declarations(manifest) is False


def test_manifest_rejects_unknown_context_and_port_shapes() -> None:
    manifest = _manifest(contract={"profile": "boltz.binding-probability/v1", "species": "any"})
    codes = {item["code"] for item in validate_manifest(manifest)}
    assert "PROFILE_REPRESENTATION_MISMATCH" in codes
    assert validate_manifest({"io": "bad"})[0]["code"] == "PROFILE_REPRESENTATION_MISMATCH"


class _SequenceModel(BioModule):
    execution_policy = "each_window"

    def inputs(self):
        return {}

    def outputs(self):
        return {"sequence": SignalSpec.scalar(dtype="str", format="sequence")}

    def execute(self, inputs, *, context: ExecutionContext):
        return {"sequence": "ACDE"}


def test_manifest_contract_binds_to_python_signal_spec() -> None:
    model = _SequenceModel()
    _, outputs = bind_manifest_ports(model, _manifest(contract=SEQUENCE))
    assert outputs["sequence"].contract == SEQUENCE


def test_manifest_and_python_structure_or_contract_cannot_disagree() -> None:
    class Conflict(_SequenceModel):
        def outputs(self):
            return {"sequence": SignalSpec.scalar(dtype="str", format="sequence", contract=HUMAN_SEQUENCE)}

    with pytest.raises(ValueError, match="contract differs"):
        bind_manifest_ports(Conflict(), _manifest(contract=SEQUENCE))

    wrong = _manifest(contract=SEQUENCE)
    wrong["io"]["outputs"][0]["format"] = "fasta"
    with pytest.raises(ValueError, match="Port representation"):
        bind_manifest_ports(_SequenceModel(), wrong)


def test_contract_digest_qualifies_profile_definition_and_context() -> None:
    assert contract_digest(SEQUENCE) != contract_digest(HUMAN_SEQUENCE)
    assert len(contract_digest(SEQUENCE)) == 71
    envelope = SignalEnvelope(
        contract_digest=contract_digest(HUMAN_SEQUENCE),
        value="ACDE",
        actual_context={"species": "NCBITaxon:9606"},
    )
    envelope.validate_contract(HUMAN_SEQUENCE)
    with pytest.raises(ValueError, match="different contract"):
        envelope.validate_contract(SEQUENCE)


def test_provenance_contains_only_profiles_used_by_manifest() -> None:
    manifest = _manifest(contract=SEQUENCE)
    provenance = compatibility_provenance(manifest)
    assert provenance["standard"] == "biosimulant.model-compatibility"
    assert provenance["version"] == "0"
    assert provenance["catalogue_version"] == "0.1.0"
    assert [item["ref"] for item in provenance["profiles"]] == ["protein.sequence/v1"]
    assert compatibility_provenance(_manifest(contract=None)) is None


def test_cli_reports_standard_profiles_validation_and_comparison(tmp_path: Path, capsys) -> None:
    producer = tmp_path / "producer.yaml"
    consumer = tmp_path / "consumer.yaml"
    producer.write_text(yaml.safe_dump(_manifest(contract=SEQUENCE)), encoding="utf-8")
    consumer.write_text(yaml.safe_dump(_manifest(direction="inputs", contract=SEQUENCE)), encoding="utf-8")

    main(["compatibility", "standard"])
    assert json.loads(capsys.readouterr().out)["version"] == "0"
    main(["compatibility", "profiles"])
    assert json.loads(capsys.readouterr().out)["count"] == 6
    main(["compatibility", "show", "protein.sequence/v1"])
    assert json.loads(capsys.readouterr().out)["ref"] == "protein.sequence/v1"
    main(["compatibility", "validate", str(producer)])
    assert json.loads(capsys.readouterr().out)["valid"] is True
    main([
        "compatibility",
        "compare",
        f"{producer}#outputs.sequence",
        f"{consumer}#inputs.sequence",
        "--sample-json",
        '"ACDE"',
    ])
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_yaml_loader_and_enforcement_errors_are_explicit(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("io: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_yaml(broken)
    blocked = compatibility_module.CompatibilityResult(
        (compatibility_module.CompatibilityIssue("blocked", "Stop.", "STOP"),)
    )
    with pytest.raises(ValueError, match="context: Stop"):
        enforce_result(blocked, context="context")


class _BadSequenceProducer(BioModule):
    execution_policy = "each_window"

    def outputs(self):
        return {"sequence": SignalSpec.scalar(dtype="str", format="sequence", contract=SEQUENCE)}

    def execute(self, inputs, *, context: ExecutionContext):
        return {"sequence": "ACD1"}


class _SequenceConsumer(BioModule):
    execution_policy = "each_window"

    def inputs(self):
        return {"sequence": SignalSpec.scalar(dtype="str", format="sequence", contract=SEQUENCE)}

    def execute(self, inputs, *, context: ExecutionContext):
        return {}


def test_world_rechecks_actual_values_on_internal_wires() -> None:
    world = BioWorld(communication_step=1.0)
    world.add_biomodule("producer", _BadSequenceProducer())
    world.add_biomodule("consumer", _SequenceConsumer())
    world.connect("producer.sequence", "consumer.sequence")
    with pytest.raises(ValueError, match="unsupported character"):
        world.run(2.0)


def test_result_objects_checker_edges_and_warning_logging(monkeypatch, caplog) -> None:
    with pytest.raises(ValueError, match="level"):
        CompatibilityIssue("ok", "message", "CODE")
    with pytest.raises(ValueError, match="message and code"):
        CompatibilityIssue("warning", "", "CODE")
    warning = CompatibilityIssue("warning", "Review this.", "REVIEW")
    result = CompatibilityResult((warning,))
    assert result.status == "warning"
    assert result.compatible is True
    assert result.to_dict()["issues"] == [warning.to_dict()]
    enforce_result(result, context="example")
    assert "Review this" in caplog.text

    monkeypatch.setitem(
        compatibility_module._CHECKERS,
        "finite_number",
        lambda value: CompatibilityResult((warning,)),
    )
    assert check_payload(AFFINITY, 1.0).status == "warning"
    monkeypatch.setitem(compatibility_module._CHECKERS, "finite_number", lambda value: ["bad"])
    assert check_payload(AFFINITY, 1.0).issues[-1].code == "CHECKER_FAILED"
    monkeypatch.delitem(compatibility_module._CHECKERS, "finite_number")
    assert compatibility_module._run_checker(
        compatibility_module.get_profile("boltz.log10-ic50-micromolar/v1"), 1.0
    )[0].code == "CHECKER_UNAVAILABLE"
    with pytest.raises(ValueError, match="invalid compatibility contract"):
        contract_digest({"profile": "missing/v1"})


def test_manifest_validation_and_representation_branch_edges() -> None:
    malformed = {
        "compatibility": {"standard": "bad", "version": "0", "extra": True},
        "io": {
            "inputs": [
                42,
                {"name": ""},
                {"name": "x"},
                {"name": "x", "accepted_profiles": "bad"},
                {"name": "y", "accepted_profiles": [42]},
                {"name": "z", "accepted_profiles": [{"contract": {"profile": "missing/v1"}}]},
            ],
            "outputs": "bad",
        },
    }
    findings = validate_manifest(malformed)
    assert len(findings) >= 8
    assert manifest_has_compatibility_declarations({"compatibility": STANDARD}) is True
    assert manifest_has_compatibility_declarations({"io": {"outputs": [{"contract": SEQUENCE}]}}) is True
    assert manifest_has_compatibility_declarations({"io": "bad"}) is False

    assert compatibility_module._shape_allowed([2, 3], [2, "*"]) is True
    assert compatibility_module._shape_allowed(None, [2]) is False
    assert compatibility_module._shape_allowed([2], [2, 3]) is False
    assert compatibility_module._field_matches("schema", {"x": "str"}, {"x": "str"}) is True
    assert compatibility_module._field_matches("accepted_units", ["1"], ("1",)) is True
    assert compatibility_module._port_representation(
        {"signal_type": "scalar", "dtype": "float64", "emitted_unit": "1"},
        direction="outputs",
    )["unit"] == "1"
    assert compatibility_module._port_representation(
        {"signal_type": "scalar", "dtype": "float64", "accepted_units": ["1"]},
        direction="inputs",
    )["unit"] == "1"


def test_additional_connection_context_and_checker_paths() -> None:
    source = SignalSpec.scalar(
        dtype="str",
        format="sequence",
        contract={"profile": "protein.sequence/v1", "identifier_namespace": "RefSeq"},
    )
    missing_target = SignalSpec.scalar(
        dtype="str",
        format="sequence",
        contract={"profile": "protein.sequence/v1", "identifier_namespace": "UniProtKB"},
    )
    no_namespace = SignalSpec.scalar(dtype="str", format="sequence", contract=SEQUENCE)
    assert check_compatibility(no_namespace, missing_target).issues[-1].code == "CONTEXT_MISSING"
    assert check_compatibility(source, missing_target).issues[-1].code == "CONTEXT_MISMATCH"

    event = SignalSpec.event(contract=SEQUENCE)
    assert check_compatibility(event, no_namespace).status == "blocked"
    invalid = SignalSpec.scalar(dtype="str", format="sequence", contract={"profile": "missing/v1"})
    assert check_compatibility(invalid, invalid).status == "blocked"

    accepted = SignalSpec.scalar(
        dtype="str",
        accepted_profiles=(
            AcceptedSignalProfile(
                signal_type="scalar",
                dtype="str",
                format="sequence",
                contract=SEQUENCE,
            ),
        ),
    )
    assert check_compatibility(no_namespace, accepted, sample="ACDE").status == "ok"


def test_binding_rejects_malformed_and_mismatched_models() -> None:
    class Empty(BioModule):
        def inputs(self):
            return {}

        def outputs(self):
            return {}

    with pytest.raises(ValueError, match="io.inputs"):
        bind_manifest_ports(Empty(), {"io": {"inputs": "bad", "outputs": []}})

    class NonMapping(BioModule):
        def inputs(self):
            return []

        def outputs(self):
            return {}

    with pytest.raises(ValueError, match="return a dict"):
        bind_manifest_ports(NonMapping(), {"io": {"inputs": [], "outputs": []}})

    with pytest.raises(ValueError, match="only in model.yaml"):
        bind_manifest_ports(Empty(), _manifest(contract=None))

    malformed_profiles = _manifest(direction="inputs", contract=None)
    malformed_profiles["io"]["inputs"][0]["accepted_profiles"] = []
    class InputModel(BioModule):
        def inputs(self):
            return {"sequence": SignalSpec.scalar(dtype="str", format="sequence")}

        def outputs(self):
            return {}

    inputs, _ = bind_manifest_ports(InputModel(), malformed_profiles)
    assert inputs["sequence"].format == "sequence"


def test_file_and_yaml_error_paths(tmp_path: Path) -> None:
    a3m_contract = {"profile": "protein.multiple-sequence-alignment/v1"}
    cif_contract = {"profile": "protein-ligand.complex-structure-mmcif/v1"}
    assert check_payload(a3m_contract, 42).status == "blocked"
    assert check_payload(a3m_contract, str(tmp_path / "missing.a3m")).status == "blocked"
    empty = tmp_path / "empty.a3m"
    empty.write_text("", encoding="utf-8")
    assert check_payload(a3m_contract, str(empty)).status == "blocked"
    header_only = tmp_path / "header.a3m"
    header_only.write_text(">query\n", encoding="utf-8")
    assert check_payload(a3m_contract, str(header_only)).status == "blocked"
    invalid_utf8 = tmp_path / "invalid.cif"
    invalid_utf8.write_bytes(b"\xff")
    assert check_payload(cif_contract, str(invalid_utf8)).status == "blocked"

    not_mapping = tmp_path / "list.yaml"
    not_mapping.write_text("- one\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top level"):
        load_yaml(not_mapping)
    invalid_encoding = tmp_path / "bad.yaml"
    invalid_encoding.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_yaml(invalid_encoding)
    too_large = tmp_path / "large.yaml"
    too_large.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="4 MiB"):
        load_yaml(too_large)


def test_checker_registry_fails_closed(monkeypatch) -> None:
    monkeypatch.delitem(compatibility_module._CHECKERS, "probability")
    with pytest.raises(RuntimeError, match="probability"):
        compatibility_module._validate_checker_registry()


def test_remaining_smiles_and_manifest_safety_edges(monkeypatch) -> None:
    assert check_payload(SMILES, 42).status == "blocked"
    assert check_payload(SMILES, "").status == "blocked"
    assert check_payload(SMILES, "C C").status == "blocked"
    assert check_payload(SMILES, "C(").status == "blocked"

    class FakeChem:
        @staticmethod
        def MolFromSmiles(value):
            return None if value == "not-smiles" else object()

    monkeypatch.setitem(__import__("sys").modules, "rdkit", type("RDKit", (), {"Chem": FakeChem}))
    assert check_payload(SMILES, "CCO").status == "ok"
    assert check_payload(SMILES, "not-smiles").status == "blocked"
    assert validate_contract({"profile": " "}).status == "blocked"
    assert validate_contract({"profile": "protein.sequence/v1", "species": ""}).status == "blocked"
    assert validate_manifest({"compatibility": "bad"})[0]["code"] == "STANDARD_MISMATCH"
