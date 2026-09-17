# SPDX-FileCopyrightText: 2026-present Biosimulant Team
#
# SPDX-License-Identifier: MIT
"""Execution policy declarations, lab execution profiles, and phase rules.

The Python ``execution_policy`` attribute stays the only thing that decides when
BioWorld invokes a module. ``model.yaml`` may repeat it as
``biosim.execution_policy`` so tools that cannot import model code can decide
whether simulated-time settings apply to a lab. The runtime refuses to run a
module whose declaration disagrees with the code.

Nothing in this module imports model code.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .modules import BioModule, ExecutionPolicy

MANIFEST_POLICY_FIELD = "execution_policy"
POLICY_VALUES: tuple[str, ...] = tuple(item.value for item in ExecutionPolicy)

Timing = Literal["finite", "temporal", "unknown"]
TIMING_FINITE: Timing = "finite"
TIMING_TEMPORAL: Timing = "temporal"
TIMING_UNKNOWN: Timing = "unknown"

_PHASE_RANK = {
    ExecutionPolicy.ONCE_BEFORE_RUN: 0,
    ExecutionPolicy.EACH_WINDOW: 1,
    ExecutionPolicy.ONCE_AFTER_RUN: 2,
}
_ONCE_POLICIES = (ExecutionPolicy.ONCE_BEFORE_RUN, ExecutionPolicy.ONCE_AFTER_RUN)
_MANIFEST_MARKER = "_biosimulant_manifest_execution_policy"


def _allowed_values() -> str:
    return ", ".join(POLICY_VALUES)


def parse_execution_policy(value: Any) -> ExecutionPolicy:
    """Return ``value`` as an ExecutionPolicy or raise ``ValueError``."""

    try:
        return ExecutionPolicy(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"execution_policy must be one of: {_allowed_values()}") from exc


def declared_execution_policy(model_manifest: Mapping[str, Any] | None) -> ExecutionPolicy | None:
    """Return ``biosim.execution_policy`` from a model manifest, or ``None`` when absent."""

    if not isinstance(model_manifest, Mapping):
        return None
    biosim_block = model_manifest.get("biosim")
    if not isinstance(biosim_block, Mapping) or MANIFEST_POLICY_FIELD not in biosim_block:
        return None
    raw = biosim_block.get(MANIFEST_POLICY_FIELD)
    if raw is None:
        return None
    try:
        return parse_execution_policy(raw)
    except ValueError as exc:
        raise ValueError(
            f"biosim.execution_policy must be one of: {_allowed_values()} (got {raw!r})"
        ) from exc


def resolve_execution_policy(module: BioModule) -> ExecutionPolicy:
    """Return the policy BioWorld will use for ``module``.

    This mirrors BioWorld registration: the instance or class attribute wins,
    and modules that declare nothing run each window.
    """

    return parse_execution_policy(getattr(module, "execution_policy", ExecutionPolicy.EACH_WINDOW))


def bind_manifest_execution_policy(
    module: BioModule,
    model_manifest: Mapping[str, Any] | None,
) -> ExecutionPolicy | None:
    """Check a constructed module against its ``model.yaml`` declaration.

    Returns the declared policy (or ``None`` when undeclared) and marks the module
    so BioWorld treats the policy as explicitly declared. Raises ``ValueError``
    when the declaration and the code disagree.
    """

    declared = declared_execution_policy(model_manifest)
    if declared is None:
        return None
    resolved = resolve_execution_policy(module)
    if resolved is not declared:
        raise ValueError(
            f"model.yaml declares biosim.execution_policy '{declared.value}' but "
            f"{type(module).__name__} resolves to '{resolved.value}'. Change one so they match, "
            "or remove biosim.execution_policy if the policy depends on parameters"
        )
    try:
        setattr(module, _MANIFEST_MARKER, declared)
    except Exception:  # pragma: no cover - defensive: frozen modules
        pass
    return declared


def has_manifest_execution_policy(module: BioModule) -> bool:
    return isinstance(getattr(module, _MANIFEST_MARKER, None), ExecutionPolicy)


@dataclass(frozen=True)
class LabExecutionProfile:
    """Summary of when a lab's modules run, derived from declared policies."""

    timing: Timing
    policies: dict[str, str | None] = field(default_factory=dict)
    undeclared: tuple[str, ...] = ()

    @property
    def uses_time_settings(self) -> bool:
        return self.timing != TIMING_FINITE

    def to_dict(self) -> dict[str, Any]:
        return {
            "timing": self.timing,
            "policies": dict(self.policies),
            "undeclared": list(self.undeclared),
        }


def describe_lab_execution(
    policies: Mapping[str, ExecutionPolicy | str | None],
) -> LabExecutionProfile:
    """Classify a flattened lab from each alias's declared policy.

    ``temporal`` when any module runs each window, ``finite`` when every module is
    declared and none runs each window, otherwise ``unknown``.
    """

    normalized: dict[str, ExecutionPolicy | None] = {}
    for alias, value in policies.items():
        normalized[str(alias)] = None if value is None else parse_execution_policy(value)
    undeclared = tuple(alias for alias, value in normalized.items() if value is None)
    if any(value is ExecutionPolicy.EACH_WINDOW for value in normalized.values()):
        timing: Timing = TIMING_TEMPORAL
    elif normalized and not undeclared:
        timing = TIMING_FINITE
    else:
        timing = TIMING_UNKNOWN
    return LabExecutionProfile(
        timing=timing,
        policies={alias: None if value is None else value.value for alias, value in normalized.items()},
        undeclared=undeclared,
    )


def unknown_lab_execution() -> LabExecutionProfile:
    return LabExecutionProfile(timing=TIMING_UNKNOWN)


def _module_of_ref(ref: Any) -> str | None:
    if not isinstance(ref, str):
        return None
    module, separator, port = ref.rpartition(".")
    if not separator or not module or not port:
        return None
    return module


def module_edges_from_wiring(wiring: Iterable[Any]) -> list[tuple[str, str]]:
    """Return ``(source_alias, target_alias)`` pairs from lab wiring entries.

    Accepts lab manifest entries (``{"from": "a.out", "to": ["b.in"]}``) and
    ``(source_ref, target_ref)`` pairs. Refs use the last dot as the port
    separator, matching ``BioWorld.connect``.
    """

    edges: list[tuple[str, str]] = []
    for entry in wiring or []:
        if isinstance(entry, Mapping):
            source = _module_of_ref(entry.get("from"))
            raw_targets = entry.get("to")
            targets = raw_targets if isinstance(raw_targets, list) else [raw_targets]
        elif isinstance(entry, (tuple, list)) and len(entry) == 2:
            source = _module_of_ref(entry[0])
            targets = [entry[1]]
        else:
            continue
        if source is None:
            continue
        for target_ref in targets:
            target = _module_of_ref(target_ref)
            if target is not None:
                edges.append((source, target))
    return edges


def execution_phase_findings(
    policies: Mapping[str, ExecutionPolicy | str | None],
    edges: Iterable[tuple[str, str]],
) -> list[str]:
    """Return phase-rule violations for declared policies.

    Data may only move forward: before-run modules feed any phase, each-window
    modules feed each-window or after-run modules, and after-run modules feed only
    after-run modules. Once-policy modules may not form cycles. Edges touching an
    alias without a declared policy are skipped.
    """

    known: dict[str, ExecutionPolicy] = {}
    for alias, value in policies.items():
        if value is not None:
            known[str(alias)] = parse_execution_policy(value)

    findings: list[str] = []
    phase_edges: dict[ExecutionPolicy, dict[str, set[str]]] = {policy: {} for policy in _ONCE_POLICIES}
    for source, target in edges:
        source_policy = known.get(source)
        target_policy = known.get(target)
        if source_policy is None or target_policy is None:
            continue
        if _PHASE_RANK[source_policy] > _PHASE_RANK[target_policy]:
            findings.append(
                "invalid execution phase edge: "
                f"{source} ({source_policy.value}) -> {target} ({target_policy.value})"
            )
            continue
        if source_policy is target_policy and source_policy in phase_edges:
            phase_edges[source_policy].setdefault(source, set()).add(target)

    for policy, graph in phase_edges.items():
        nodes = [alias for alias, value in known.items() if value is policy]
        indegree = {alias: 0 for alias in nodes}
        for targets in graph.values():
            for target in targets:
                indegree[target] += 1
        ready = [alias for alias in nodes if indegree[alias] == 0]
        visited = 0
        while ready:
            alias = ready.pop(0)
            visited += 1
            for target in graph.get(alias, set()):
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if visited != len(nodes):
            cyclic = [alias for alias in nodes if indegree[alias] > 0]
            findings.append(f"{policy.value} modules contain a dependency cycle: {', '.join(cyclic)}")
    return findings


# --- Static source reading ------------------------------------------------

SourceStatus = Literal["resolved", "dynamic", "unknown"]


@dataclass(frozen=True)
class SourcePolicyReading:
    """What a model's source says about its policy, without importing it.

    ``resolved`` carries the policy the runtime will use. ``dynamic`` means the
    policy depends on runtime values (for example parameters). ``unknown`` means
    the source could not be read far enough to tell.
    """

    status: SourceStatus
    policy: ExecutionPolicy | None = None
    detail: str = ""


def entrypoint_source_path(model_dir: str | Path, entrypoint: Any) -> tuple[Path | None, str | None]:
    """Return the file and class name an entrypoint refers to, when it is model-local."""

    if not isinstance(entrypoint, str) or not entrypoint.strip():
        return None, None
    if ":" in entrypoint:
        module_path, _, symbol = entrypoint.partition(":")
    else:
        module_path, _, symbol = entrypoint.rpartition(".")
    if not module_path or not symbol:
        return None, None
    base = Path(model_dir) / Path(*module_path.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate, symbol
    return None, symbol


def _policy_literal(node: ast.AST | None) -> ExecutionPolicy | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return ExecutionPolicy(node.value)
        except ValueError:
            return None
    if isinstance(node, ast.Attribute):
        owner = node.value
        owner_is_enum = (isinstance(owner, ast.Name) and owner.id == "ExecutionPolicy") or (
            isinstance(owner, ast.Attribute) and owner.attr == "ExecutionPolicy"
        )
        if owner_is_enum and node.attr in ExecutionPolicy.__members__:
            return ExecutionPolicy[node.attr]
    return None


def _is_policy_target(node: ast.AST) -> tuple[bool, bool]:
    """Return ``(matches, instance_level)`` for an assignment target."""

    if isinstance(node, ast.Name) and node.id == MANIFEST_POLICY_FIELD:
        return True, False
    if (
        isinstance(node, ast.Attribute)
        and node.attr == MANIFEST_POLICY_FIELD
        and isinstance(node.value, ast.Name)
        and node.value.id in {"self", "cls"}
    ):
        return True, True
    return False, False


def _policy_assignments(class_node: ast.ClassDef) -> tuple[list[ast.AST | None], list[ast.AST | None]]:
    class_level: list[ast.AST | None] = []
    instance_level: list[ast.AST | None] = []

    def visit_assignment(node: ast.AST, *, in_method: bool) -> None:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            return
        for target in targets:
            matches, instance = _is_policy_target(target)
            if not matches:
                continue
            if in_method or instance:
                instance_level.append(value)
            else:
                class_level.append(value)

    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                visit_assignment(child, in_method=True)
        else:
            visit_assignment(node, in_method=False)
    return class_level, instance_level


def _defines_method(class_node: ast.ClassDef, name: str) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in class_node.body
    )


def _base_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


_RUNTIME_PACKAGES = frozenset({"biosim", "biosimulant"})
# Runtime-provided bases and the policy a subclass inherits when it sets nothing.
_RUNTIME_BASE_POLICIES = {
    "SignalEmitterBioModule": ExecutionPolicy.EACH_WINDOW,
    "StatefulBioModule": ExecutionPolicy.EACH_WINDOW,
    "TelluriumSBMLBioModule": ExecutionPolicy.EACH_WINDOW,
    "LibCellMLBioModule": ExecutionPolicy.EACH_WINDOW,
    "OnnxClassifierModule": ExecutionPolicy.EACH_WINDOW,
}


def _runtime_imports(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """Return names imported from the runtime and module aliases bound to it."""

    names: dict[str, str] = {}
    module_aliases: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.split(".")[0] not in _RUNTIME_PACKAGES:
                continue
            for alias in node.names:
                names[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _RUNTIME_PACKAGES:
                    module_aliases.add(alias.asname or alias.name.split(".")[0])
    return names, module_aliases


def _runtime_base_policy(
    base: ast.AST,
    imported_names: dict[str, str],
    module_aliases: set[str],
) -> ExecutionPolicy | None:
    if isinstance(base, ast.Name):
        original = imported_names.get(base.id)
        return _RUNTIME_BASE_POLICIES.get(original) if original else None
    if isinstance(base, ast.Attribute) and _attribute_root(base) in module_aliases:
        return _RUNTIME_BASE_POLICIES.get(base.attr)
    return None


def _attribute_root(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def read_source_execution_policy(source: str, class_name: str) -> SourcePolicyReading:
    """Read the policy a class will run with from its source, without importing it.

    Follows base classes defined in the same file. A direct ``BioModule`` base
    without an assignment resolves to ``each_window``, as does any class that
    overrides ``advance_window``. Other imported bases are ``unknown``.
    """

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return SourcePolicyReading("unknown", detail="source is not valid Python")
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    imported_names, module_aliases = _runtime_imports(tree)
    if class_name not in classes:
        return SourcePolicyReading("unknown", detail=f"class {class_name} is not defined in the entrypoint file")

    def read(name: str, seen: frozenset[str]) -> SourcePolicyReading:
        class_node = classes[name]
        class_level, instance_level = _policy_assignments(class_node)
        if instance_level:
            values = {_policy_literal(value) for value in [*class_level, *instance_level]}
            if len(values) == 1 and None not in values:
                (policy,) = values
                return SourcePolicyReading("resolved", policy)
            return SourcePolicyReading(
                "dynamic",
                detail=f"{name} assigns execution_policy inside a method",
            )
        if class_level:
            policy = _policy_literal(class_level[-1])
            if policy is None:
                return SourcePolicyReading(
                    "dynamic",
                    detail=f"{name} assigns execution_policy from a non-literal value",
                )
            return SourcePolicyReading("resolved", policy)
        if _defines_method(class_node, "advance_window"):
            return SourcePolicyReading("resolved", ExecutionPolicy.EACH_WINDOW)
        base_names = [_base_name(base) for base in class_node.bases]
        for base in base_names:
            if base in classes and base not in seen:
                inherited = read(base, seen | {base})
                if inherited.status != "unknown":
                    return inherited
        for base_node in class_node.bases:
            runtime_policy = _runtime_base_policy(base_node, imported_names, module_aliases)
            if runtime_policy is not None:
                return SourcePolicyReading("resolved", runtime_policy)
        if "BioModule" in base_names:
            return SourcePolicyReading("resolved", ExecutionPolicy.EACH_WINDOW)
        external = [base for base in base_names if base and base not in classes]
        if external:
            return SourcePolicyReading(
                "unknown",
                detail=f"{name} inherits from {', '.join(external)}, which is defined elsewhere",
            )
        return SourcePolicyReading("unknown", detail=f"{name} does not subclass BioModule in this file")

    return read(class_name, frozenset({class_name}))


def read_model_dir_execution_policy(
    model_dir: str | Path,
    model_manifest: Mapping[str, Any],
) -> SourcePolicyReading:
    """Read the entrypoint class's policy from a model directory's source."""

    biosim_block = model_manifest.get("biosim") if isinstance(model_manifest, Mapping) else None
    entrypoint = biosim_block.get("entrypoint") if isinstance(biosim_block, Mapping) else None
    path, symbol = entrypoint_source_path(model_dir, entrypoint)
    if path is None or symbol is None:
        return SourcePolicyReading("unknown", detail="entrypoint source is not a model-local file")
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return SourcePolicyReading("unknown", detail=f"could not read {path.name}: {exc}")
    return read_source_execution_policy(source, symbol)


def source_declaration_findings(
    declared: ExecutionPolicy | None,
    reading: SourcePolicyReading,
) -> tuple[list[str], list[str]]:
    """Compare a declaration with a static source reading.

    Returns ``(errors, warnings)``. Undeclared models produce neither.
    """

    if declared is None:
        return [], []
    if reading.status == "resolved" and reading.policy is not None:
        if reading.policy is declared:
            return [], []
        return (
            [
                f"model.yaml declares biosim.execution_policy '{declared.value}' but the source "
                f"resolves to '{reading.policy.value}'"
            ],
            [],
        )
    reason = reading.detail or "the source could not be read"
    return (
        [],
        [
            f"biosim.execution_policy '{declared.value}' can't be verified from source ({reason}); "
            "the runtime checks it when the model loads"
        ],
    )


__all__ = [
    "LabExecutionProfile",
    "MANIFEST_POLICY_FIELD",
    "POLICY_VALUES",
    "SourcePolicyReading",
    "TIMING_FINITE",
    "TIMING_TEMPORAL",
    "TIMING_UNKNOWN",
    "bind_manifest_execution_policy",
    "declared_execution_policy",
    "describe_lab_execution",
    "entrypoint_source_path",
    "execution_phase_findings",
    "has_manifest_execution_policy",
    "module_edges_from_wiring",
    "parse_execution_policy",
    "read_model_dir_execution_policy",
    "read_source_execution_policy",
    "resolve_execution_policy",
    "source_declaration_findings",
    "unknown_lab_execution",
]
