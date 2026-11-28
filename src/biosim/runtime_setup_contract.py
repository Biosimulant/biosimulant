"""Portable, side-effect-free contract for ``lab.yaml runtime.setup``.

This module has no runtime or provider dependencies. The platform distributes
an exact generated copy so its API can validate without importing biosim.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from copy import deepcopy
from collections.abc import Callable, Mapping
from typing import Any

SETUP_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 900
MAX_TIMEOUT_SECONDS = 3600
MAX_CONTEXT_BYTES = 32 * 1024 * 1024
MAX_CONTEXT_FILES = 5000
_IGNORED = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".biosimulant",
        ".bsimcache",
        "node_modules",
        "dist",
    }
)


class SetupError(ValueError):
    def __init__(self, code: str, message: str, *, field: str = "runtime.setup"):
        self.code, self.field = code, field
        super().__init__(message)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def parse_setup(runtime: Any) -> dict[str, Any] | None:
    if not isinstance(runtime, Mapping) or "setup" not in runtime:
        return None
    value = runtime["setup"]
    if not isinstance(value, Mapping) or set(value) - {"command", "timeout_seconds"}:
        raise SetupError(
            "runtime_setup_invalid",
            "runtime.setup must contain command and optional timeout_seconds only.",
        )
    command = value.get("command")
    if (
        not isinstance(command, list)
        or not command
        or len(command) > 256
        or any(not isinstance(arg, str) or "\0" in arg for arg in command)
        or not command[0].strip()
        or sum(len(arg.encode()) for arg in command) > 65536
    ):
        raise SetupError(
            "runtime_setup_invalid",
            "runtime.setup.command must be a nonempty argument list (at most 64 KiB); invoke a shell explicitly when needed.",
            field="runtime.setup.command",
        )
    executable = command[0]
    if (
        executable.startswith(("/", "\\"))
        or ":" in executable
        or ".." in executable.replace("\\", "/").split("/")
    ):
        raise SetupError(
            "runtime_setup_invalid",
            "Setup executables must be names on PATH or relative paths within the Lab.",
            field="runtime.setup.command",
        )
    timeout = value.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= MAX_TIMEOUT_SECONDS
    ):
        raise SetupError(
            "runtime_setup_invalid",
            f"runtime.setup.timeout_seconds must be an integer from 1 to {MAX_TIMEOUT_SECONDS}.",
            field="runtime.setup.timeout_seconds",
        )
    return {"command": list(command), "timeout_seconds": timeout}


def source_path(path: str) -> str:
    if (
        not isinstance(path, str)
        or not path
        or path.startswith(("/", "\\"))
        or "\\" in path
        or ":" in path
        or "\0" in path
        or ".." in path.split("/")
    ):
        raise SetupError(
            "runtime_setup_context_invalid", f"Invalid setup source path: {path!r}."
        )
    return posixpath.normpath(path)


def include_source(path: str) -> bool:
    parts = path.split("/")
    return (
        not any(part in _IGNORED for part in parts)
        and parts[-1] != "environment.lock"
        and not parts[-1].startswith(".env")
        and not parts[-1].endswith((".pyc", ".pyo"))
    )


def context_files(files: Mapping[str, bytes | str]) -> dict[str, bytes]:
    selected = {}
    size = 0
    for name, content in sorted(files.items()):
        path = source_path(name)
        if path == "." or path in selected:
            raise SetupError(
                "runtime_setup_context_invalid",
                "Setup context has a duplicate or invalid source path.",
            )
        if not include_source(path):
            continue
        raw = content.encode() if isinstance(content, str) else content
        size += len(raw)
        if size > MAX_CONTEXT_BYTES or len(selected) >= MAX_CONTEXT_FILES:
            raise SetupError(
                "runtime_setup_context_too_large",
                "Setup source context exceeds 32 MiB or 5000 files.",
            )
        selected[path] = raw
    return selected


def context_digest(files: Mapping[str, bytes | str]) -> str:
    return digest(
        {
            path: hashlib.sha256(raw).hexdigest()
            for path, raw in context_files(files).items()
        }
    )


def setup_plan(
    files: Mapping[str, bytes | str],
    *,
    load_yaml: Callable,
    root: str = "",
    manifest: Mapping | None = None,
) -> dict[str, Any] | None:
    """Collect embedded Labs in dependency-first order; never execute source.

    Package-backed Labs must first be vendored/materialized by the package
    resolver. Refuse unresolved children when setup is requested, rather than
    silently dropping a child's environment preparation.
    """
    steps, unresolved, missing, conflicts = [], [], [], []
    visited: set[str] = set()
    active: set[str] = set()
    root_runtime = {}

    def visit(directory: str, supplied: Mapping | None = None, depth: int = 0):
        nonlocal root_runtime
        if depth > 5:
            raise SetupError(
                "runtime_setup_invalid", "Lab setup nesting exceeds five levels."
            )
        directory = "" if directory in ("", ".") else source_path(directory)
        if directory in active:
            raise SetupError(
                "runtime_setup_invalid", "Circular child Lab setup dependency."
            )
        if directory in visited:
            return
        visited.add(directory)
        active.add(directory)
        lab = supplied
        if lab is None:
            for filename in ("lab.yaml", "lab.yml"):
                content = files.get(posixpath.join(directory, filename))
                if content is not None:
                    lab = load_yaml(content)
                    break
        if not isinstance(lab, Mapping):
            missing.append(directory or ".")
            active.remove(directory)
            return
        runtime = lab.get("runtime") or {}
        if depth == 0:
            root_runtime = runtime if isinstance(runtime, Mapping) else {}
        elif isinstance(runtime, Mapping):
            if runtime.get("python_version") is not None and str(
                runtime["python_version"]
            ) != str(root_runtime.get("python_version")):
                conflicts.append(
                    f"{directory}: declare the same Python version in the root and child Lab"
                )
            if runtime.get("environment") and runtime.get(
                "environment"
            ) != root_runtime.get("environment"):
                conflicts.append(
                    f"{directory}: move the shared environment recipe into the root Lab"
                )
        for child in lab.get("children") or []:
            if not isinstance(child, Mapping):
                raise SetupError(
                    "runtime_setup_invalid", "Lab children must be mappings."
                )
            if isinstance(child.get("path"), str):
                visit(
                    posixpath.join(directory, source_path(child["path"])),
                    depth=depth + 1,
                )
            else:
                unresolved.append(child.get("package", "unknown"))
        setup = parse_setup(lab.get("runtime"))
        if setup:
            steps.append({"root": directory, **setup})
        active.remove(directory)

    visit(root, manifest)
    if not steps:
        return None
    if conflicts:
        raise SetupError("runtime_setup_environment_conflict", "; ".join(conflicts))
    if missing:
        raise SetupError(
            "runtime_setup_context_invalid",
            "Missing setup Lab manifests: " + ", ".join(missing),
        )
    if unresolved:
        raise SetupError(
            "runtime_setup_unresolved_dependency",
            "Vendor package-backed child Labs before environment setup: "
            + ", ".join(map(str, unresolved)),
        )
    # Per-run inputs/settings do not change software preparation; canonical
    # serialization survives packaging and labs serve's run override copies.
    identity_files = dict(files)
    for path, content in files.items():
        if path.split("/")[-1] in ("lab.yaml", "lab.yml"):
            lab = deepcopy(load_yaml(content))
            if isinstance(lab, dict):
                runtime = lab.get("runtime")
                if isinstance(runtime, dict):
                    for key in (
                        "duration",
                        "communication_step",
                        "settle_steps",
                        "initial_inputs",
                    ):
                        runtime.pop(key, None)
                for model in lab.get("models") or []:
                    if isinstance(model, dict):
                        model.pop("parameters", None)
                identity_files[path] = json.dumps(
                    lab, sort_keys=True, separators=(",", ":")
                )
    plan = {
        "setup_version": SETUP_VERSION,
        "steps": steps,
        "context_sha256": context_digest(identity_files),
    }
    return {**plan, "setup_sha256": digest(plan)}
