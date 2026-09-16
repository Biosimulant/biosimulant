from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import yaml

import pytest

from biosim import (
    AcceptedSignalProfile,
    BioModule,
    BioWorld,
    ExecutionContext,
    SignalEnvelope,
    SignalSpec,
)
from biosim.__main__ import main
from biosim.compatibility import (
    bind_manifest_ports,
    build_lock,
    compare_contracts,
    load_yaml,
    lock_bytes,
    normalize_manifest,
    resolve_contracts,
    validate_manifest,
)
from biosim.pack import build_package
from biosimulant_model_compatibility_standard import digest, get_bundle


def _manifest() -> dict:
    bundle = get_bundle()
    ref = "https://biosimulant.com/standards/model-compatibility/profiles/proteome/protein-sequence/v0.1"
    return {
        "schema_version": "2.0",
        "standard": "other",
        "title": "Compatibility example",
        "biosim": {"entrypoint": "src.model:Model"},
        "compatibility": {
            "standard": "https://biosimulant.com/standards/model-compatibility/v0.1",
            "profiles": [{"ref": ref, "sha256": bundle.profile_index[ref]["sha256"]}],
        },
        "io": {
            "inputs": [],
            "outputs": [{
                "name": "quantity",
                "signal_type": "scalar",
                "dtype": "float64",
                "contract": {
                    "profile_refs": [ref],
                    "semantic": {
                        "concept": "https://biosimulant.com/standards/model-compatibility/terms/proteome/protein-sequence"
                    },
                    "representation": {
                        "kind": "scalar",
                        "alphabet": "IUPAC-amino-acid",
                        "encoding": "single-letter",
                    },
                    "identifiers": {
                        "namespace": "UniProtKB",
                        "namespace_version": "2026_03",
                    },
                    "biological_context": {"species": "NCBITaxon:9606"},
                },
            }],
        },
    }


def test_signal_spec_and_accepted_profile_preserve_contract_round_trip():
    refinement = {"representation": {"kind": "dense_vector"}}
    profile = AcceptedSignalProfile(signal_type="array", dtype="float64", shape=(2,), contract=refinement)
    spec = SignalSpec.array(
        dtype="float32",
        shape=(2,),
        contract={"semantic": {"concept": "gene_expression"}},
        accepted_profiles=[profile],
    )
    assert SignalSpec.from_dict(spec.to_dict()).to_dict() == spec.to_dict()
    assert spec.to_dict()["accepted_profiles"][0]["contract"] == refinement


def test_package_build_adds_lock_without_rewriting_source(tmp_path: Path):
    source = tmp_path / "model"
    (source / "src").mkdir(parents=True)
    manifest = _manifest()
    original = yaml.safe_dump(manifest, sort_keys=False)
    (source / "model.yaml").write_text(original, encoding="utf-8")
    (source / "src" / "model.py").write_text("class Model:\n    pass\n", encoding="utf-8")

    package = build_package(source)
    assert (source / "model.yaml").read_text(encoding="utf-8") == original
    with ZipFile(package) as archive:
        lock = json.loads(archive.read("payload/compatibility.lock.json"))
    assert lock["bundle_sha256"] == get_bundle().digest
    assert lock["contracts"][0]["port"] == "quantity"


def test_cli_validate_and_conformance(tmp_path: Path, capsys):
    manifest = tmp_path / "model.yaml"
    manifest.write_text(yaml.safe_dump(_manifest(), sort_keys=False), encoding="utf-8")
    main(["compatibility", "validate", str(manifest)])
    assert json.loads(capsys.readouterr().out)["valid"] is True

    main(["compatibility", "conformance"])
    result = json.loads(capsys.readouterr().out)
    profile_count = len(get_bundle().catalogue["profiles"])
    fixture_count = sum(
        len(
            get_bundle().read_json(
                f"fixtures/profiles/{profile['domain']}/{profile['name']}.json"
            )["cases"]
        )
        for profile in get_bundle().catalogue["profiles"]
    )
    assert profile_count > 0
    assert result["profiles"] == profile_count
    assert result["profile_fixtures_passed"] == fixture_count
    assert result["release"] == "0.0.1"
    assert result["ga_ready"] is False
    assert result["ga_blockers"]


def test_compatibility_wrappers_keep_legacy_optional_and_execute_opt_in():
    legacy = {"schema_version": "2.0", "io": {"inputs": [], "outputs": []}}
    assert validate_manifest(legacy) == []
    assert normalize_manifest(legacy) == legacy
    assert build_lock(legacy) is None
    assert lock_bytes(legacy) is None

    manifest = _manifest()
    assert validate_manifest(manifest) == []
    normalized = normalize_manifest(manifest)
    assert normalized["compatibility"]["standard"].endswith("/v0.1")
    lock = build_lock(manifest)
    assert lock is not None
    assert json.loads(lock_bytes(manifest))["digest"] == lock["digest"]

    contract = manifest["io"]["outputs"][0]["contract"]
    assert compare_contracts(contract, contract)["status"] == "EXACT"
    assert resolve_contracts(contract, contract)["resolution"] == "RESOLVED"
    assert resolve_contracts(None, contract)["reason"] == "UNKNOWN_CONTRACT"


def test_cli_inspection_compare_normalize_and_lock(tmp_path: Path, capsys):
    manifest_path = tmp_path / "model.yaml"
    manifest_path.write_text(
        yaml.safe_dump(_manifest(), sort_keys=False), encoding="utf-8"
    )

    normalized_path = tmp_path / "normalized.json"
    main(
        [
            "compatibility",
            "normalize",
            str(manifest_path),
            "--output",
            str(normalized_path),
        ]
    )
    assert json.loads(normalized_path.read_text())["compatibility"]

    consumer = _manifest()
    consumer["io"]["inputs"] = [
        {
            "name": "quantity",
            "signal_type": "scalar",
            "dtype": "float64",
            "contract": consumer["io"]["outputs"][0]["contract"],
        }
    ]
    consumer_path = tmp_path / "consumer.yaml"
    consumer_path.write_text(
        yaml.safe_dump(consumer, sort_keys=False), encoding="utf-8"
    )
    snapshot_unsigned = {
        "ref": "https://biosimulant.com/snapshots/example-ontology/v1",
        "equivalences": [],
    }
    snapshot = {**snapshot_unsigned, "sha256": digest(snapshot_unsigned)}
    snapshots_path = tmp_path / "ontology-snapshots.json"
    snapshots_path.write_text(json.dumps([snapshot]), encoding="utf-8")
    main(
        [
            "compatibility",
            "compare",
            f"{manifest_path}#outputs.quantity",
            f"{consumer_path}#inputs.quantity",
            "--ontology-snapshots",
            str(snapshots_path),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "EXACT"
    assert report["snapshots"]["ontology"] == [
        {"ref": snapshot["ref"], "sha256": snapshot["sha256"]}
    ]

    main(["compatibility", "profiles", "list", "--domain", "proteome"])
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] > 0
    profile_ref = listed["profiles"][0]["ref"]
    main(["compatibility", "profiles", "show", profile_ref])
    assert json.loads(capsys.readouterr().out)["$id"] == profile_ref

    lock_path = tmp_path / "inspectable.lock.json"
    main(
        [
            "compatibility",
            "lock",
            str(manifest_path),
            "--output",
            str(lock_path),
        ]
    )
    lock_result = json.loads(capsys.readouterr().out)
    assert lock_result["output"] == str(lock_path)
    assert json.loads(lock_path.read_text())["digest"] == lock_result["digest"]


def _cli_error(capsys, argv: list[str]) -> str:
    """Run a compatibility command that should fail and return its stderr."""
    with pytest.raises(SystemExit) as raised:
        main(["compatibility", *argv])
    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    return captured.err


def test_cli_and_yaml_errors_are_explicit(tmp_path: Path, capsys):
    invalid_yaml = tmp_path / "invalid.yaml"
    invalid_yaml.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="top level must be a YAML mapping"):
        load_yaml(invalid_yaml)
    assert "top level must be a YAML mapping" in _cli_error(
        capsys, ["validate", str(invalid_yaml)]
    )

    legacy = tmp_path / "legacy.yaml"
    legacy.write_text("schema_version: '2.0'\n", encoding="utf-8")
    assert "has no `compatibility` block" in _cli_error(capsys, ["lock", str(legacy)])

    assert "Point to a port like model.yaml#outputs." in _cli_error(
        capsys, ["compare", str(legacy), str(legacy)]
    )

    assert "No output port named 'x'" in _cli_error(
        capsys, ["compare", f"{legacy}#outputs.x", f"{legacy}#outputs.x"]
    )

    producer = tmp_path / "producer.yaml"
    producer.write_text(yaml.safe_dump(_manifest()), encoding="utf-8")
    assert "The target must be an input port" in _cli_error(
        capsys, ["compare", f"{producer}#outputs.quantity", f"{legacy}#outputs.x"]
    )

    missing = tmp_path / "missing.yaml"
    assert str(missing) in _cli_error(capsys, ["validate", str(missing)])

    broken_yaml = tmp_path / "broken.yaml"
    broken_yaml.write_text("io: [unclosed\n", encoding="utf-8")
    assert "invalid YAML" in _cli_error(capsys, ["validate", str(broken_yaml)])

    assert "Unknown profile 'not-a-profile'" in _cli_error(
        capsys, ["profiles", "show", "not-a-profile"]
    )


def test_manifest_port_shape_and_profile_contradictions_are_explicit():
    manifest = _manifest()
    manifest["io"]["inputs"] = [
        {
            "name": "dose",
            "signal_type": "scalar",
            "dtype": "float64",
            "accepted_profiles": [{"dtype": "float32"}],
        }
    ]
    with pytest.raises(ValueError, match="lists 1 accepted profile"):
        bind_manifest_ports(_ContractBoundModel(), manifest)

    malformed = {"io": {"inputs": "not-a-list", "outputs": []}}
    with pytest.raises(ValueError, match="must be a list"):
        bind_manifest_ports(_ContractBoundModel(), malformed)

    duplicate = {
        "io": {
            "inputs": [
                {"name": "dose", "signal_type": "scalar", "dtype": "float64"},
                {"name": "dose", "signal_type": "scalar", "dtype": "float64"},
            ],
            "outputs": [{"name": "quantity"}],
        }
    }
    with pytest.raises(ValueError, match="lists port 'dose' more than once"):
        bind_manifest_ports(_ContractBoundModel(), duplicate)


def test_manifest_profile_refinement_is_bound_and_bad_ports_fail():
    class AcceptedModel(BioModule):
        def inputs(self):
            return {
                "dose": SignalSpec.scalar(
                    dtype="float64",
                    accepted_profiles=[
                        AcceptedSignalProfile(
                            signal_type="scalar",
                            dtype="float32",
                            accepted_units=("nM", "uM"),
                        )
                    ],
                )
            }

        def outputs(self):
            return {}

    refinement = {"measurement": {"unit": "nM"}}
    manifest = {
        "io": {
            "inputs": [
                {
                    "name": "dose",
                    "signal_type": "scalar",
                    "dtype": "float64",
                    "accepted_profiles": [
                        {
                            "signal_type": "scalar",
                            "dtype": "float32",
                            "accepted_units": ["nM", "uM"],
                            "contract": refinement,
                        }
                    ],
                }
            ],
            "outputs": [],
        }
    }
    inputs, _ = bind_manifest_ports(AcceptedModel(), manifest)
    assert inputs["dose"].accepted_profiles[0].contract == refinement

    for bad_port, message in ((42, "must be a mapping"), ({"name": ""}, "non-empty")):
        with pytest.raises(ValueError, match=message):
            bind_manifest_ports(
                AcceptedModel(), {"io": {"inputs": [bad_port], "outputs": []}}
            )

    class NonMappingModel(BioModule):
        def inputs(self):
            return []

        def outputs(self):
            return {}

    with pytest.raises(ValueError, match="must each return a dict"):
        bind_manifest_ports(NonMappingModel(), manifest)


class _ContractBoundModel(BioModule):
    execution_policy = "each_window"

    def inputs(self):
        return {"dose": SignalSpec.scalar(dtype="float64")}

    def outputs(self):
        return {"quantity": SignalSpec.scalar(dtype="float64")}

    def execute(self, inputs, *, context: ExecutionContext):
        return {"quantity": 1.0}


def test_opted_in_manifest_contract_is_bound_to_runtime_specs():
    manifest = _manifest()
    manifest["io"]["inputs"] = [{
        "name": "dose",
        "signal_type": "scalar",
        "dtype": "float64",
        "contract": {"semantic": {"concept": "dose"}},
    }]
    model = _ContractBoundModel()
    inputs, outputs = bind_manifest_ports(model, manifest)
    assert inputs["dose"].contract == {"semantic": {"concept": "dose"}}
    assert outputs["quantity"].contract == manifest["io"]["outputs"][0]["contract"]

    world = BioWorld(communication_step=1.0)
    world.add_biomodule("model", model)
    assert world._modules["model"].input_specs["dose"].contract == inputs["dose"].contract


def test_opted_in_manifest_python_contradiction_is_rejected():
    manifest = _manifest()
    manifest["io"]["inputs"] = [{
        "name": "dose",
        "signal_type": "array",
        "dtype": "float64",
        "shape": [2],
    }]
    with pytest.raises(
        ValueError, match="is 'array' in model.yaml but 'scalar' in the Python module"
    ):
        bind_manifest_ports(_ContractBoundModel(), manifest)


def test_legacy_module_is_not_implicitly_bound():
    model = _ContractBoundModel()
    world = BioWorld(communication_step=1.0)
    world.add_biomodule("model", model)
    assert world._modules["model"].input_specs["dose"].contract is None


def test_cli_plan_materializes_a_schema_valid_direct_edge(tmp_path: Path, capsys):
    contract = {"semantic": {"concept": "concentration", "subject": "compound"}}
    producer = tmp_path / "producer"
    consumer = tmp_path / "consumer"
    producer.mkdir()
    consumer.mkdir()
    (producer / "model.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "io": {"inputs": [], "outputs": [{"name": "x", "contract": contract}]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (consumer / "model.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "io": {"inputs": [{"name": "x", "contract": contract}], "outputs": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    lab = tmp_path / "lab.yaml"
    lab.write_text(
        yaml.safe_dump(
            {
                "models": [
                    {"alias": "producer", "path": "producer"},
                    {"alias": "consumer", "path": "consumer"},
                ],
                "wiring": [{"from": "producer.x", "to": "consumer.x"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    snapshot_unsigned = {
        "ref": "https://biosimulant.com/snapshots/example-ontology/v1",
        "equivalences": [],
    }
    snapshot = {**snapshot_unsigned, "sha256": digest(snapshot_unsigned)}
    snapshots_path = tmp_path / "ontology-snapshots.json"
    snapshots_path.write_text(json.dumps([snapshot]), encoding="utf-8")
    main(
        [
            "compatibility",
            "plan",
            str(lab),
            "--ontology-snapshots",
            str(snapshots_path),
        ]
    )
    plan = json.loads(capsys.readouterr().out)
    assert plan["policy"]["decision"] == "ALLOW"
    assert plan["reports"][0]["report"]["status"] == "EXACT"
    assert len(plan["edges"]) == 1
    assert {
        "kind": "ontology_snapshot",
        "ref": snapshot["ref"],
        "sha256": snapshot["sha256"],
    } in plan["immutable_references"]


def test_signal_envelope_is_digest_bound_and_survives_signal_serialization():
    contract = {"semantic": {"concept": "dose"}}
    standard = get_bundle()
    from biosimulant_model_compatibility_standard import digest

    envelope = SignalEnvelope(
        contract_digest=digest(contract),
        value=2.5,
        provenance={"run": "source-run"},
    )
    envelope.validate_contract(contract)
    spec = SignalSpec.scalar(dtype="float64", contract=contract)
    from biosim.runtime import coerce_typed_inputs

    signal = coerce_typed_inputs(
        {"dose": envelope.to_dict()},
        {"dose": spec},
        source="test",
    )["dose"]
    restored = type(signal).from_dict(signal.to_dict())
    assert restored.compatibility_envelope.provenance == {"run": "source-run"}
    assert standard.digest.startswith("sha256:")

    invalid = SignalEnvelope(contract_digest="sha256:" + "0" * 64, value=2.5)
    with pytest.raises(ValueError, match="made for a different contract"):
        invalid.validate_contract(contract)


def test_signal_envelope_uses_normalized_digest_and_rejects_context_conflicts():
    from biosimulant_model_compatibility_standard import digest, normalize_contract

    contract = {
        "semantic": {"concept": "expression", "qualifiers": ["z", "a"]},
        "biological_context": {"species": "NCBITaxon:9606"},
    }
    envelope = SignalEnvelope(
        contract_digest=digest(normalize_contract(contract)),
        value=[1.0],
        actual_context={"species": "NCBITaxon:9606", "tissue": "UBERON:0002107"},
    )
    envelope.validate_contract(contract)

    conflicting = SignalEnvelope(
        contract_digest=digest(normalize_contract(contract)),
        value=[1.0],
        actual_context={"species": "NCBITaxon:10090"},
    )
    with pytest.raises(ValueError, match="actual_context.species"):
        conflicting.validate_contract(contract)


def test_canonical_output_signal_envelope_is_validated_at_world_boundary():
    from biosimulant_model_compatibility_standard import digest

    contract = {"semantic": {"concept": "concentration"}}

    class EnvelopeProducer(BioModule):
        execution_policy = "each_window"

        def outputs(self):
            return {"quantity": SignalSpec.scalar(dtype="float64", contract=contract)}

        def execute(self, inputs, *, context: ExecutionContext):
            return {
                "quantity": SignalEnvelope(
                    contract_digest=digest(contract),
                    value=1.0,
                    provenance={"model": "producer"},
                ).to_dict()
            }

    world = BioWorld(communication_step=1.0)
    world.add_biomodule("producer", EnvelopeProducer())
    world.run(1.0)
    signal = world.get_outputs("producer")["quantity"]
    assert signal.compatibility_envelope.provenance == {"model": "producer"}
