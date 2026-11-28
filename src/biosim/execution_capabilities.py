"""Conservative host capability checks for local Biosimulant lab execution."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from .pack import PackageError, _safe_yaml_load

_ACCELERATOR_KEYS = frozenset(
    {
        "accelerator",
        "backend",
        "compute_backend",
        "device",
        "gpu",
        "gpu_type",
        "runtime_device",
    }
)
_CPU_KEYS = frozenset({"cpu_cores", "cpus", "cores"})
_MEMORY_MB_KEYS = frozenset({"memory_mb", "ram_mb"})
_MEMORY_GB_KEYS = frozenset({"memory_gb", "ram_gb"})
_CUDA_NAMES = ("cuda", "nvidia", "t4", "a10", "a100", "h100", "l4")


def _positive_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def _maximum_declared_number(value: Any, keys: frozenset[str]) -> float | None:
    found: list[float] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key).strip().lower().replace("-", "_")
                if key in keys:
                    number = _positive_number(child)
                    if number is not None:
                        found.append(number)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return max(found) if found else None


def _accelerator_value(raw: Any) -> str | None:
    if raw is True:
        return "gpu"
    if not isinstance(raw, str):
        if isinstance(raw, Mapping):
            for key in ("type", "name", "device", "accelerator"):
                if key in raw:
                    return _accelerator_value(raw[key])
        return None
    value = raw.strip().lower().replace("_", "-")
    if not value or value in {"auto", "cpu", "none", "false", "off"}:
        return None
    if value == "mps" or "apple" in value or "metal" in value:
        return "mps"
    if any(name in value for name in _CUDA_NAMES):
        return value if value not in {"cuda", "nvidia"} else "cuda"
    if value == "gpu":
        return "gpu"
    return None


def declared_execution_requirements(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Extract explicit hardware constraints without interpreting scientific content."""

    accelerators: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                key = str(raw_key).strip().lower().replace("-", "_")
                if key in _ACCELERATOR_KEYS:
                    accelerator = _accelerator_value(child)
                    if accelerator and accelerator not in accelerators:
                        accelerators.append(accelerator)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(manifest)
    memory_mb = _maximum_declared_number(manifest, _MEMORY_MB_KEYS)
    memory_gb = _maximum_declared_number(manifest, _MEMORY_GB_KEYS)
    if memory_gb is not None:
        memory_mb = max(memory_mb or 0.0, memory_gb * 1024.0)
    return {
        "accelerators": accelerators,
        "requires_gpu": bool(accelerators),
        "cpu_cores": _maximum_declared_number(manifest, _CPU_KEYS),
        "memory_mb": int(memory_mb) if memory_mb is not None else None,
    }


def _total_memory_mb() -> int | None:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None
    if not isinstance(page_size, int) or not isinstance(page_count, int):
        return None
    return int(page_size * page_count / (1024 * 1024))


def _cuda_devices() -> list[dict[str, Any]]:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return []
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    devices: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        name, separator, memory = line.partition(",")
        if not separator or not name.strip():
            continue
        try:
            memory_mb = int(memory.strip())
        except ValueError:
            memory_mb = None
        devices.append({"name": name.strip(), "memory_mb": memory_mb})
    return devices


def detect_host_execution_capabilities() -> dict[str, Any]:
    """Probe only local hardware and installed driver-visible accelerators."""

    system = platform.system() or "unknown"
    machine = platform.machine() or "unknown"
    cuda_devices = _cuda_devices()
    mps_hardware = system == "Darwin" and machine.lower() in {"arm64", "aarch64"}
    return {
        "platform": {"system": system, "machine": machine},
        "cpu": {"logical_cores": os.cpu_count(), "memory_mb": _total_memory_mb()},
        "accelerators": {
            "cuda": {
                "available": bool(cuda_devices),
                "devices": cuda_devices,
                "probe": "nvidia-smi",
            },
            "mps": {
                "available": mps_hardware,
                "devices": ([{"name": "Apple Silicon GPU", "memory_mb": None}] if mps_hardware else []),
                "probe": "platform-hardware",
            },
        },
    }


def evaluate_local_execution_capability(
    manifest: Mapping[str, Any],
    *,
    host: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate explicit lab hardware requirements against a host snapshot."""

    requirements = declared_execution_requirements(manifest)
    host_snapshot = dict(host or detect_host_execution_capabilities())
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    from .runtime_setup_contract import SetupError, parse_setup
    try:
        setup = parse_setup(manifest.get("runtime"))
        if setup:
            requirements["runtime_setup"] = {"supported": True, "version": 1, **setup}
            warnings.append({"code": "runtime_setup_declared", "message": "This Lab runs setup before dependencies with the current user's permissions. Setup must support this host OS; no privilege elevation is performed."})
    except SetupError as exc:
        blockers.append({"code": exc.code, "message": str(exc)})
    runtime = manifest.get("runtime")
    if isinstance(runtime, Mapping) and runtime.get("environment"):
        blockers.append({"code": "runtime_environment_requires_cloud", "message": "This Lab declares a hosted base/source/lock environment; a local virtual environment cannot honor it."})
    cpu = host_snapshot.get("cpu") if isinstance(host_snapshot.get("cpu"), Mapping) else {}
    logical_cores = cpu.get("logical_cores")
    required_cores = requirements["cpu_cores"]
    if required_cores is not None and (
        not isinstance(logical_cores, int) or logical_cores < required_cores
    ):
        blockers.append(
            {
                "code": "insufficient_cpu",
                "message": f"Lab declares {required_cores:g} CPU cores; host reports {logical_cores!r}.",
            }
        )
    memory_mb = cpu.get("memory_mb")
    required_memory_mb = requirements["memory_mb"]
    if required_memory_mb is not None and (
        not isinstance(memory_mb, int) or memory_mb < required_memory_mb
    ):
        blockers.append(
            {
                "code": "insufficient_memory",
                "message": (
                    f"Lab declares {required_memory_mb} MiB memory; host reports {memory_mb!r}."
                ),
            }
        )

    accelerators = (
        host_snapshot.get("accelerators")
        if isinstance(host_snapshot.get("accelerators"), Mapping)
        else {}
    )
    cuda = accelerators.get("cuda") if isinstance(accelerators.get("cuda"), Mapping) else {}
    mps = accelerators.get("mps") if isinstance(accelerators.get("mps"), Mapping) else {}
    cuda_available = cuda.get("available") is True
    mps_available = mps.get("available") is True
    selected_backend = "cpu"
    for request in requirements["accelerators"]:
        if request == "mps":
            supported = mps_available
            backend = "mps"
        elif request == "gpu":
            supported = cuda_available or mps_available
            backend = "cuda" if cuda_available else "mps"
        else:
            devices = cuda.get("devices") if isinstance(cuda.get("devices"), list) else []
            requested_name = request.removeprefix("gpu-").removeprefix("nvidia-")
            requested_name = requested_name.replace("-", "")
            named_match = any(
                requested_name in str(device.get("name") or "").lower().replace("-", "")
                for device in devices
                if isinstance(device, Mapping)
            )
            supported = cuda_available and (request == "cuda" or named_match)
            backend = "cuda"
        if supported:
            selected_backend = backend
        else:
            blockers.append(
                {
                    "code": "accelerator_unavailable",
                    "message": f"Lab requests {request!r}, which this host probe did not find.",
                }
            )

    if selected_backend == "mps":
        warnings.append(
            {
                "code": "mps_framework_support_unverified",
                "message": (
                    "Apple Silicon GPU hardware is present; the model's installed framework "
                    "must still support MPS."
                ),
            }
        )
    warnings.append(
        {
            "code": "local_execution_not_managed",
            "message": (
                "A local run is useful for iteration but does not create a managed Run or "
                "Experiment Passport."
            ),
        }
    )
    return {
        "local_supported": not blockers,
        "selected_backend": selected_backend if not blockers else None,
        "requirements": requirements,
        "host": host_snapshot,
        "blockers": blockers,
        "warnings": warnings,
    }


def load_lab_manifest_for_capability(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    if target.is_dir():
        for name in ("lab.yaml", "lab.yml"):
            manifest = target / name
            if manifest.is_file():
                return _safe_yaml_load(manifest.read_bytes())
        raise PackageError(f"Could not find lab.yaml or lab.yml in {target}")
    if target.suffix != ".bsilab" or not target.is_file():
        raise PackageError(f"Expected a lab source tree or .bsilab package: {target}")
    try:
        with ZipFile(target, "r") as archive:
            package_manifest = _safe_yaml_load(archive.read("package.yaml"))
            entry_manifest = package_manifest.get("entry_manifest")
            if not isinstance(entry_manifest, str):
                raise PackageError("package.yaml is missing a valid entry_manifest")
            return _safe_yaml_load(archive.read(entry_manifest))
    except KeyError as exc:
        raise PackageError(f"Lab package is missing {exc.args[0]}") from exc


def inspect_local_execution_capability(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    result = evaluate_local_execution_capability(load_lab_manifest_for_capability(target))
    return {"command": "labs.capabilities", "lab": str(target), **result}


__all__ = [
    "declared_execution_requirements",
    "detect_host_execution_capabilities",
    "evaluate_local_execution_capability",
    "inspect_local_execution_capability",
    "load_lab_manifest_for_capability",
]
