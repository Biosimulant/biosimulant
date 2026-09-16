"""Small, runtime-owned checks for model port compatibility.

Compatibility is deliberately kept close to :class:`biosim.signals.SignalSpec`.
Ports describe a few observable facts, and Python functions check the facts and
the values that actually cross a wire. There is no separate profile catalogue,
rule language, lock file, or generated specification.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, TypeAlias

from .modules import BioModule
from .signals import SignalSpec


logger = logging.getLogger(__name__)

CompatibilityStatus: TypeAlias = Literal["ok", "warning", "blocked"]
IssueLevel: TypeAlias = Literal["warning", "blocked"]
NO_SAMPLE = object()
_CONTRACT_FIELDS = {"type", "species", "identifier_namespace"}


@dataclass(frozen=True)
class CompatibilityIssue:
    """One human-readable reason that a connection needs attention."""

    level: IssueLevel
    message: str
    code: str

    def __post_init__(self) -> None:
        if self.level not in {"warning", "blocked"}:
            raise ValueError("CompatibilityIssue.level must be 'warning' or 'blocked'")
        if not self.message.strip():
            raise ValueError("CompatibilityIssue.message must not be empty")
        if not self.code.strip():
            raise ValueError("CompatibilityIssue.code must not be empty")

    def to_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class CompatibilityResult:
    """The result of one port or payload check."""

    issues: tuple[CompatibilityIssue, ...] = ()

    @property
    def status(self) -> CompatibilityStatus:
        if any(issue.level == "blocked" for issue in self.issues):
            return "blocked"
        if self.issues:
            return "warning"
        return "ok"

    @property
    def compatible(self) -> bool:
        return self.status != "blocked"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "compatible": self.compatible,
            "issues": [issue.to_dict() for issue in self.issues],
        }


CompatibilityChecker: TypeAlias = Callable[
    [Mapping[str, Any], Mapping[str, Any], Any],
    CompatibilityResult | Iterable[CompatibilityIssue],
]

_CHECKERS: dict[str, CompatibilityChecker] = {}


def register_checker(
    type_name: str,
    checker: CompatibilityChecker,
    *,
    replace: bool = False,
) -> None:
    """Register a value-aware checker for one semantic port type.

    Model packages may register a namespaced type during import. Replacing an
    existing checker is explicit so two dependencies cannot silently change the
    meaning of the same type.
    """

    clean_name = str(type_name).strip()
    if not clean_name or "." not in clean_name:
        raise ValueError("type_name must be a dotted name such as 'chemical.smiles'")
    if not callable(checker):
        raise TypeError("checker must be callable")
    if clean_name in _CHECKERS and not replace:
        raise ValueError(f"A checker is already registered for '{clean_name}'")
    _CHECKERS[clean_name] = checker


def registered_types() -> tuple[str, ...]:
    """Return the semantic types with a checker in this process."""

    return tuple(sorted(_CHECKERS))


def contract_digest(contract: Mapping[str, Any]) -> str:
    """Return a deterministic SHA-256 for a small port contract."""

    payload = json.dumps(
        dict(contract),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _issue(level: IssueLevel, code: str, message: str) -> CompatibilityIssue:
    return CompatibilityIssue(level=level, code=code, message=message)


def validate_contract(contract: Mapping[str, Any] | None) -> CompatibilityResult:
    """Check the deliberately small contract shape used by a port."""

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

    type_name = contract.get("type")
    if not isinstance(type_name, str) or not type_name.strip():
        issues.append(
            _issue(
                "blocked",
                "MISSING_TYPE",
                "contract.type must be a non-empty dotted name",
            )
        )
    elif "." not in type_name:
        issues.append(
            _issue(
                "blocked",
                "INVALID_TYPE",
                "contract.type must be a dotted name such as 'chemical.smiles'",
            )
        )

    for field in ("species", "identifier_namespace"):
        value = contract.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            issues.append(
                _issue(
                    "blocked",
                    "INVALID_" + field.upper(),
                    f"contract.{field} must be a non-empty string when supplied",
                )
            )
    return CompatibilityResult(tuple(issues))


def _compare_declared_facts(
    source: Mapping[str, Any] | None,
    target: Mapping[str, Any] | None,
) -> list[CompatibilityIssue]:
    issues: list[CompatibilityIssue] = []
    if source is None and target is None:
        return issues
    if source is None:
        return [
            _issue(
                "warning",
                "SOURCE_UNDECLARED",
                "The receiving port declares a semantic type, but the source port does not.",
            )
        ]
    if target is None:
        return [
            _issue(
                "warning",
                "TARGET_UNDECLARED",
                "The source port declares a semantic type, but the receiving port does not.",
            )
        ]

    source_validation = validate_contract(source)
    target_validation = validate_contract(target)
    issues.extend(source_validation.issues)
    issues.extend(target_validation.issues)
    if source_validation.status == "blocked" or target_validation.status == "blocked":
        return issues

    source_type = str(source["type"])
    target_type = str(target["type"])
    if source_type != target_type:
        issues.append(
            _issue(
                "blocked",
                "TYPE_MISMATCH",
                f"The source is '{source_type}', but the receiving port expects '{target_type}'.",
            )
        )
        return issues

    source_species = source.get("species")
    target_species = target.get("species")
    if target_species not in {None, "any"}:
        if source_species in {None, "any"}:
            issues.append(
                _issue(
                    "warning",
                    "SPECIES_UNDECLARED",
                    f"The receiving port expects {target_species}, but the source species is not specific.",
                )
            )
        elif source_species != target_species:
            issues.append(
                _issue(
                    "blocked",
                    "SPECIES_MISMATCH",
                    f"The source declares {source_species}, but the receiving port expects {target_species}.",
                )
            )

    source_namespace = source.get("identifier_namespace")
    target_namespace = target.get("identifier_namespace")
    if target_namespace is not None:
        if source_namespace is None:
            issues.append(
                _issue(
                    "warning",
                    "IDENTIFIER_NAMESPACE_UNDECLARED",
                    f"The receiving port expects {target_namespace} identifiers, but the source does not declare a namespace.",
                )
            )
        elif source_namespace != target_namespace:
            issues.append(
                _issue(
                    "blocked",
                    "IDENTIFIER_NAMESPACE_MISMATCH",
                    f"The source uses {source_namespace} identifiers, but the receiving port expects {target_namespace}.",
                )
            )
    return issues


def _run_type_checker(
    type_name: str,
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    sample: Any,
) -> tuple[CompatibilityIssue, ...]:
    checker = _CHECKERS.get(type_name)
    if checker is None:
        return (
            _issue(
                "warning",
                "CHECKER_UNAVAILABLE",
                f"No value-aware checker is registered for '{type_name}'; only the declared port facts were compared.",
            ),
        )
    try:
        checked = checker(source, target, sample)
        if isinstance(checked, CompatibilityResult):
            return checked.issues
        issues = tuple(checked)
        if not all(isinstance(issue, CompatibilityIssue) for issue in issues):
            raise TypeError("checker returned an item that is not CompatibilityIssue")
        return issues
    except Exception as exc:  # a broken checker must never silently allow a wire
        return (
            _issue(
                "blocked",
                "CHECKER_FAILED",
                f"The '{type_name}' checker failed: {exc}",
            ),
        )


def check_compatibility(
    source: SignalSpec,
    target: SignalSpec,
    *,
    sample: Any = NO_SAMPLE,
) -> CompatibilityResult:
    """Check whether an output spec and value can feed an input spec."""

    issues: list[CompatibilityIssue] = []
    if source.kind != target.kind:
        issues.append(
            _issue(
                "blocked",
                "SIGNAL_KIND_MISMATCH",
                f"incompatible signal kinds: source '{source.kind}' cannot feed target '{target.kind}'.",
            )
        )
        return CompatibilityResult(tuple(issues))

    profile = target.match_input_profile(source)
    if profile is None:
        issues.append(
            _issue(
                "blocked",
                "REPRESENTATION_MISMATCH",
                "incompatible input profiles: the source signal type, data type, shape, format, or unit is not accepted by the receiving port.",
            )
        )
        return CompatibilityResult(tuple(issues))
    if target.interpolation == "linear" and not source.is_numeric:
        issues.append(
            _issue(
                "blocked",
                "INVALID_INTERPOLATION",
                "Linear interpolation requires a numeric source signal.",
            )
        )

    source_contract = source.contract
    target_contract = profile.contract if profile.contract is not None else target.contract
    issues.extend(_compare_declared_facts(source_contract, target_contract))

    checker_source: Mapping[str, Any] | None = source_contract
    checker_target: Mapping[str, Any] | None = target_contract
    if checker_source is None:
        checker_source = checker_target
    if checker_target is None:
        checker_target = checker_source
    if (
        checker_source is not None
        and checker_target is not None
        and validate_contract(checker_source).status != "blocked"
        and validate_contract(checker_target).status != "blocked"
        and checker_source.get("type") == checker_target.get("type")
    ):
        issues.extend(
            _run_type_checker(
                str(checker_source["type"]),
                checker_source,
                checker_target,
                sample,
            )
        )
    return CompatibilityResult(tuple(issues))


def check_payload(contract: Mapping[str, Any] | None, value: Any) -> CompatibilityResult:
    """Check one raw value against the semantic type declared by a port."""

    validation = validate_contract(contract)
    if contract is None or validation.status == "blocked":
        return validation
    return CompatibilityResult(
        validation.issues
        + _run_type_checker(str(contract["type"]), contract, contract, value)
    )


def enforce_result(result: CompatibilityResult, *, context: str) -> None:
    """Raise for blocked results and log warnings for automatic runtime checks."""

    blocked = [issue.message for issue in result.issues if issue.level == "blocked"]
    if blocked:
        raise ValueError(f"{context}: " + "; ".join(blocked))
    for issue in result.issues:
        logger.warning("%s: %s", context, issue.message)


def validate_manifest(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate compatibility declarations in a model manifest."""

    findings: list[dict[str, Any]] = []
    if "compatibility" in manifest:
        findings.append(
            {
                "path": "/compatibility",
                "level": "blocked",
                "code": "LEGACY_COMPATIBILITY_BLOCK",
                "message": "Remove the top-level compatibility block; put a small contract directly on each relevant port.",
            }
        )

    io = manifest.get("io")
    if io is None:
        return findings
    if not isinstance(io, Mapping):
        findings.append(
            {
                "path": "/io",
                "level": "blocked",
                "code": "INVALID_IO",
                "message": "io must be a mapping",
            }
        )
        return findings

    for direction in ("inputs", "outputs"):
        ports = io.get(direction, [])
        if not isinstance(ports, list):
            findings.append(
                {
                    "path": f"/io/{direction}",
                    "level": "blocked",
                    "code": "INVALID_PORTS",
                    "message": f"io.{direction} must be a list",
                }
            )
            continue
        names: set[str] = set()
        for index, port in enumerate(ports):
            path = f"/io/{direction}/{index}"
            if not isinstance(port, Mapping):
                findings.append(
                    {
                        "path": path,
                        "level": "blocked",
                        "code": "INVALID_PORT",
                        "message": "port must be a mapping",
                    }
                )
                continue
            name = port.get("name")
            if not isinstance(name, str) or not name:
                findings.append(
                    {
                        "path": path + "/name",
                        "level": "blocked",
                        "code": "INVALID_PORT_NAME",
                        "message": "port name must be a non-empty string",
                    }
                )
            elif name in names:
                findings.append(
                    {
                        "path": path + "/name",
                        "level": "blocked",
                        "code": "DUPLICATE_PORT_NAME",
                        "message": f"port '{name}' is listed more than once",
                    }
                )
            else:
                names.add(name)

            for issue in validate_contract(port.get("contract")).issues:
                findings.append({"path": path + "/contract", **issue.to_dict()})
            profiles = port.get("accepted_profiles", [])
            if profiles is None:
                profiles = []
            if not isinstance(profiles, list):
                findings.append(
                    {
                        "path": path + "/accepted_profiles",
                        "level": "blocked",
                        "code": "INVALID_ACCEPTED_PROFILES",
                        "message": "accepted_profiles must be a list",
                    }
                )
                continue
            for profile_index, profile in enumerate(profiles):
                if not isinstance(profile, Mapping):
                    findings.append(
                        {
                            "path": f"{path}/accepted_profiles/{profile_index}",
                            "level": "blocked",
                            "code": "INVALID_ACCEPTED_PROFILE",
                            "message": "accepted profile must be a mapping",
                        }
                    )
                    continue
                for issue in validate_contract(profile.get("contract")).issues:
                    findings.append(
                        {
                            "path": f"{path}/accepted_profiles/{profile_index}/contract",
                            **issue.to_dict(),
                        }
                    )
    return findings


def manifest_has_compatibility_declarations(manifest: Mapping[str, Any]) -> bool:
    io = manifest.get("io")
    if not isinstance(io, Mapping):
        return False
    for direction in ("inputs", "outputs"):
        ports = io.get(direction)
        if not isinstance(ports, list):
            continue
        for port in ports:
            if not isinstance(port, Mapping):
                continue
            if any(
                field in port
                for field in (
                    "contract",
                    "format",
                    "emitted_unit",
                    "accepted_units",
                    "accepted_profiles",
                )
            ):
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
    "format",
    "interpolation",
    "max_age",
    "stale_policy",
)
_PROFILE_FIELDS = (
    "signal_type",
    "dtype",
    "shape",
    "schema",
    "accepted_units",
    "format",
)


def _shape_matches(declared: Any, implemented: Any) -> bool:
    if declared is None or implemented is None:
        return declared is implemented
    if not isinstance(declared, (list, tuple)) or not isinstance(implemented, (list, tuple)):
        return declared == implemented
    if len(declared) != len(implemented):
        return False
    return all(
        expected in {"*", None} or expected == actual
        for expected, actual in zip(declared, implemented)
    )


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


def _check_fields(
    *,
    label: str,
    declared: Mapping[str, Any],
    implemented: Mapping[str, Any],
    fields: tuple[str, ...],
) -> None:
    for field in fields:
        if field not in declared:
            continue
        expected = declared[field]
        actual = implemented.get(field)
        if not _field_matches(field, expected, actual):
            raise ValueError(
                f"{label}.{field} is {expected!r} in model.yaml but {actual!r} in the Python module"
            )


def _bind_port(
    *,
    direction: str,
    name: str,
    declared: Mapping[str, Any],
    implemented: SignalSpec,
) -> SignalSpec:
    implemented_data = implemented.to_dict()
    label = f"io.{direction}.{name}"
    _check_fields(
        label=label,
        declared=declared,
        implemented=implemented_data,
        fields=_PORT_FIELDS,
    )

    declared_profiles = declared.get("accepted_profiles")
    if declared_profiles is not None:
        if direction != "inputs":
            raise ValueError(f"{label}.accepted_profiles is only valid on inputs")
        if not isinstance(declared_profiles, list):
            raise ValueError(f"{label}.accepted_profiles must be a list")
        implemented_profiles = implemented_data.get("accepted_profiles")
        if implemented_profiles is None:
            if declared_profiles:
                raise ValueError(
                    f"model.yaml lists {len(declared_profiles)} accepted profile(s) for "
                    f"{label}; the Python module lists none"
                )
            implemented_profiles = []
        if len(declared_profiles) != len(implemented_profiles):
            raise ValueError(
                f"model.yaml lists {len(declared_profiles)} accepted profile(s) for "
                f"{label}; the Python module lists {len(implemented_profiles)}"
            )
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
                    raise ValueError(
                        f"{label}.accepted_profiles[{index}].contract differs between model.yaml and the Python module"
                    )
                implemented_profiles[index]["contract"] = copy.deepcopy(
                    declared_profile["contract"]
                )
        implemented_data["accepted_profiles"] = implemented_profiles or None

    if "contract" in declared:
        if implemented.contract is not None and implemented.contract != declared["contract"]:
            raise ValueError(
                f"{label}.contract differs between model.yaml and the Python module"
            )
        implemented_data["contract"] = copy.deepcopy(declared["contract"])
    return SignalSpec.from_dict(implemented_data)


def bind_manifest_ports(
    module: BioModule, manifest: Mapping[str, Any]
) -> tuple[dict[str, SignalSpec], dict[str, SignalSpec]]:
    """Check manifest ports against Python and attach manifest contracts."""

    raw_inputs = module.inputs()
    raw_outputs = module.outputs()
    if not isinstance(raw_inputs, Mapping) or not isinstance(raw_outputs, Mapping):
        raise ValueError("The Python module's inputs() and outputs() must each return a dict")

    code_inputs = {
        name: spec if isinstance(spec, SignalSpec) else SignalSpec.from_dict(spec)
        for name, spec in raw_inputs.items()
    }
    code_outputs = {
        name: spec if isinstance(spec, SignalSpec) else SignalSpec.from_dict(spec)
        for name, spec in raw_outputs.items()
    }
    manifest_inputs = _ports_by_name(manifest, "inputs")
    manifest_outputs = _ports_by_name(manifest, "outputs")

    for label, declared, implemented in (
        ("input", manifest_inputs, code_inputs),
        ("output", manifest_outputs, code_outputs),
    ):
        missing = sorted(set(declared) - set(implemented))
        extra = sorted(set(implemented) - set(declared))
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append(f"only in model.yaml: {', '.join(missing)}")
            if extra:
                details.append(f"only in Python: {', '.join(extra)}")
            raise ValueError(
                f"{label.capitalize()} ports in model.yaml don't match the Python module "
                f"({'; '.join(details)})"
            )

    bound_inputs = {
        name: _bind_port(
            direction="inputs",
            name=name,
            declared=manifest_inputs[name],
            implemented=spec,
        )
        for name, spec in code_inputs.items()
    }
    bound_outputs = {
        name: _bind_port(
            direction="outputs",
            name=name,
            declared=manifest_outputs[name],
            implemented=spec,
        )
        for name, spec in code_outputs.items()
    }
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


def _sample_strings(value: Any, *, record_key: str) -> tuple[list[str], list[CompatibilityIssue]]:
    if value is NO_SAMPLE:
        return [], []
    if isinstance(value, Mapping) and record_key in value:
        value = value[record_key]
    if isinstance(value, str):
        return [value], []
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value), []
    return [], [
        _issue(
            "blocked",
            "INVALID_VALUE_CARRIER",
            f"Expected a string, a list of strings, or a record with a '{record_key}' field.",
        )
    ]


def _check_smiles(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    sample: Any,
) -> Iterable[CompatibilityIssue]:
    values, issues = _sample_strings(sample, record_key="smiles")
    if issues or sample is NO_SAMPLE:
        return issues
    for value in values:
        if not value:
            issues.append(_issue("blocked", "EMPTY_SMILES", "A SMILES value is empty."))
            continue
        if ">" in value:
            issues.append(
                _issue(
                    "blocked",
                    "REACTION_SMILES",
                    "A reaction SMILES was supplied to a molecular SMILES port.",
                )
            )
        if any(character.isspace() for character in value):
            issues.append(
                _issue("blocked", "SMILES_WHITESPACE", "A SMILES value contains whitespace.")
            )
        if value.count("[") != value.count("]") or value.count("(") != value.count(")"):
            issues.append(
                _issue(
                    "blocked",
                    "UNBALANCED_SMILES",
                    "A SMILES value has unbalanced brackets or parentheses.",
                )
            )
    if any(issue.level == "blocked" for issue in issues):
        return issues

    try:
        from rdkit import Chem  # type: ignore
    except ImportError:
        issues.append(
            _issue(
                "warning",
                "SMILES_PARSER_UNAVAILABLE",
                "Only basic SMILES checks ran because RDKit is not installed; register a parser-backed checker for strict validation.",
            )
        )
        return issues
    for value in values:
        if Chem.MolFromSmiles(value) is None:
            issues.append(
                _issue("blocked", "UNPARSEABLE_SMILES", f"RDKit could not parse SMILES {value!r}.")
            )
    return issues


def _check_protein_sequence(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    sample: Any,
) -> Iterable[CompatibilityIssue]:
    values, issues = _sample_strings(sample, record_key="sequence")
    if issues or sample is NO_SAMPLE:
        return issues
    allowed = set("ACDEFGHIKLMNPQRSTVWYBXZJUO")
    for value in values:
        sequence = "".join(value.split()).upper()
        if not sequence:
            issues.append(
                _issue("blocked", "EMPTY_SEQUENCE", "A protein sequence value is empty.")
            )
            continue
        invalid = sorted(set(sequence) - allowed)
        if invalid:
            issues.append(
                _issue(
                    "blocked",
                    "INVALID_SEQUENCE_CHARACTER",
                    "A protein sequence contains unsupported character(s): " + ", ".join(invalid),
                )
            )
    return issues


def _check_protein_structure(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    sample: Any,
) -> Iterable[CompatibilityIssue]:
    if sample is NO_SAMPLE:
        return ()
    if sample is None or (isinstance(sample, str) and not sample):
        return (_issue("blocked", "EMPTY_STRUCTURE", "The protein structure value is empty."),)
    return ()


register_checker("chemical.smiles", _check_smiles)
register_checker("protein.sequence", _check_protein_sequence)
register_checker("protein.structure", _check_protein_structure)
