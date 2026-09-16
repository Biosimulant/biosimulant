from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from zipfile import ZipFile

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
    contract_digest,
    register_checker,
    registered_types,
    validate_port_spec_direction,
)
from biosim.__main__ import main
from biosim.compatibility import bind_manifest_ports, load_yaml, validate_manifest
from biosim.compatibility import (
    enforce_result,
    manifest_has_compatibility_declarations,
    validate_contract,
)
from biosim.pack import build_package


SMILES_CONTRACT = {"type": "chemical.smiles"}
SEQUENCE_CONTRACT = {
    "type": "protein.sequence",
    "species": "NCBITaxon:9606",
    "identifier_namespace": "UniProtKB",
}


def _port_manifest() -> dict:
    return {
        "schema_version": "2.0",
        "standard": "other",
        "title": "Compatibility example",
        "biosim": {"entrypoint": "src.model:Model"},
        "io": {
            "inputs": [],
            "outputs": [
                {
                    "name": "sequence",
                    "signal_type": "scalar",
                    "dtype": "str",
                    "format": "iupac-amino-acid",
                    "contract": copy.deepcopy(SEQUENCE_CONTRACT),
                }
            ],
        },
    }


def test_signal_spec_round_trip_keeps_simple_compatibility_facts() -> None:
    spec = SignalSpec.scalar(
        dtype="float64",
        accepted_units=("nM", "uM"),
        format="number",
        contract={"type": "measurement.concentration", "species": "any"},
    )
    assert SignalSpec.from_dict(spec.to_dict()).to_dict() == spec.to_dict()

    ambiguous = SignalSpec.scalar(
        accepted_units=("nM",),
        accepted_profiles=(AcceptedSignalProfile(signal_type="scalar"),),
    )
    with pytest.raises(ValueError, match="accepted_units or accepted_profiles"):
        validate_port_spec_direction(ambiguous, direction="input")


def test_static_checks_cover_semantics_species_namespace_format_and_units() -> None:
    source = SignalSpec.scalar(
        dtype="str",
        format="iupac-amino-acid",
        contract=SEQUENCE_CONTRACT,
    )
    target = SignalSpec.scalar(
        dtype="str",
        format="iupac-amino-acid",
        contract=SEQUENCE_CONTRACT,
    )
    assert check_compatibility(source, target).status == "ok"

    mouse = SignalSpec.scalar(
        dtype="str",
        format="iupac-amino-acid",
        contract={**SEQUENCE_CONTRACT, "species": "NCBITaxon:10090"},
    )
    result = check_compatibility(mouse, target)
    assert result.status == "blocked"
    assert result.issues[-1].code == "SPECIES_MISMATCH"

    wrong_format = SignalSpec.scalar(
        dtype="str",
        format="fasta",
        contract=SEQUENCE_CONTRACT,
    )
    assert check_compatibility(wrong_format, target).status == "blocked"

    concentration = {"type": "measurement.concentration"}
    nm = SignalSpec.scalar(dtype="float64", emitted_unit="nM", contract=concentration)
    kcal_target = SignalSpec.scalar(
        dtype="float64",
        accepted_units=("kcal/mol",),
        contract=concentration,
    )
    assert check_compatibility(nm, kcal_target).status == "blocked"


def test_missing_declaration_warns_instead_of_guessing() -> None:
    source = SignalSpec.scalar(dtype="str")
    target = SignalSpec.scalar(dtype="str", contract=SMILES_CONTRACT)
    result = check_compatibility(source, target)
    assert result.status == "warning"
    assert result.issues[0].code == "SOURCE_UNDECLARED"

    unimplemented = {"type": "example.unimplemented"}
    generic = SignalSpec.scalar(dtype="float64", contract=unimplemented)
    result = check_compatibility(generic, generic)
    assert result.status == "warning"
    assert result.issues[-1].code == "CHECKER_UNAVAILABLE"


def test_value_checks_reject_reaction_smiles_and_invalid_sequences() -> None:
    smiles = check_payload(SMILES_CONTRACT, "CCO>>CC=O")
    assert smiles.status == "blocked"
    assert any(issue.code == "REACTION_SMILES" for issue in smiles.issues)

    sequence = check_payload(SEQUENCE_CONTRACT, "ACD1")
    assert sequence.status == "blocked"
    assert sequence.issues[-1].code == "INVALID_SEQUENCE_CHARACTER"


def test_builtin_checkers_cover_supported_carriers_and_parser_results(monkeypatch) -> None:
    fake_chem = SimpleNamespace(
        MolFromSmiles=lambda value: None if value == "not-smiles" else object()
    )
    monkeypatch.setitem(sys.modules, "rdkit", SimpleNamespace(Chem=fake_chem))

    assert check_payload(SMILES_CONTRACT, "CCO").status == "ok"
    assert check_payload(SMILES_CONTRACT, {"smiles": "CCO"}).status == "ok"
    assert check_payload(SMILES_CONTRACT, ["CCO", "CCN"]).status == "ok"
    assert check_payload(SMILES_CONTRACT, "not-smiles").issues[-1].code == "UNPARSEABLE_SMILES"
    assert check_payload(SMILES_CONTRACT, "").issues[-1].code == "EMPTY_SMILES"
    assert check_payload(SMILES_CONTRACT, "C C").issues[-1].code == "SMILES_WHITESPACE"
    assert check_payload(SMILES_CONTRACT, "C(").issues[-1].code == "UNBALANCED_SMILES"
    assert check_payload(SMILES_CONTRACT, 42).issues[-1].code == "INVALID_VALUE_CARRIER"

    assert check_payload(SEQUENCE_CONTRACT, {"sequence": "ACDE"}).status == "ok"
    assert check_payload(SEQUENCE_CONTRACT, ["ACD", "EFG"]).status == "ok"
    assert check_payload(SEQUENCE_CONTRACT, "").issues[-1].code == "EMPTY_SEQUENCE"
    assert check_payload(SEQUENCE_CONTRACT, 42).issues[-1].code == "INVALID_VALUE_CARRIER"

    structure = {"type": "protein.structure"}
    assert check_payload(structure, "ATOM").status == "ok"
    assert check_payload(structure, None).status == "blocked"


def test_yaml_loader_rejects_invalid_encoding_syntax_and_large_files(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("io: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_yaml(broken)

    invalid_encoding = tmp_path / "invalid-encoding.yaml"
    invalid_encoding.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="invalid YAML"):
        load_yaml(invalid_encoding)

    large = tmp_path / "large.yaml"
    large.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="4 MiB"):
        load_yaml(large)


def test_model_can_register_a_namespaced_type_checker() -> None:
    def check_score(source, target, sample):
        if sample is not None and not 0 <= sample <= 1:
            return [
                CompatibilityIssue(
                    level="blocked",
                    code="SCORE_RANGE",
                    message="The score must be between zero and one.",
                )
            ]
        return []

    register_checker("example.test_score", check_score)
    assert "example.test_score" in registered_types()
    contract = {"type": "example.test_score"}
    spec = SignalSpec.scalar(dtype="float64", contract=contract)
    assert check_compatibility(spec, spec, sample=1.5).status == "blocked"


def test_checker_registration_and_result_validation_edges() -> None:
    for kwargs, message in (
        ({"level": "ok", "code": "X", "message": "x"}, "level"),
        ({"level": "warning", "code": "X", "message": ""}, "message"),
        ({"level": "warning", "code": "", "message": "x"}, "code"),
    ):
        with pytest.raises(ValueError, match=message):
            CompatibilityIssue(**kwargs)

    with pytest.raises(ValueError, match="dotted name"):
        register_checker("not_dotted", lambda source, target, sample: [])
    with pytest.raises(TypeError, match="callable"):
        register_checker("example.not_callable", object())

    warning = CompatibilityIssue(
        level="warning", code="EXAMPLE_WARNING", message="Needs a closer look."
    )
    register_checker(
        "example.result_object",
        lambda source, target, sample: CompatibilityResult((warning,)),
    )
    contract = {"type": "example.result_object"}
    assert check_payload(contract, 1).issues == (warning,)
    with pytest.raises(ValueError, match="already registered"):
        register_checker("example.result_object", lambda source, target, sample: [])
    register_checker(
        "example.result_object",
        lambda source, target, sample: [],
        replace=True,
    )
    assert check_payload(contract, 1).status == "ok"

    register_checker("example.bad_return", lambda source, target, sample: ["bad"])
    assert check_payload({"type": "example.bad_return"}, 1).issues[0].code == "CHECKER_FAILED"

    def broken(source, target, sample):
        raise RuntimeError("broken checker")

    register_checker("example.broken", broken)
    result = check_payload({"type": "example.broken"}, 1)
    assert result.status == "blocked"
    assert "broken checker" in result.issues[0].message


def test_contract_validation_and_all_generic_comparison_outcomes() -> None:
    assert validate_contract(None).status == "ok"
    assert validate_contract("bad").issues[0].code == "INVALID_CONTRACT"
    assert validate_contract({}).issues[0].code == "MISSING_TYPE"
    assert validate_contract({"type": "protein"}).issues[0].code == "INVALID_TYPE"
    invalid_context = validate_contract(
        {
            "type": "protein.sequence",
            "species": "",
            "identifier_namespace": 42,
        }
    )
    assert {issue.code for issue in invalid_context.issues} == {
        "INVALID_SPECIES",
        "INVALID_IDENTIFIER_NAMESPACE",
    }
    assert check_payload(None, "anything").status == "ok"

    protein = SignalSpec.scalar(dtype="str", contract={"type": "protein.sequence"})
    smiles = SignalSpec.scalar(dtype="str", contract={"type": "chemical.smiles"})
    assert check_compatibility(protein, smiles).issues[0].code == "TYPE_MISMATCH"
    assert check_compatibility(protein, SignalSpec.scalar(dtype="str")).issues[0].code == "TARGET_UNDECLARED"

    target = SignalSpec.scalar(
        dtype="str",
        contract={
            "type": "protein.sequence",
            "species": "NCBITaxon:9606",
            "identifier_namespace": "UniProtKB",
        },
    )
    source = SignalSpec.scalar(
        dtype="str",
        contract={"type": "protein.sequence", "species": "any"},
    )
    issues = check_compatibility(source, target).issues
    assert {issue.code for issue in issues} == {
        "SPECIES_UNDECLARED",
        "IDENTIFIER_NAMESPACE_UNDECLARED",
    }

    wrong_namespace = SignalSpec.scalar(
        dtype="str",
        contract={
            "type": "protein.sequence",
            "species": "NCBITaxon:9606",
            "identifier_namespace": "RefSeq",
        },
    )
    assert any(
        issue.code == "IDENTIFIER_NAMESPACE_MISMATCH"
        for issue in check_compatibility(wrong_namespace, target).issues
    )

    event = SignalSpec.event(contract={"type": "protein.sequence"})
    assert check_compatibility(event, target).issues[0].code == "SIGNAL_KIND_MISMATCH"

    blocked = CompatibilityResult(
        (CompatibilityIssue("blocked", "Stop.", "STOP"),)
    )
    with pytest.raises(ValueError, match="context: Stop"):
        enforce_result(blocked, context="context")


def test_manifest_validation_rejects_old_block_and_unknown_contract_fields() -> None:
    old = _port_manifest()
    old["compatibility"] = {"standard": "old"}
    findings = validate_manifest(old)
    assert findings[0]["code"] == "LEGACY_COMPATIBILITY_BLOCK"

    invalid = _port_manifest()
    invalid["io"]["outputs"][0]["contract"]["profile_refs"] = []
    findings = validate_manifest(invalid)
    assert any(item["code"] == "UNKNOWN_CONTRACT_FIELD" for item in findings)


def test_manifest_validation_reports_each_malformed_shape() -> None:
    assert validate_manifest({}) == []
    assert validate_manifest({"io": "bad"})[0]["code"] == "INVALID_IO"
    assert validate_manifest({"io": {"inputs": "bad"}})[0]["code"] == "INVALID_PORTS"

    manifest = {
        "io": {
            "inputs": [
                42,
                {"name": ""},
                {"name": "x"},
                {"name": "x", "accepted_profiles": "bad"},
                {"name": "y", "accepted_profiles": [42]},
                {
                    "name": "z",
                    "accepted_profiles": [
                        {"contract": {"type": "not-dotted"}}
                    ],
                },
                {"name": "none", "accepted_profiles": None},
            ],
            "outputs": [],
        }
    }
    codes = {item["code"] for item in validate_manifest(manifest)}
    assert {
        "INVALID_PORT",
        "INVALID_PORT_NAME",
        "DUPLICATE_PORT_NAME",
        "INVALID_ACCEPTED_PROFILES",
        "INVALID_ACCEPTED_PROFILE",
        "INVALID_TYPE",
    } <= codes

    assert manifest_has_compatibility_declarations({}) is False
    assert manifest_has_compatibility_declarations({"io": "bad"}) is False
    assert (
        manifest_has_compatibility_declarations(
            {"io": {"inputs": "bad", "outputs": [42]}}
        )
        is False
    )
    assert (
        manifest_has_compatibility_declarations(
            {"io": {"inputs": [{"name": "x", "accepted_units": ["nM"]}]}}
        )
        is True
    )


def test_manifest_binding_reports_port_and_profile_errors() -> None:
    class EmptyModel(BioModule):
        def inputs(self):
            return {}

        def outputs(self):
            return {}

    for manifest, message in (
        ({"io": {"inputs": "bad", "outputs": []}}, "must be a list"),
        ({"io": {"inputs": [42], "outputs": []}}, "must be a mapping"),
        ({"io": {"inputs": [{"name": ""}], "outputs": []}}, "non-empty"),
        (
            {"io": {"inputs": [{"name": "x"}, {"name": "x"}], "outputs": []}},
            "more than once",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            bind_manifest_ports(EmptyModel(), manifest)

    class NonMappingModel(BioModule):
        def inputs(self):
            return []

        def outputs(self):
            return {}

    with pytest.raises(ValueError, match="return a dict"):
        bind_manifest_ports(NonMappingModel(), {"io": {"inputs": [], "outputs": []}})

    class ProfileModel(BioModule):
        def inputs(self):
            return {
                "dose": SignalSpec.scalar(
                    dtype="float64",
                    accepted_profiles=(
                        AcceptedSignalProfile(
                            signal_type="scalar",
                            dtype="float32",
                            accepted_units=("nM",),
                            format="number",
                            contract={"type": "measurement.concentration"},
                        ),
                    ),
                )
            }

        def outputs(self):
            return {}

    base_profile = {
        "signal_type": "scalar",
        "dtype": "float32",
        "accepted_units": ["nM"],
        "format": "number",
        "contract": {"type": "measurement.concentration"},
    }
    manifest = {
        "io": {
            "inputs": [
                {
                    "name": "dose",
                    "signal_type": "scalar",
                    "dtype": "float64",
                    "accepted_profiles": [base_profile],
                }
            ],
            "outputs": [],
        }
    }
    inputs, _ = bind_manifest_ports(ProfileModel(), manifest)
    assert inputs["dose"].accepted_profiles[0].format == "number"

    conflict = copy.deepcopy(manifest)
    conflict["io"]["inputs"][0]["accepted_profiles"][0]["contract"] = {
        "type": "measurement.affinity"
    }
    with pytest.raises(ValueError, match="contract differs"):
        bind_manifest_ports(ProfileModel(), conflict)

    missing_profiles = copy.deepcopy(manifest)
    missing_profiles["io"]["inputs"][0]["accepted_profiles"] = [base_profile, base_profile]
    with pytest.raises(ValueError, match="lists 2 accepted profile"):
        bind_manifest_ports(ProfileModel(), missing_profiles)

    malformed_profile = copy.deepcopy(manifest)
    malformed_profile["io"]["inputs"][0]["accepted_profiles"] = [42]
    with pytest.raises(ValueError, match="must be a mapping"):
        bind_manifest_ports(ProfileModel(), malformed_profile)

    class NoProfileModel(BioModule):
        def inputs(self):
            return {"dose": SignalSpec.scalar(dtype="float64")}

        def outputs(self):
            return {}

    with pytest.raises(ValueError, match="lists 1 accepted profile"):
        bind_manifest_ports(NoProfileModel(), copy.deepcopy(manifest))

    nonlist_profiles = copy.deepcopy(manifest)
    nonlist_profiles["io"]["inputs"][0]["accepted_profiles"] = "bad"
    with pytest.raises(ValueError, match="must be a list"):
        bind_manifest_ports(ProfileModel(), nonlist_profiles)

    wrong_format = copy.deepcopy(manifest)
    wrong_format["io"]["inputs"][0]["accepted_profiles"][0]["format"] = "text"
    with pytest.raises(ValueError, match=r"\.format is"):
        bind_manifest_ports(ProfileModel(), wrong_format)

    class OutputProfileModel(BioModule):
        def inputs(self):
            return {}

        def outputs(self):
            return {
                "x": SignalSpec.scalar(
                    accepted_profiles=(AcceptedSignalProfile(signal_type="scalar"),)
                )
            }

    with pytest.raises(ValueError, match="only valid on inputs"):
        bind_manifest_ports(
            OutputProfileModel(),
            {
                "io": {
                    "inputs": [],
                    "outputs": [
                        {
                            "name": "x",
                            "signal_type": "scalar",
                            "accepted_profiles": [{"signal_type": "scalar"}],
                        }
                    ],
                }
            },
        )


class _ContractBoundModel(BioModule):
    execution_policy = "each_window"

    def inputs(self):
        return {
            "dose": SignalSpec.scalar(
                dtype="float64",
                accepted_units=("nM",),
            )
        }

    def outputs(self):
        return {
            "sequence": SignalSpec.scalar(
                dtype="str",
                format="iupac-amino-acid",
            )
        }

    def execute(self, inputs, *, context: ExecutionContext):
        return {"sequence": "ACDE"}


def test_manifest_contracts_bind_to_runtime_specs() -> None:
    manifest = _port_manifest()
    manifest["io"]["inputs"] = [
        {
            "name": "dose",
            "signal_type": "scalar",
            "dtype": "float64",
            "accepted_units": ["nM"],
            "contract": {"type": "measurement.concentration"},
        }
    ]
    model = _ContractBoundModel()
    inputs, outputs = bind_manifest_ports(model, manifest)
    assert inputs["dose"].contract == {"type": "measurement.concentration"}
    assert outputs["sequence"].contract == SEQUENCE_CONTRACT

    world = BioWorld(communication_step=1.0)
    world.add_biomodule("model", model)
    assert world._modules["model"].output_specs["sequence"].contract == SEQUENCE_CONTRACT


def test_manifest_and_python_contracts_cannot_disagree() -> None:
    class ConflictingModel(_ContractBoundModel):
        def outputs(self):
            return {
                "sequence": SignalSpec.scalar(
                    dtype="str",
                    format="iupac-amino-acid",
                    contract={**SEQUENCE_CONTRACT, "species": "NCBITaxon:10090"},
                )
            }

    manifest = _port_manifest()
    manifest["io"]["inputs"] = [
        {
            "name": "dose",
            "signal_type": "scalar",
            "dtype": "float64",
            "accepted_units": ["nM"],
        }
    ]
    with pytest.raises(ValueError, match="contract differs"):
        bind_manifest_ports(ConflictingModel(), manifest)


def test_package_keeps_port_contract_without_generating_a_compatibility_lock(
    tmp_path: Path,
) -> None:
    source = tmp_path / "model"
    (source / "src").mkdir(parents=True)
    manifest = _port_manifest()
    (source / "model.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    (source / "src" / "model.py").write_text(
        "class Model:\n    pass\n", encoding="utf-8"
    )

    package = build_package(source)
    with ZipFile(package) as archive:
        names = set(archive.namelist())
        packaged = yaml.safe_load(archive.read("payload/model.yaml"))
    assert "payload/compatibility.lock.json" not in names
    assert packaged["io"]["outputs"][0]["contract"] == SEQUENCE_CONTRACT


def test_cli_validates_compares_and_lists_current_types(tmp_path: Path, capsys) -> None:
    producer = tmp_path / "producer.yaml"
    producer.write_text(yaml.safe_dump(_port_manifest()), encoding="utf-8")

    consumer_manifest = _port_manifest()
    consumer_manifest["io"] = {
        "inputs": [
            {
                "name": "sequence",
                "signal_type": "scalar",
                "dtype": "str",
                "format": "iupac-amino-acid",
                "contract": SEQUENCE_CONTRACT,
            }
        ],
        "outputs": [],
    }
    consumer = tmp_path / "consumer.yaml"
    consumer.write_text(yaml.safe_dump(consumer_manifest), encoding="utf-8")

    main(["compatibility", "validate", str(producer)])
    assert json.loads(capsys.readouterr().out)["valid"] is True

    main(
        [
            "compatibility",
            "compare",
            f"{producer}#outputs.sequence",
            f"{consumer}#inputs.sequence",
            "--sample-json",
            '"ACDE"',
        ]
    )
    assert json.loads(capsys.readouterr().out)["status"] == "ok"

    main(["compatibility", "types"])
    listed = json.loads(capsys.readouterr().out)
    assert "chemical.smiles" in listed["types"]


def test_cli_and_yaml_errors_are_explicit(tmp_path: Path, capsys) -> None:
    invalid_yaml = tmp_path / "invalid.yaml"
    invalid_yaml.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top level must be a YAML mapping"):
        load_yaml(invalid_yaml)

    with pytest.raises(SystemExit) as raised:
        main(["compatibility", "compare", str(invalid_yaml), str(invalid_yaml)])
    assert raised.value.code == 2
    assert "Point to a port" in capsys.readouterr().err


def test_signal_envelope_uses_the_local_contract_digest() -> None:
    envelope = SignalEnvelope(
        contract_digest=contract_digest(SEQUENCE_CONTRACT),
        value="ACDE",
        actual_context={"species": "NCBITaxon:9606"},
        provenance={"run": "source-run"},
    )
    envelope.validate_contract(SEQUENCE_CONTRACT)
    assert SignalEnvelope.from_dict(envelope.to_dict()).provenance == {
        "run": "source-run"
    }

    invalid = SignalEnvelope(contract_digest="sha256:" + "0" * 64, value="ACDE")
    with pytest.raises(ValueError, match="different contract"):
        invalid.validate_contract(SEQUENCE_CONTRACT)


class _BadSequenceProducer(BioModule):
    execution_policy = "each_window"

    def outputs(self):
        return {
            "sequence": SignalSpec.scalar(dtype="str", contract=SEQUENCE_CONTRACT)
        }

    def execute(self, inputs, *, context: ExecutionContext):
        return {"sequence": "ACD1"}


class _SequenceConsumer(BioModule):
    execution_policy = "each_window"

    def inputs(self):
        return {
            "sequence": SignalSpec.scalar(dtype="str", contract=SEQUENCE_CONTRACT)
        }

    def execute(self, inputs, *, context: ExecutionContext):
        return {}


def test_world_rechecks_the_actual_value_when_it_crosses_a_wire() -> None:
    world = BioWorld(communication_step=1.0)
    world.add_biomodule("producer", _BadSequenceProducer())
    world.add_biomodule("consumer", _SequenceConsumer())
    world.connect("producer.sequence", "consumer.sequence")
    with pytest.raises(ValueError, match="unsupported character"):
        world.run(2.0)
