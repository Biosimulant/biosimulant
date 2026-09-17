from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from biosim.execution import unknown_lab_execution
from biosim.pack import (
    PackageError,
    _local_lab_release_identity,
    _safe_yaml_dump,
    _safe_yaml_load,
    build_package,
    inspect_lab_execution,
    prepare_lab_package,
)
from biosim.run_overrides import (
    apply_run_overrides as _apply_run_overrides,
    map_initial_inputs,
)
from biosim.workspace import get_lab as workspace_get_lab
from biosim.workspace import save_lab as workspace_save_lab
from biosim.world import WorldEvent


ACTIVE_STATUSES = {"queued", "pending", "running", "cancelling"}
STATIC_DIR = Path(__file__).resolve().parent / "static"
GPU_ACCELERATOR_VALUES = {"gpu", "cuda", "mps"}
GPU_ACCELERATOR_PARAMETER_KEYS = {
    "accelerator",
    "backend",
    "compute_backend",
    "device",
    "runtime_device",
}


def _map_initial_inputs(manifest: Mapping[str, Any], value: Any) -> dict[str, Any]:
    return map_initial_inputs(manifest, value)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _api_ok(data: Mapping[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"ok": True, "data": dict(data), "error": None},
    )


def _api_error(message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "data": None, "error": {"message": message}},
    )


class _RunOutputBridge:
    def __init__(self, stream: Any, line_handler: Any) -> None:
        self._stream = stream
        self._line_handler = line_handler
        self._buffer = ""
        self._lock = threading.Lock()
        self.encoding = getattr(stream, "encoding", None)
        self.errors = getattr(stream, "errors", None)

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        with self._lock:
            self._stream.write(text)
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self._line_handler(line.rstrip("\r"))
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._buffer:
                self._line_handler(self._buffer.rstrip("\r"))
                self._buffer = ""
            self._stream.flush()

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", lambda: False)())


class RunCancelled(Exception):
    """Raised internally when a local run is cancelled."""


class RunConflictError(PackageError):
    """Raised when a new local run conflicts with an active run."""


def _terminate_process_tree(process: subprocess.Popen[Any], *, grace_seconds: float = 2.0) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception:
            process.terminate()
    else:
        process.terminate()

    deadline = time.monotonic() + grace_seconds
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)

    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except Exception:
            process.kill()
    else:
        process.kill()


class _RunCancellationController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cancel_requested = False
        self._processes: set[subprocess.Popen[Any]] = set()

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    def check_cancelled(self) -> None:
        if self.cancel_requested:
            raise RunCancelled()

    def request_cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            processes = list(self._processes)
        for process in processes:
            _terminate_process_tree(process)

    @contextmanager
    def track_process(self, process: subprocess.Popen[Any]) -> Iterator[None]:
        terminate_now = False
        with self._lock:
            if self._cancel_requested:
                terminate_now = True
            else:
                self._processes.add(process)
        if terminate_now:
            _terminate_process_tree(process)
        try:
            yield
        finally:
            with self._lock:
                self._processes.discard(process)


async def _request_object(request: Request) -> dict[str, Any] | JSONResponse:
    body = await request.body()
    if not body:
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _api_error("Request body must be valid JSON")
    if not isinstance(payload, dict):
        return _api_error("Request body must be an object")
    return payload


def _display_url(host: str, port: int) -> str:
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}/"


def _lab_manifest_path(path: Path) -> Path:
    for name in ("lab.yaml", "lab.yml"):
        candidate = path / name
        if candidate.is_file():
            return candidate
    raise PackageError(f"Could not find lab.yaml or lab.yml in {path}")


def _model_manifest_path(path: Path) -> Path:
    for name in ("model.yaml", "model.yml", "biosim.yaml", "biosim.yml"):
        candidate = path / name
        if candidate.is_file():
            return candidate
    raise PackageError(f"Could not find model.yaml or model.yml in {path}")


def _load_lab_manifest(path: Path) -> dict[str, Any]:
    return _safe_yaml_load(_lab_manifest_path(path).read_bytes())


def _write_lab_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    _lab_manifest_path(path).write_bytes(_safe_yaml_dump(dict(manifest)))


def _has_obvious_cuda_runtime() -> bool:
    disabled = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip().lower()
    if disabled in {"-1", "none", "no", "false"}:
        return False
    return shutil.which("nvidia-smi") is not None or shutil.which("nvcc") is not None


def _is_accelerator_parameter(name: str) -> bool:
    key = name.strip().lower().replace("-", "_")
    return (
        key in GPU_ACCELERATOR_PARAMETER_KEYS
        or key.endswith("_accelerator")
        or key.endswith("_device")
    )


def _gpu_warning_message(*, alias: str, parameter: str, value: str) -> str:
    host = f"{platform.system() or sys.platform} {platform.machine() or 'unknown'}".strip()
    if sys.platform == "darwin":
        detail = (
            "this host is macOS, where GPU requests may use Apple MPS or another backend "
            "that some models do not support reliably"
        )
    elif value == "cuda" or value == "gpu":
        if _has_obvious_cuda_runtime():
            detail = (
                "a CUDA-capable runtime appears to be present, but GPU support still depends "
                "on the model and installed drivers"
            )
        else:
            detail = "no obvious CUDA runtime was detected on this host"
    elif value == "mps":
        detail = "Apple MPS acceleration can be unsupported or unreliable for some models"
    else:
        detail = "the requested accelerator may not be available on this host"
    return (
        f"Model '{alias}' requests GPU acceleration via {parameter}={value!r}; {detail}. "
        "The run will continue with the lab's configured accelerator."
        f" Host: {host}."
    )


def _execution_profile_for_lab(lab_path: Path) -> dict[str, Any]:
    try:
        return inspect_lab_execution(lab_path)["profile"]
    except Exception:
        # An unreadable lab keeps today's Run dialog rather than failing the page.
        return unknown_lab_execution().to_dict()


def _compute_warnings_for_manifest(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    models = manifest.get("models")
    if not isinstance(models, list):
        return warnings
    for index, entry in enumerate(models):
        if not isinstance(entry, Mapping):
            continue
        alias = entry.get("alias")
        alias_text = alias.strip() if isinstance(alias, str) and alias.strip() else f"model_{index}"
        parameters = entry.get("parameters")
        if not isinstance(parameters, Mapping):
            continue
        for parameter, raw_value in parameters.items():
            if not isinstance(parameter, str) or not _is_accelerator_parameter(parameter):
                continue
            if not isinstance(raw_value, str):
                continue
            value = raw_value.strip().lower()
            if value not in GPU_ACCELERATOR_VALUES:
                continue
            warnings.append(
                {
                    "code": f"{value}-accelerator-requested",
                    "message": _gpu_warning_message(
                        alias=alias_text,
                        parameter=parameter,
                        value=value,
                    ),
                    "model_alias": alias_text,
                    "parameter": parameter,
                    "value": value,
                }
            )
    return warnings


@contextmanager
def _package_file_for_lab(path: Path) -> Iterator[Path]:
    target = path.expanduser().resolve()
    if target.is_file():
        if target.suffix != ".bsilab":
            raise PackageError(f"Expected a .bsilab package: {target}")
        yield target
        return
    if not target.is_dir():
        raise PackageError(f"Lab path not found: {target}")
    _lab_manifest_path(target)
    package_name, version = _local_lab_release_identity(target)
    with tempfile.TemporaryDirectory(prefix="biosim-lab-") as temp_dir:
        yield build_package(
            target,
            output_path=Path(temp_dir) / f"{target.name or 'lab'}.bsilab",
            package_name=package_name,
            version=version,
        )


def _load_wiring_layout(path: Path) -> dict[str, Any] | None:
    layout_path = path / "wiring-layout.json"
    if not layout_path.is_file():
        return None
    loaded = json.loads(layout_path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else None


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _model_io_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    io = manifest.get("io")
    return dict(io) if isinstance(io, Mapping) else None


def _model_biosim_payload(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    bsim = manifest.get("biosim")
    if not isinstance(bsim, Mapping):
        return None
    payload: dict[str, Any] = {}
    for key in ("init_kwargs", "parameters"):
        value = bsim.get(key)
        if isinstance(value, Mapping):
            payload[key] = dict(value)
        elif isinstance(value, list):
            payload[key] = list(value)
    return payload or None


def _resolved_model_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": manifest.get("title"),
        "description": manifest.get("description"),
        "io": _model_io_from_manifest(manifest),
        "biosim": _model_biosim_payload(manifest),
        "manifest": dict(manifest),
    }
    return payload


def _resolved_space_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    models = manifest.get("models")
    io = manifest.get("io")
    return {
        "title": manifest.get("title"),
        "description": manifest.get("description"),
        "io": dict(io) if isinstance(io, Mapping) else None,
        "model_count": len(models) if isinstance(models, list) else 0,
    }


def _port_payload(name: str, spec: Any) -> dict[str, Any]:
    try:
        data = spec.to_dict()
    except Exception:
        data = {}
    accepted_units: list[str] = []
    for profile in data.get("accepted_profiles") or []:
        if not isinstance(profile, Mapping):
            continue
        for unit in profile.get("accepted_units") or []:
            if isinstance(unit, str) and unit not in accepted_units:
                accepted_units.append(unit)
    payload = {"name": name}
    if data.get("description") is not None:
        payload["description"] = data.get("description")
    if data.get("emitted_unit") is not None:
        payload["emitted_unit"] = data.get("emitted_unit")
    if accepted_units:
        payload["accepted_units"] = accepted_units
    return payload


def _extract_world_ports(world: Any) -> dict[str, dict[str, list[dict[str, Any]]]]:
    modules = getattr(world, "_modules", {})
    if not isinstance(modules, Mapping):
        return {}
    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for alias, entry in modules.items():
        input_specs = getattr(entry, "input_specs", {})
        output_specs = getattr(entry, "output_specs", {})
        out[str(alias)] = {
            "inputs": [
                _port_payload(name, spec)
                for name, spec in input_specs.items()
                if isinstance(name, str)
            ]
            if isinstance(input_specs, Mapping)
            else [],
            "outputs": [
                _port_payload(name, spec)
                for name, spec in output_specs.items()
                if isinstance(name, str)
            ]
            if isinstance(output_specs, Mapping)
            else [],
        }
    return out


def _rewrite_alias_in_ref(value: str, old_alias: str, new_alias: str) -> str | None:
    dot_prefix = f"{old_alias}."
    colon_prefix = f"{old_alias}:"
    if value == old_alias:
        return new_alias
    if value.startswith(dot_prefix):
        return f"{new_alias}.{value[len(dot_prefix):]}"
    if value.startswith(colon_prefix):
        return f"{new_alias}:{value[len(colon_prefix):]}"
    return None


def _rewrite_alias_in_value(value: Any, old_alias: str, new_alias: str) -> Any:
    if isinstance(value, str):
        return _rewrite_alias_in_ref(value, old_alias, new_alias) or value
    if isinstance(value, list):
        return [_rewrite_alias_in_value(item, old_alias, new_alias) for item in value]
    if isinstance(value, dict):
        return {
            key: _rewrite_alias_in_value(item, old_alias, new_alias)
            for key, item in value.items()
        }
    return value


def _rewrite_alias_references(manifest: dict[str, Any], old_alias: str, new_alias: str) -> None:
    wiring = manifest.get("wiring")
    if isinstance(wiring, list):
        manifest["wiring"] = [
            _rewrite_alias_in_value(entry, old_alias, new_alias)
            for entry in wiring
        ]
    io = manifest.get("io")
    if isinstance(io, dict):
        for direction in ("inputs", "outputs"):
            ports = io.get(direction)
            if not isinstance(ports, list):
                continue
            for port in ports:
                if not isinstance(port, dict):
                    continue
                maps_to = port.get("maps_to")
                if isinstance(maps_to, str):
                    rewritten = _rewrite_alias_in_ref(maps_to, old_alias, new_alias)
                    if rewritten:
                        port["maps_to"] = rewritten
    runtime = manifest.get("runtime")
    if isinstance(runtime, dict):
        initial_inputs = runtime.get("initial_inputs")
        if isinstance(initial_inputs, dict):
            renamed: dict[str, Any] = {}
            removed: list[str] = []
            for key, value in initial_inputs.items():
                rewritten = _rewrite_alias_in_ref(str(key), old_alias, new_alias)
                if rewritten:
                    removed.append(str(key))
                    renamed[rewritten] = value
            for key in removed:
                initial_inputs.pop(key, None)
            initial_inputs.update(renamed)


def _artifact_id_for_path(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()


def _artifact_url(run_id: str, artifact_id: str) -> str:
    return f"/api/runs/{quote(run_id, safe='')}/artifacts/{quote(artifact_id, safe='')}"


def _coerce_artifact_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _rewrite_structure_artifact_source(
    data: dict[str, Any],
    *,
    run_id: str | None,
    artifacts: dict[str, Path] | None,
) -> None:
    source = data.get("source")
    if not isinstance(source, Mapping):
        return
    next_source = dict(source)
    data["source"] = next_source
    if isinstance(next_source.get("url"), str) and str(next_source.get("url")).strip():
        return
    path = _coerce_artifact_path(next_source.get("path"))
    if path is None:
        return
    artifact_id = next_source.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        artifact_id = _artifact_id_for_path(path)
    artifact_id = artifact_id.strip()
    next_source["artifact_id"] = artifact_id
    next_source["path"] = str(path)
    if run_id is None or artifacts is None or not path.is_file():
        return
    artifacts[artifact_id] = path
    next_source["url"] = _artifact_url(run_id, artifact_id)


def _sanitize_visuals(
    visuals: Any,
    *,
    run_id: str | None = None,
    artifacts: dict[str, Path] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(visuals, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for entry in visuals:
        if not isinstance(entry, Mapping):
            continue
        next_entry = {"module": entry.get("module"), "visuals": []}
        for visual in entry.get("visuals") or []:
            if isinstance(visual, Mapping):
                next_visual = dict(visual)
                data = next_visual.get("data")
                if isinstance(data, Mapping):
                    next_data = dict(data)
                    next_visual["data"] = next_data
                    render = next_visual.get("render")
                    if isinstance(render, str) and render.lower() == "structure3d":
                        _rewrite_structure_artifact_source(
                            next_data,
                            run_id=run_id,
                            artifacts=artifacts,
                        )
                next_entry["visuals"].append(next_visual)
        if next_entry["visuals"]:
            sanitized.append(next_entry)
    return sanitized


RUN_STORE_SCHEMA_VERSION = 1
RUN_INTERRUPTED_MESSAGE = "Run interrupted because labs serve stopped before completion."


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def _safe_artifact_filename(
    artifact_id: str,
    source_name: str,
    used_names: set[str],
) -> str:
    stem = "".join(
        char if char.isalnum() or char in "._-" else "-"
        for char in artifact_id.strip()
    ).strip("._-")
    if not stem:
        stem = hashlib.sha1(artifact_id.encode("utf-8")).hexdigest()[:12]
    suffix = Path(source_name).suffix
    candidate = f"{stem[:96]}{suffix}" if suffix and not stem.endswith(suffix) else stem[:96]
    if not candidate:
        candidate = "artifact"
    name = candidate
    counter = 2
    while name in used_names:
        base = Path(candidate).stem
        ext = Path(candidate).suffix
        name = f"{base}-{counter}{ext}"
        counter += 1
    used_names.add(name)
    return name


def _rewrite_result_artifact_sources(
    results: dict[str, Any],
    *,
    run_id: str,
    artifacts: Mapping[str, Path],
) -> None:
    visuals = results.get("visuals")
    if not isinstance(visuals, list):
        return
    for module in visuals:
        if not isinstance(module, Mapping):
            continue
        module_visuals = module.get("visuals")
        if not isinstance(module_visuals, list):
            continue
        for visual in module_visuals:
            if not isinstance(visual, dict):
                continue
            render = visual.get("render")
            if not isinstance(render, str) or render.lower() != "structure3d":
                continue
            data = visual.get("data")
            if not isinstance(data, dict):
                continue
            source = data.get("source")
            if not isinstance(source, dict):
                continue
            artifact_id = source.get("artifact_id")
            if not isinstance(artifact_id, str):
                continue
            path = artifacts.get(artifact_id)
            if path is None:
                continue
            source["path"] = str(path)
            source["url"] = _artifact_url(run_id, artifact_id)


def _copytree_ignore_run_history(src: str, names: list[str]) -> set[str]:
    if Path(src).name == ".biosimulant" and "runs" in names:
        return {"runs"}
    return set()


@dataclass
class RunRecord:
    id: str
    lab_id: str
    parameters: dict[str, Any] | None
    simulation_config: dict[str, Any] | None
    status: str = "pending"
    execution_target: str = "local"
    hub_run_id: str | None = None
    results_summary: dict[str, Any] | None = None
    results_path: str | None = None
    error_message: str | None = None
    duration_seconds: float | None = None
    progress: dict[str, Any] | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str = field(default_factory=_now)
    results: dict[str, Any] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Path] = field(default_factory=dict)
    seq: int = 0
    world: Any = None
    cancel_requested: bool = False
    cancellation: _RunCancellationController = field(default_factory=_RunCancellationController)
    thread: threading.Thread | None = None
    last_progress_log_at: float = 0.0
    last_progress_log_pct: float = -10.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "lab_id": self.lab_id,
            "model_id": None,
            "status": self.status,
            "execution_target": self.execution_target,
            "hub_run_id": self.hub_run_id,
            "parameters": self.parameters,
            "simulation_config": self.simulation_config,
            "results_summary": self.results_summary,
            "results_path": self.results_path,
            "error_message": self.error_message,
            "duration_seconds": self.duration_seconds,
            "progress": self.progress,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
        }

    def add_log(self, level: str, message: str, *, source: str = "biosimulant") -> None:
        self.seq += 1
        self.logs.append(
            {
                "id": self.seq,
                "run_id": self.id,
                "seq": self.seq,
                "level": level,
                "source": source,
                "message": message,
                "timestamp": _now(),
            }
        )


class _LocalRunStore:
    def __init__(self, lab_path: Path) -> None:
        self.root = lab_path / ".biosimulant" / "runs"

    def load_runs(self) -> dict[str, RunRecord]:
        if not self.root.is_dir():
            return {}
        runs: dict[str, RunRecord] = {}
        for run_dir in sorted(self.root.iterdir()):
            if not run_dir.is_dir():
                continue
            try:
                run, interrupted = self._load_run_dir(run_dir)
            except Exception:
                continue
            runs[run.id] = run
            if interrupted:
                self.save_run(run)
                self.replace_logs(run)
        return runs

    def save_run(self, run: RunRecord) -> None:
        run_dir = self._run_dir(run.id)
        payload = {
            "schema_version": RUN_STORE_SCHEMA_VERSION,
            "run": run.to_dict(),
            "artifacts": self._artifact_manifest(run, run_dir),
        }
        _write_json_atomic(run_dir / "run.json", payload)

    def save_results(self, run: RunRecord) -> None:
        _write_json_atomic(self._run_dir(run.id) / "results.json", run.results)

    def append_log(self, run: RunRecord, entry: Mapping[str, Any]) -> None:
        run_dir = self._run_dir(run.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        with (run_dir / "logs.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(entry), sort_keys=True) + "\n")

    def replace_logs(self, run: RunRecord) -> None:
        run_dir = self._run_dir(run.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "logs.jsonl"
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            for entry in run.logs:
                handle.write(json.dumps(dict(entry), sort_keys=True) + "\n")
        os.replace(temp_path, path)

    def persist_artifacts(
        self,
        run_id: str,
        artifacts: Mapping[str, Path],
        results: dict[str, Any],
    ) -> dict[str, Path]:
        run_dir = self._run_dir(run_id)
        artifact_dir = run_dir / "artifacts"
        used_names: set[str] = set()
        durable: dict[str, Path] = {}
        for artifact_id, source in artifacts.items():
            if not isinstance(artifact_id, str) or not artifact_id.strip():
                continue
            source_path = source.expanduser().resolve()
            if not source_path.is_file():
                continue
            artifact_dir.mkdir(parents=True, exist_ok=True)
            filename = _safe_artifact_filename(
                artifact_id,
                source_path.name,
                used_names,
            )
            destination = (artifact_dir / filename).resolve()
            if source_path != destination:
                shutil.copy2(source_path, destination)
            durable[artifact_id] = destination
        _rewrite_result_artifact_sources(results, run_id=run_id, artifacts=durable)
        return durable

    def _load_run_dir(self, run_dir: Path) -> tuple[RunRecord, bool]:
        payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("run.json must contain an object")
        raw_run = payload.get("run") if isinstance(payload.get("run"), Mapping) else payload
        if not isinstance(raw_run, Mapping):
            raise ValueError("run record must contain an object")
        run_id = str(raw_run.get("id") or run_dir.name)
        if run_id != run_dir.name:
            run_id = run_dir.name
        lab_id = raw_run.get("lab_id")
        run = RunRecord(
            id=run_id,
            lab_id=str(lab_id) if lab_id is not None else "",
            parameters=(
                dict(raw_run.get("parameters"))
                if isinstance(raw_run.get("parameters"), Mapping)
                else None
            ),
            simulation_config=(
                dict(raw_run.get("simulation_config"))
                if isinstance(raw_run.get("simulation_config"), Mapping)
                else None
            ),
        )
        self._apply_run_metadata(run, raw_run)
        run.results = self._load_results(run_dir)
        run.logs = self._load_logs(run_dir)
        run.seq = max((int(entry.get("seq", 0)) for entry in run.logs), default=0)
        run.artifacts = self._load_artifacts(run_dir, payload.get("artifacts"))
        interrupted = run.status in ACTIVE_STATUSES
        if interrupted:
            run.status = "interrupted"
            run.error_message = RUN_INTERRUPTED_MESSAGE
            run.completed_at = run.completed_at or _now()
            run.add_log("warning", RUN_INTERRUPTED_MESSAGE, source="runtime")
        return run, interrupted

    def _apply_run_metadata(self, run: RunRecord, raw_run: Mapping[str, Any]) -> None:
        status = raw_run.get("status")
        if isinstance(status, str) and status.strip():
            run.status = status.strip()
        execution_target = raw_run.get("execution_target")
        if execution_target in {"local", "remote"}:
            run.execution_target = str(execution_target)
        for field_name in (
            "hub_run_id",
            "results_summary",
            "results_path",
            "error_message",
            "duration_seconds",
            "progress",
            "started_at",
            "completed_at",
            "created_at",
        ):
            value = raw_run.get(field_name)
            if field_name == "created_at" and not isinstance(value, str):
                continue
            setattr(run, field_name, value)

    def _load_results(self, run_dir: Path) -> dict[str, Any]:
        path = run_dir / "results.json"
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return dict(payload) if isinstance(payload, Mapping) else {}

    def _load_logs(self, run_dir: Path) -> list[dict[str, Any]]:
        path = run_dir / "logs.jsonl"
        if not path.is_file():
            return []
        logs: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, Mapping):
                logs.append(dict(payload))
        return logs

    def _load_artifacts(self, run_dir: Path, raw_artifacts: Any) -> dict[str, Path]:
        if not isinstance(raw_artifacts, Mapping):
            return {}
        artifacts: dict[str, Path] = {}
        for artifact_id, entry in raw_artifacts.items():
            if not isinstance(artifact_id, str):
                continue
            raw_path: Any
            if isinstance(entry, Mapping):
                raw_path = entry.get("path")
            else:
                raw_path = entry
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            path = Path(raw_path)
            if not path.is_absolute():
                path = run_dir / path
            artifacts[artifact_id] = path.expanduser().resolve()
        return artifacts

    def _artifact_manifest(self, run: RunRecord, run_dir: Path) -> dict[str, dict[str, str]]:
        artifacts: dict[str, dict[str, str]] = {}
        for artifact_id, path in run.artifacts.items():
            if not isinstance(artifact_id, str):
                continue
            resolved = path.expanduser().resolve()
            try:
                stored_path = resolved.relative_to(run_dir).as_posix()
            except ValueError:
                stored_path = str(resolved)
            artifacts[artifact_id] = {"path": stored_path}
        return artifacts

    def _run_dir(self, run_id: str) -> Path:
        return self.root / run_id


class LabServeSession:
    def __init__(self, lab_path: Path, *, install_deps: bool = True) -> None:
        self.lab_path = lab_path.expanduser().resolve()
        self.install_deps = install_deps
        self._lock = threading.RLock()
        self._runtime_prepare_lock = threading.Lock()
        self._run_store = _LocalRunStore(self.lab_path)
        self._runs: dict[str, RunRecord] = self._run_store.load_runs()
        self._runtime_port_payloads: dict[str, dict[str, list[dict[str, Any]]]] | None = None
        self._runtime_metadata_status = "pending"
        self._runtime_metadata_error: str | None = None
        self._runtime_metadata_thread: threading.Thread | None = None
        self._runtime_metadata_generation = 0

    def lab_payload(self) -> dict[str, Any]:
        record = workspace_get_lab(self.lab_path)
        manifest = _load_lab_manifest(self.lab_path)
        enriched_manifest = self._enriched_manifest(manifest)
        compute_warnings = _compute_warnings_for_manifest(manifest)
        execution = _execution_profile_for_lab(self.lab_path)
        self._ensure_runtime_metadata()
        runtime_status = self._runtime_metadata_snapshot()
        metadata = record.metadata or {}
        timestamp = _now()
        return {
            "id": record.id,
            "title": record.title or enriched_manifest.get("title") or record.package,
            "description": record.description,
            "tags": enriched_manifest.get("tags") if isinstance(enriched_manifest.get("tags"), list) else [],
            "file_path": str(record.path),
            "manifest": enriched_manifest,
            "compute_warnings": compute_warnings,
            "execution": execution,
            "wiring_layout": _load_wiring_layout(self.lab_path),
            "runtime_metadata_status": runtime_status["status"],
            "runtime_metadata_error": runtime_status["error"],
            "created_at": metadata.get("created_at") or timestamp,
            "updated_at": metadata.get("updated_at") or timestamp,
        }

    def _enriched_manifest(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        enriched = copy.deepcopy(dict(manifest))
        with self._lock:
            port_payloads = copy.deepcopy(self._runtime_port_payloads or {})
        models = enriched.get("models")
        if isinstance(models, list):
            for entry in models:
                if not isinstance(entry, dict):
                    continue
                self._enrich_model_entry(entry, port_payloads)
        children = enriched.get("children")
        if isinstance(children, list):
            for entry in children:
                if not isinstance(entry, dict):
                    continue
                self._enrich_child_entry(entry)
        return enriched

    def _runtime_metadata_snapshot(self) -> dict[str, str | None]:
        with self._lock:
            return {
                "status": self._runtime_metadata_status,
                "error": self._runtime_metadata_error,
            }

    def _ensure_runtime_metadata(self) -> None:
        with self._lock:
            if self._runtime_metadata_status != "pending":
                return
            self._runtime_metadata_status = "running"
            self._runtime_metadata_error = None
            generation = self._runtime_metadata_generation
            thread = threading.Thread(
                target=self._runtime_metadata_worker,
                args=(generation,),
                name="biosim-runtime-metadata",
                daemon=True,
            )
            self._runtime_metadata_thread = thread
        thread.start()

    def _invalidate_runtime_metadata(self) -> None:
        with self._lock:
            self._runtime_metadata_generation += 1
            self._runtime_port_payloads = None
            self._runtime_metadata_status = "pending"
            self._runtime_metadata_error = None

    def _runtime_metadata_worker(self, generation: int) -> None:
        try:
            port_payloads = self._introspected_ports()
        except Exception as exc:
            with self._lock:
                if generation != self._runtime_metadata_generation:
                    return
                self._runtime_metadata_status = "failed"
                self._runtime_metadata_error = str(exc)
            return
        with self._lock:
            if generation != self._runtime_metadata_generation:
                return
            self._runtime_port_payloads = port_payloads
            self._runtime_metadata_status = "ready"
            self._runtime_metadata_error = None

    def _introspected_ports(self) -> dict[str, dict[str, list[dict[str, Any]]]]:
        with (
            _package_file_for_lab(self.lab_path) as package_file,
            tempfile.TemporaryDirectory(prefix="biosim-serve-introspect-") as unpack_dir,
        ):
            with self._runtime_prepare_lock:
                prepared = prepare_lab_package(
                    package_file,
                    install_deps=False,
                    unpack_root=unpack_dir,
                )
            return _extract_world_ports(prepared.world)

    def _log_run(
        self,
        run: RunRecord,
        level: str,
        message: str,
        *,
        source: str = "biosimulant",
        echo_terminal: bool = False,
    ) -> None:
        with self._lock:
            self._append_run_log_locked(run, level, message, source=source)
        if echo_terminal:
            print(f"[{run.id}] {source}: {message}", flush=True)

    def _append_run_log_locked(
        self,
        run: RunRecord,
        level: str,
        message: str,
        *,
        source: str = "biosimulant",
    ) -> None:
        run.add_log(level, message, source=source)
        self._run_store.append_log(run, run.logs[-1])

    def _persist_run_locked(self, run: RunRecord) -> None:
        self._run_store.save_run(run)

    def _persist_run_results_locked(self, run: RunRecord) -> None:
        self._run_store.save_results(run)

    def _record_runtime_output_line(self, run: RunRecord, line: str) -> None:
        line = line.strip()
        if not line.startswith("BSIM_PROGRESS:"):
            return
        raw_payload = line.removeprefix("BSIM_PROGRESS:").strip()
        level = "info"
        source = "model"
        message = raw_payload
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(payload, Mapping):
                phase = payload.get("phase")
                if isinstance(phase, str) and phase.strip():
                    source = phase.strip()
                    if source == "error":
                        level = "error"
                raw_message = payload.get("message")
                if isinstance(raw_message, str) and raw_message.strip():
                    message = raw_message.strip()
                duration = payload.get("duration")
                if isinstance(duration, (int, float)):
                    message = f"{message} ({duration:g}s elapsed)"
        self._log_run(run, level, message, source=source)

    @contextmanager
    def _capture_run_output(self, run: RunRecord) -> Iterator[None]:
        stdout_bridge = _RunOutputBridge(
            sys.stdout,
            lambda line: self._record_runtime_output_line(run, line),
        )
        stderr_bridge = _RunOutputBridge(
            sys.stderr,
            lambda line: self._record_runtime_output_line(run, line),
        )
        with redirect_stdout(stdout_bridge), redirect_stderr(stderr_bridge):
            try:
                yield
            finally:
                stdout_bridge.flush()
                stderr_bridge.flush()

    def _enrich_model_entry(
        self,
        entry: dict[str, Any],
        port_payloads: Mapping[str, dict[str, list[dict[str, Any]]]],
    ) -> None:
        model_path = entry.get("path")
        resolved_model = None
        resolution_error = None
        if isinstance(model_path, str) and model_path.strip():
            try:
                model_dir = (self.lab_path / model_path).resolve()
                manifest = _safe_yaml_load(_model_manifest_path(model_dir).read_bytes())
                resolved_model = _resolved_model_from_manifest(manifest)
            except Exception as exc:
                resolution_error = str(exc)
        alias = entry.get("alias")
        if isinstance(alias, str) and alias in port_payloads:
            resolved_model = dict(resolved_model or {})
            resolved_model["io"] = {
                "inputs": port_payloads[alias].get("inputs", []),
                "outputs": port_payloads[alias].get("outputs", []),
            }
        entry["resolved_model"] = resolved_model
        entry["resolution_error"] = resolution_error

    def _enrich_child_entry(self, entry: dict[str, Any]) -> None:
        child_path = entry.get("path")
        if not isinstance(child_path, str) or not child_path.strip():
            entry["resolved_space"] = None
            return
        try:
            child_dir = (self.lab_path / child_path).resolve()
            manifest = _load_lab_manifest(child_dir)
            entry["resolved_space"] = _resolved_space_from_manifest(manifest)
            entry["resolution_error"] = None
        except Exception as exc:
            entry["resolved_space"] = None
            entry["resolution_error"] = str(exc)

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [run.to_dict() for run in self._runs.values()]

    def get_run(self, run_id: str) -> RunRecord:
        with self._lock:
            try:
                return self._runs[run_id]
            except KeyError as exc:
                raise PackageError(f"Run not found: {run_id}") from exc

    def get_run_artifact(self, run_id: str, artifact_id: str) -> Path:
        run = self.get_run(run_id)
        with self._lock:
            path = run.artifacts.get(artifact_id)
        if path is None:
            raise PackageError(f"Artifact not found for run {run_id}: {artifact_id}")
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise PackageError(f"Artifact file is no longer available: {artifact_id}")
        return resolved

    def _active_run_locked(self) -> RunRecord | None:
        for run in sorted(self._runs.values(), key=lambda item: item.created_at):
            if run.status in ACTIVE_STATUSES:
                return run
        return None

    def create_run(self, body: Mapping[str, Any]) -> RunRecord:
        with self._lock:
            active = self._active_run_locked()
            if active is not None:
                raise RunConflictError(
                    f"Run {active.id} is already {active.status}; cancel or wait for it before starting another local run."
                )
        lab = self.lab_payload()
        run = RunRecord(
            id=f"run-{uuid.uuid4().hex[:12]}",
            lab_id=str(lab["id"]),
            parameters=dict(body.get("parameters")) if isinstance(body.get("parameters"), Mapping) else None,
            simulation_config=dict(body.get("simulation_config")) if isinstance(body.get("simulation_config"), Mapping) else None,
        )
        with self._lock:
            active = self._active_run_locked()
            if active is not None:
                raise RunConflictError(
                    f"Run {active.id} is already {active.status}; cancel or wait for it before starting another local run."
                )
            self._runs[run.id] = run
            self._persist_run_locked(run)
        thread = threading.Thread(target=self._run_worker, args=(run,), daemon=True)
        run.thread = thread
        thread.start()
        return run

    def _compute_warnings_for_run(self, run: RunRecord) -> list[dict[str, str]]:
        manifest = _load_lab_manifest(self.lab_path)
        _apply_run_overrides(
            manifest,
            parameters=run.parameters,
            simulation_config=run.simulation_config,
        )
        return _compute_warnings_for_manifest(manifest)

    def _log_compute_warnings(self, run: RunRecord) -> None:
        for warning in self._compute_warnings_for_run(run):
            message = warning.get("message")
            if not message:
                continue
            self._log_run(
                run,
                "warning",
                message,
                source="compute",
                echo_terminal=True,
            )

    def cancel_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        with self._lock:
            if run.status not in ACTIVE_STATUSES:
                return run
            already_requested = run.cancel_requested
            run.cancel_requested = True
            run.status = "cancelling"
            if not already_requested:
                self._append_run_log_locked(run, "info", "Cancellation requested")
            self._persist_run_locked(run)
            world = run.world
            cancellation = run.cancellation
        if world is not None:
            try:
                world.request_stop()
            except Exception:
                pass
        cancellation.request_cancel()
        return run

    def update_model(self, alias: str, body: Mapping[str, Any]) -> dict[str, Any]:
        manifest = _load_lab_manifest(self.lab_path)
        models = manifest.get("models")
        if not isinstance(models, list):
            raise PackageError("Lab manifest has no models array")
        target = None
        for entry in models:
            if isinstance(entry, dict) and entry.get("alias") == alias:
                target = entry
                break
        if target is None:
            raise PackageError(f"Model alias not found: {alias}")
        new_alias = body.get("alias")
        if isinstance(new_alias, str):
            new_alias = new_alias.strip()
        else:
            new_alias = None
        if new_alias and new_alias != alias:
            if any(
                isinstance(entry, Mapping) and entry.get("alias") == new_alias
                for entry in models
            ):
                raise PackageError(f"Another model already uses alias '{new_alias}'")
            target["alias"] = new_alias
            _rewrite_alias_references(manifest, alias, new_alias)
        for key in ("parameters",):
            if key in body:
                if body[key] is None:
                    target.pop(key, None)
                elif isinstance(body[key], Mapping):
                    target[key] = dict(body[key])
                else:
                    target[key] = body[key]
        _write_lab_manifest(self.lab_path, manifest)
        workspace_save_lab(self.lab_path, allow_draft=True)
        self._invalidate_runtime_metadata()
        return self.lab_payload()

    def update_world(self, body: Mapping[str, Any]) -> dict[str, Any]:
        manifest = _load_lab_manifest(self.lab_path)
        if not isinstance(manifest, dict):
            raise PackageError("Lab manifest is not an object")
        if "inputs" in body or "outputs" in body:
            io = manifest.setdefault("io", {})
            if not isinstance(io, dict):
                io = {}
                manifest["io"] = io
            if "inputs" in body:
                io["inputs"] = body["inputs"]
            if "outputs" in body:
                io["outputs"] = body["outputs"]
        if "runtime" in body:
            manifest["runtime"] = body["runtime"]
        if "wiring" in body:
            manifest["wiring"] = body["wiring"]
        _write_lab_manifest(self.lab_path, manifest)
        workspace_save_lab(self.lab_path, allow_draft=True)
        self._invalidate_runtime_metadata()
        return self.lab_payload()

    def save_layout(self, body: Mapping[str, Any]) -> dict[str, Any]:
        workspace_save_lab(
            self.lab_path,
            wiring_layout={"nodes": body.get("nodes", [])},
            allow_draft=True,
        )
        return self.lab_payload()

    def _run_worker(self, run: RunRecord) -> None:
        started = time.time()
        with self._lock:
            run.status = "cancelling" if run.cancel_requested else "running"
            run.started_at = _now()
            self._persist_run_locked(run)
        self._log_run(run, "info", "Run started", echo_terminal=True)
        self._log_compute_warnings(run)
        try:
            result = self._execute_run(run)
            finished = time.time()
            with self._lock:
                if run.cancel_requested:
                    run.status = "cancelled"
                    completion_log = ("info", "Run cancelled")
                else:
                    run.status = "completed"
                    completion_log = ("info", "Run completed")
                run.results = result
                run.results_summary = {
                    "visual_modules": len(result.get("visuals", [])),
                }
                run.duration_seconds = finished - started
                run.completed_at = _now()
                run.world = None
                self._persist_run_results_locked(run)
                self._persist_run_locked(run)
            self._log_run(
                run,
                completion_log[0],
                completion_log[1],
                echo_terminal=True,
            )
        except RunCancelled:
            self._finalize_cancelled_run(run, started)
        except Exception as exc:
            if run.cancel_requested:
                run.cancellation.request_cancel()
                self._finalize_cancelled_run(run, started)
                return
            finished = time.time()
            with self._lock:
                run.status = "failed"
                run.error_message = str(exc)
                run.duration_seconds = finished - started
                run.completed_at = _now()
                run.world = None
                self._persist_run_locked(run)
            self._log_run(run, "error", str(exc), source="runtime", echo_terminal=True)

    def _finalize_cancelled_run(self, run: RunRecord, started: float) -> None:
        finished = time.time()
        with self._lock:
            run.status = "cancelled"
            run.duration_seconds = finished - started
            run.completed_at = _now()
            run.world = None
            run.error_message = None
            self._persist_run_locked(run)
        self._log_run(run, "info", "Run cancelled", echo_terminal=True)

    def _execute_run(self, run: RunRecord) -> dict[str, Any]:
        run.cancellation.check_cancelled()
        with tempfile.TemporaryDirectory(prefix="biosim-serve-run-") as temp_dir:
            source = Path(temp_dir) / "lab"
            shutil.copytree(self.lab_path, source, ignore=_copytree_ignore_run_history)
            manifest = _load_lab_manifest(source)
            _apply_run_overrides(
                manifest,
                parameters=run.parameters,
                simulation_config=run.simulation_config,
            )
            _write_lab_manifest(source, manifest)
            with (
                _package_file_for_lab(source) as package_file,
                tempfile.TemporaryDirectory(prefix="biosim-serve-unpack-") as unpack_dir,
            ):
                self._log_run(
                    run,
                    "info",
                    "Preparing runtime/dependencies...",
                    source="runtime",
                    echo_terminal=True,
                )
                lock_acquired = self._runtime_prepare_lock.acquire(blocking=False)
                if not lock_acquired:
                    self._log_run(
                        run,
                        "info",
                        "Waiting for existing runtime preparation...",
                        source="runtime",
                        echo_terminal=True,
                    )
                    while not lock_acquired:
                        run.cancellation.check_cancelled()
                        lock_acquired = self._runtime_prepare_lock.acquire(timeout=0.1)
                try:
                    run.cancellation.check_cancelled()
                    prepared = prepare_lab_package(
                        package_file,
                        install_deps=self.install_deps,
                        unpack_root=unpack_dir,
                        dependency_logger=lambda line: self._log_run(
                            run,
                            "info",
                            line,
                            source="pip",
                            echo_terminal=True,
                        ),
                        dependency_process_tracker=run.cancellation.track_process,
                        cancel_checker=run.cancellation.check_cancelled,
                    )
                finally:
                    if lock_acquired:
                        self._runtime_prepare_lock.release()
                run.cancellation.check_cancelled()
                self._log_run(
                    run,
                    "info",
                    "Runtime prepared",
                    source="runtime",
                    echo_terminal=True,
                )
                world = prepared.world

                def listener(event: WorldEvent, payload: dict[str, Any]) -> None:
                    if event == WorldEvent.STEP:
                        now = time.time()
                        pct = float(payload.get("progress_pct") or 0.0)
                        progress = {
                            "t": payload.get("t"),
                            "start": payload.get("start"),
                            "end": payload.get("end"),
                            "duration": payload.get("duration"),
                            "remaining": payload.get("remaining"),
                            "progress": payload.get("progress"),
                            "progress_pct": pct,
                        }
                        with self._lock:
                            run.progress = progress
                            should_log = (
                                now - run.last_progress_log_at >= 2.0
                                or pct - run.last_progress_log_pct >= 10.0
                                or pct >= 100.0
                            )
                            if should_log:
                                run.last_progress_log_at = now
                                run.last_progress_log_pct = pct
                                self._append_run_log_locked(
                                    run,
                                    "info",
                                    f"simulation window progress ({pct:.1f}%)",
                                    source="world",
                                )
                                self._persist_run_locked(run)
                        return
                    with self._lock:
                        if event == WorldEvent.STARTED:
                            run.progress = {
                                "t": payload.get("t"),
                                "start": payload.get("start"),
                                "end": payload.get("end"),
                                "duration": payload.get("duration"),
                                "remaining": payload.get("remaining"),
                                "progress": payload.get("progress"),
                                "progress_pct": payload.get("progress_pct"),
                            }
                        message = event.value
                        if event == WorldEvent.STARTED:
                            message = "simulation window started"
                        elif event == WorldEvent.FINISHED:
                            message = "simulation window complete"
                        self._append_run_log_locked(run, "info", message, source="world")
                        self._persist_run_locked(run)

                world.on(listener)
                with self._lock:
                    run.world = world
                self._log_run(
                    run,
                    "info",
                    "Simulation started",
                    source="runtime",
                    echo_terminal=True,
                )
                try:
                    run.cancellation.check_cancelled()
                    with self._capture_run_output(run):
                        world.run(duration=prepared.duration)
                    run.cancellation.check_cancelled()
                    if prepared.settle_steps:
                        self._log_run(
                            run,
                            "info",
                            "Finalizing outputs...",
                            source="runtime",
                            echo_terminal=True,
                        )
                        with self._capture_run_output(run):
                            world.settle(prepared.settle_steps)
                        run.cancellation.check_cancelled()
                        self._log_run(
                            run,
                            "info",
                            "Output finalization complete",
                            source="runtime",
                            echo_terminal=True,
                        )
                    self._log_run(
                        run,
                        "info",
                        "Collecting visuals...",
                        source="runtime",
                        echo_terminal=True,
                    )
                    run.cancellation.check_cancelled()
                    artifacts: dict[str, Path] = {}
                    with self._capture_run_output(run):
                        visuals = _sanitize_visuals(
                            world.collect_visuals(),
                            run_id=run.id,
                            artifacts=artifacts,
                        )
                    result = {
                        "visuals": visuals,
                        "duration": prepared.duration,
                        "communication_step": prepared.communication_step,
                        "settle_steps": prepared.settle_steps,
                        "modules": prepared.modules,
                    }
                    durable_artifacts = self._run_store.persist_artifacts(
                        run.id,
                        artifacts,
                        result,
                    )
                    with self._lock:
                        run.artifacts = durable_artifacts
                    run.cancellation.check_cancelled()
                    return result
                finally:
                    world.off(listener)


def create_app(session: LabServeSession) -> FastAPI:
    app = FastAPI(title="Biosimulant Labs Serve")

    @app.exception_handler(RunConflictError)
    async def run_conflict_handler(_request: Request, exc: RunConflictError) -> JSONResponse:
        return _api_error(str(exc), status_code=409)

    @app.exception_handler(PackageError)
    async def package_error_handler(_request: Request, exc: PackageError) -> JSONResponse:
        return _api_error(str(exc), status_code=400)

    @app.exception_handler(Exception)
    async def generic_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        return _api_error(str(exc), status_code=500)

    assets_dir = STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="labs_serve_assets")

    @app.get("/")
    @app.get("/labs-serve.html")
    def index() -> FileResponse:
        html = STATIC_DIR / "index.html"
        legacy_html = STATIC_DIR / "labs-serve.html"
        if not html.is_file() and legacy_html.is_file():
            html = legacy_html
        if not html.is_file():
            raise PackageError("The labs serve UI bundle is missing")
        return FileResponse(html, media_type="text/html; charset=utf-8")

    @app.get("/ui")
    @app.get("/ui/")
    def legacy_ui_redirect() -> RedirectResponse:
        return RedirectResponse("/", status_code=307)

    @app.get("/api/lab")
    def get_lab() -> JSONResponse:
        return _api_ok({"lab": session.lab_payload()})

    @app.get("/api/runs")
    def list_runs() -> JSONResponse:
        return _api_ok({"runs": session.list_runs()})

    @app.post("/api/runs")
    async def create_run(request: Request) -> JSONResponse:
        body = await _request_object(request)
        if isinstance(body, JSONResponse):
            return body
        run = session.create_run(body)
        return _api_ok({"run": run.to_dict()}, status_code=201)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> JSONResponse:
        return _api_ok({"run": session.get_run(run_id).to_dict()})

    @app.get("/api/runs/{run_id}/results")
    def get_run_results(run_id: str) -> JSONResponse:
        return _api_ok({"results": session.get_run(run_id).results})

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
    def get_run_artifact(run_id: str, artifact_id: str) -> FileResponse:
        path = session.get_run_artifact(run_id, artifact_id)
        return FileResponse(path)

    @app.get("/api/runs/{run_id}/logs")
    def get_run_logs(run_id: str, since_seq: int | None = None) -> JSONResponse:
        logs = session.get_run(run_id).logs
        if since_seq is not None:
            logs = [entry for entry in logs if int(entry.get("seq", 0)) > since_seq]
        return _api_ok({"logs": logs})

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> JSONResponse:
        run = session.cancel_run(run_id)
        return _api_ok({"run": run.to_dict(), "cancelled": True})

    @app.put("/api/lab/models/{alias}")
    async def update_model(alias: str, request: Request) -> JSONResponse:
        body = await _request_object(request)
        if isinstance(body, JSONResponse):
            return body
        return _api_ok({"lab": session.update_model(alias, body)})

    @app.put("/api/lab/world")
    async def update_world(request: Request) -> JSONResponse:
        body = await _request_object(request)
        if isinstance(body, JSONResponse):
            return body
        return _api_ok({"lab": session.update_world(body)})

    @app.put("/api/lab/layout")
    async def save_layout(request: Request) -> JSONResponse:
        body = await _request_object(request)
        if isinstance(body, JSONResponse):
            return body
        return _api_ok({"lab": session.save_layout(body)})

    return app


def serve_lab(
    lab_path: Path,
    *,
    host: str,
    port: int,
    open_browser: bool,
    install_deps: bool = True,
    emit_json: bool = False,
) -> None:
    import uvicorn

    session = LabServeSession(lab_path, install_deps=install_deps)
    app = create_app(session)
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(128)
    except OSError as exc:
        sock.close()
        raise PackageError(
            f"Could not bind labs serve to {host}:{port}: {exc}. Use --port to choose another port."
        ) from exc

    actual_port = int(sock.getsockname()[1])
    url = _display_url(host, actual_port)
    if emit_json:
        print(
            json.dumps(
                {
                    "command": "serve",
                    "serving": True,
                    "url": url,
                    "host": host,
                    "port": actual_port,
                }
            ),
            flush=True,
        )
    print(f"Starting Biosimulant lab UI: {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    config = uvicorn.Config(app, host=host, port=actual_port, access_log=False)
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        return
