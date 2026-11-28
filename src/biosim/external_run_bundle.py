"""Export portable external run metadata and exact files, entirely offline."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from .__about__ import __version__

BUNDLE_VERSION = "biosimulant.external-run/1"
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_RESULTS_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 32
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}\Z")
_DIGEST = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")


def external_run_source(kind: str, source_id: str, digest: str) -> dict[str, str]:
    if kind not in {"workspace_revision", "lab_version"} or not _DIGEST.fullmatch(
        digest
    ):
        raise ValueError(
            "Supply an exact workspace revision or Lab version and its digest."
        )
    return {"kind": kind, "id": str(UUID(source_id)), "digest": digest}


def _safe_name(value: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")[:180]
    return candidate or "output"


def _inside(path: Path, root: Path) -> Path | None:
    try:
        if path.is_symlink():
            return None
        resolved = path.resolve()
        if resolved != root and resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def write_external_run_bundle(
    destination: Path,
    *,
    source: dict[str, str],
    external_id: str,
    status: str,
    started_at: str,
    completed_at: str,
    duration_seconds: float | None,
    results: dict[str, Any] | None,
    output_root: Path,
    parameters: dict[str, Any] | None = None,
    simulation_config: dict[str, Any] | None = None,
    input_values: dict[str, Any] | None = None,
    error_message: str | None = None,
    retained_artifacts: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Write an immutable bundle directory; never read files outside output_root.

    The source is the caller's claim, not proof that those bytes ran. Files
    discovered from outputs are bounded, and original host paths are omitted.
    """
    if status not in {"completed", "failed"} or (
        status == "completed" and results is None
    ):
        raise ValueError("Only completed or failed runs can be exported.")
    source = external_run_source(source["kind"], source["id"], source["digest"])
    external_id = str(UUID(external_id))
    destination = destination.expanduser().absolute()
    output_root = output_root.resolve()
    if destination.exists() or destination.is_symlink():
        raise ValueError(
            "The bundle directory already exists; keep it or choose a new one."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix="external-run-", dir=destination.parent) as temp:
        stage = Path(temp) / "bundle"
        stage.mkdir()
        files: list[dict[str, Any]] = []
        paths: dict[Path, str] = {}
        identifiers = {"workspace-results"}

        def output_entry(
            path: Path, artifact_id: str, file_name: str, role: str
        ) -> None:
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise ValueError(
                            f"Output {file_name} exceeds the import file limit."
                        )
                    digest.update(chunk)
            if not size:
                raise ValueError(f"Output {file_name} is empty.")
            if (
                len(files) == MAX_FILES
                or sum(f["size_bytes"] for f in files) + size > MAX_TOTAL_BYTES
            ):
                raise ValueError(
                    "The output inventory exceeds the import bundle limit."
                )
            files.append(
                {
                    "artifact_id": artifact_id,
                    "file_name": file_name,
                    "role": role,
                    "path": path.relative_to(stage).as_posix(),
                    "size_bytes": size,
                    "sha256": digest.hexdigest(),
                }
            )

        def keep(raw: Path, preferred: str | None = None) -> str | None:
            path = _inside(raw, output_root)
            if path is None:
                return None
            if path in paths:
                return paths[path]
            file_name = _safe_name(path.name)
            artifact_id = (
                preferred
                if preferred
                and _ID.fullmatch(preferred)
                and preferred not in identifiers
                else "output-"
                + hashlib.sha256(
                    path.relative_to(output_root).as_posix().encode()
                ).hexdigest()[:24]
            )
            if artifact_id in identifiers:
                raise ValueError("An output artifact ID collides with another file.")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError(f"Output {file_name} exceeds the import file limit.")
            target = stage / "files" / artifact_id / file_name
            target.parent.mkdir(parents=True)
            shutil.copyfile(path, target)
            output_entry(target, artifact_id, file_name, "output")
            identifiers.add(artifact_id)
            paths[path] = artifact_id
            return artifact_id

        local_to_portable: dict[str, str] = {}
        for artifact_id, path in (retained_artifacts or {}).items():
            if portable := keep(path, artifact_id):
                local_to_portable[artifact_id] = portable

        def clean(value: Any, *, output: bool = False) -> Any:
            if isinstance(value, list):
                return [clean(item, output=output) for item in value]
            if isinstance(value, dict):
                if output and value.get("kind") == "artifact":
                    artifact_id = value.get("artifact_id")
                    raw_path = value.get("path")
                    portable = (
                        local_to_portable.get(artifact_id)
                        if isinstance(artifact_id, str)
                        else None
                    )
                    if portable is None and isinstance(raw_path, str):
                        portable = keep(
                            output_root / raw_path,
                            artifact_id if isinstance(artifact_id, str) else None,
                        )
                    return {
                        "kind": "artifact",
                        **({"artifact_id": portable} if portable else {}),
                    }
                return {
                    key: clean(item, output=output or key in {"outputs", "visuals"})
                    for key, item in value.items()
                    if not isinstance(item, str)
                    or not (
                        not output
                        and key in {"path", "source_path", "report_file", "unpack_root"}
                        and Path(item).is_absolute()
                    )
                }
            if output and isinstance(value, str) and value.startswith("artifact://"):
                return "artifact:" + local_to_portable.get(
                    value.removeprefix("artifact://"), "unavailable"
                )
            if isinstance(value, str) and (
                value.startswith(("/", "~/", "file://"))
                or re.match(r"^[A-Za-z]:[\\/]", value)
            ):
                if (
                    output
                    and value.startswith("/")
                    and (artifact_id := keep(Path(value)))
                ):
                    return "artifact:" + artifact_id
                return "[local path omitted]"
            return value

        if results is not None:
            document = stage / "results.json"
            encoded = (
                json.dumps(clean(results), ensure_ascii=False, allow_nan=False).encode(
                    "utf-8"
                )
                + b"\n"
            )
            if len(encoded) > MAX_RESULTS_BYTES:
                raise ValueError("Results JSON exceeds the import limit.")
            document.write_bytes(encoded)
            output_entry(document, "workspace-results", "results.json", "results")
        manifest = {
            "schema_version": BUNDLE_VERSION,
            "external_id": external_id,
            "source": source,
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": duration_seconds,
            "runtime_version": __version__,
            "runner": "biosimulant",
            "parameters": clean(parameters or {}),
            "simulation_config": clean(simulation_config or {}),
            "input_values": clean(input_values or {}),
            "error_message": error_message,
            "files": files,
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        stage.rename(destination)
    return manifest
