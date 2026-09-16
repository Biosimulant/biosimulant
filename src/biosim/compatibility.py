"""Versioned scientific interface checks for Biosimulant model ports."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, TypeAlias

from biosimulant_model_compatibility_standard import (
    CATALOGUE_SHA256,
    CATALOGUE_VERSION,
    STANDARD_ID,
    STANDARD_VERSION,
    get_profile,
    list_profiles,
    profile_digest,
)

from .modules import BioModule
from .signals import SignalSpec

logger = logging.getLogger(__name__)

CompatibilityStatus: TypeAlias = Literal["ok", "warning", "blocked"]
IssueLevel: TypeAlias = Literal["warning", "blocked"]
NO_SAMPLE = object()
_CONTRACT_FIELDS = {"profile", "species", "identifier_namespace"}
_STANDARD_FIELDS = {"standard", "version"}


@dataclass(frozen=True)
class CompatibilityIssue:
    level: IssueLevel
    message: str
    code: str

    def __post_init__(self) -> None:
        if self.level not in {"warning", "blocked"}:
            raise ValueError("CompatibilityIssue.level must be 'warning' or 'blocked'")
        if not self.message.strip() or not self.code.strip():
            raise ValueError("CompatibilityIssue message and code must not be empty")

    def to_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class CompatibilityResult:
    issues: tuple[CompatibilityIssue, ...] = ()

    @property
    def status(self) -> CompatibilityStatus:
        if any(issue.level == "blocked" for issue in self.issues):
            return "blocked"
        return "warning" if self.issues else "ok"

    @property
    def compatible(self) -> bool:
        return self.status != "blocked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "compatible": self.compatible,
            "issues": [issue.to_dict() for issue in self.issues],
        }


Checker: TypeAlias = Callable[[Any], CompatibilityResult | Iterable[CompatibilityIssue]]


def _issue(level: IssueLevel, code: str, message: str) -> CompatibilityIssue:
    return CompatibilityIssue(level=level, code=code, message=message)


def _profile_ref(profile: Mapping[str, Any]) -> str:
    identity = profile["profile"]
    return f"{identity['id']}/v{identity['version']}"


def _profile_or_none(ref: Any) -> Mapping[str, Any] | None:
    if not isinstance(ref, str) or not ref.strip():
        return None
    try:
        return get_profile(ref)
    except KeyError:
        return None


def validate_contract(contract: Mapping[str, Any] | None) -> CompatibilityResult:
    if contract is None:
        return CompatibilityResult()
    if not isinstance(contract, Mapping):
        return CompatibilityResult(
            (_issue("blocked", "INVALID_CONTRACT", "contract must be a mapping"),)
        )

    issues: list[CompatibilityIssue] = []
    unknown = sorted(set(contract) - _CONTRACT_FIELDS)
    if unknown:
        issues.append(
            _issue(
                "blocked",
                "UNKNOWN_CONTRACT_FIELD",
                "contract has unsupported field(s): " + ", ".join(unknown),
            )
        )
    ref = contract.get("profile")
    if not isinstance(ref, str) or not ref.strip():
        issues.append(
            _issue("blocked", "PROFILE_UNKNOWN", "contract.profile must name an exact profile")
        )
        return CompatibilityResult(tuple(issues))
    profile = _profile_or_none(ref)
    if profile is None:
        issues.append(
            _issue("blocked", "PROFILE_UNKNOWN", f"Compatibility profile '{ref}' is not installed.")
        )
        return CompatibilityResult(tuple(issues))

    permitted_context = set(profile["context_fields"])
    for field in ("species", "identifier_namespace"):
        value = contract.get(field)
        if field in contract and field not in permitted_context:
            issues.append(
                _issue(
                    "blocked",
                    "PROFILE_REPRESENTATION_MISMATCH",
                    f"Profile '{ref}' does not permit contract.{field}.",
                )
            )
        if value is not None and (not isinstance(value, str) or not value.strip()):
            issues.append(
                _issue(
                    "blocked",
                    "CONTEXT_MISMATCH",
                    f"contract.{field} must be a non-empty string when supplied",
                )
            )
    checker = profile["checker"]
    if checker not in _CHECKERS:
        issues.append(
            _issue(
                "blocked",
                "CHECKER_UNAVAILABLE",
                f"Checker '{checker}' for profile '{ref}' is unavailable.",
            )
        )
    return CompatibilityResult(tuple(issues))


def contract_digest(contract: Mapping[str, Any]) -> str:
    validation = validate_contract(contract)
    if validation.status == "blocked":
        raise ValueError("invalid compatibility contract: " + "; ".join(i.message for i in validation.issues))
    ref = str(contract["profile"])
    payload = {
        "standard": STANDARD_ID,
        "version": STANDARD_VERSION,
        "profile": ref,
        "profile_sha256": profile_digest(ref),
        "context": {
            key: contract[key]
            for key in ("species", "identifier_namespace")
            if key in contract
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _port_representation(port: Mapping[str, Any], *, direction: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in ("signal_type", "dtype", "format", "value_type", "shape"):
        if port.get(field) is not None:
            result[field] = port[field]
    if direction == "outputs":
        if port.get("emitted_unit") is not None:
            result["unit"] = port["emitted_unit"]
    else:
        units = port.get("accepted_units")
        if isinstance(units, (list, tuple)) and len(units) == 1:
            result["unit"] = units[0]
        elif port.get("emitted_unit") is not None:
            result["unit"] = port["emitted_unit"]
    return result


def _shape_allowed(actual: Any, expected: Any) -> bool:
    if expected is None:
        return actual is None
    if actual is None or not isinstance(actual, (list, tuple)) or len(actual) != len(expected):
        return False
    return all(wanted == "*" or wanted == got for got, wanted in zip(actual, expected))


def _representation_allowed(port: Mapping[str, Any], profile: Mapping[str, Any], *, direction: str) -> bool:
    actual = _port_representation(port, direction=direction)
    for representation in profile["representations"]:
        if actual.get("signal_type") != representation.get("signal_type"):
            continue
        if actual.get("dtype") not in representation.get("dtypes", []):
            continue
        allowed_formats = representation.get("formats")
        if allowed_formats is None:
            if actual.get("format") is not None:
                continue
        elif actual.get("format") not in allowed_formats:
            continue
        if actual.get("value_type") != representation.get("value_type"):
            continue
        if actual.get("unit") != representation.get("unit"):
            continue
        if not _shape_allowed(actual.get("shape"), representation.get("shape")):
            continue
        return True
    return False


def _compare_context(source: Mapping[str, Any], target: Mapping[str, Any]) -> list[CompatibilityIssue]:
    issues: list[CompatibilityIssue] = []
    target_species = target.get("species")
    source_species = source.get("species")
    if target_species not in {None, "any"}:
        if source_species in {None, "any"}:
            issues.append(
                _issue(
                    "blocked",
                    "CONTEXT_MISSING",
                    f"The target requires species '{target_species}', but the source does not declare it.",
                )
            )
        elif source_species != target_species:
            issues.append(
                _issue(
                    "blocked",
                    "CONTEXT_MISMATCH",
                    f"The source species is '{source_species}', but the target requires '{target_species}'.",
                )
            )

    target_namespace = target.get("identifier_namespace")
    source_namespace = source.get("identifier_namespace")
    if target_namespace is not None:
        if source_namespace is None:
            issues.append(
                _issue(
                    "blocked",
                    "CONTEXT_MISSING",
                    f"The target requires identifier namespace '{target_namespace}', but the source does not declare it.",
                )
            )
        elif source_namespace != target_namespace:
            issues.append(
                _issue(
                    "blocked",
                    "CONTEXT_MISMATCH",
                    f"The source identifier namespace is '{source_namespace}', but the target requires '{target_namespace}'.",
                )
            )
    return issues


def _run_checker(profile: Mapping[str, Any], sample: Any) -> tuple[CompatibilityIssue, ...]:
    checker_id = str(profile["checker"])
    checker = _CHECKERS.get(checker_id)
    if checker is None:
        return (
            _issue(
                "blocked",
                "CHECKER_UNAVAILABLE",
                f"Checker '{checker_id}' for profile '{_profile_ref(profile)}' is unavailable.",
            ),
        )
    if sample is NO_SAMPLE:
        return ()
    try:
        checked = checker(sample)
        if isinstance(checked, CompatibilityResult):
            return checked.issues
        issues = tuple(checked)
        if not all(isinstance(issue, CompatibilityIssue) for issue in issues):
            raise TypeError("checker returned an item that is not CompatibilityIssue")
        return issues
    except Exception as exc:
        return (
            _issue(
                "blocked",
                "CHECKER_FAILED",
                f"Checker '{checker_id}' failed: {exc}",
            ),
        )


def check_compatibility(source: SignalSpec, target: SignalSpec, *, sample: Any = NO_SAMPLE) -> CompatibilityResult:
    issues: list[CompatibilityIssue] = []
    if source.kind != target.kind:
        return CompatibilityResult(
            (
                _issue(
                    "blocked",
                    "PROFILE_REPRESENTATION_MISMATCH",
                    f"incompatible signal kinds: source '{source.kind}' cannot feed target '{target.kind}'.",
                ),
            )
        )
    accepted = target.match_input_profile(source)
    if accepted is None:
        return CompatibilityResult(
            (
                _issue(
                    "blocked",
                    "PROFILE_REPRESENTATION_MISMATCH",
                    "incompatible input profiles: the source signal type, dtype, shape, format or unit is not accepted by the target.",
                ),
            )
        )
    if target.interpolation == "linear" and not source.is_numeric:
        issues.append(
            _issue("blocked", "PROFILE_REPRESENTATION_MISMATCH", "Linear interpolation requires a numeric source signal.")
        )

    source_contract = source.contract
    target_contract = accepted.contract if accepted.contract is not None else target.contract
    if source_contract is None and target_contract is None:
        return CompatibilityResult(tuple(issues))
    if source_contract is None or target_contract is None:
        issues.append(
            _issue(
                "blocked",
                "STANDARD_REQUIRED",
                "Both connected ports must declare the same compatibility profile, or neither may declare one.",
            )
        )
        return CompatibilityResult(tuple(issues))

    source_validation = validate_contract(source_contract)
    target_validation = validate_contract(target_contract)
    issues.extend(source_validation.issues)
    issues.extend(target_validation.issues)
    if source_validation.status == "blocked" or target_validation.status == "blocked":
        return CompatibilityResult(tuple(issues))
    source_ref = str(source_contract["profile"])
    target_ref = str(target_contract["profile"])
    if source_ref != target_ref:
        issues.append(
            _issue(
                "blocked",
                "PROFILE_MISMATCH",
                f"The source uses '{source_ref}', but the target expects '{target_ref}'.",
            )
        )
        return CompatibilityResult(tuple(issues))
    issues.extend(_compare_context(source_contract, target_contract))
    if any(issue.level == "blocked" for issue in issues):
        return CompatibilityResult(tuple(issues))
    issues.extend(_run_checker(get_profile(source_ref), sample))
    return CompatibilityResult(tuple(issues))


def check_payload(contract: Mapping[str, Any] | None, value: Any) -> CompatibilityResult:
    validation = validate_contract(contract)
    if contract is None or validation.status == "blocked":
        return validation
    return CompatibilityResult(
        validation.issues + _run_checker(get_profile(str(contract["profile"])), value)
    )


def enforce_result(result: CompatibilityResult, *, context: str) -> None:
    blocked = [issue.message for issue in result.issues if issue.level == "blocked"]
    if blocked:
        raise ValueError(f"{context}: " + "; ".join(blocked))
    for issue in result.issues:
        logger.warning("%s: %s", context, issue.message)


def _finding(path: str, issue: CompatibilityIssue) -> dict[str, Any]:
    return {"path": path, **issue.to_dict()}


def validate_manifest(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    compatibility = manifest.get("compatibility")
    if compatibility is not None:
        if not isinstance(compatibility, Mapping):
            findings.append(
                _finding("/compatibility", _issue("blocked", "STANDARD_MISMATCH", "compatibility must be a mapping"))
            )
        else:
            unknown = sorted(set(compatibility) - _STANDARD_FIELDS)
            if unknown:
                findings.append(
                    _finding(
                        "/compatibility",
                        _issue("blocked", "STANDARD_MISMATCH", "compatibility has unsupported field(s): " + ", ".join(unknown)),
                    )
                )
            if compatibility.get("standard") != STANDARD_ID or str(compatibility.get("version")) != STANDARD_VERSION:
                findings.append(
                    _finding(
                        "/compatibility",
                        _issue(
                            "blocked",
                            "STANDARD_MISMATCH",
                            f"compatibility must declare standard '{STANDARD_ID}' version '{STANDARD_VERSION}'.",
                        ),
                    )
                )

    io = manifest.get("io")
    if io is None:
        return findings
    if not isinstance(io, Mapping):
        findings.append(_finding("/io", _issue("blocked", "PROFILE_REPRESENTATION_MISMATCH", "io must be a mapping")))
        return findings

    has_contract = False
    for direction in ("inputs", "outputs"):
        ports = io.get(direction, [])
        if not isinstance(ports, list):
            findings.append(
                _finding(f"/io/{direction}", _issue("blocked", "PROFILE_REPRESENTATION_MISMATCH", f"io.{direction} must be a list"))
            )
            continue
        names: set[str] = set()
        for index, port in enumerate(ports):
            path = f"/io/{direction}/{index}"
            if not isinstance(port, Mapping):
                findings.append(_finding(path, _issue("blocked", "PROFILE_REPRESENTATION_MISMATCH", "port must be a mapping")))
                continue
            name = port.get("name")
            if not isinstance(name, str) or not name:
                findings.append(_finding(path + "/name", _issue("blocked", "PROFILE_REPRESENTATION_MISMATCH", "port name must be a non-empty string")))
            elif name in names:
                findings.append(_finding(path + "/name", _issue("blocked", "PROFILE_REPRESENTATION_MISMATCH", f"port '{name}' is listed more than once")))
            else:
                names.add(name)

            contract = port.get("contract")
            if contract is not None:
                has_contract = True
                validation = validate_contract(contract)
                findings.extend(_finding(path + "/contract", issue) for issue in validation.issues)
                if validation.status != "blocked":
                    profile = get_profile(str(contract["profile"]))
                    if not _representation_allowed(port, profile, direction=direction):
                        findings.append(
                            _finding(
                                path,
                                _issue(
                                    "blocked",
                                    "PROFILE_REPRESENTATION_MISMATCH",
                                    f"Port representation is not allowed by '{contract['profile']}'.",
                                ),
                            )
                        )
            accepted_profiles = port.get("accepted_profiles") or []
            if not isinstance(accepted_profiles, list):
                findings.append(_finding(path + "/accepted_profiles", _issue("blocked", "PROFILE_REPRESENTATION_MISMATCH", "accepted_profiles must be a list")))
                continue
            for profile_index, accepted in enumerate(accepted_profiles):
                accepted_path = f"{path}/accepted_profiles/{profile_index}"
                if not isinstance(accepted, Mapping):
                    findings.append(_finding(accepted_path, _issue("blocked", "PROFILE_REPRESENTATION_MISMATCH", "accepted profile must be a mapping")))
                    continue
                accepted_contract = accepted.get("contract")
                if accepted_contract is not None:
                    has_contract = True
                    findings.extend(
                        _finding(accepted_path + "/contract", issue)
                        for issue in validate_contract(accepted_contract).issues
                    )
    if has_contract and compatibility is None:
        findings.insert(
            0,
            _finding(
                "/compatibility",
                _issue("blocked", "STANDARD_REQUIRED", "A model with profiled ports must declare the compatibility standard."),
            ),
        )
    return findings


def manifest_has_compatibility_declarations(manifest: Mapping[str, Any]) -> bool:
    if "compatibility" in manifest:
        return True
    io = manifest.get("io")
    if not isinstance(io, Mapping):
        return False
    for direction in ("inputs", "outputs"):
        ports = io.get(direction)
        if isinstance(ports, list) and any(isinstance(port, Mapping) and "contract" in port for port in ports):
            return True
    return False


_PORT_FIELDS = (
    "signal_type",
    "kind",
    "dtype",
    "shape",
    "schema",
    "emitted_unit",
    "accepted_units",
    "value_type",
    "format",
    "interpolation",
    "max_age",
    "stale_policy",
    "required",
)
_PROFILE_FIELDS = (
    "signal_type",
    "dtype",
    "shape",
    "schema",
    "accepted_units",
    "value_type",
    "format",
)


def _shape_matches(declared: Any, implemented: Any) -> bool:
    if declared is None or implemented is None:
        return declared is implemented
    if not isinstance(declared, (list, tuple)) or not isinstance(implemented, (list, tuple)):
        return declared == implemented
    if len(declared) != len(implemented):
        return False
    return all(expected in {"*", None} or expected == actual for expected, actual in zip(declared, implemented))


def _field_matches(field: str, declared: Any, implemented: Any) -> bool:
    if field == "shape":
        return _shape_matches(declared, implemented)
    if field == "schema" and isinstance(declared, Mapping) and isinstance(implemented, Mapping):
        return dict(declared) == dict(implemented)
    if field == "accepted_units" and isinstance(declared, (list, tuple)) and isinstance(implemented, (list, tuple)):
        return tuple(declared) == tuple(implemented)
    return declared == implemented


def _ports_by_name(manifest: Mapping[str, Any], direction: str) -> dict[str, dict[str, Any]]:
    io = manifest.get("io")
    raw = io.get(direction) if isinstance(io, Mapping) else None
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError(f"io.{direction} must be a list")
    ports: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(raw):
        if not isinstance(value, Mapping):
            raise ValueError(f"io.{direction}[{index}] must be a mapping")
        name = value.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"io.{direction}[{index}].name must be a non-empty string")
        if name in ports:
            raise ValueError(f"io.{direction} lists port '{name}' more than once")
        ports[name] = dict(value)
    return ports


def _check_fields(*, label: str, declared: Mapping[str, Any], implemented: Mapping[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        if field in declared and not _field_matches(field, declared[field], implemented.get(field)):
            raise ValueError(f"{label}.{field} is {declared[field]!r} in model.yaml but {implemented.get(field)!r} in the Python module")


def _bind_port(*, direction: str, name: str, declared: Mapping[str, Any], implemented: SignalSpec) -> SignalSpec:
    implemented_data = implemented.to_dict()
    label = f"io.{direction}.{name}"
    _check_fields(label=label, declared=declared, implemented=implemented_data, fields=_PORT_FIELDS)
    declared_profiles = declared.get("accepted_profiles")
    if declared_profiles is not None:
        if direction != "inputs":
            raise ValueError(f"{label}.accepted_profiles is only valid on inputs")
        if not isinstance(declared_profiles, list):
            raise ValueError(f"{label}.accepted_profiles must be a list")
        implemented_profiles = implemented_data.get("accepted_profiles") or []
        if len(declared_profiles) != len(implemented_profiles):
            raise ValueError(f"model.yaml lists {len(declared_profiles)} accepted profile(s) for {label}; the Python module lists {len(implemented_profiles)}")
        for index, declared_profile in enumerate(declared_profiles):
            if not isinstance(declared_profile, Mapping):
                raise ValueError(f"{label}.accepted_profiles[{index}] must be a mapping")
            _check_fields(
                label=f"{label}.accepted_profiles[{index}]",
                declared=declared_profile,
                implemented=implemented_profiles[index],
                fields=_PROFILE_FIELDS,
            )
            if "contract" in declared_profile:
                code_contract = implemented_profiles[index].get("contract")
                if code_contract is not None and code_contract != declared_profile["contract"]:
                    raise ValueError(f"{label}.accepted_profiles[{index}].contract differs between model.yaml and the Python module")
                implemented_profiles[index]["contract"] = copy.deepcopy(declared_profile["contract"])
        implemented_data["accepted_profiles"] = implemented_profiles or None
    if "contract" in declared:
        if implemented.contract is not None and implemented.contract != declared["contract"]:
            raise ValueError(f"{label}.contract differs between model.yaml and the Python module")
        implemented_data["contract"] = copy.deepcopy(declared["contract"])
    return SignalSpec.from_dict(implemented_data)


def bind_manifest_ports(module: BioModule, manifest: Mapping[str, Any]) -> tuple[dict[str, SignalSpec], dict[str, SignalSpec]]:
    findings = validate_manifest(manifest)
    blocked = [item for item in findings if item["level"] == "blocked"]
    if blocked:
        raise ValueError("; ".join(f"{item['path']}: {item['message']}" for item in blocked))
    raw_inputs = module.inputs()
    raw_outputs = module.outputs()
    if not isinstance(raw_inputs, Mapping) or not isinstance(raw_outputs, Mapping):
        raise ValueError("The Python module's inputs() and outputs() must each return a dict")
    code_inputs = {name: spec if isinstance(spec, SignalSpec) else SignalSpec.from_dict(spec) for name, spec in raw_inputs.items()}
    code_outputs = {name: spec if isinstance(spec, SignalSpec) else SignalSpec.from_dict(spec) for name, spec in raw_outputs.items()}
    manifest_inputs = _ports_by_name(manifest, "inputs")
    manifest_outputs = _ports_by_name(manifest, "outputs")
    for label, declared, implemented in (("input", manifest_inputs, code_inputs), ("output", manifest_outputs, code_outputs)):
        missing = sorted(set(declared) - set(implemented))
        extra = sorted(set(implemented) - set(declared))
        if missing or extra:
            details = []
            if missing:
                details.append("only in model.yaml: " + ", ".join(missing))
            if extra:
                details.append("only in Python: " + ", ".join(extra))
            raise ValueError(f"{label.capitalize()} ports in model.yaml don't match the Python module ({'; '.join(details)})")
    bound_inputs = {name: _bind_port(direction="inputs", name=name, declared=manifest_inputs[name], implemented=spec) for name, spec in code_inputs.items()}
    bound_outputs = {name: _bind_port(direction="outputs", name=name, declared=manifest_outputs[name], implemented=spec) for name, spec in code_outputs.items()}
    setattr(module, "_biosimulant_manifest_input_specs", bound_inputs)
    setattr(module, "_biosimulant_manifest_output_specs", bound_outputs)
    return bound_inputs, bound_outputs


def load_yaml(path: str | Path) -> dict[str, Any]:
    import yaml

    manifest_path = Path(path)
    data = manifest_path.read_bytes()
    if len(data) > 4 * 1024 * 1024:
        raise ValueError(f"{path}: file is larger than the 4 MiB safety limit")
    try:
        value = yaml.safe_load(data.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top level must be a YAML mapping")
    return value


def compatibility_provenance(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    refs: set[str] = set()
    io = manifest.get("io")
    if isinstance(io, Mapping):
        for direction in ("inputs", "outputs"):
            ports = io.get(direction)
            if not isinstance(ports, list):
                continue
            for port in ports:
                if not isinstance(port, Mapping):
                    continue
                contract = port.get("contract")
                if isinstance(contract, Mapping) and isinstance(contract.get("profile"), str):
                    refs.add(str(contract["profile"]))
    if not refs:
        return None
    return {
        "standard": STANDARD_ID,
        "version": STANDARD_VERSION,
        "catalogue_package": "biosimulant-model-compatibility-standard",
        "catalogue_version": CATALOGUE_VERSION,
        "catalogue_sha256": CATALOGUE_SHA256,
        "profiles": [{"ref": ref, "sha256": profile_digest(ref)} for ref in sorted(refs)],
    }


def _require_string(sample: Any, label: str) -> tuple[str | None, tuple[CompatibilityIssue, ...]]:
    if not isinstance(sample, str):
        return None, (_issue("blocked", "VALUE_INVALID", f"{label} must be a string."),)
    return sample, ()


def _check_protein_sequence(sample: Any) -> Iterable[CompatibilityIssue]:
    raw, issues = _require_string(sample, "Protein sequence")
    if issues:
        return issues
    sequence = "".join(raw.split()).upper()
    if not sequence:
        return (_issue("blocked", "VALUE_INVALID", "Protein sequence must not be empty."),)
    invalid = sorted(set(sequence) - set("ACDEFGHIKLMNPQRSTVWYBXZJUO"))
    if invalid:
        return (_issue("blocked", "VALUE_INVALID", "Protein sequence contains unsupported character(s): " + ", ".join(invalid)),)
    return ()


def _check_molecular_smiles(sample: Any) -> Iterable[CompatibilityIssue]:
    value, issues = _require_string(sample, "SMILES")
    if issues:
        return issues
    if not value:
        return (_issue("blocked", "VALUE_INVALID", "SMILES must not be empty."),)
    if ">" in value:
        return (_issue("blocked", "VALUE_INVALID", "Reaction SMILES is not accepted by the molecular SMILES profile."),)
    if any(character.isspace() for character in value):
        return (_issue("blocked", "VALUE_INVALID", "SMILES must not contain whitespace."),)
    if value.count("[") != value.count("]") or value.count("(") != value.count(")"):
        return (_issue("blocked", "VALUE_INVALID", "SMILES has unbalanced brackets or parentheses."),)
    try:
        from rdkit import Chem  # type: ignore
    except ImportError:
        return (_issue("warning", "PARSER_UNAVAILABLE", "Basic SMILES checks passed; RDKit is not installed for parser validation."),)
    if Chem.MolFromSmiles(value) is None:
        return (_issue("blocked", "VALUE_INVALID", "RDKit could not parse the SMILES value."),)
    return ()


def _read_checked_file(sample: Any, *, label: str) -> tuple[Path | None, str | None, tuple[CompatibilityIssue, ...]]:
    raw, issues = _require_string(sample, label)
    if issues:
        return None, None, issues
    path = Path(raw)
    if not path.is_file():
        return None, None, (_issue("blocked", "VALUE_INVALID", f"{label} path does not name a regular file."),)
    try:
        if path.stat().st_size <= 0:
            return None, None, (_issue("blocked", "VALUE_INVALID", f"{label} file is empty."),)
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, None, (_issue("blocked", "VALUE_INVALID", f"{label} file cannot be read: {exc}"),)
    return path, text, ()


def _check_a3m_file(sample: Any) -> Iterable[CompatibilityIssue]:
    _, text, issues = _read_checked_file(sample, label="A3M")
    if issues:
        return issues
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith(">"):
        return (_issue("blocked", "VALUE_INVALID", "A3M must begin with a FASTA-style header."),)
    records = 0
    query_parts: list[str] = []
    for line in lines:
        if line.startswith(">"):
            records += 1
        elif records == 1:
            query_parts.append(line)
    if records < 1 or not "".join(query_parts).strip():
        return (_issue("blocked", "VALUE_INVALID", "A3M must contain a non-empty query sequence."),)
    return ()


def _check_mmcif_file(sample: Any) -> Iterable[CompatibilityIssue]:
    _, text, issues = _read_checked_file(sample, label="mmCIF")
    if issues:
        return issues
    if not any(line.lstrip().lower().startswith("data_") for line in text.splitlines()):
        return (_issue("blocked", "VALUE_INVALID", "mmCIF file does not contain a data block."),)
    return ()


def _number(sample: Any) -> float | None:
    if isinstance(sample, bool) or not isinstance(sample, (int, float)):
        return None
    value = float(sample)
    return value if math.isfinite(value) else None


def _check_probability(sample: Any) -> Iterable[CompatibilityIssue]:
    value = _number(sample)
    if value is None or not 0 <= value <= 1:
        return (_issue("blocked", "VALUE_INVALID", "Probability must be a finite number from 0 through 1."),)
    return ()


def _check_finite_number(sample: Any) -> Iterable[CompatibilityIssue]:
    if _number(sample) is None:
        return (_issue("blocked", "VALUE_INVALID", "Value must be a finite number."),)
    return ()


_CHECKERS: dict[str, Checker] = {
    "protein_sequence": _check_protein_sequence,
    "molecular_smiles": _check_molecular_smiles,
    "a3m_file": _check_a3m_file,
    "mmcif_file": _check_mmcif_file,
    "probability": _check_probability,
    "finite_number": _check_finite_number,
}


def _validate_checker_registry() -> None:
    missing = sorted({_profile["checker"] for _profile in list_profiles()} - set(_CHECKERS))
    if missing:
        raise RuntimeError("Compatibility catalogue references unavailable checker(s): " + ", ".join(missing))


_validate_checker_registry()
