"""Record a real external execution without confusing it with managed evidence.

Training and file preparation retain their native commands. The recorder keeps
their inputs, environment, outcome and outputs for import into ordinary Runs.
It does not confer a Passport or make a scientific validation claim.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:password|passwd|secret|token|credential|credentials|authorization|api[_-]?key|access[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
_REDACTED = "[REDACTED]"


def _safe_provenance(command, configuration, environment):
    """Remove credential fields and supplied secret values before persistence."""
    secrets = sorted(
        {
            value
            for key, value in (environment or {}).items()
            if value and _SECRET_KEY.search(key)
        },
        key=len,
        reverse=True,
    )

    def text(value):
        for secret in secrets:
            value = value.replace(secret, _REDACTED)
        return value

    def clean(value):
        if isinstance(value, Mapping):
            return {
                text(str(key)): (
                    _REDACTED if _SECRET_KEY.search(str(key)) else clean(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        return text(value) if isinstance(value, str) else value

    arguments = []
    redact_next = False
    for argument in command:
        flag, separator, _value = argument.partition("=")
        if redact_next:
            arguments.append(_REDACTED)
            redact_next = False
        elif _SECRET_KEY.search(flag.lstrip("-")):
            arguments.append(flag + "=" + _REDACTED if separator else flag)
            redact_next = not separator
        else:
            arguments.append(text(argument))
    return arguments, clean(configuration)


def _digest(document: Mapping[str, Any]) -> str:
    body = {k: v for k, v in document.items() if k != "record_sha256"}
    return hashlib.sha256(
        json.dumps(
            body, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def file_identity(path: Path, *, root: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    relative = resolved.relative_to(root.resolve())
    if not resolved.is_file():
        raise ValueError(f"Expected regular file: {relative}")
    digest = hashlib.sha256()
    size = 0
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {
        "path": relative.as_posix(),
        "size_bytes": size,
        "sha256": digest.hexdigest(),
    }


def verify_execution_record(record: Mapping[str, Any], *, root: Path) -> None:
    """Verify the statement and every captured file; never execute imported code."""
    if record.get("schema_version") != 1 or record.get("evidence_origin") != "imported":
        raise ValueError("Unsupported execution record")
    if record.get("record_sha256") != _digest(record):
        raise ValueError("Execution record checksum mismatch")
    for collection in ("inputs", "outputs"):
        for identity in record.get(collection, {}).values():
            observed = file_identity(root / identity["path"], root=root)
            if observed != identity:
                raise ValueError(f"Changed {collection} file: {identity['path']}")


def _git_identity(root: Path) -> dict[str, Any] | None:
    try:

        def read(*args: str) -> bytes:
            return subprocess.check_output(
                ["git", *args], cwd=root, stderr=subprocess.DEVNULL
            )

        commit = read("rev-parse", "HEAD").decode().strip()
        dirty = bool(read("status", "--porcelain"))
        return {
            "commit": commit,
            "dirty": dirty,
            "tracked_diff_sha256": hashlib.sha256(
                read("diff", "HEAD", "--binary")
            ).hexdigest(),
        }
    except (OSError, subprocess.CalledProcessError):
        return None


def _write(path: Path, record: dict[str, Any]) -> None:
    record["record_sha256"] = _digest(record)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(record, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    try:
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def record_execution(
    command: Sequence[str],
    *,
    cwd: Path,
    record_path: Path,
    inputs: Mapping[str, Path],
    outputs: Mapping[str, Path],
    configuration: Mapping[str, Any] | None = None,
    packages: Sequence[str] = (),
    timeout_seconds: float,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Execute once, retaining failures/timeouts and verified produced files.

    Paths must resolve inside cwd. Secrets belong in the environment, whose
    values are never recorded. Credential fields, flags and supplied secret
    values are redacted. Arbitrary source code must contain no embedded secrets.
    Existing record files cannot be overwritten. Output logs are inherited,
    not copied into the provenance record. Missing outputs fail the record.
    """
    if not command or any(not isinstance(arg, str) for arg in command):
        raise ValueError("Command must be a nonempty argument list")
    if timeout_seconds <= 0:
        raise ValueError("A positive execution timeout is required")
    root = cwd.resolve(strict=True)
    record_path = record_path.resolve()
    record_path.parent.mkdir(parents=True, exist_ok=True)
    # Fail before launching if any identity or output path cannot be retained.
    before = {
        name: file_identity(root / path, root=root) for name, path in inputs.items()
    }
    for path in outputs.values():
        (root / path).resolve().relative_to(root)
    configuration = dict(configuration or {})
    json.dumps(configuration, allow_nan=False)
    safe_command, safe_configuration = _safe_provenance(
        command, configuration, environment
    )
    versions = {name: importlib.metadata.version(name) for name in packages}
    with record_path.open("x"):
        pass
    record: dict[str, Any] = {
        "schema_version": 1,
        "evidence_origin": "imported",
        "stage": safe_configuration.pop("stage", "execution"),
        "command": safe_command,
        "configuration": safe_configuration,
        "source": _git_identity(root),
        "inputs": before,
        "outputs": {},
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "packages": versions,
        },
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "timeout_seconds": timeout_seconds,
    }
    _write(record_path, record)
    started = time.monotonic()
    process = None
    interrupted = False
    try:
        process = subprocess.Popen(
            list(command),
            cwd=root,
            env=None if environment is None else {**os.environ, **environment},
            start_new_session=os.name == "posix",
        )
        record["exit_code"] = process.wait(timeout=timeout_seconds)
        record["status"] = "completed" if record["exit_code"] == 0 else "failed"
    except subprocess.TimeoutExpired:
        record["status"] = "timed_out"
    except KeyboardInterrupt:
        record["status"] = "cancelled"
        interrupted = True
    except OSError as exc:
        record["status"] = "failed"
        record["error_code"] = type(exc).__name__
    finally:
        if process is not None and process.poll() is None:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait()
        record["duration_seconds"] = time.monotonic() - started
        record["completed_at"] = datetime.now(timezone.utc).isoformat()
        for name, path in outputs.items():
            try:
                record["outputs"][name] = file_identity(root / path, root=root)
            except (OSError, ValueError):
                if record["status"] == "completed":
                    record["status"] = "failed"
                    record["error_code"] = "required_output_missing"
        try:
            after = {
                name: file_identity(root / path, root=root)
                for name, path in inputs.items()
            }
            if after != before:
                record["status"] = "failed"
                record["error_code"] = "input_changed_during_execution"
        except (OSError, ValueError):
            record["status"] = "failed"
            record["error_code"] = "input_missing_after_execution"
        _write(record_path, record)
    if interrupted:
        raise KeyboardInterrupt
    return record
