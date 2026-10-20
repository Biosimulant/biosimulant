"""Resolve lab dependencies outside the interpreter that executes the models."""
from __future__ import annotations

import importlib.metadata as metadata
import json
import os
import platform
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .__about__ import __version__
from ._errors import PackageError


def _redact_dependency_line(line: str) -> str:
    # Installer output can repeat authenticated index URLs and query tokens.
    line = re.sub(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", r"\1[redacted]@", line)
    return re.sub(r"(https?://[^\s?#]+)[?#][^\s]+", r"\1?[redacted]", line)


def collect_requirements(
    manifests: list[tuple[str, Mapping[str, Any]]], *, python_version: str | None = None,
) -> list[str]:
    environment = default_environment()
    foreign_python = python_version is not None and python_version != environment["python_version"]
    conditional: set[str] = set()
    requirements: dict[str, Requirement] = {}
    origins: dict[str, str] = {}
    for origin, manifest in manifests:
        runtime = manifest.get("runtime") or {}
        if not isinstance(runtime, Mapping):
            raise PackageError(f"Invalid runtime in {origin}")
        dependencies = runtime.get("dependencies") or {}
        if not isinstance(dependencies, Mapping):
            raise PackageError(f"Invalid dependencies in {origin}")
        packages = dependencies.get("packages") or []
        if not isinstance(packages, list):
            raise PackageError(f"Dependency packages in {origin} must be a list")
        for raw in packages:
            try:
                req = Requirement(raw)
            except (InvalidRequirement, TypeError) as exc:
                raise PackageError(f"Invalid dependency in {origin}: {raw!r}") from exc
            pins = list(req.specifier)
            if req.url or len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
                raise PackageError(f"Dependency in {origin} must use an exact pin: {raw}")
            name = canonicalize_name(req.name)
            if name == "biosimulant":
                # The executor owns its runtime version; models cannot replace it.
                continue
            if req.marker is not None:
                if foreign_python:
                    # The resolver evaluates markers under the selected interpreter.
                    conditional.add(str(req))
                    continue
                if not req.marker.evaluate(environment):
                    continue
            previous = requirements.get(name)
            if previous is not None and previous.specifier != req.specifier:
                raise PackageError(
                    f"Conflicting dependency {name}: {previous} ({origins[name]}) "
                    f"and {req} ({origin}). Models in one lab need compatible versions."
                )
            if previous is not None:
                req.extras.update(previous.extras)
            req.marker = None  # Only active requirements reach the target environment.
            requirements[name] = req
            origins[name] = origin
    return [str(requirements[name]) for name in sorted(requirements)] + sorted(conditional)


def require_current_environment(requirements: list[str]) -> None:
    """Live-object APIs cannot safely replace packages in their caller's process."""
    problems: list[str] = []
    pending = [Requirement(raw) for raw in requirements]
    seen: set[str] = set()
    distributions = {"numpy"}  # Typed signals use NumPy even when a model declares no dependencies.
    while pending:
        req = pending.pop()
        key = str(req)
        if key in seen:
            continue
        seen.add(key)
        distributions.add(canonicalize_name(req.name))
        try:
            installed = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            problems.append(f"{req} is not installed")
            continue
        if not req.specifier.contains(installed, prereleases=True):
            problems.append(f"{req} required; installed {installed}")
        for raw in metadata.requires(req.name) or []:
            dependency = Requirement(raw)
            if dependency.marker is None or any(
                dependency.marker.evaluate({"extra": extra})
                for extra in {"", *req.extras}
            ):
                pending.append(dependency)
    problems.extend(_loaded_version_conflicts(distributions))
    if problems:
        raise PackageError(
            "Incompatible Python environment: " + "; ".join(problems)
            + ". Use run_package() or `biosimulant labs run` to prepare an isolated "
            "environment, or install the lab dependencies before starting Python."
        )


def _loaded_version_conflicts(required: set[str]) -> list[str]:
    problems: list[str] = []
    for module_name, distributions in metadata.packages_distributions().items():
        if not any(canonicalize_name(name) in required for name in distributions):
            continue
        module = sys.modules.get(module_name)
        loaded = vars(module).get("__version__") if module is not None else None
        if not isinstance(loaded, str) or len(distributions) != 1:
            continue
        try:
            installed = metadata.version(distributions[0])
            if Version(loaded) != Version(installed):
                problems.append(f"{module_name} loaded {loaded}, installed {installed}")
        except (metadata.PackageNotFoundError, InvalidVersion):
            continue
    return problems


def environment_record() -> dict[str, Any]:
    packages = {
        canonicalize_name(dist.metadata["Name"]): dist.version
        for dist in metadata.distributions() if dist.metadata["Name"]
    }
    imported = {}
    for name, distributions in metadata.packages_distributions().items():
        module = sys.modules.get(name)
        version = vars(module).get("__version__") if module is not None else None
        if isinstance(version, str):
            imported[name] = {
                "version": version, "path": getattr(module, "__file__", None),
                "distributions": distributions,
            }
    return {
        "python": platform.python_version(), "platform": platform.platform(),
        "biosimulant": __version__, "packages": dict(sorted(packages.items())),
        "imported": imported,
    }


def run_dependency_command(
    command: list[str], *,
    dependency_logger: Callable[[str], None] | None = None,
    process_tracker: Callable[[Any], Any] | None = None,
    cancel_checker: Callable[[], None] | None = None,
) -> str:
    from .pack import _new_process_group_kwargs

    if cancel_checker is not None:
        cancel_checker()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, **_new_process_group_kwargs(),
    )
    lines: list[str] = []
    recent: deque[str] = deque(maxlen=40)
    output: queue.Queue[str | None] = queue.Queue()
    assert process.stdout is not None

    def read_output() -> None:
        try:
            for line in process.stdout:
                output.put(line)
        finally:
            output.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    with process_tracker(process) if process_tracker is not None else nullcontext():
        reader.start()
        try:
            while True:
                if cancel_checker is not None:
                    cancel_checker()
                try:
                    line = output.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                lines.append(line)
                recent.append(_redact_dependency_line(line.rstrip()))
                if dependency_logger is not None:
                    dependency_logger(_redact_dependency_line(line.rstrip()))
            returncode = process.wait()
        finally:
            terminate_process(process)
            reader.join()
            process.stdout.close()
    if cancel_checker is not None:
        cancel_checker()
    if returncode:
        raise PackageError("Dependency preparation failed:\n" + "\n".join(recent))
    return "".join(lines)


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()


@contextmanager
def isolated_environment(
    requirements: list[str], *,
    python: str | Path = sys.executable,
    dependency_logger: Callable[[str], None] | None = None,
    process_tracker: Callable[[Any], Any] | None = None,
    cancel_checker: Callable[[], None] | None = None,
) -> Iterator[Path]:
    from .managed_runtime import _local_source_spec, _uv_command, _venv_python_path

    callbacks = dict(
        dependency_logger=dependency_logger, process_tracker=process_tracker,
        cancel_checker=cancel_checker,
    )
    uv = _uv_command()
    with tempfile.TemporaryDirectory(prefix="biosimulant-lab-env-") as directory:
        root = Path(directory)
        run_dependency_command([*uv, "venv", "--python", str(python), str(root)], **callbacks)
        executable = _venv_python_path(root)
        source = _local_source_spec() or f"biosimulant=={__version__}"
        from .tellurium_artifacts import WHEELHOUSE_ENV, artifact_requirements, requests_candidate
        artifacts: list[str] = []
        wheelhouse = os.environ.get(WHEELHOUSE_ENV)
        if wheelhouse and requests_candidate(requirements):
            target = json.loads(run_dependency_command([
                str(executable), "-c", "import json,platform,sys;print(json.dumps(dict("
                "python_version=f'{sys.version_info.major}.{sys.version_info.minor}',"
                "python_full_version=platform.python_version(),"
                "sys_platform=sys.platform,machine=platform.machine(),implementation=sys.implementation.name)))",
            ], **callbacks))
            artifacts = artifact_requirements(requirements, Path(wheelhouse), **target)
        # A single solve includes the runtime and every model's direct/transitive requirements.
        run_dependency_command(
            [*uv, "pip", "install", "--python", str(executable), source, *requirements, *artifacts],
            **callbacks,
        )
        try:
            run_dependency_command([*uv, "pip", "check", "--python", str(executable)], **callbacks)
        except PackageError as exc:
            # Collect evidence before TemporaryDirectory removes the failed env.
            # Keep this best-effort, bounded, and cancellable like installation.
            details = []
            diagnostic_callbacks = {**callbacks, "dependency_logger": None}
            for label, command in [
                ("installer", [*uv, "--version"]),
                ("interpreter", [str(executable), "-c",
                    "import platform,sys;print(sys.version);print(platform.platform());print(platform.machine())"]),
                ("resolved packages", [*uv, "pip", "freeze", "--python", str(executable)]),
                ("wheel tags", [str(executable), "-c",
                    "import importlib.metadata as m; "
                    "print('\\n'.join(d.metadata['Name']+'=='+d.version+' '+"
                    "str(d.read_text('WHEEL')) for d in m.distributions() "
                    "if d.metadata['Name'].lower() in {'antimony','phrasedml'}))"]),
            ]:
                try:
                    output = run_dependency_command(command, **diagnostic_callbacks)
                    details.append(label + ": " + _redact_dependency_line(output[-6000:]))
                except PackageError:
                    details.append(label + ": unavailable")
            raise PackageError("Dependency validation failed after installation.\n" + str(exc)
                               + "\n" + "\n".join(details)) from exc
        yield executable
