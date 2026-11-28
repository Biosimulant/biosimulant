"""Bounded setup execution shared by local runners and hosted image builders."""

from __future__ import annotations

import json
import os
import platform
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any

if __package__:
    from .runtime_setup_contract import (
        SetupError,
        include_source,
        setup_plan,
        MAX_CONTEXT_BYTES,
        MAX_CONTEXT_FILES,
    )
else:  # Exact generated copy beside the hosted environment builder.
    from runtime_setup_contract import (
        SetupError,
        include_source,
        setup_plan,
        MAX_CONTEXT_BYTES,
        MAX_CONTEXT_FILES,
    )

PREPARED_ENV = "BIOSIM_SETUP_RECORD"


def source_files(root: Path) -> dict[str, bytes]:
    files, size = {}, 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in dirs:
            if (base / name).is_symlink() and include_source(
                (base / name).relative_to(root).as_posix()
            ):
                raise SetupError(
                    "runtime_setup_context_invalid",
                    f"Setup source cannot contain symbolic links: {base / name}",
                )
        dirs[:] = sorted(
            name
            for name in dirs
            if include_source((base / name).relative_to(root).as_posix())
            and not (base / name).is_symlink()
        )
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if not include_source(relative):
                continue
            if path.is_symlink():
                raise SetupError(
                    "runtime_setup_context_invalid",
                    f"Setup source cannot contain symbolic links: {relative}",
                )
            size += path.stat().st_size
            if size > MAX_CONTEXT_BYTES or len(files) >= MAX_CONTEXT_FILES:
                raise SetupError(
                    "runtime_setup_context_too_large",
                    "Setup source context exceeds 32 MiB or 5000 files.",
                )
            files[relative] = path.read_bytes()
    return files


def plan_for_directory(root: Path) -> dict[str, Any] | None:
    import yaml

    # Legacy Labs need no setup context (and may contain large input files).
    manifests = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = [
            name
            for name in dirs
            if include_source((base / name).relative_to(root).as_posix())
            and not (base / name).is_symlink()
        ]
        for name in names:
            if name in ("lab.yaml", "lab.yml"):
                path = base / name
                manifests[path.relative_to(root).as_posix()] = path.read_bytes()
    if not any(
        isinstance(lab := yaml.safe_load(raw), dict)
        and isinstance(lab.get("runtime"), dict)
        and "setup" in lab["runtime"]
        for raw in manifests.values()
    ):
        return None
    return setup_plan(source_files(root), load_yaml=yaml.safe_load)


def identity(plan: Mapping, python: str | Path) -> dict:
    executable = shutil.which(str(python)) or str(python)
    # Normalize directory aliases (macOS /var -> /private/var), while keeping
    # the final interpreter symlink: resolving that would erase venv identity.
    executable_path = Path(executable).absolute()
    executable = executable_path.parent.resolve() / executable_path.name
    return {
        "setup_sha256": plan["setup_sha256"],
        "python": os.path.normcase(str(executable)),
        "platform": sys.platform,
        "machine": platform.machine(),
        "runner_version": 1,
    }


def require_prepared(
    plan: Mapping | None, *, python: str | Path = sys.executable
) -> None:
    if plan is None:
        return
    record_path = os.environ.get(PREPARED_ENV)
    try:
        record = json.loads(Path(record_path or "").read_text())
    except (OSError, ValueError):
        record = {}
    expected = identity(plan, python)
    if record.get("identity") != expected or record.get("status") != "succeeded":
        raise SetupError(
            "runtime_setup_not_prepared",
            "The current environment was not prepared for this Lab. Use biosimulant labs run/serve with preparation enabled, or start a new managed run.",
        )


def require_supported_environment(manifest: Mapping) -> None:
    """A local venv cannot honor a container base or hosted source/lock recipe."""
    runtime = manifest.get("runtime") or {}
    if isinstance(runtime, Mapping) and runtime.get("environment"):
        if (
            not os.environ.get(PREPARED_ENV)
            and os.environ.get("BIOSIM_REMOTE_EXECUTION") != "1"
        ):
            raise SetupError(
                "runtime_environment_requires_cloud",
                "runtime.environment base/source/lock recipes require a prepared hosted environment. The local runner cannot silently ignore this declaration.",
                field="runtime.environment",
            )


def preparation_environment(
    python: Path, inherited: Mapping[str, str]
) -> dict[str, str]:
    env = dict(inherited)
    record = python.parent.parent / "setup-record.json"
    if record.is_file():
        env[PREPARED_ENV] = str(record)
        env["BIOSIM_ENV_PREFIX"] = str(record.parent)
        env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    return env


def _scrub(text: str) -> str:
    text = re.sub(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", r"\1[redacted]@", text)
    return re.sub(r"(https?://[^\s?#]+)[?#][^\s]+", r"\1?[redacted]", text)


def _terminate(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.wait(timeout=10)


def _windows_job(process):
    """Kill descendants when the owning handle closes, including after parent exit."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("min_working", ctypes.c_size_t),
            ("max_working", ctypes.c_size_t),
            ("active", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", ctypes.c_uint64 * 6),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if (
        not handle
        or not kernel.SetInformationJobObject(
            handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        )
        or not kernel.AssignProcessToJobObject(handle, int(process._handle))
    ):
        if handle:
            kernel.CloseHandle(handle)
        _terminate(process)
        raise SetupError(
            "runtime_setup_process_control_unavailable",
            "Windows could not attach setup to a process job for cancellation.",
        )
    return lambda: kernel.CloseHandle(handle)


def execute_setup(
    plan: Mapping,
    root: Path,
    *,
    python: str | Path,
    prefix: Path,
    logger: Callable[[str], None] | None = None,
    process_tracker: Callable | None = None,
    cancel_checker: Callable | None = None,
    total_timeout_seconds: int = 3600,
) -> dict:
    """Execute a validated plan. No implicit shell, privilege escalation or retry."""
    root, prefix = root.resolve(), prefix.resolve()
    python = Path(os.path.abspath(shutil.which(str(python)) or str(python)))
    prefix.mkdir(parents=True, exist_ok=True)
    record_path = prefix / "setup-record.json"
    record_path.unlink(missing_ok=True)
    env = dict(os.environ)
    env.pop(PREPARED_ENV, None)
    env["BIOSIM_ENV_PREFIX"] = str(prefix)
    env["PYTHONUNBUFFERED"] = "1"
    env["PATH"] = os.pathsep.join(
        [
            str(Path(python).parent),
            str(prefix / ("Scripts" if os.name == "nt" else "bin")),
            env.get("PATH", ""),
        ]
    )
    deadline = time.monotonic() + total_timeout_seconds
    results = []
    for step in plan["steps"]:
        if cancel_checker:
            cancel_checker()
        cwd = (root / step["root"]).resolve()
        if not cwd.is_relative_to(root):
            raise SetupError(
                "runtime_setup_context_invalid",
                "Setup working directory escaped the Lab.",
            )
        command = list(step["command"])
        if command[0] == "python":
            command[0] = str(python)
        if "/" in command[0] or "\\" in command[0]:
            if step["command"][0] != "python":
                executable = (cwd / command[0]).resolve()
                if not executable.is_relative_to(root):
                    raise SetupError(
                        "runtime_setup_context_invalid",
                        "Setup executable escaped the Lab.",
                    )
                command[0] = str(executable)
        env["BIOSIM_LAB_ROOT"] = str(cwd)
        started = time.monotonic()
        limit = min(deadline, started + step["timeout_seconds"])
        if limit <= started:
            raise SetupError(
                "runtime_setup_timeout", "The overall setup deadline was exceeded."
            )
        if logger:
            logger(f"Preparing environment: setup in {step['root'] or '.'}")
        kwargs = (
            {"start_new_session": True}
            if os.name == "posix"
            else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
        except OSError as exc:
            raise SetupError(
                "runtime_setup_executable_unavailable",
                f"Cannot start setup in {step['root'] or '.'}: {exc}",
            ) from exc
        close_job = _windows_job(process)
        output: queue.Queue[bytes | None] = queue.Queue(maxsize=128)
        stop = threading.Event()
        tail: deque[str] = deque(maxlen=40)

        def read():
            try:
                while not stop.is_set():
                    chunk = process.stdout.read1(4096)
                    if not chunk:
                        break
                    while not stop.is_set():
                        try:
                            output.put(chunk, timeout=0.1)
                            break
                        except queue.Full:
                            pass
            finally:
                while not stop.is_set():
                    try:
                        output.put(None, timeout=0.1)
                        break
                    except queue.Full:
                        pass

        reader = threading.Thread(target=read, daemon=True)
        step_succeeded = False
        with process_tracker(process) if process_tracker else nullcontext():
            reader.start()
            try:
                while True:
                    if cancel_checker:
                        cancel_checker()
                    if time.monotonic() >= limit:
                        raise SetupError(
                            "runtime_setup_timeout",
                            f"Setup timed out in {step['root'] or '.'}.",
                        )
                    try:
                        chunk = output.get(timeout=0.1)
                    except queue.Empty:
                        if process.poll() is not None:
                            # Background descendants must not keep inherited pipes
                            # open after their setup entry point has exited.
                            if close_job:
                                close_job()
                                close_job = None
                            else:
                                _terminate(process)
                        continue
                    if chunk is None:
                        break
                    text = _scrub(chunk.decode("utf-8", errors="replace"))
                    tail.append(text)
                    if logger:
                        logger(text.rstrip())
                while process.poll() is None:
                    if cancel_checker:
                        cancel_checker()
                    if time.monotonic() >= limit:
                        raise SetupError(
                            "runtime_setup_timeout",
                            f"Setup timed out in {step['root'] or '.'}.",
                        )
                    stop.wait(0.05)
                if process.returncode:
                    raise SetupError(
                        "runtime_setup_failed",
                        f"Setup failed in {step['root'] or '.'} (exit {process.returncode}):\n"
                        + "".join(tail)[-12000:],
                    )
                results.append(
                    {
                        "root": step["root"],
                        "exit_code": 0,
                        "duration_seconds": time.monotonic() - started,
                    }
                )
                step_succeeded = True
            finally:
                if not step_succeeded:
                    record_path.unlink(missing_ok=True)
                stop.set()
                if close_job:
                    close_job()
                _terminate(process)
                reader.join(timeout=5)
                process.stdout.close()
    record = {
        "record_version": 1,
        "status": "succeeded",
        "identity": identity(plan, python),
        "steps": results,
    }
    temporary = record_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, sort_keys=True))
    temporary.replace(record_path)
    return record
