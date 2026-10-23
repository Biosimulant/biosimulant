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








def test_structural_mismatch_blocks_before_profile_comparison() -> None:
    source = SignalSpec.scalar(dtype="str", format="sequence", contract=SEQUENCE)
    wrong_format = SignalSpec.scalar(dtype="str", format="fasta", contract=SEQUENCE)
    wrong_dtype = SignalSpec.scalar(dtype="float64", contract=PROBABILITY)
    numeric_target = SignalSpec.scalar(dtype="float32", contract=PROBABILITY)
    assert check_compatibility(source, wrong_format).issues[0].code == "PROFILE_REPRESENTATION_MISMATCH"
    assert check_compatibility(wrong_dtype, numeric_target).issues[0].code == "PROFILE_REPRESENTATION_MISMATCH"








def test_manifest_without_profiles_remains_valid() -> None:
    manifest = _manifest(contract=None)
    manifest.pop("compatibility")
    assert validate_manifest(manifest) == []
    assert manifest_has_compatibility_declarations(manifest) is False




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


@pytest.mark.parametrize("catalogue_version", [None, "0.1.0"])
def test_provenance_contains_only_profiles_used_by_manifest(monkeypatch, catalogue_version) -> None:
    catalogue_sha256 = None if catalogue_version is None else "sha256:" + "a" * 64
    monkeypatch.setattr(compatibility_module, "CATALOGUE_VERSION", catalogue_version)
    monkeypatch.setattr(compatibility_module, "CATALOGUE_SHA256", catalogue_sha256)
    manifest = _manifest(contract=SEQUENCE)
    provenance = compatibility_provenance(manifest)
    assert provenance["standard"] == "biosimulant.model-compatibility"
    assert provenance["version"] == "0"
    assert provenance["catalogue_version"] == catalogue_version
    assert provenance["catalogue_sha256"] == catalogue_sha256
    assert [item["ref"] for item in provenance["profiles"]] == ["protein.sequence/v1"]
    assert compatibility_provenance(_manifest(contract=None)) is None




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


class _ProfiledSequenceProducer(BioModule):
    execution_policy = "each_window"

    def outputs(self):
        return {
            "sequence": SignalSpec.scalar(
                dtype="str",
                format="sequence",
                contract=SEQUENCE,
            )
        }

    def execute(self, inputs, *, context: ExecutionContext):
        return {"sequence": "ACDE"}


class _PlainBadSequenceProducer(BioModule):
    execution_policy = "each_window"

    def outputs(self):
        return {"sequence": SignalSpec.scalar(dtype="str", format="sequence")}

    def execute(self, inputs, *, context: ExecutionContext):
        return {"sequence": "ACD1"}


class _SequenceConsumer(BioModule):
    execution_policy = "each_window"

    def inputs(self):
        return {"sequence": SignalSpec.scalar(dtype="str", format="sequence", contract=SEQUENCE)}

    def execute(self, inputs, *, context: ExecutionContext):
        return {}


class _PlainSequenceConsumer(BioModule):
    execution_policy = "each_window"

    def inputs(self):
        return {"sequence": SignalSpec.scalar(dtype="str", format="sequence")}

    def execute(self, inputs, *, context: ExecutionContext):
        return {}












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







@pytest.mark.parametrize("contract", [{}, {"profile":"future/v9"}, {"profile":"protein.sequence/v1","species":"anything","extra":"kept"}])
def test_scientific_declarations_are_descriptive(contract):
    assert validate_contract(contract).status == "ok"
    manifest=_manifest(contract=contract)
    manifest.pop("compatibility")
    assert validate_manifest(manifest)==[]
    assert check_payload(contract,"ACD1").status=="ok"

@pytest.mark.parametrize("value", [0, -1, 1.5, float("inf"), "invalid", None])
def test_no_payload_grading(value):
    assert check_payload(PROBABILITY,value).status=="ok"


def test_species_and_profile_mismatches_do_not_block_structurally_matching_ports():
    a=SignalSpec.scalar(dtype="str",contract={"profile":"new/v1","species":"human"})
    b=SignalSpec.scalar(dtype="str",contract={"profile":"another/v2","species":"mouse"})
    assert check_compatibility(a,b,sample="ANY").status=="ok"
    assert check_compatibility(a,SignalSpec.scalar(dtype="float64")).status=="blocked"


def test_duplicate_or_malformed_ports_still_fail():
    m=_manifest();m['io']['outputs']*=2
    assert validate_manifest(m)
    m['io']='not a mapping'
    assert validate_manifest(m)
