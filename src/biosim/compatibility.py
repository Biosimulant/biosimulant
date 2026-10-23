"""Structural interface checks for Biosimulant model ports."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
from typing import Any, Callable, Iterable, Literal, Mapping, TypeAlias

try:
    from biosimulant_model_compatibility_standard import (
        CATALOGUE_SHA256, CATALOGUE_VERSION, STANDARD_ID, STANDARD_VERSION,
        get_profile, list_profiles, profile_digest,
    )
except ImportError:
    # Historical catalogue metadata is optional and never required to execute a Lab.
    CATALOGUE_SHA256 = None
    CATALOGUE_VERSION = None
    STANDARD_ID = "biosimulant.model-compatibility"
    STANDARD_VERSION = "0"

    def get_profile(ref):
        raise KeyError(ref)

    def list_profiles():
        return []

    def profile_digest(ref):
        raise KeyError(ref)


def _legacy_profile_digest(ref):
    try:
        return profile_digest(ref)
    except KeyError:
        return None


from .__about__ import __version__
from ._errors import PackageError
from .modules import BioModule
from .signals import SignalSpec

logger = logging.getLogger(__name__)

CompatibilityStatus: TypeAlias = Literal["ok", "warning", "blocked"]
IssueLevel: TypeAlias = Literal["warning", "blocked"]
WireMode: TypeAlias = Literal["verified", "partial", "structural", "blocked"]
ValueCheckKind: TypeAlias = Literal["initial_input", "wire_value", "output_value"]
CATALOGUE_PACKAGE = "biosimulant-model-compatibility-standard"
RECORD_SCHEMA_VERSION = "1"
MAX_WIRE_ISSUES = 20
MAX_VIOLATIONS = 100
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






def validate_contract(contract: Mapping[str, Any] | None) -> CompatibilityResult:
    """Read legacy descriptive metadata without grading its scientific meaning."""
    if contract is not None and not isinstance(contract, Mapping):
        return CompatibilityResult((_issue("blocked", "INVALID_CONTRACT", "contract must be a mapping"),))
    return CompatibilityResult()


def contract_digest(contract: Mapping[str, Any]) -> str:
    validation = validate_contract(contract)
    if validation.status == "blocked":
        raise ValueError("invalid compatibility contract: " + "; ".join(i.message for i in validation.issues))
    ref = str(contract.get("profile") or "")
    payload = {
        "standard": STANDARD_ID,
        "version": STANDARD_VERSION,
        "profile": ref,
        "profile_sha256": _legacy_profile_digest(ref),
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

    target_contract = accepted.contract if accepted.contract is not None else target.contract
    return CompatibilityResult(tuple(issues))


def check_declared_contracts(
    source_contract: Mapping[str, Any] | None,
    target_contract: Mapping[str, Any] | None,
) -> CompatibilityResult:
    """Compare the profiles two ports declare, without port structure or values."""

    return CompatibilityResult(tuple(_contract_issues(source_contract, target_contract, NO_SAMPLE)))


def effective_target_contract(source: SignalSpec, target: SignalSpec) -> Mapping[str, Any] | None:
    """Return the contract the target applies to values from this source."""

    accepted = target.match_input_profile(source)
    if accepted is not None and accepted.contract is not None:
        return accepted.contract
    return target.contract


def _mode_for_contracts(result, source_contract, target_contract) -> WireMode:
    return "blocked" if result.status == "blocked" else "structural"


def wire_mode(source: SignalSpec, target: SignalSpec, result: CompatibilityResult) -> WireMode:
    """Classify a checked wire as verified, partial, structural or blocked."""

    return _mode_for_contracts(result, source.contract, effective_target_contract(source, target))


def _contract_issues(source_contract, target_contract, sample) -> list[CompatibilityIssue]:
    # Scientific profiles remain readable metadata; they cannot admit or reject work.
    return []


def check_payload(contract: Mapping[str, Any] | None, value: Any) -> CompatibilityResult:
    """Legacy API: scientific payload grading has been removed."""
    return CompatibilityResult()


def enforce_result(
    result: CompatibilityResult,
    *,
    context: str,
    suppressed_warning_codes: tuple[str, ...] = (),
    recorder: "CompatibilityRecorder | None" = None,
) -> None:
    blocked = [issue.message for issue in result.issues if issue.level == "blocked"]
    if blocked:
        message = f"{context}: " + "; ".join(blocked)
        if recorder is not None:
            raise recorder.blocked_error(message)
        raise ValueError(message)
    for issue in result.issues:
        if issue.code not in suppressed_warning_codes:
            logger.warning("%s: %s", context, issue.message)


def _finding(path: str, issue: CompatibilityIssue) -> dict[str, Any]:
    return {"path": path, **issue.to_dict()}


def validate_manifest(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate port structure, preserving old scientific declarations as metadata."""
    findings: list[dict[str, Any]] = []
    io = manifest.get("io")
    if io is None:
        return findings
    if not isinstance(io, Mapping):
        return [_finding("/io", _issue("blocked", "PROFILE_REPRESENTATION_MISMATCH", "io must be a mapping"))]
    for direction in ("inputs", "outputs"):
        try:
            _ports_by_name(manifest, direction)
        except ValueError as exc:
            findings.append(_finding(f"/io/{direction}", _issue("blocked", "PROFILE_REPRESENTATION_MISMATCH", str(exc))))
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
                implemented_profiles[index]["contract"] = copy.deepcopy(declared_profile["contract"])
        implemented_data["accepted_profiles"] = implemented_profiles or None
    if "contract" in declared:
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
        "catalogue_package": CATALOGUE_PACKAGE,
        "catalogue_version": CATALOGUE_VERSION,
        "catalogue_sha256": CATALOGUE_SHA256,
        "profiles": [{"ref": ref, "sha256": _legacy_profile_digest(ref)} for ref in sorted(refs)],
        "scope": "structural",
    }


class CompatibilityError(PackageError):
    """A wire, input or output value was blocked by a compatibility check."""

    code = "compatibility_blocked"

    def __init__(self, message: str, *, compatibility: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.compatibility = copy.deepcopy(dict(compatibility)) if compatibility is not None else None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.compatibility is not None:
            payload["compatibility"] = copy.deepcopy(self.compatibility)
        return payload


def _contract_ref(contract: Mapping[str, Any] | None) -> str | None:
    if isinstance(contract, Mapping) and isinstance(contract.get("profile"), str):
        return str(contract["profile"])
    return None


def _contract_context(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(contract, Mapping):
        return {}
    return {key: contract[key] for key in ("species", "identifier_namespace") if key in contract}


def _spec_contracts(spec: SignalSpec) -> list[Mapping[str, Any]]:
    contracts = [spec.contract] if spec.contract is not None else []
    for accepted in spec.accepted_profiles or ():
        if accepted.contract is not None:
            contracts.append(accepted.contract)
    return contracts


def _endpoint(module: str, port: str, contract: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "module": module,
        "port": port,
        "profile": _contract_ref(contract),
        "context": _contract_context(contract),
    }


def _file_fingerprint(value: Any) -> tuple[str, int, int] | None:
    if not isinstance(value, (str, os.PathLike)):
        return None
    try:
        path = os.path.abspath(os.fspath(value))
        details = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    if not stat.S_ISREG(details.st_mode):
        return None
    return path, details.st_mtime_ns, details.st_size


def _worst_status(current: str, result: CompatibilityResult) -> CompatibilityStatus:
    order = {"ok": 0, "warning": 1, "blocked": 2}
    return current if order.get(current, 0) >= order[result.status] else result.status  # type: ignore[return-value]


class CompatibilityRecorder:
    """Collects the compatibility outcome of every wire and profiled port in one run.

    The record holds counts and issues, never the checked values themselves.
    """

    def __init__(self) -> None:
        self._wires: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._ports: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._profiles: set[str] = set()
        self._violations: list[dict[str, Any]] = []
        self._violation_count = 0
        self._value_checks = 0
        self._value_failures = 0
        self._truncated = False
        self._checked_output_files: dict[tuple[str, str], tuple[str, int, int]] = {}

    def record_port(self, module: str, direction: str, port: str, spec: SignalSpec) -> None:
        self.record_declared_port(module, direction, port, spec.contract, _spec_contracts(spec))

    def record_declared_port(
        self,
        module: str,
        direction: str,
        port: str,
        contract: Mapping[str, Any] | None,
        contracts: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        refs = sorted(
            {
                ref
                for ref in (_contract_ref(item) for item in (contract, *contracts))
                if ref is not None
            }
        )
        if not refs:
            return
        self._profiles.update(refs)
        direction = "input" if direction.startswith("input") else "output"
        profile = _contract_ref(contract) or (refs[0] if len(refs) == 1 else None)
        entry: dict[str, Any] = {
            "module": module,
            "direction": direction,
            "port": port,
            "profile": profile,
            "value_checks": {"count": 0, "failures": 0},
        }
        if profile is None:
            entry["accepted_profiles"] = refs
        self._ports.setdefault((module, direction, port), entry)

    def record_connection(
        self,
        source_module: str,
        source_port: str,
        source_spec: SignalSpec,
        target_module: str,
        target_port: str,
        target_spec: SignalSpec,
        result: CompatibilityResult,
    ) -> dict[str, Any]:
        return self.record_declared_wire(
            source_module,
            source_port,
            source_spec.contract,
            target_module,
            target_port,
            effective_target_contract(source_spec, target_spec),
            result,
        )

    def record_declared_wire(
        self,
        source_module: str,
        source_port: str,
        source_contract: Mapping[str, Any] | None,
        target_module: str,
        target_port: str,
        target_contract: Mapping[str, Any] | None,
        result: CompatibilityResult,
    ) -> dict[str, Any]:
        for contract in (source_contract, target_contract):
            ref = _contract_ref(contract)
            if ref is not None:
                self._profiles.add(ref)
        key = (source_module, source_port, target_module, target_port)
        wire = self._wires.get(key)
        if wire is None:
            wire = {
                "source": _endpoint(source_module, source_port, source_contract),
                "target": _endpoint(target_module, target_port, target_contract),
                "mode": _mode_for_contracts(result, source_contract, target_contract),
                "status": "ok",
                "issues": [],
                "value_checks": {"count": 0, "failures": 0},
            }
            self._wires[key] = wire
        self._merge_result(wire, result)
        if result.status == "blocked":
            self._add_violation("connect", target_module, target_port, f"{source_module}.{source_port}", result, None)
        return wire

    def record_value_check(
        self,
        kind: ValueCheckKind,
        module: str,
        port: str,
        peer: str | None,
        result: CompatibilityResult,
        sim_time: float | None,
        *,
        profile: str | None = None,
    ) -> None:
        # Kept for old callers; payload grading and its counters have been retired.
        return None

    def check_output(
        self,
        module: str,
        port: str,
        contract: Mapping[str, Any],
        value: Any,
        sim_time: float | None,
    ) -> CompatibilityResult | None:
        """Check one emitted value, skipping a file already checked unchanged."""

        return None

    def _merge_result(self, wire: dict[str, Any], result: CompatibilityResult) -> None:
        wire["status"] = _worst_status(wire["status"], result)
        if result.status == "blocked":
            wire["mode"] = "blocked"
        issues = wire["issues"]
        for issue in result.issues:
            item = issue.to_dict()
            if item in issues:
                continue
            if len(issues) >= MAX_WIRE_ISSUES:
                self._truncated = True
                break
            issues.append(item)

    def _add_violation(
        self,
        stage: str,
        module: str,
        port: str,
        peer: str | None,
        result: CompatibilityResult,
        sim_time: float | None,
    ) -> None:
        self._violation_count += 1
        if len(self._violations) >= MAX_VIOLATIONS:
            self._truncated = True
            return
        blocked = [issue for issue in result.issues if issue.level == "blocked"]
        self._violations.append(
            {
                "stage": stage,
                "module": module,
                "port": port,
                "peer": peer,
                "code": blocked[0].code if blocked else "BLOCKED",
                "message": "; ".join(issue.message for issue in blocked) or "Compatibility check blocked the value.",
                "sim_time": sim_time,
            }
        )

    def blocked_error(self, message: str) -> CompatibilityError:
        return CompatibilityError(message, compatibility=self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        wires = [copy.deepcopy(wire) for wire in self._wires.values()]
        profiles: list[dict[str, Any]] = []
        for ref in sorted(self._profiles):
            try:
                digest: str | None = profile_digest(ref)
            except KeyError:
                digest = None
            profiles.append({"ref": ref, "sha256": digest})
        return {
            "schema_version": RECORD_SCHEMA_VERSION,
            "scope": "structural",
            "standard": STANDARD_ID,
            "version": STANDARD_VERSION,
            "catalogue_package": CATALOGUE_PACKAGE,
            "catalogue_version": CATALOGUE_VERSION,
            "catalogue_sha256": CATALOGUE_SHA256,
            "runtime_version": __version__,
            "profiles": profiles,
            "summary": {
                "wires": len(wires),
                "verified": sum(1 for wire in wires if wire["mode"] == "verified"),
                "partial": sum(1 for wire in wires if wire["mode"] == "partial"),
                "structural": sum(1 for wire in wires if wire["mode"] == "structural"),
                "blocked": sum(1 for wire in wires if wire["mode"] == "blocked"),
                "value_checks": self._value_checks,
                "value_failures": self._value_failures,
                "violations": self._violation_count,
            },
            "wires": wires,
            "ports": [copy.deepcopy(entry) for entry in self._ports.values()],
            "violations": copy.deepcopy(self._violations),
            "truncated": self._truncated,
        }
