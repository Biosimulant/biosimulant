"""The `biosimulant compatibility` commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from .compatibility import (
    CompatibilitySupportUnavailable,
    _standard,
    build_lock,
    load_yaml,
    normalize_manifest,
    validate_manifest,
)

# Port direction in a selector -> (role in `compare`, port kind).
_PORT_ROLES = {"outputs": ("source", "output"), "inputs": ("target", "input")}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Check whether model ports can connect, using the Biosimulant Model "
            "Compatibility Standard."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="Check the compatibility block in a model.yaml")
    validate.add_argument("manifest", type=Path, help="Path to model.yaml")

    normalize = commands.add_parser(
        "normalize", help="Print a model.yaml's compatibility data in normalized JSON form"
    )
    normalize.add_argument("manifest", type=Path, help="Path to model.yaml")
    normalize.add_argument(
        "--output", type=Path, help="Write the JSON to this file instead of printing it"
    )

    compare = commands.add_parser(
        "compare", help="Check whether an output port can feed an input port"
    )
    compare.add_argument(
        "producer", metavar="SOURCE", help="Output port, e.g. model.yaml#outputs.concentration"
    )
    compare.add_argument(
        "consumer", metavar="TARGET", help="Input port, e.g. model.yaml#inputs.dose"
    )
    compare.add_argument(
        "--ontology-snapshots",
        type=Path,
        help="JSON file with exact ontology snapshots used by comparison rules",
    )
    compare.add_argument(
        "--mapping-snapshots",
        type=Path,
        help="JSON file with exact identifier-mapping snapshots used by comparison rules",
    )

    profiles = commands.add_parser("profiles", help="List or show compatibility profiles")
    profile_commands = profiles.add_subparsers(dest="profiles_command", required=True)
    list_profiles = profile_commands.add_parser("list", help="List the available profiles")
    list_profiles.add_argument("--domain", help="Only list profiles in this domain, e.g. core")
    show_profile = profile_commands.add_parser("show", help="Print one profile's full definition")
    show_profile.add_argument("profile_ref", help="Profile ref (a URL), as shown by `profiles list`")

    lock = commands.add_parser("lock", help="Write compatibility.lock.json for a model")
    lock.add_argument("manifest", type=Path, help="Path to model.yaml")
    lock.add_argument(
        "--output",
        type=Path,
        default=Path("compatibility.lock.json"),
        help="Where to write the lock file (default: compatibility.lock.json)",
    )

    plan = commands.add_parser(
        "plan", help="Check every wiring connection in a lab and print a plan"
    )
    plan.add_argument("lab", type=Path, help="Path to lab.yaml, or the folder that contains it")
    plan.add_argument(
        "--policy",
        type=Path,
        help="JSON file with the compatibility policy that decides which connections are allowed",
    )
    plan.add_argument(
        "--capabilities",
        type=Path,
        help="JSON file listing adapter or inference capabilities that can convert data between ports",
    )
    plan.add_argument(
        "--ontology-snapshots",
        type=Path,
        help="JSON file with exact ontology snapshots used by rules and preconditions",
    )
    plan.add_argument(
        "--mapping-snapshots",
        type=Path,
        help="JSON file with exact identifier-mapping snapshots used by rules and preconditions",
    )
    plan.add_argument(
        "--output", type=Path, help="Write the plan to this file instead of printing it"
    )

    commands.add_parser("conformance", help="Run the standard's conformance tests")
    return parser


def _select_port(selector: str, expected_direction: str) -> tuple[dict[str, Any] | None, list[str]]:
    role, kind = _PORT_ROLES[expected_direction]
    if "#" not in selector:
        raise ValueError(
            "Point to a port like model.yaml#outputs.concentration or "
            f"model.yaml#inputs.dose (got {selector!r})"
        )
    path_text, fragment = selector.rsplit("#", 1)
    direction, separator, name = fragment.partition(".")
    if not separator or direction != expected_direction or not name:
        raise ValueError(
            f"The {role} must be an {kind} port: PATH#{expected_direction}.PORT (got {selector!r})"
        )
    manifest = load_yaml(path_text)
    for port in manifest.get("io", {}).get(direction, []):
        if port.get("name") == name:
            contract = port.get("contract")
            return contract, list(contract.get("profile_refs", [])) if isinstance(contract, dict) else []
    raise ValueError(f"No {kind} port named {name!r} in {path_text}")


def _read_json(path: Path) -> Any:
    data = path.read_bytes()
    if len(data) > 4 * 1024 * 1024:
        raise ValueError(f"{path} is larger than the 4 MiB safety limit")
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc


def _read_object_list(path: Path | None, option: str) -> list[dict[str, Any]]:
    value = _read_json(path) if path else []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"The {option} file must contain a JSON array of objects")
    return value


def _local_lab_plan(
    path: Path,
    policy_path: Path | None,
    capabilities_path: Path | None,
    ontology_snapshots_path: Path | None,
    mapping_snapshots_path: Path | None,
) -> dict[str, Any]:
    standard = _standard()
    lab_path = path / "lab.yaml" if path.is_dir() else path
    lab = load_yaml(lab_path)
    root = lab_path.parent
    models: dict[str, dict[str, Any]] = {}
    for entry in lab.get("models", []):
        alias, model_path = entry.get("alias"), entry.get("path")
        if isinstance(alias, str) and isinstance(model_path, str):
            candidate = (root / model_path).resolve()
            manifest_path = candidate / "model.yaml" if candidate.is_dir() else candidate
            models[alias] = load_yaml(manifest_path)

    def port(ref: str, direction: str) -> tuple[dict[str, Any] | None, list[str]]:
        alias, separator, name = ref.rpartition(".")
        if not separator or alias not in models:
            return None, []
        for item in models[alias].get("io", {}).get(direction, []):
            if item.get("name") == name:
                contract = item.get("contract")
                return contract, list(contract.get("profile_refs", [])) if isinstance(contract, dict) else []
        return None, []

    policy = _read_json(policy_path) if policy_path else {}
    capabilities = _read_object_list(capabilities_path, "--capabilities")
    ontology_snapshots = _read_object_list(
        ontology_snapshots_path, "--ontology-snapshots"
    )
    mapping_snapshots = _read_object_list(
        mapping_snapshots_path, "--mapping-snapshots"
    )
    if not isinstance(policy, dict):
        raise ValueError("The --policy file must contain a JSON object")

    reports = []
    materialized_nodes: list[dict[str, Any]] = []
    materialized_edges: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = [
        {"kind": "lab", "sha256": standard.digest(lab)},
        {"kind": "standard-bundle", "sha256": standard.get_bundle().digest},
    ]
    decisions: list[str] = []
    technical_statuses: list[str] = []
    for edge_index, edge in enumerate(lab.get("wiring", [])):
        source_ref = edge.get("from")
        if not isinstance(source_ref, str):
            continue
        source, source_refs = port(source_ref, "outputs")
        raw_targets = edge.get("to", [])
        targets = [raw_targets] if isinstance(raw_targets, str) else raw_targets
        for target_ref in targets:
            if not isinstance(target_ref, str):
                continue
            target, target_refs = port(target_ref, "inputs")
            # resolve_contracts takes no profile refs, so a port that does not satisfy the profile
            # it declares would otherwise produce an ALLOWed edge. Check each side against its own
            # declared profiles first, and block the edge when it does not hold up.
            contract_findings = [
                {
                    "port": ref,
                    "reason_code": finding.reason_code,
                    "message": finding.message,
                    "path": finding.path,
                }
                for contract, refs, ref in (
                    (source, source_refs, source_ref),
                    (target, target_refs, target_ref),
                )
                if isinstance(contract, dict) and refs
                for finding in standard.validate_contract(dict(contract), list(refs))
            ]
            if contract_findings:
                technical_statuses.append("INCOMPATIBLE")
                reports.append(
                    {
                        "edge_index": edge_index,
                        "source": source_ref,
                        "target": target_ref,
                        "report": {
                            "schema_version": "0.1",
                            "standard": "https://biosimulant.com/standards/model-compatibility/v0.1",
                            "bundle_sha256": standard.get_bundle().digest,
                            "status": "INCOMPATIBLE",
                            "policy_decision": "BLOCK",
                            "findings": [
                                {
                                    "dimension": "contract",
                                    "state": "INCOMPATIBLE",
                                    "severity": "error",
                                    "reason_code": item["reason_code"],
                                    "explanation": f"{item['port']}: {item['message']}",
                                }
                                for item in contract_findings
                            ],
                            "source": {
                                "contract_digest": None,
                                "profile_refs": sorted(set(source_refs)),
                            },
                            "target": {
                                "contract_digest": None,
                                "profile_refs": sorted(set(target_refs)),
                            },
                        },
                        "resolution": "UNRESOLVED",
                    }
                )
                decisions.append("BLOCK")
                continue
            resolution = standard.resolve_contracts(
                source,
                target,
                capabilities,
                policy=policy,
                ontology_snapshots=ontology_snapshots,
                mapping_snapshots=mapping_snapshots,
            )
            report = resolution["report"]
            reports.append(
                {
                    "edge_index": edge_index,
                    "source": source_ref,
                    "target": target_ref,
                    "report": report,
                    "resolution": resolution["resolution"],
                }
            )
            if resolution["resolution"] == "RESOLVED":
                edge_plan = resolution["plan"]
                technical_statuses.append(edge_plan["technical_status"])
                prefix = f"edge-{edge_index}-{len(reports)}"
                for node in edge_plan["nodes"]:
                    materialized_nodes.append({**node, "id": f"{prefix}-{node['id']}"})
                for plan_edge in edge_plan["edges"]:
                    materialized_edges.append(
                        {
                            **plan_edge,
                            "from": f"{prefix}-{plan_edge['from']}",
                            "to": f"{prefix}-{plan_edge['to']}",
                            "source_port": source_ref,
                            "target_port": target_ref,
                        }
                    )
                references.extend(edge_plan.get("immutable_references", []))
                decisions.append(edge_plan["policy"]["decision"])
            else:
                technical_statuses.append(report["status"])
                decisions.append("BLOCK")
    decision = (
        "BLOCK"
        if "BLOCK" in decisions
        else "APPROVAL_REQUIRED"
        if "APPROVAL_REQUIRED" in decisions
        else "ALLOW"
    )
    status_rank = {
        "EXACT": 0,
        "DIRECT_COMPATIBLE": 1,
        "LOSSLESS_CONVERSION_AVAILABLE": 2,
        "CONDITIONAL": 3,
        "LOSSY_CONVERSION_REQUIRES_APPROVAL": 4,
        "INFERENCE_MODEL_REQUIRED": 5,
        "UNKNOWN": 6,
        "INCOMPATIBLE": 7,
    }
    technical_status = max(
        technical_statuses or ["UNKNOWN"], key=status_rank.__getitem__
    )
    plan_without_digest = {
        "schema_version": "0.1",
        "standard": "https://biosimulant.com/standards/model-compatibility/v0.1",
        "bundle_sha256": standard.get_bundle().digest,
        "technical_status": technical_status,
        "nodes": materialized_nodes,
        "edges": materialized_edges,
        "reports": reports,
        "policy": {
            "decision": decision,
            "digest": standard.digest(policy),
            "rules": policy,
        },
        "approvals": [],
        "immutable_references": references,
    }
    result = {**plan_without_digest, "digest": standard.digest(plan_without_digest)}
    findings = standard.validate_object(result, "resolution-plan.schema.json")
    if findings:
        raise ValueError(
            "Internal error: the generated plan failed schema validation: "
            + "; ".join(item.message for item in findings)
        )
    return result


def _conformance() -> dict[str, Any]:
    standard = _standard()
    bundle = standard.get_bundle()
    bundle.verify_integrity()
    profiles = bundle.catalogue["profiles"]
    passed = 0
    for summary in profiles:
        fixture = bundle.read_json(f"fixtures/profiles/{summary['domain']}/{summary['name']}.json")
        profile_ref = fixture["profile_ref"]
        for case in fixture["cases"]:
            if "contract" in case:
                findings = standard.validate_contract(case["contract"], [profile_ref])
                valid = not findings
                if valid != case["valid"]:
                    raise ValueError(
                        f"Conformance failed for {profile_ref}/{case['name']}: "
                        f"expected valid={case['valid']}, got valid={valid}"
                    )
                reason_code = case.get("reason_code")
                if reason_code and not any(
                    item.reason_code == reason_code for item in findings
                ):
                    raise ValueError(
                        f"Conformance failed for {profile_ref}/{case['name']}: "
                        f"expected {reason_code}"
                    )
            else:
                report = standard.compare_contracts(
                    case["source"],
                    case["target"],
                    target_profile_refs=[profile_ref],
                )
                if report["status"] != case["status"]:
                    raise ValueError(
                        f"Conformance failed for {profile_ref}/{case['name']}: "
                        f"expected {case['status']}, got {report['status']}"
                    )
                reason_code = case.get("reason_code")
                if reason_code and not any(
                    item["reason_code"] == reason_code
                    for item in report.get("findings", [])
                ):
                    raise ValueError(
                        f"Conformance failed for {profile_ref}/{case['name']}: "
                        f"expected {reason_code}"
                    )
            passed += 1
    return {
        "valid": True,
        "release": bundle.manifest["release"],
        "profiles": len(profiles),
        "profile_fixtures_passed": passed,
        "bundle_sha256": bundle.digest,
        "catalogue_status": bundle.catalogue["status"],
    }


def _error_message(exc: Exception) -> str:
    if isinstance(exc, OSError) and exc.filename is not None and exc.strerror:
        return f"{exc.filename}: {exc.strerror}"
    if isinstance(exc, yaml.YAMLError):
        return f"invalid YAML: {exc}"
    return str(exc)


def main(argv: list[str], *, prog: str = "biosimulant compatibility") -> None:
    args = _parser(prog).parse_args(argv)
    try:
        _run(args, prog=prog)
    except (ValueError, OSError, yaml.YAMLError, CompatibilitySupportUnavailable) as exc:
        print(f"error: {_error_message(exc)}", file=sys.stderr)
        raise SystemExit(2) from exc


def _run(args: argparse.Namespace, *, prog: str) -> None:
    if args.command == "validate":
        manifest = load_yaml(args.manifest)
        findings = validate_manifest(manifest)
        print(_json({"valid": not findings, "opted_in": "compatibility" in manifest, "findings": findings}))
        if findings:
            raise SystemExit(1)
        return
    if args.command == "normalize":
        result = normalize_manifest(load_yaml(args.manifest))
        content = _json(result) + "\n"
        if args.output:
            args.output.write_text(content, encoding="utf-8")
        else:
            print(content, end="")
        return
    if args.command == "compare":
        source, source_refs = _select_port(args.producer, "outputs")
        target, target_refs = _select_port(args.consumer, "inputs")
        ontology_snapshots = _read_object_list(
            args.ontology_snapshots, "--ontology-snapshots"
        )
        mapping_snapshots = _read_object_list(
            args.mapping_snapshots, "--mapping-snapshots"
        )
        print(
            _json(
                _standard().compare_contracts(
                    source,
                    target,
                    source_profile_refs=source_refs,
                    target_profile_refs=target_refs,
                    ontology_snapshots=ontology_snapshots,
                    mapping_snapshots=mapping_snapshots,
                )
            )
        )
        return
    if args.command == "profiles":
        bundle = _standard().get_bundle()
        if args.profiles_command == "show":
            try:
                profile = bundle.profile(args.profile_ref)
            except KeyError:
                raise ValueError(
                    f"Unknown profile {args.profile_ref!r}. "
                    f"Run `{prog} profiles list` to see available profiles."
                ) from None
            print(_json(profile))
        else:
            profiles = bundle.catalogue["profiles"]
            if args.domain:
                profiles = [profile for profile in profiles if profile["domain"] == args.domain]
            print(_json({"count": len(profiles), "profiles": profiles}))
        return
    if args.command == "lock":
        result = build_lock(load_yaml(args.manifest))
        if result is None:
            raise ValueError(
                f"{args.manifest} has no `compatibility` block, so there's nothing to lock"
            )
        args.output.write_text(_json(result) + "\n", encoding="utf-8")
        print(_json({"output": str(args.output), "digest": result["digest"]}))
        return
    if args.command == "plan":
        result = _local_lab_plan(
            args.lab,
            args.policy,
            args.capabilities,
            args.ontology_snapshots,
            args.mapping_snapshots,
        )
        content = _json(result) + "\n"
        if args.output:
            args.output.write_text(content, encoding="utf-8")
        else:
            print(content, end="")
        return
    if args.command == "conformance":
        print(_json(_conformance()))
