from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .__about__ import __version__
from .pack import (
    PackageError,
    _current_python_minor,
    _declared_lab_python_version,
    _loaded_package_from_path,
    run_package,
)

BIOSIM_MANAGED_RUNTIME_CHILD_ENV = "BIOSIM_MANAGED_RUNTIME_CHILD"
BIOSIM_RUNTIME_CACHE_ENV = "BIOSIM_RUNTIME_CACHE_DIR"
BIOSIM_UV_PATH_ENV = "BIOSIM_UV_PATH"
DEFAULT_RUNTIME_CACHE = Path.home() / ".cache" / "biosim" / "runtimes"


RunPackage = Callable[[str | Path], dict[str, Any]]


def run_package_with_managed_python(
    package_file: str | Path,
    *,
    install_deps: bool = True,
    dependency_root: str | Path | None = None,
    in_process_runner: RunPackage | None = None,
) -> dict[str, Any]:
    runner = in_process_runner or (
        lambda path: run_package(path, install_deps=install_deps)
    )
    if os.environ.get(BIOSIM_MANAGED_RUNTIME_CHILD_ENV) == "1":
        return runner(package_file)

    requested = requested_package_python_version(package_file)
    if requested is None or requested == _current_python_minor():
        return runner(package_file)

    python_path = ensure_executor_python(requested)
    _log_managed_python_handoff("package run", requested, python_path)
    if dependency_root is None:
        return run_child_package(python_path, package_file, install_deps=install_deps)
    return run_child_package(
        python_path,
        package_file,
        install_deps=install_deps,
        dependency_root=dependency_root,
    )


def run_labs_serve_with_managed_python(
    package_file: str | Path,
    argv: Sequence[str],
) -> int | None:
    if os.environ.get(BIOSIM_MANAGED_RUNTIME_CHILD_ENV) == "1":
        return None

    requested = requested_package_python_version(package_file)
    if requested is None or requested == _current_python_minor():
        return None

    python_path = ensure_executor_python(requested)
    env = dict(os.environ)
    env[BIOSIM_MANAGED_RUNTIME_CHILD_ENV] = "1"
    _log_managed_python_handoff("labs serve", requested, python_path)
    completed = subprocess.run(
        [str(python_path), "-m", "biosim", *argv],
        env=env,
    )
    return completed.returncode


def _log_managed_python_handoff(command: str, python_version: str, python_path: Path) -> None:
    _log_runtime_status(
        f"Re-launching {command} under managed Python {python_version}: {python_path}"
    )


def _log_runtime_status(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def requested_package_python_version(package_file: str | Path) -> str | None:
    with tempfile.TemporaryDirectory(prefix="biosim-runtime-inspect-") as temp_dir:
        loaded = _loaded_package_from_path(
            Path(package_file).expanduser().resolve(),
            unpack_root=Path(temp_dir),
        )
        if loaded.package_type != "lab":
            return None
        return _declared_lab_python_version(loaded.manifest)


def ensure_executor_python(python_version: str) -> Path:
    cache_root = _runtime_cache_root()
    _log_runtime_status(
        f"Preparing managed Python runtime {python_version} in {cache_root}"
    )
    uv_command = _uv_command()
    base_python = _ensure_python_install(uv_command, cache_root, python_version)
    venv_dir = cache_root / "executor-venvs" / python_version
    venv_python = _venv_python_path(venv_dir)
    expected_marker = _runtime_marker_payload(python_version)
    marker = venv_dir / ".biosim-runtime.json"

    if venv_python.exists() and marker.exists():
        try:
            if json.loads(marker.read_text(encoding="utf-8")) == expected_marker:
                _log_runtime_status(f"Using cached managed runtime: {venv_python}")
                return venv_python
        except (OSError, json.JSONDecodeError):
            pass

    if venv_dir.exists():
        _log_runtime_status(f"Removing stale managed runtime: {venv_dir}")
        shutil.rmtree(venv_dir)
    _log_runtime_status(f"Creating managed runtime venv: {venv_dir}")
    _run_uv(
        uv_command,
        ["venv", "--python", str(base_python), str(venv_dir)],
        "uv failed to create the Biosimulant managed runtime venv",
    )
    _install_biosimulant(uv_command, venv_python)
    marker.write_text(
        json.dumps(expected_marker, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return venv_python


def run_child_package(
    python_path: Path,
    package_file: str | Path,
    *,
    install_deps: bool,
    dependency_root: str | Path | None = None,
) -> dict[str, Any]:
    code = (
        "import json, sys; "
        "from biosim.pack import run_package; "
        "root = sys.argv[3] if len(sys.argv) > 3 else None; "
        "result = run_package(sys.argv[1], install_deps=(sys.argv[2] == '1'), dependency_root=root); "
        "print(json.dumps(result, sort_keys=True))"
    )
    env = dict(os.environ)
    env[BIOSIM_MANAGED_RUNTIME_CHILD_ENV] = "1"
    # Line-buffer the child so its progress reaches the parent while it runs.
    env["PYTHONUNBUFFERED"] = "1"
    command = [
        str(python_path),
        "-c",
        code,
        str(package_file),
        "1" if install_deps else "0",
    ]
    if dependency_root is not None:
        command.append(str(dependency_root))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    relay_lock = threading.Lock()
    readers = [
        threading.Thread(
            target=_relay_child_stream,
            args=(process.stdout, stdout_lines, relay_lock),
            kwargs={"skip_json_result": True},
            daemon=True,
        ),
        threading.Thread(
            target=_relay_child_stream,
            args=(process.stderr, stderr_lines, relay_lock),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    returncode = process.wait()
    for reader in readers:
        reader.join()
    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
    if returncode != 0:
        raise PackageError(
            "Managed Python runtime failed to run the package.\n"
            f"stdout:\n{_tail(stdout)}\n"
            f"stderr:\n{_tail(stderr)}"
        )
    return _parse_json_result(stdout)


def _relay_child_stream(
    stream: Any,
    lines: list[str],
    lock: threading.Lock,
    *,
    skip_json_result: bool = False,
) -> None:
    """Collect a child stream and relay each line to stderr as it arrives.

    The parent's stdout is reserved for its own result (for example ``--json``),
    so child output goes to stderr. The child's final JSON result line is kept
    but not relayed.
    """
    if stream is None:
        return
    for line in stream:
        lines.append(line)
        if skip_json_result and _is_json_object_line(line):
            continue
        with lock:
            sys.stderr.write(line if line.endswith("\n") else f"{line}\n")
            sys.stderr.flush()


def _is_json_object_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return False
    try:
        return isinstance(json.loads(stripped), dict)
    except json.JSONDecodeError:
        return False


def _runtime_cache_root() -> Path:
    raw = os.environ.get(BIOSIM_RUNTIME_CACHE_ENV)
    return Path(raw).expanduser().resolve() if raw else DEFAULT_RUNTIME_CACHE


def _uv_command() -> list[str]:
    configured = os.environ.get(BIOSIM_UV_PATH_ENV)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return [str(path)]
        raise PackageError(f"{BIOSIM_UV_PATH_ENV} does not point to a file: {path}")

    discovered = shutil.which("uv")
    if discovered:
        return [discovered]

    probe = subprocess.run(
        [sys.executable, "-m", "uv", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode == 0:
        return [sys.executable, "-m", "uv"]

    raise PackageError(
        "uv is required for OSS managed Python runtimes. "
        "Install Biosimulant with uv support or set BIOSIM_UV_PATH."
    )


def _ensure_python_install(
    uv_command: list[str],
    cache_root: Path,
    python_version: str,
) -> Path:
    python_dir = cache_root / "python" / python_version
    marker = python_dir / ".ready"
    if marker.exists():
        candidate = Path(marker.read_text(encoding="utf-8").strip())
        if candidate.exists():
            _log_runtime_status(f"Using cached Python {python_version}: {candidate}")
            return candidate

    python_dir.mkdir(parents=True, exist_ok=True)
    _log_runtime_status(f"Installing Python {python_version} with uv: {python_dir}")
    _run_uv(
        uv_command,
        ["python", "install", python_version, "--install-dir", str(python_dir)],
        f"uv failed to install Python {python_version}",
    )
    python_path = _find_python_binary(python_dir, python_version)
    marker.write_text(str(python_path) + "\n", encoding="utf-8")
    return python_path


def _install_biosimulant(uv_command: list[str], python_path: Path) -> None:
    source_spec = _local_source_spec()
    spec = source_spec or f"biosimulant=={__version__}"
    _log_runtime_status(f"Installing Biosimulant into managed runtime: {python_path}")
    args = ["pip", "install"]
    if source_spec is not None and Path(source_spec).is_dir():
        args.extend(["-e", spec])
    else:
        args.append(spec)
    args.extend(["--python", str(python_path)])
    _run_uv(
        uv_command,
        args,
        "uv failed to install Biosimulant into the managed runtime",
    )


def _local_source_spec() -> str | None:
    configured = os.environ.get("BIOSIMULANT_MANAGED_SOURCE_SPEC", "").strip()
    if configured:
        source = Path(configured).expanduser().resolve()
        if source.is_file() and source.suffix == ".whl":
            return str(source)
        if source.is_dir() and (source / "pyproject.toml").is_file():
            return str(source)
        raise PackageError(
            "BIOSIMULANT_MANAGED_SOURCE_SPEC must reference a wheel or project directory"
        )
    repo_root = Path(__file__).resolve().parents[2]
    if (
        (repo_root / "pyproject.toml").is_file()
        and (repo_root / "src" / "biosim").is_dir()
    ):
        return str(repo_root)
    return None


def _runtime_marker_payload(python_version: str) -> dict[str, str]:
    return {
        "biosimulant": _local_source_spec() or f"biosimulant=={__version__}",
        "python_version": python_version,
    }


def _run_uv(uv_command: list[str], args: list[str], error_message: str) -> None:
    command = [*uv_command, *args]
    _log_runtime_status(f"Running: {shlex.join(command)}")
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise PackageError(f"{error_message}: {exc}") from exc

    output_lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        output_lines.append(line.rstrip("\n"))
        output_lines = output_lines[-80:]
        sys.stderr.write(line)
        sys.stderr.flush()

    returncode = process.wait()
    if returncode != 0:
        raise PackageError(
            f"{error_message}.\n"
            f"command: {shlex.join(command)}\n"
            f"output:\n{_tail_lines(output_lines)}"
        )


def _find_python_binary(root: Path, python_version: str) -> Path:
    names = (
        ["python.exe"]
        if os.name == "nt"
        else [f"python{python_version}", "python3", "python"]
    )
    for name in names:
        for candidate in root.rglob(name):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    raise PackageError(f"Could not locate Python {python_version} after uv install")


def _venv_python_path(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _parse_json_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise PackageError(
        "Managed Python runtime completed without a JSON package result.\n"
        f"stdout:\n{_tail(stdout)}"
    )


def _tail(text: str, *, lines: int = 40) -> str:
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _tail_lines(lines: list[str], *, count: int = 40) -> str:
    return "\n".join(lines[-count:])
