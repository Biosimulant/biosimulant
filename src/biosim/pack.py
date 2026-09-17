from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from contextlib import nullcontext
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .__about__ import __version__
from ._errors import PackageError
from .execution import (
    bind_manifest_execution_policy,
    declared_execution_policy,
    describe_lab_execution,
    execution_phase_findings,
    module_edges_from_wiring,
    read_model_dir_execution_policy,
    source_declaration_findings,
    unknown_lab_execution,
)
from .modules import BioModule
from .runtime import (
    LabTree,
    LabTreeChild,
    LabTreeModel,
    LabTreeWire,
    coerce_typed_inputs,
    extract_communication_step,
    extract_settle_steps,
    flatten_lab_tree,
    lab_io_from_mapping,
    load_entrypoint,
)
from .wiring import WiringBuilder
from .world import BioWorld

PACKAGE_EXTENSION = ".bsimpkg"
PACKAGE_EXTENSIONS = (".bsimpkg", ".bsimodel", ".bsilab")
_TYPE_EXTENSION = {"model": ".bsimodel", "lab": ".bsilab"}
PACKAGE_SCHEMA_VERSION = "1.0"
DEFAULT_PACKAGE_VERSION = "0.1.0"
DEFAULT_PACKAGE_NAMESPACE = "local"
DEFAULT_REGISTRY_ENV = "BIOSIM_PACKAGE_REGISTRY_DIR"
DEFAULT_CACHE_ENV = "BIOSIM_PACKAGE_CACHE_DIR"
DEFAULT_CACHE_HOME = Path.home() / ".cache" / "biosim" / "packages"
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
SUPPORTED_LAB_PYTHON_VERSIONS = frozenset(
    {"3.10", "3.11", "3.12", "3.13", "3.14"}
)
_FORBIDDEN_LEGACY_SOURCE_KEYS = frozenset(
    {"repo", "manifest_path", "upstream_repo", "upstream_manifest_path"}
)
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_PACKAGE_SEGMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
DependencyLogger = Callable[[str], None]
CancelChecker = Callable[[], None]
ProcessTracker = Callable[[subprocess.Popen[str]], Any]


def _new_process_group_kwargs() -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        }
    return {}


@dataclass
class PackageValidationResult:
    valid: bool
    metadata: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class _LoadedPackage:
    package_path: Path
    package_type: str
    package_yaml: dict[str, Any]
    manifest: dict[str, Any]
    unpacked_root: Path
    payload_root: Path


@dataclass
class LabPackageRuntime:
    package: str
    version: str
    world: BioWorld
    manifest: dict[str, Any]
    lab_path: Path
    duration: float
    communication_step: float
    settle_steps: int
    modules: list[dict[str, Any]]


def _require_yaml():
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Package support requires PyYAML. Install with: pip install pyyaml"
        ) from exc
    return yaml


def _safe_yaml_dump(value: Any) -> bytes:
    yaml = _require_yaml()
    return yaml.safe_dump(value, sort_keys=False).encode("utf-8")


def _safe_yaml_load(data: bytes | str) -> dict[str, Any]:
    yaml = _require_yaml()
    loaded = yaml.safe_load(data) or {}
    if not isinstance(loaded, dict):
        raise PackageError("YAML document must be a mapping")
    return loaded


def _package_cache_dir() -> Path:
    raw = os.getenv(DEFAULT_CACHE_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_CACHE_HOME


def _package_registry_dir() -> Path | None:
    raw = os.getenv(DEFAULT_REGISTRY_ENV)
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _package_to_parts(package_name: str) -> list[str]:
    parts = [part.strip() for part in package_name.split("/") if part.strip()]
    if not parts:
        raise PackageError("Package name must not be empty")
    return parts


def _package_slug(package_name: str) -> str:
    return "__".join(_package_to_parts(package_name))


def _package_segment_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "lab"


def _default_package_name(path: Path) -> str:
    return f"{DEFAULT_PACKAGE_NAMESPACE}/{path.name}"


def _default_lab_package_name(path: Path) -> str:
    return f"{DEFAULT_PACKAGE_NAMESPACE}/{_package_segment_slug(path.name)}"


def _validate_version(version: str) -> str:
    clean = version.strip()
    if not _SEMVER_RE.fullmatch(clean):
        raise PackageError(
            "Package version must be SemVer MAJOR.MINOR.PATCH with optional prerelease and no build metadata"
        )
    return clean


def _validate_package_ref(package_name: str) -> str:
    clean = package_name.strip()
    parts = clean.split("/")
    if len(parts) not in {1, 2} or any(not part for part in parts):
        raise PackageError("Package names must be `name` or `namespace/name`")
    for part in parts:
        if not _PACKAGE_SEGMENT_RE.fullmatch(part):
            raise PackageError(
                "Package names must contain only lowercase letters, digits, and dashes, "
                "and must start and end with a lowercase letter or digit"
            )
    return clean


def _validate_lab_release_identity(
    manifest: Mapping[str, Any],
    *,
    package_name_override: str | None,
    version_override: str | None,
    source_path: Path | None = None,
    allow_default_identity: bool = False,
) -> tuple[str, str]:
    declared_package = _manifest_declared_package(manifest)
    declared_version = _manifest_declared_version(manifest)

    if package_name_override is not None:
        package_name = _validate_package_ref(package_name_override)
        if declared_package is not None:
            declared_package = _validate_package_ref(declared_package)
            if declared_package != package_name:
                raise PackageError("--package must match lab.yaml package")
    elif declared_package is not None:
        package_name = _validate_package_ref(declared_package)
    elif allow_default_identity and source_path is not None:
        package_name = _validate_package_ref(_default_lab_package_name(source_path))
    else:
        raise PackageError(
            "lab.yaml must declare a non-empty package or pass --package"
        )

    if version_override is not None:
        version = _validate_version(version_override)
        if declared_version is not None:
            declared_version = _validate_version(declared_version)
            if declared_version != version:
                raise PackageError("--version must match lab.yaml version")
    elif declared_version is not None:
        version = _validate_version(declared_version)
    elif allow_default_identity:
        version = DEFAULT_PACKAGE_VERSION
    else:
        raise PackageError(
            "lab.yaml must declare a non-empty version or pass --version"
        )
    return package_name, version


def _local_lab_release_identity(source_dir: str | Path) -> tuple[str, str]:
    source_path = Path(source_dir).expanduser().resolve()
    manifest_path = _find_manifest_in_dir(source_path, ("lab.yaml", "lab.yml"))
    manifest = _safe_yaml_load(manifest_path.read_bytes())
    return _validate_lab_release_identity(
        manifest,
        package_name_override=None,
        version_override=None,
        source_path=source_path,
        allow_default_identity=True,
    )


def _is_exact_pin(dep: str) -> bool:
    if "==" not in dep:
        return False
    left, right = dep.split("==", 1)
    return (
        bool(left.strip())
        and bool(right.strip())
        and all(op not in dep for op in (">=", "<=", "~=", "!=", ">", "<"))
    )


def _is_runtime_distribution_pin(dep: str) -> bool:
    name = re.split(r"[\[;=<>!~\s]", dep.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower() == "biosimulant"


def _validate_dependencies(manifest: Mapping[str, Any]) -> None:
    runtime = manifest.get("runtime")
    if not isinstance(runtime, Mapping):
        return
    dependencies = runtime.get("dependencies")
    if not isinstance(dependencies, Mapping):
        return
    packages = dependencies.get("packages")
    if not isinstance(packages, list):
        return
    for dep in packages:
        if not isinstance(dep, str) or not _is_exact_pin(dep):
            raise PackageError(f"Dependency '{dep}' must be pinned with == only")


def _declared_lab_python_version(manifest: Mapping[str, Any]) -> str | None:
    runtime = manifest.get("runtime")
    if not isinstance(runtime, Mapping):
        return None
    raw = runtime.get("python_version")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise PackageError(
            "Lab manifest runtime.python_version must be a string or number like '3.11'"
        )
    version = str(raw).strip()
    if not version:
        raise PackageError("Lab manifest runtime.python_version must not be empty")
    if version not in SUPPORTED_LAB_PYTHON_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_LAB_PYTHON_VERSIONS))
        raise PackageError(
            f"Lab manifest runtime.python_version must be one of: {supported}"
        )
    return version


def _current_python_minor() -> str:
    return f"{sys.version_info[0]}.{sys.version_info[1]}"


def _ensure_lab_python_version_matches_current(manifest: Mapping[str, Any]) -> None:
    declared = _declared_lab_python_version(manifest)
    if declared is None:
        return
    current = _current_python_minor()
    if declared != current:
        raise PackageError(
            "Lab manifest requires Python "
            f"{declared}, but this process is running Python {current}. "
            "Run the lab with a matching Python interpreter."
        )


def _manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


_LEGACY_LOGICAL_HASH_WARNING = (
    "package.yaml sha256 uses a legacy logical hash that includes "
    ".biosimulant-project.json"
)


def _logical_hash(entries: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(entries):
        if (
            name in {"package.yaml", "integrity/sha256sums.txt"}
            or Path(name).name == ".biosimulant-project.json"
        ):
            continue
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entries[name])
        digest.update(b"\0")
    return digest.hexdigest()


def _legacy_logical_hash_with_project_metadata(entries: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(entries):
        if name in {"package.yaml", "integrity/sha256sums.txt"}:
            continue
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entries[name])
        digest.update(b"\0")
    return digest.hexdigest()


def _checksums_text(entries: Mapping[str, bytes]) -> str:
    lines: list[str] = []
    for name in sorted(entries):
        if name == "integrity/sha256sums.txt":
            continue
        lines.append(f"{hashlib.sha256(entries[name]).hexdigest()}  {name}")
    return "\n".join(lines) + "\n"


def _normalized_manifest_bytes(path: Path) -> tuple[dict[str, Any], bytes]:
    manifest = _safe_yaml_load(path.read_bytes())
    return manifest, _safe_yaml_dump(manifest)


def _manifest_declared_package(manifest: Mapping[str, Any]) -> str | None:
    value = manifest.get("package")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _manifest_declared_version(manifest: Mapping[str, Any]) -> str | None:
    value = manifest.get("version")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _collect_model_entries(source_dir: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    manifest_path = source_dir / "model.yaml"
    if not manifest_path.exists():
        raise PackageError(f"Model package source is missing {manifest_path}")
    manifest, manifest_bytes = _normalized_manifest_bytes(manifest_path)
    _validate_model_manifest(manifest)
    _validate_dependencies(manifest)

    entries: dict[str, bytes] = {"payload/model.yaml": manifest_bytes}
    for name in ("src", "artifacts", "data", "tests"):
        entries.update(_collect_tree(source_dir, name))
    entries.update(_collect_glob_files(source_dir, ("README*", "*.md")))
    return manifest, entries


def _collect_lab_entries(source_dir: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    manifest_path = source_dir / "lab.yaml"
    if not manifest_path.exists():
        raise PackageError(f"Lab package source is missing {manifest_path}")
    manifest, manifest_bytes = _normalized_manifest_bytes(manifest_path)
    _validate_lab_manifest(manifest)
    _validate_lab_lock_for_dir(manifest, source_dir)

    entries: dict[str, bytes] = {"payload/lab.yaml": manifest_bytes}
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source_dir).as_posix()
        if rel in {"lab.yaml", "lab.yml", "package.yaml"}:
            continue
        if _should_ignore_lab_source_file(path.relative_to(source_dir)):
            continue
        entries[f"payload/{rel}"] = path.read_bytes()
    return manifest, entries


_IGNORED_LAB_SOURCE_DIRS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".biosimulant",
        ".tox",
        "dist",
    }
)
_IGNORED_LAB_SOURCE_FILES = frozenset({".DS_Store"})
_IGNORED_LAB_SOURCE_SUFFIXES = (".pyc", ".pyo")


def _should_ignore_lab_source_file(relative_path: Path) -> bool:
    for part in relative_path.parts[:-1]:
        if part in _IGNORED_LAB_SOURCE_DIRS:
            return True
    name = relative_path.name
    if name in _IGNORED_LAB_SOURCE_FILES:
        return True
    return name.endswith(_IGNORED_LAB_SOURCE_SUFFIXES)


def _collect_tree(root: Path, name: str) -> dict[str, bytes]:
    base = root / name
    if not base.exists():
        return {}
    if not base.is_dir():
        raise PackageError(f"Expected directory at {base}")
    out: dict[str, bytes] = {}
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        out[f"payload/{rel}"] = path.read_bytes()
    return out


def _collect_glob_files(root: Path, patterns: Iterable[str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            if path.name in {"model.yaml", "lab.yaml", "package.yaml"}:
                continue
            out[f"payload/{path.name}"] = path.read_bytes()
    return out


def _sanitize_package_source(source: Mapping[str, Any] | None) -> dict[str, Any]:
    if source is None:
        return {}
    if not isinstance(source, Mapping):
        raise PackageError("Package source metadata must be a mapping")
    forbidden = sorted(
        key
        for key in _FORBIDDEN_LEGACY_SOURCE_KEYS
        if key in source and source.get(key) is not None
    )
    if forbidden:
        raise PackageError(
            "Package source metadata must not include legacy source keys "
            f"({', '.join(forbidden)}). Use path/package/version provenance instead."
        )
    return {
        str(key): deepcopy(value) for key, value in source.items() if value is not None
    }


def _build_package_yaml(
    *,
    package_type: str,
    package_name: str,
    version: str,
    visibility: str,
    manifest: Mapping[str, Any],
    entry_manifest: str,
    source: Mapping[str, Any] | None,
    logical_sha256: str,
) -> dict[str, Any]:
    sanitized_source = _sanitize_package_source(source)
    package_yaml: dict[str, Any] = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_type": package_type,
        "package": package_name,
        "version": version,
        "title": manifest.get("title") or package_name,
        "description": manifest.get("description"),
        "entry_manifest": entry_manifest,
        "visibility": visibility,
        "sha256": logical_sha256,
        "source": sanitized_source,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    provenance = {}
    for key in ("path", "commit"):
        value = sanitized_source.get(key)
        if value is not None:
            provenance[key] = value
    if provenance:
        package_yaml["provenance"] = provenance
    if package_type == "model":
        runtime = (
            manifest.get("runtime")
            if isinstance(manifest.get("runtime"), Mapping)
            else {}
        )
        package_yaml["runtime"] = (
            {"dependencies": dict(runtime.get("dependencies") or {})}
            if isinstance(runtime, Mapping)
            else {"dependencies": {}}
        )
        package_yaml["manifest_fingerprint"] = _manifest_fingerprint(manifest)
    return package_yaml


def _write_zip(target: Path, entries: Mapping[str, bytes]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as zipf:
        for name in sorted(entries):
            info = ZipInfo(name, date_time=FIXED_ZIP_TIME)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zipf.writestr(info, entries[name])


def build_package(
    source_dir: str | Path,
    *,
    output_path: str | Path | None = None,
    package_name: str | None = None,
    version: str | None = None,
    visibility: str = "private",
    source: Mapping[str, Any] | None = None,
    vendor_dependencies: bool = False,
    registry_url: str | None = None,
) -> Path:
    source_path = Path(source_dir).expanduser().resolve()
    if not source_path.is_dir():
        raise PackageError(f"Package source must be a directory: {source_path}")

    if (source_path / "model.yaml").exists():
        package_type = "model"
        manifest, entries = _collect_model_entries(source_path)
    elif (source_path / "lab.yaml").exists():
        return export_lab_package(
            source_path,
            output_path=output_path,
            package_name=package_name,
            version=version,
            visibility=visibility,
            source=source,
            vendor_dependencies=vendor_dependencies,
            registry_url=registry_url,
        )
    else:
        raise PackageError(f"Could not find model.yaml or lab.yaml in {source_path}")

    package_name = (
        package_name
        or _manifest_declared_package(manifest)
        or _default_package_name(source_path)
    )
    version = _validate_version(
        version
        if version is not None
        else (_manifest_declared_version(manifest) or DEFAULT_PACKAGE_VERSION)
    )

    logical_sha256 = _logical_hash(entries)
    package_yaml = _build_package_yaml(
        package_type=package_type,
        package_name=package_name,
        version=version,
        visibility=visibility,
        manifest=manifest,
        entry_manifest=f"payload/{package_type}.yaml",
        source=source,
        logical_sha256=logical_sha256,
    )
    entries["package.yaml"] = _safe_yaml_dump(package_yaml)
    entries["integrity/sha256sums.txt"] = _checksums_text(entries).encode("utf-8")

    if output_path is None:
        ext = _TYPE_EXTENSION.get(package_type, PACKAGE_EXTENSION)
        file_name = f"{_package_slug(package_name)}-{version}{ext}"
        output_path = source_path / "dist" / file_name
    target = Path(output_path).expanduser().resolve()
    _write_zip(target, entries)
    return target


def export_lab_package(
    source_dir: str | Path,
    *,
    output_path: str | Path | None = None,
    package_name: str | None = None,
    version: str | None = None,
    visibility: str = "private",
    source: Mapping[str, Any] | None = None,
    vendor_dependencies: bool = False,
    registry_url: str | None = None,
) -> Path:
    source_path = Path(source_dir).expanduser().resolve()
    if vendor_dependencies:
        from .hub import materialize_vendored_lab

        if output_path is None:
            source_manifest = _safe_yaml_load((source_path / "lab.yaml").read_bytes())
            source_package, source_version = _validate_lab_release_identity(
                source_manifest,
                package_name_override=package_name,
                version_override=version,
            )
            output_path = source_path / "dist" / (
                f"{_package_slug(source_package)}-{source_version}.bsilab"
            )
        with tempfile.TemporaryDirectory(prefix="biosim-vendor-lab-") as temp_dir:
            vendored_source = materialize_vendored_lab(
                source_path,
                Path(temp_dir) / "lab",
                registry_url=registry_url,
            )
            return export_lab_package(
                vendored_source,
                output_path=output_path,
                package_name=package_name,
                version=version,
                visibility=visibility,
                source=source,
            )
    manifest, entries = _collect_lab_entries(source_path)
    package_name, version = _validate_lab_release_identity(
        manifest,
        package_name_override=package_name,
        version_override=version,
    )
    logical_sha256 = _logical_hash(entries)
    package_yaml = _build_package_yaml(
        package_type="lab",
        package_name=package_name,
        version=version,
        visibility=visibility,
        manifest=manifest,
        entry_manifest="payload/lab.yaml",
        source=source,
        logical_sha256=logical_sha256,
    )
    entries["package.yaml"] = _safe_yaml_dump(package_yaml)
    entries["integrity/sha256sums.txt"] = _checksums_text(entries).encode("utf-8")

    if output_path is None:
        file_name = f"{_package_slug(package_name)}-{version}.bsilab"
        output_path = source_path / "dist" / file_name
    target = Path(output_path).expanduser().resolve()
    _write_zip(target, entries)
    return target


def _validate_paths(names: Iterable[str]) -> None:
    for name in names:
        if name.startswith("/") or ".." in Path(name).parts:
            raise PackageError(f"Invalid archive path: {name}")


def _resolve_embedded_archive_dir(
    *, package_root: str, current_dir: str, dependency_path: str
) -> str:
    relative = _normalize_embedded_path(dependency_path)
    resolved = posixpath.normpath(posixpath.join(current_dir, relative))
    if package_root:
        if resolved != package_root and not resolved.startswith(f"{package_root}/"):
            raise PackageError(
                f"Embedded dependency escapes package root: {dependency_path}"
            )
    elif resolved.startswith("../") or resolved == "..":
        raise PackageError(
            f"Embedded dependency escapes package root: {dependency_path}"
        )
    return resolved


def _find_archive_manifest(
    entries: Mapping[str, bytes], directory: str, candidates: tuple[str, ...]
) -> str:
    for candidate in candidates:
        archive_path = posixpath.join(directory, candidate) if directory else candidate
        if archive_path in entries:
            return archive_path
    raise PackageError(
        f"Expected one of {', '.join(candidates)} inside {directory or '<root>'}"
    )


def _normalize_embedded_path(path: str) -> str:
    normalized = posixpath.normpath(path.replace("\\", "/").lstrip("/"))
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise PackageError(f"Invalid embedded dependency path: {path}")
    return normalized


def validate_package(path: str | Path) -> PackageValidationResult:
    package_path = Path(path).expanduser().resolve()
    result = PackageValidationResult(valid=False)
    if package_path.suffix not in PACKAGE_EXTENSIONS:
        result.errors.append(
            f"Package file must use one of {', '.join(PACKAGE_EXTENSIONS)}"
        )
        return result
    if not package_path.exists():
        result.errors.append(f"Package file not found: {package_path}")
        return result

    try:
        with ZipFile(package_path, "r") as zipf:
            names = zipf.namelist()
            _validate_paths(names)
            if "package.yaml" not in names:
                raise PackageError("Archive is missing package.yaml")
            if "integrity/sha256sums.txt" not in names:
                raise PackageError("Archive is missing integrity/sha256sums.txt")

            entries = {name: zipf.read(name) for name in names}
            package_yaml = _safe_yaml_load(entries["package.yaml"])
            entry_manifest = package_yaml.get("entry_manifest")
            if not isinstance(entry_manifest, str) or entry_manifest not in entries:
                raise PackageError(
                    "package.yaml entry_manifest must point to a file in the archive"
                )

            expected_checksums = _parse_checksums(
                entries["integrity/sha256sums.txt"].decode("utf-8")
            )
            for name, expected in expected_checksums.items():
                if name not in entries:
                    raise PackageError(
                        f"Checksum entry references missing file: {name}"
                    )
                actual = hashlib.sha256(entries[name]).hexdigest()
                if actual != expected:
                    raise PackageError(f"Checksum mismatch for {name}")

            actual_logical_hash = _logical_hash(entries)
            declared_hash = package_yaml.get("sha256")
            if declared_hash and declared_hash != actual_logical_hash:
                legacy_logical_hash = _legacy_logical_hash_with_project_metadata(
                    entries
                )
                if declared_hash != legacy_logical_hash:
                    raise PackageError(
                        "Logical package hash does not match package.yaml sha256"
                    )
                result.warnings.append(_LEGACY_LOGICAL_HASH_WARNING)

            manifest = _safe_yaml_load(entries[entry_manifest])
            package_type = package_yaml.get("package_type")
            if package_type == "model":
                _validate_model_manifest(manifest)
                _validate_dependencies(manifest)
            elif package_type == "lab":
                _validate_lab_manifest(manifest)
                _validate_lab_lock_for_archive(manifest, entries, entry_manifest)
                _validate_embedded_lab_package(entries, manifest, entry_manifest)
            else:
                raise PackageError(f"Unsupported package_type: {package_type}")

            result.valid = True
            result.metadata = package_yaml
            if not declared_hash:
                result.warnings.append("package.yaml sha256 is missing")
            return result
    except Exception as exc:
        result.errors.append(str(exc))
        return result


def validate_lab_source(path: str | Path) -> PackageValidationResult:
    source_path = Path(path).expanduser().resolve()
    result = PackageValidationResult(valid=False)
    if not source_path.is_dir():
        result.errors.append(f"Lab source path not found: {source_path}")
        return result

    try:
        manifest_path = _find_manifest_in_dir(source_path, ("lab.yaml", "lab.yml"))
        manifest = _safe_yaml_load(manifest_path.read_bytes())
        _validate_lab_manifest(manifest)
        _validate_lab_lock_for_dir(manifest, source_path)
        _validate_lab_source_dir(
            source_root=source_path,
            current_lab_dir=source_path,
            parsed_manifest=manifest,
            visited=set(),
        )
        execution = inspect_lab_execution(source_path)
        if execution["errors"]:
            raise PackageError("; ".join(execution["errors"]))
        result.warnings.extend(execution["warnings"])
        compatibility = static_lab_compatibility(source_path, manifest)
        if compatibility is not None and compatibility["summary"]["blocked"]:
            blocked = [
                f"{wire['source']['module']}.{wire['source']['port']} -> "
                f"{wire['target']['module']}.{wire['target']['port']}: "
                + "; ".join(issue["message"] for issue in wire["issues"] if issue["level"] == "blocked")
                for wire in compatibility["wires"]
                if wire["mode"] == "blocked"
            ]
            raise PackageError("Incompatible wires: " + " | ".join(blocked))
        package_name, version = _validate_lab_release_identity(
            manifest,
            package_name_override=None,
            version_override=None,
            source_path=source_path,
            allow_default_identity=True,
        )
        result.valid = True
        result.metadata = {
            "schema_version": PACKAGE_SCHEMA_VERSION,
            "package_type": "lab",
            "package": package_name,
            "version": version,
            "title": manifest.get("title") or package_name,
            "description": manifest.get("description"),
            "entry_manifest": manifest_path.name,
            "source_format": "source-tree",
            "execution": execution["profile"],
        }
        if compatibility is not None:
            result.metadata["compatibility"] = compatibility
        return result
    except Exception as exc:
        result.errors.append(str(exc))
        return result


def _declared_port_contracts(
    port: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]]]:
    if not isinstance(port, Mapping):
        return None, []
    contract = port.get("contract") if isinstance(port.get("contract"), Mapping) else None
    accepted = [
        item["contract"]
        for item in port.get("accepted_profiles") or []
        if isinstance(item, Mapping) and isinstance(item.get("contract"), Mapping)
    ]
    return contract, accepted


def static_lab_compatibility(
    source_path: str | Path, manifest: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Classify each wire of a source lab from model.yaml profiles, without running it.

    Structure and values are checked when the lab runs. Labs with Hub package
    children return ``None`` because their models are not available locally.
    """

    from .compatibility import (
        CompatibilityIssue,
        CompatibilityRecorder,
        CompatibilityResult,
        check_declared_contracts,
    )

    root = Path(source_path).expanduser().resolve()
    lab_manifest = manifest if manifest is not None else _load_lab_manifest_from_dir(root)
    if _package_children(lab_manifest):
        return None
    models, wiring, _parsed = _flatten_embedded_lab_dir(payload_root=root, current_lab_dir=root)
    recorder = CompatibilityRecorder()
    declared_ports: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for entry in models:
        alias = str(entry["alias"])
        model_manifest = _load_model_manifest_from_dir(Path(str(entry["model_dir"])))
        io = model_manifest.get("io") if isinstance(model_manifest.get("io"), Mapping) else {}
        for direction in ("inputs", "outputs"):
            ports = io.get(direction)
            for port in ports if isinstance(ports, list) else []:
                if not isinstance(port, Mapping) or not isinstance(port.get("name"), str):
                    continue
                declared_ports[(alias, direction, port["name"])] = port
                contract, accepted = _declared_port_contracts(port)
                recorder.record_declared_port(alias, direction, port["name"], contract, accepted)

    for edge in wiring:
        source_ref = str(edge["from"])
        source_module, _, source_port = source_ref.rpartition(".")
        source_decl = declared_ports.get((source_module, "outputs", source_port))
        source_contract, _ = _declared_port_contracts(source_decl)
        for target_ref in edge["to"]:
            target_module, _, target_port = str(target_ref).rpartition(".")
            target_decl = declared_ports.get((target_module, "inputs", target_port))
            target_contract, accepted = _declared_port_contracts(target_decl)
            if target_contract is None and len({repr(item) for item in accepted}) == 1:
                target_contract = accepted[0]
            checked = check_declared_contracts(source_contract, target_contract)
            undeclared = [
                ref
                for ref, decl in ((source_ref, source_decl), (str(target_ref), target_decl))
                if decl is None
            ]
            if undeclared:
                checked = CompatibilityResult(
                    checked.issues
                    + (
                        CompatibilityIssue(
                            level="warning",
                            code="PORT_NOT_DECLARED",
                            message=(
                                f"model.yaml does not declare {', '.join(undeclared)}; "
                                "its profile is checked when the lab runs."
                            ),
                        ),
                    )
                )
            recorder.record_declared_wire(
                source_module,
                source_port,
                source_contract,
                target_module,
                target_port,
                target_contract,
                checked,
            )
    return recorder.to_dict()


def _parse_checksums(text: str) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        digest, _, name = line.partition("  ")
        if not digest or not name:
            raise PackageError(f"Invalid checksum entry: {line}")
        checksums[name] = digest
    return checksums


def unpack_package(path: str | Path, *, dest: str | Path | None = None) -> Path:
    validation = validate_package(path)
    if not validation.valid:
        raise PackageError("; ".join(validation.errors))
    package_path = Path(path).expanduser().resolve()
    if dest is None:
        dest_path = Path(tempfile.mkdtemp(prefix="biosim-pack-"))
    else:
        dest_path = Path(dest).expanduser().resolve()
        dest_path.mkdir(parents=True, exist_ok=True)
    with ZipFile(package_path, "r") as zipf:
        zipf.extractall(dest_path)
    return dest_path


def publish_package(
    path: str | Path, *, registry_dir: str | Path | None = None
) -> Path:
    validation = validate_package(path)
    if not validation.valid or not validation.metadata:
        raise PackageError("; ".join(validation.errors))
    registry = (
        Path(registry_dir).expanduser().resolve()
        if registry_dir
        else _package_registry_dir()
    )
    if registry is None:
        raise PackageError(
            f"Set {DEFAULT_REGISTRY_ENV} or pass registry_dir to publish packages"
        )
    package_name = str(validation.metadata["package"])
    version = str(validation.metadata["version"])
    target_dir = registry.joinpath(*_package_to_parts(package_name), version)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{_package_slug(package_name)}-{version}{PACKAGE_EXTENSION}"
    shutil.copy2(Path(path), target)
    return target


def fetch_package(
    package_name: str,
    version: str,
    *,
    registry_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
) -> Path:
    version = _validate_version(version)
    registry = (
        Path(registry_dir).expanduser().resolve()
        if registry_dir
        else _package_registry_dir()
    )
    cache = (
        Path(cache_dir).expanduser().resolve() if cache_dir else _package_cache_dir()
    )
    cache_target_dir = cache.joinpath(*_package_to_parts(package_name), version)
    cache_target_dir.mkdir(parents=True, exist_ok=True)
    cache_target = (
        cache_target_dir / f"{_package_slug(package_name)}-{version}{PACKAGE_EXTENSION}"
    )
    if cache_target.exists():
        return cache_target
    if registry is None:
        raise PackageError(
            f"Package {package_name}@{version} is not in cache and no registry is configured"
        )
    registry_target = registry.joinpath(
        *_package_to_parts(package_name),
        version,
        f"{_package_slug(package_name)}-{version}{PACKAGE_EXTENSION}",
    )
    if not registry_target.exists():
        raise PackageError(
            f"Package {package_name}@{version} was not found in registry {registry}"
        )
    shutil.copy2(registry_target, cache_target)
    return cache_target


def _load_entrypoint(entrypoint: str, *, model_path: str | Path | None = None):
    return load_entrypoint(entrypoint, model_path=model_path, error_cls=PackageError)


def _module_paths_for_payload(payload_root: Path) -> list[str]:
    return [str(payload_root)]


BIOSIM_REMOTE_EXECUTION_ENV = "BIOSIM_REMOTE_EXECUTION"
BIOSIM_REMOTE_EXECUTION_MOUNT_ROOT_ENV = "BIOSIM_REMOTE_EXECUTION_MOUNT_ROOT"
_REMOTE_EXECUTION_MOUNT_ROOT_TOKEN = "${REMOTE_EXECUTION_MOUNT_ROOT}"


def _remote_execution_init_kwargs(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``runtime.remote.init_kwargs`` when running on Biosimulant remote compute.

    Remote compute sets ``BIOSIM_REMOTE_EXECUTION=1``. The manifest's remote init
    kwargs then override lab parameters, as the hosted executor does, so a model
    can use its installed runtime dependencies and a cache under
    ``${REMOTE_EXECUTION_MOUNT_ROOT}`` instead of provisioning its own copies.
    """
    if os.environ.get(BIOSIM_REMOTE_EXECUTION_ENV) != "1":
        return {}
    runtime = manifest.get("runtime")
    remote = runtime.get("remote") if isinstance(runtime, Mapping) else None
    raw = remote.get("init_kwargs") if isinstance(remote, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    mount_root = os.environ.get(BIOSIM_REMOTE_EXECUTION_MOUNT_ROOT_ENV, "").rstrip("/")

    def expand(value: Any) -> Any:
        if isinstance(value, str):
            if _REMOTE_EXECUTION_MOUNT_ROOT_TOKEN not in value:
                return value
            if not mount_root:
                raise PackageError(
                    f"{BIOSIM_REMOTE_EXECUTION_MOUNT_ROOT_ENV} must be set to expand "
                    f"{_REMOTE_EXECUTION_MOUNT_ROOT_TOKEN} in runtime.remote.init_kwargs"
                )
            return value.replace(_REMOTE_EXECUTION_MOUNT_ROOT_TOKEN, mount_root)
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, Mapping):
            return {key: expand(item) for key, item in value.items()}
        return value

    return {str(key): expand(value) for key, value in raw.items()}


def _bind_execution_policy(module: BioModule, manifest: Mapping[str, Any]) -> None:
    try:
        bind_manifest_execution_policy(module, manifest)
    except ValueError as exc:
        raise PackageError(f"model.yaml doesn't match the Python module: {exc}") from exc


def inspect_lab_execution(path: str | Path) -> dict[str, Any]:
    """Describe when a local lab's modules run without importing model code.

    Returns ``{"profile", "errors", "warnings", "models"}``. ``profile`` follows
    :class:`biosim.execution.LabExecutionProfile`. Errors cover a declaration the
    source provably contradicts and invalid phase wiring between declared
    modules; warnings cover declarations the source can't confirm and models
    whose source shows a policy that model.yaml doesn't declare yet.
    """

    lab_dir = Path(path).expanduser().resolve()
    manifest = _load_lab_manifest_from_dir(lab_dir)
    errors: list[str] = []
    warnings: list[str] = []
    policies: dict[str, Any] = {}
    models_report: dict[str, dict[str, Any]] = {}

    if _package_children(manifest):
        # Package-backed children resolve through the Hub; describe only what is
        # local and leave each package child undeclared.
        model_entries: list[dict[str, Any]] = []
        for entry in manifest.get("models") or []:
            if not isinstance(entry, Mapping):
                continue
            alias, embedded_path = entry.get("alias"), entry.get("path")
            if isinstance(alias, str) and isinstance(embedded_path, str):
                model_entries.append(
                    {
                        "alias": alias,
                        "model_dir": str(_resolve_embedded_dir(lab_dir, lab_dir, embedded_path)),
                    }
                )
        wiring = manifest.get("wiring") if isinstance(manifest.get("wiring"), list) else []
        for child in manifest.get("children") or []:
            if isinstance(child, Mapping) and isinstance(child.get("alias"), str):
                policies[child["alias"]] = None
    else:
        model_entries, wiring, _parsed = _flatten_embedded_lab_dir(
            payload_root=lab_dir,
            current_lab_dir=lab_dir,
        )

    for entry in model_entries:
        alias = str(entry["alias"])
        model_dir = Path(str(entry["model_dir"]))
        model_manifest = _load_model_manifest_from_dir(model_dir)
        declared = declared_execution_policy(model_manifest)
        reading = read_model_dir_execution_policy(model_dir, model_manifest)
        model_errors, model_warnings = source_declaration_findings(declared, reading)
        errors.extend(f"{alias}: {message}" for message in model_errors)
        warnings.extend(f"{alias}: {message}" for message in model_warnings)
        if declared is None and reading.status == "resolved" and reading.policy is not None:
            warnings.append(
                f"{alias}: add `execution_policy: {reading.policy.value}` under biosim in model.yaml "
                "so tools can tell when this model runs"
            )
        policies[alias] = declared
        models_report[alias] = {
            "declared": declared.value if declared is not None else None,
            "source": {
                "status": reading.status,
                "policy": reading.policy.value if reading.policy is not None else None,
                "detail": reading.detail,
            },
        }

    errors.extend(execution_phase_findings(policies, module_edges_from_wiring(wiring)))
    profile = describe_lab_execution(policies) if policies else unknown_lab_execution()
    return {
        "profile": profile.to_dict(),
        "errors": errors,
        "warnings": warnings,
        "models": models_report,
    }


def _bind_compatibility_ports(module: BioModule, manifest: Mapping[str, Any]) -> None:
    """Apply model.yaml port declarations, including contracts, to the module's specs."""

    from .compatibility import bind_manifest_ports, manifest_has_compatibility_declarations

    if manifest_has_compatibility_declarations(manifest):
        try:
            bind_manifest_ports(module, manifest)
        except (TypeError, ValueError) as exc:
            raise PackageError(f"model.yaml doesn't match the Python module: {exc}") from exc


def _instantiate_model_from_package(
    loaded: _LoadedPackage, parameters: Mapping[str, Any] | None = None
) -> tuple[BioModule, dict[str, Any]]:
    manifest = loaded.manifest
    bsim_block = (
        manifest.get("biosim") if isinstance(manifest.get("biosim"), Mapping) else {}
    )
    entrypoint = bsim_block.get("entrypoint")
    if not isinstance(entrypoint, str):
        raise PackageError("Model manifest is missing biosim.entrypoint")

    init_kwargs = {}
    if isinstance(bsim_block.get("init_kwargs"), Mapping):
        init_kwargs.update(dict(bsim_block.get("init_kwargs") or {}))
    if parameters:
        init_kwargs.update(dict(parameters))
    init_kwargs.update(_remote_execution_init_kwargs(manifest))

    original_sys_path = list(sys.path)
    try:
        for item in reversed(_module_paths_for_payload(loaded.payload_root)):
            if item not in sys.path:
                sys.path.insert(0, item)
        factory = _load_entrypoint(entrypoint, model_path=loaded.payload_root)
        module = factory(**init_kwargs)
    finally:
        sys.path[:] = original_sys_path
    if not isinstance(module, BioModule):
        raise PackageError(f"Entrypoint {entrypoint} did not construct a BioModule")
    _bind_execution_policy(module, manifest)
    _bind_compatibility_ports(module, manifest)
    return module, {
        "communication_step": bsim_block.get("communication_step"),
        "setup": (
            dict(bsim_block.get("setup") or {})
            if isinstance(bsim_block.get("setup"), Mapping)
            else {}
        ),
    }


def _loaded_package_from_path(
    package_path: Path, unpack_root: Path | None = None
) -> _LoadedPackage:
    unpacked_root = unpack_package(package_path, dest=unpack_root)
    package_yaml = _safe_yaml_load((unpacked_root / "package.yaml").read_bytes())
    entry_manifest = package_yaml["entry_manifest"]
    manifest = _safe_yaml_load((unpacked_root / entry_manifest).read_bytes())
    payload_root = unpacked_root / "payload"
    return _LoadedPackage(
        package_path=package_path,
        package_type=str(package_yaml["package_type"]),
        package_yaml=package_yaml,
        manifest=manifest,
        unpacked_root=unpacked_root,
        payload_root=payload_root,
    )


def _find_manifest_in_dir(directory: Path, candidates: tuple[str, ...]) -> Path:
    for candidate in candidates:
        manifest_path = directory / candidate
        if manifest_path.is_file():
            return manifest_path
    raise PackageError(f"Expected one of {', '.join(candidates)} inside {directory}")


def _load_lab_manifest_from_dir(directory: Path) -> dict[str, Any]:
    manifest_path = _find_manifest_in_dir(directory, ("lab.yaml", "lab.yml"))
    manifest = _safe_yaml_load(manifest_path.read_bytes())
    try:
        _validate_lab_manifest(manifest)
    except PackageError as exc:
        raise PackageError(
            f"Invalid embedded lab manifest at {manifest_path}: {exc}"
        ) from exc
    return manifest


def _load_model_manifest_from_dir(directory: Path) -> dict[str, Any]:
    manifest_path = _find_manifest_in_dir(
        directory, ("model.yaml", "model.yml", "biosim.yaml", "biosim.yml")
    )
    manifest = _safe_yaml_load(manifest_path.read_bytes())
    try:
        _validate_model_manifest(manifest)
        _validate_dependencies(manifest)
    except PackageError as exc:
        raise PackageError(
            f"Invalid embedded model manifest at {manifest_path}: {exc}"
        ) from exc
    return manifest


def _validate_lab_source_dir(
    *,
    source_root: Path,
    current_lab_dir: Path,
    parsed_manifest: Mapping[str, Any],
    visited: set[Path],
) -> None:
    current = current_lab_dir.resolve()
    if current in visited:
        raise PackageError(
            f"Circular embedded child lab reference detected at {current}"
        )
    visited = visited | {current}

    models = parsed_manifest.get("models")
    if isinstance(models, list):
        for entry in models:
            if not isinstance(entry, Mapping):
                continue
            embedded_path = entry.get("path")
            if not isinstance(embedded_path, str):
                continue
            model_dir = _resolve_embedded_dir(
                source_root,
                current_lab_dir,
                embedded_path,
            )
            _load_model_manifest_from_dir(model_dir)

    children = parsed_manifest.get("children")
    if not isinstance(children, list):
        return
    for entry in children:
        if not isinstance(entry, Mapping):
            continue
        embedded_path = entry.get("path")
        if not isinstance(embedded_path, str):
            continue
        child_dir = _resolve_embedded_dir(source_root, current_lab_dir, embedded_path)
        parsed_child = _load_lab_manifest_from_dir(child_dir)
        _validate_lab_source_dir(
            source_root=source_root,
            current_lab_dir=child_dir,
            parsed_manifest=parsed_child,
            visited=visited,
        )


def _resolve_embedded_dir(
    payload_root: Path, current_lab_dir: Path, dependency_path: str
) -> Path:
    normalized = _normalize_embedded_path(dependency_path)
    resolved = (current_lab_dir / normalized).resolve()
    root = payload_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise PackageError(
            f"Embedded dependency escapes package root: {dependency_path}"
        )
    if not resolved.exists():
        raise PackageError(f"Embedded dependency path not found: {dependency_path}")
    return resolved


def _instantiate_model_from_dir(
    model_dir: Path,
    *,
    manifest: Mapping[str, Any] | None = None,
    parameters: Mapping[str, Any] | None = None,
) -> tuple[BioModule, dict[str, Any]]:
    manifest = dict(manifest or _load_model_manifest_from_dir(model_dir))
    bsim_block = (
        manifest.get("biosim") if isinstance(manifest.get("biosim"), Mapping) else {}
    )
    entrypoint = bsim_block.get("entrypoint")
    if not isinstance(entrypoint, str):
        raise PackageError("Model manifest is missing biosim.entrypoint")

    init_kwargs = {}
    if isinstance(bsim_block.get("init_kwargs"), Mapping):
        init_kwargs.update(dict(bsim_block.get("init_kwargs") or {}))
    if parameters:
        init_kwargs.update(dict(parameters))
    init_kwargs.update(_remote_execution_init_kwargs(manifest))

    original_sys_path = list(sys.path)
    try:
        model_sys_path = str(model_dir.resolve())
        if model_sys_path not in sys.path:
            sys.path.insert(0, model_sys_path)
        factory = _load_entrypoint(entrypoint, model_path=model_dir.resolve())
        module = factory(**init_kwargs)
    finally:
        sys.path[:] = original_sys_path
    if not isinstance(module, BioModule):
        raise PackageError(f"Entrypoint {entrypoint} did not construct a BioModule")
    _bind_execution_policy(module, manifest)
    _bind_compatibility_ports(module, manifest)
    return module, {
        "communication_step": bsim_block.get("communication_step"),
        "setup": (
            dict(bsim_block.get("setup") or {})
            if isinstance(bsim_block.get("setup"), Mapping)
            else {}
        ),
    }


def _validate_embedded_lab_package(
    entries: Mapping[str, bytes],
    parsed_manifest: Mapping[str, Any],
    entry_manifest: str,
) -> None:
    package_root = posixpath.dirname(entry_manifest)
    _validate_embedded_lab_package_dir(
        entries=entries,
        parsed_manifest=parsed_manifest,
        package_root=package_root,
        current_dir=package_root,
        visited=set(),
    )


def _validate_embedded_lab_package_dir(
    *,
    entries: Mapping[str, bytes],
    parsed_manifest: Mapping[str, Any],
    package_root: str,
    current_dir: str,
    visited: set[str],
) -> None:
    if current_dir in visited:
        raise PackageError(
            f"Circular embedded child lab reference detected at {current_dir}"
        )
    visited = visited | {current_dir}

    models = parsed_manifest.get("models")
    if isinstance(models, list):
        for entry in models:
            if not isinstance(entry, Mapping):
                continue
            embedded_path = entry.get("path")
            if not isinstance(embedded_path, str):
                continue
            model_dir = _resolve_embedded_archive_dir(
                package_root=package_root,
                current_dir=current_dir,
                dependency_path=embedded_path,
            )
            manifest_path = _find_archive_manifest(
                entries,
                model_dir,
                ("model.yaml", "model.yml", "biosim.yaml", "biosim.yml"),
            )
            try:
                parsed_model = _safe_yaml_load(entries[manifest_path])
                _validate_model_manifest(parsed_model)
                _validate_dependencies(parsed_model)
            except PackageError as exc:
                raise PackageError(
                    f"Invalid embedded model manifest at {manifest_path}: {exc}"
                ) from exc

    children = parsed_manifest.get("children")
    if not isinstance(children, list):
        return
    for entry in children:
        if not isinstance(entry, Mapping):
            continue
        embedded_path = entry.get("path")
        if not isinstance(embedded_path, str):
            continue
        child_dir = _resolve_embedded_archive_dir(
            package_root=package_root,
            current_dir=current_dir,
            dependency_path=embedded_path,
        )
        manifest_path = _find_archive_manifest(
            entries, child_dir, ("lab.yaml", "lab.yml")
        )
        try:
            parsed_child = _safe_yaml_load(entries[manifest_path])
            _validate_lab_manifest(parsed_child)
        except PackageError as exc:
            raise PackageError(
                f"Invalid embedded child lab manifest at {manifest_path}: {exc}"
            ) from exc
        _validate_embedded_lab_package_dir(
            entries=entries,
            parsed_manifest=parsed_child,
            package_root=package_root,
            current_dir=child_dir,
            visited=visited,
        )


def _declared_input_specs(module: BioModule) -> Mapping[str, Any]:
    """Input specs a run applies: model.yaml-bound specs when the model has them."""

    manifest_inputs = getattr(module, "_biosimulant_manifest_input_specs", None)
    if isinstance(manifest_inputs, Mapping):
        return manifest_inputs
    declared = module.inputs()
    return declared if isinstance(declared, dict) else {}


def _run_model_loaded_package(
    loaded: _LoadedPackage,
    *,
    install_deps: bool = True,
    dependency_logger: DependencyLogger | None = None,
    dependency_process_tracker: ProcessTracker | None = None,
    cancel_checker: CancelChecker | None = None,
) -> dict[str, Any]:
    if install_deps:
        _install_declared_dependencies(
            loaded.manifest,
            dependency_logger=dependency_logger,
            process_tracker=dependency_process_tracker,
            cancel_checker=cancel_checker,
        )
    module, meta = _instantiate_model_from_package(loaded)
    runtime = (
        loaded.manifest.get("runtime")
        if isinstance(loaded.manifest.get("runtime"), Mapping)
        else {}
    )
    communication_step = extract_communication_step(
        None, runtime, fallback=meta["communication_step"], error_cls=PackageError
    )
    world = BioWorld(communication_step=communication_step)
    world.add_biomodule("model", module)
    world.setup({"model": meta["setup"]})
    initial_inputs = (
        runtime.get("initial_inputs")
        if isinstance(runtime.get("initial_inputs"), Mapping)
        else {}
    )
    if initial_inputs:
        module.set_inputs(
            coerce_typed_inputs(
                initial_inputs,
                _declared_input_specs(module),
                source="run",
                time_value=0.0,
                error_cls=PackageError,
            )
        )
    world.run(communication_step)
    outputs = world.get_outputs("model")
    return {
        "package": loaded.package_yaml["package"],
        "version": loaded.package_yaml["version"],
        "outputs": sorted(outputs.keys()),
        "state": module.snapshot(),
    }


def _flatten_embedded_lab_dir(
    *,
    payload_root: Path,
    current_lab_dir: Path,
    prefix: str = "",
    visited: set[Path] | None = None,
    depth: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if prefix:
        raise PackageError("Internal lab tree flattening no longer accepts a prefix")
    tree, parsed_lab = _embedded_lab_tree_from_dir(
        payload_root=payload_root,
        current_lab_dir=current_lab_dir,
        visited=visited,
        depth=depth,
    )
    flattened = flatten_lab_tree(tree, max_depth=5, error_cls=PackageError)
    return flattened.models, flattened.wiring, parsed_lab


def _embedded_lab_tree_from_dir(
    *,
    payload_root: Path,
    current_lab_dir: Path,
    visited: set[Path] | None = None,
    depth: int = 0,
) -> tuple[LabTree, dict[str, Any]]:
    if depth > 5:
        raise PackageError("Lab nesting exceeds maximum depth of 5")

    current_key = current_lab_dir.resolve()
    visited_paths = set(visited or set())
    if current_key in visited_paths:
        raise PackageError(
            f"Circular embedded child lab reference detected at {current_lab_dir}"
        )
    visited_paths.add(current_key)

    parsed_lab = _load_lab_manifest_from_dir(current_lab_dir)
    models = parsed_lab.get("models")
    wiring = parsed_lab.get("wiring")
    if not isinstance(models, list) or not isinstance(wiring, list):
        raise PackageError("Lab manifest must contain models and wiring")

    tree_models: list[LabTreeModel] = []
    tree_wiring: list[LabTreeWire] = []
    tree_children: list[LabTreeChild] = []

    for entry in models:
        if not isinstance(entry, Mapping):
            raise PackageError("Lab model entries must be mappings")
        alias = entry.get("alias")
        embedded_path = entry.get("path")
        if not isinstance(alias, str) or not alias.strip():
            raise PackageError("Lab model entries require a non-empty alias")
        if not isinstance(embedded_path, str) or not embedded_path.strip():
            raise PackageError("Lab model entries require a path reference")
        model_dir = _resolve_embedded_dir(payload_root, current_lab_dir, embedded_path)
        _load_model_manifest_from_dir(model_dir)
        parameters = (
            entry.get("parameters")
            if isinstance(entry.get("parameters"), Mapping)
            else None
        )
        tree_models.append(
            LabTreeModel(
                alias=alias,
                ref={"model_dir": str(model_dir)},
                parameters=parameters,
            )
        )

    children = parsed_lab.get("children")
    if isinstance(children, list):
        for entry in children:
            if not isinstance(entry, Mapping):
                raise PackageError("Lab child entries must be mappings")
            alias = entry.get("alias")
            embedded_path = entry.get("path")
            if not isinstance(alias, str) or not alias.strip():
                raise PackageError("Lab child entries require alias")
            if not isinstance(embedded_path, str) or not embedded_path.strip():
                raise PackageError("Lab child entries require a path reference")
            child_dir = _resolve_embedded_dir(
                payload_root, current_lab_dir, embedded_path
            )
            child_tree, child_manifest = _embedded_lab_tree_from_dir(
                payload_root=payload_root,
                current_lab_dir=child_dir,
                visited=visited_paths,
                depth=depth + 1,
            )
            tree_children.append(
                LabTreeChild(
                    alias=alias,
                    tree=child_tree,
                    io=lab_io_from_mapping(child_manifest.get("io")),
                )
            )

    for entry in wiring:
        if not isinstance(entry, Mapping):
            raise PackageError("Wiring entries must be mappings")
        from_ref = entry.get("from")
        to_refs = entry.get("to")
        if not isinstance(from_ref, str) or not isinstance(to_refs, list):
            raise PackageError("Wiring entries require from/to")
        if not all(isinstance(ref, str) for ref in to_refs):
            raise PackageError("Wiring targets must be strings")
        tree_wiring.append(LabTreeWire(from_ref=from_ref, to_refs=list(to_refs)))

    return (
        LabTree(
            models=tree_models,
            wiring=tree_wiring,
            children=tree_children,
            io=lab_io_from_mapping(parsed_lab.get("io")),
        ),
        parsed_lab,
    )


def _prepare_lab_loaded_package(
    loaded: _LoadedPackage,
    *,
    install_deps: bool = True,
    dependency_logger: DependencyLogger | None = None,
    dependency_process_tracker: ProcessTracker | None = None,
    cancel_checker: CancelChecker | None = None,
    dependency_root: Path | None = None,
) -> LabPackageRuntime:
    runtime = loaded.manifest.get("runtime")
    if not isinstance(runtime, Mapping):
        raise PackageError("Lab manifest must contain models, wiring, and runtime")
    _ensure_lab_python_version_matches_current(loaded.manifest)
    has_package_children = bool(_package_children(loaded.manifest))
    if has_package_children:
        models: list[dict[str, Any]] = []
        wiring: list[dict[str, Any]] = []
        parsed_lab = dict(loaded.manifest)
    else:
        models, wiring, parsed_lab = _flatten_embedded_lab_dir(
            payload_root=loaded.payload_root,
            current_lab_dir=loaded.payload_root,
        )

    communication_step = extract_communication_step(
        None, runtime, error_cls=PackageError
    )
    world = BioWorld(communication_step=communication_step)
    if has_package_children:
        # Package-backed children are intentionally resolved into a state
        # directory owned by this parent archive, never a process-wide cache.
        from .hub import _prepare_package_backed_lab

        state_root = dependency_root or (
            loaded.package_path.parent
            / ".biosimulant"
            / f"{loaded.package_path.stem}.dependencies"
        )
        parsed_lab, setup_config, resolved_models, modules_by_alias = _prepare_package_backed_lab(
            world=world,
            lab_root=loaded.payload_root,
            dependency_root=state_root,
            registry_url=None,
            install_deps=install_deps,
            dependency_logger=dependency_logger,
            dependency_process_tracker=dependency_process_tracker,
            cancel_checker=cancel_checker,
        )
        effective_runtime = (
            parsed_lab.get("runtime")
            if isinstance(parsed_lab.get("runtime"), Mapping)
            else runtime
        )
        world.setup(setup_config)
        initial_inputs = (
            effective_runtime.get("initial_inputs")
            if isinstance(effective_runtime.get("initial_inputs"), Mapping)
            else {}
        )
        allow_global_inputs = len(modules_by_alias) == 1
        for alias, module in modules_by_alias.items():
            alias_inputs = _select_alias_override(
                initial_inputs,
                alias,
                allow_global=allow_global_inputs,
            )
            if not alias_inputs:
                continue
            module.set_inputs(
                coerce_typed_inputs(
                    alias_inputs,
                    _declared_input_specs(module),
                    source="run",
                    time_value=0.0,
                    error_cls=PackageError,
                    recorder=world.compatibility,
                    module_name=alias,
                )
            )
        return LabPackageRuntime(
            package=str(loaded.package_yaml["package"]),
            version=str(loaded.package_yaml["version"]),
            world=world,
            manifest=parsed_lab,
            lab_path=loaded.payload_root / "lab.yaml",
            duration=float(effective_runtime.get("duration", 1.0)),
            communication_step=communication_step,
            settle_steps=extract_settle_steps(None, effective_runtime, error_cls=PackageError),
            modules=resolved_models,
        )
    builder = WiringBuilder(world)
    resolved_models: list[dict[str, Any]] = []
    setup_config: dict[str, dict[str, Any]] = {}
    modules_by_alias: dict[str, BioModule] = {}

    for entry in models:
        if not isinstance(entry, Mapping):
            raise PackageError("Lab model entries must be mappings")
        alias = entry.get("alias")
        if not isinstance(alias, str) or not alias.strip():
            raise PackageError("Lab model entries require a non-empty alias")
        model_dir = entry.get("model_dir")
        if not isinstance(model_dir, str) or not model_dir:
            raise PackageError("Lab model entries require a resolved model_dir")
        model_path = Path(model_dir)
        manifest = _load_model_manifest_from_dir(model_path)
        if install_deps:
            _install_declared_dependencies(
                manifest,
                dependency_logger=dependency_logger,
                process_tracker=dependency_process_tracker,
                cancel_checker=cancel_checker,
            )
        parameters = (
            entry.get("parameters")
            if isinstance(entry.get("parameters"), Mapping)
            else {}
        )
        module, meta = _instantiate_model_from_dir(
            model_path, manifest=manifest, parameters=parameters
        )
        if entry.get("min_dt") is not None or entry.get("priority") is not None:
            raise PackageError("Lab model entries cannot declare min_dt or priority")
        builder.add(alias, module)
        modules_by_alias[alias] = module
        if meta["setup"]:
            setup_config[alias] = dict(meta["setup"])
        relative_model_dir = (
            model_path.resolve().relative_to(loaded.payload_root.resolve()).as_posix()
        )
        resolved_entry: dict[str, Any] = {
            "alias": alias,
            "path": relative_model_dir,
        }
        model_package_name = manifest.get("package")
        model_version = manifest.get("version")
        if isinstance(model_package_name, str) and model_package_name:
            resolved_entry["package"] = model_package_name
        if isinstance(model_version, str) and model_version:
            resolved_entry["version"] = model_version
        resolved_models.append(resolved_entry)

    for edge in wiring:
        if not isinstance(edge, Mapping):
            raise PackageError("Wiring entries must be mappings")
        src = edge.get("from")
        dst = edge.get("to")
        if not isinstance(src, str) or not isinstance(dst, list):
            raise PackageError("Wiring entries require from/to")
        builder.connect(src, dst)
    builder.apply()

    effective_runtime = (
        parsed_lab.get("runtime")
        if isinstance(parsed_lab.get("runtime"), Mapping)
        else runtime
    )
    duration = float(effective_runtime.get("duration", 1.0))
    settle_steps = extract_settle_steps(None, effective_runtime, error_cls=PackageError)
    world.setup(setup_config)
    initial_inputs = (
        effective_runtime.get("initial_inputs")
        if isinstance(effective_runtime.get("initial_inputs"), Mapping)
        else {}
    )
    allow_global_inputs = len(modules_by_alias) == 1
    for alias, module in modules_by_alias.items():
        alias_inputs = _select_alias_override(
            initial_inputs,
            alias,
            allow_global=allow_global_inputs,
        )
        if not alias_inputs:
            continue
        module.set_inputs(
            coerce_typed_inputs(
                alias_inputs,
                _declared_input_specs(module),
                source="run",
                time_value=0.0,
                error_cls=PackageError,
                recorder=world.compatibility,
                module_name=alias,
            )
        )
    return LabPackageRuntime(
        package=str(loaded.package_yaml["package"]),
        version=str(loaded.package_yaml["version"]),
        world=world,
        manifest=parsed_lab,
        lab_path=loaded.payload_root / "lab.yaml",
        duration=duration,
        communication_step=communication_step,
        settle_steps=settle_steps,
        modules=resolved_models,
    )


def _run_lab_loaded_package(
    loaded: _LoadedPackage,
    *,
    install_deps: bool = True,
    dependency_logger: DependencyLogger | None = None,
    dependency_process_tracker: ProcessTracker | None = None,
    cancel_checker: CancelChecker | None = None,
    dependency_root: Path | None = None,
) -> dict[str, Any]:
    prepared = _prepare_lab_loaded_package(
        loaded,
        install_deps=install_deps,
        dependency_logger=dependency_logger,
        dependency_process_tracker=dependency_process_tracker,
        cancel_checker=cancel_checker,
        dependency_root=dependency_root,
    )
    world = prepared.world
    duration = prepared.duration
    settle_steps = prepared.settle_steps
    world.run(duration=duration)
    if settle_steps:
        world.settle(settle_steps)
    # A blocked value raises CompatibilityError carrying the partial record;
    # a completed run always carries the full record, even with no profiles.
    outputs = {
        module_name: {
            port_name: signal.to_dict()
            for port_name, signal in world.get_outputs(module_name).items()
        }
        for module_name in world.module_names
        if world.get_outputs(module_name)
    }
    return {
        "package": prepared.package,
        "version": prepared.version,
        "duration": duration,
        "communication_step": prepared.communication_step,
        "settle_steps": settle_steps,
        "execution": describe_lab_execution(world.execution_policies).to_dict(),
        "modules": prepared.modules,
        "outputs": outputs,
        "visuals": world.collect_visuals(),
        "compatibility": world.compatibility.to_dict(),
    }


def _scoped_ref(prefix: str, ref: str) -> str:
    return f"{prefix}{ref}" if prefix else ref


def _select_alias_override(
    payload: Mapping[str, Any] | None,
    alias: str,
    *,
    allow_global: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    overrides = payload.get(alias)
    flat_prefix = f"{alias}."
    legacy_flat = {
        str(key)[len(flat_prefix) :]: value
        for key, value in payload.items()
        if isinstance(key, str)
        and key.startswith(flat_prefix)
        and len(key) > len(flat_prefix)
        and not isinstance(value, Mapping)
    }
    if not allow_global:
        if isinstance(overrides, Mapping):
            merged = dict(legacy_flat)
            merged.update(dict(overrides))
            return merged
        return legacy_flat

    global_values = {
        key: value
        for key, value in payload.items()
        if key != alias
        and isinstance(key, str)
        and "." not in key
        and not isinstance(value, Mapping)
    }
    if legacy_flat:
        global_values.update(legacy_flat)
    if isinstance(overrides, Mapping):
        merged = dict(global_values)
        merged.update(dict(overrides))
        return merged
    if global_values:
        return global_values
    return {}


def _port_remap_for_child(
    *, prefix: str, child_alias: str, child_manifest: Mapping[str, Any]
) -> dict[str, str]:
    remap: dict[str, str] = {}
    io_block = child_manifest.get("io")
    if not isinstance(io_block, Mapping):
        return remap
    external_prefix = _scoped_ref(prefix, f"{child_alias}.")
    child_prefix = _scoped_ref(prefix, f"{child_alias}.")
    for section in ("inputs", "outputs"):
        ports = io_block.get(section)
        if not isinstance(ports, list):
            continue
        for entry in ports:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name")
            maps_to = entry.get("maps_to")
            if not isinstance(name, str) or not isinstance(maps_to, str):
                continue
            remap[f"{external_prefix}{name}"] = f"{child_prefix}{maps_to}"
    return remap


def _install_declared_dependencies(
    manifest: Mapping[str, Any],
    *,
    dependency_logger: DependencyLogger | None = None,
    process_tracker: ProcessTracker | None = None,
    cancel_checker: CancelChecker | None = None,
) -> None:
    runtime = manifest.get("runtime")
    if not isinstance(runtime, Mapping):
        return
    dependencies = runtime.get("dependencies")
    if not isinstance(dependencies, Mapping):
        return
    packages = dependencies.get("packages")
    if not isinstance(packages, list) or not packages:
        return
    bad = [
        dep for dep in packages if not isinstance(dep, str) or not _is_exact_pin(dep)
    ]
    if bad:
        raise PackageError(f"All dependencies must use exact pins: {bad}")
    runtime_pins = [dep for dep in packages if _is_runtime_distribution_pin(dep)]
    if runtime_pins:
        # Models run inside the interpreter of the runtime orchestrating them.
        # Installing another biosimulant there would replace that runtime
        # mid-run and in any cached managed-runtime venv.
        notice = (
            f"Using the running Biosimulant runtime {__version__}; "
            f"not installing {', '.join(runtime_pins)} into this interpreter."
        )
        if dependency_logger is not None:
            dependency_logger(notice)
        else:
            print(notice, file=sys.stderr, flush=True)
        packages = [dep for dep in packages if dep not in runtime_pins]
        if not packages:
            return
    _ensure_interpreter_scripts_on_path()
    command = _dependency_install_command(packages)
    if cancel_checker is not None:
        cancel_checker()
    if dependency_logger is None and process_tracker is None and cancel_checker is None:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            raise PackageError(
                f"Dependency installation failed with exit code {completed.returncode}.\n"
                f"Recent pip output:\n{_dependency_output_tail(completed.stdout, completed.stderr)}"
            )
        return

    if dependency_logger is not None:
        dependency_logger(f"Installing dependencies: {' '.join(packages)}")
    recent: deque[str] = deque(maxlen=40)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **_new_process_group_kwargs(),
    )
    with process_tracker(process) if process_tracker is not None else nullcontext():
        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                if cancel_checker is not None:
                    cancel_checker()
                line = raw_line.rstrip()
                if not line:
                    continue
                recent.append(line)
                if dependency_logger is not None:
                    dependency_logger(line)
        finally:
            process.stdout.close()
        returncode = process.wait()
    if cancel_checker is not None:
        cancel_checker()
    if returncode != 0:
        tail = "\n".join(recent) if recent else "No pip output captured."
        raise PackageError(
            f"Dependency installation failed with exit code {returncode}.\n"
            f"Recent pip output:\n{tail}"
        )


def _ensure_interpreter_scripts_on_path() -> None:
    """Expose console scripts from dependencies installed into this interpreter.

    Dependencies are installed with ``--python sys.executable``; a model that
    shells out to one of them (for example a ``boltz`` CLI) must find it on PATH
    even when this interpreter's environment was never activated.
    """
    scripts_dir = sysconfig.get_path("scripts")
    if not scripts_dir:
        return
    parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    if scripts_dir in parts:
        return
    os.environ["PATH"] = os.pathsep.join([scripts_dir, *parts])


def _dependency_install_command(packages: list[str]) -> list[str]:
    python = sys.executable
    if _uv_module_available():
        return [
            python,
            "-m",
            "uv",
            "pip",
            "install",
            "--python",
            python,
            *packages,
        ]
    return [python, "-m", "pip", "install", *packages]


def _uv_module_available() -> bool:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "uv", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _dependency_output_tail(stdout: str | None, stderr: str | None) -> str:
    lines: list[str] = []
    if stdout:
        lines.extend(stdout.splitlines())
    if stderr:
        lines.extend(stderr.splitlines())
    return "\n".join(lines[-40:]) if lines else "No pip output captured."


def run_package(
    path: str | Path,
    *,
    install_deps: bool = True,
    unpack_root: str | Path | None = None,
    dependency_logger: DependencyLogger | None = None,
    dependency_process_tracker: ProcessTracker | None = None,
    cancel_checker: CancelChecker | None = None,
    dependency_root: str | Path | None = None,
) -> dict[str, Any]:
    if unpack_root is None:
        with tempfile.TemporaryDirectory(prefix="biosim-pack-") as temp_dir:
            return run_package(
                path,
                install_deps=install_deps,
                unpack_root=temp_dir,
                dependency_logger=dependency_logger,
                dependency_process_tracker=dependency_process_tracker,
                cancel_checker=cancel_checker,
                dependency_root=dependency_root,
            )
    loaded = _loaded_package_from_path(
        Path(path).expanduser().resolve(),
        unpack_root=Path(unpack_root).expanduser().resolve(),
    )
    if loaded.package_type == "model":
        return _run_model_loaded_package(
            loaded,
            install_deps=install_deps,
            dependency_logger=dependency_logger,
            dependency_process_tracker=dependency_process_tracker,
            cancel_checker=cancel_checker,
        )
    if loaded.package_type == "lab":
        return _run_lab_loaded_package(
            loaded,
            install_deps=install_deps,
            dependency_logger=dependency_logger,
            dependency_process_tracker=dependency_process_tracker,
            cancel_checker=cancel_checker,
            dependency_root=(Path(dependency_root).expanduser().resolve() if dependency_root else None),
        )
    raise PackageError(f"Unsupported package type: {loaded.package_type}")


def prepare_lab_package(
    path: str | Path,
    *,
    install_deps: bool = True,
    unpack_root: str | Path | None = None,
    dependency_logger: DependencyLogger | None = None,
    dependency_process_tracker: ProcessTracker | None = None,
    cancel_checker: CancelChecker | None = None,
    dependency_root: str | Path | None = None,
) -> LabPackageRuntime:
    loaded = _loaded_package_from_path(
        Path(path).expanduser().resolve(),
        unpack_root=(
            Path(unpack_root).expanduser().resolve()
            if unpack_root is not None
            else None
        ),
    )
    if loaded.package_type != "lab":
        raise PackageError(f"Expected a lab package, got: {loaded.package_type}")
    return _prepare_lab_loaded_package(
        loaded,
        install_deps=install_deps,
        dependency_logger=dependency_logger,
        dependency_process_tracker=dependency_process_tracker,
        cancel_checker=cancel_checker,
        dependency_root=(Path(dependency_root).expanduser().resolve() if dependency_root else None),
    )


def _validate_model_manifest(manifest: Mapping[str, Any]) -> None:
    bsim = manifest.get("biosim")
    if not isinstance(bsim, Mapping):
        raise PackageError("Model manifest must contain a biosim block")
    entrypoint = bsim.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        raise PackageError("Model manifest must contain biosim.entrypoint")
    try:
        declared_execution_policy(manifest)
    except ValueError as exc:
        raise PackageError(str(exc)) from exc
    from .compatibility import validate_manifest

    findings = validate_manifest(manifest)
    if findings:
        details = "; ".join(
            f"{item.get('path') or '/'}: {item['message']}" for item in findings
        )
        raise PackageError(f"Invalid port compatibility declaration in model.yaml: {details}")


def _package_children(manifest: Mapping[str, Any]) -> list[tuple[str, str]]:
    children = manifest.get("children")
    if not isinstance(children, list):
        return []
    references: list[tuple[str, str]] = []
    for entry in children:
        if not isinstance(entry, Mapping):
            continue
        package = entry.get("package")
        version = entry.get("version")
        if package is None and version is None:
            continue
        if not isinstance(package, str) or not package.strip() or not isinstance(version, str) or not version.strip():
            raise PackageError("Package-backed lab children require package and exact version")
        references.append((package.strip(), version.strip()))
    return references


def _validate_lab_lock(manifest: Mapping[str, Any], value: Any, *, label: str) -> None:
    references = _package_children(manifest)
    if not references:
        return
    if not isinstance(value, Mapping) or value.get("lock_version") != 1:
        raise PackageError(f"{label} must declare lock_version: 1 for package-backed children")
    dependencies = value.get("dependencies")
    if not isinstance(dependencies, list):
        raise PackageError(f"{label} must contain a dependencies list")
    locked: set[tuple[str, str]] = set()
    for entry in dependencies:
        if not isinstance(entry, Mapping):
            raise PackageError(f"{label} contains an invalid dependency entry")
        package, version, sha256 = entry.get("package"), entry.get("version"), entry.get("artifact_sha256")
        if not all(isinstance(item, str) and item.strip() for item in (package, version, sha256)):
            raise PackageError(f"{label} dependency entries require package, version, and artifact_sha256")
        locked.add((package.strip(), version.strip()))
    missing = sorted(set(references) - locked)
    if missing:
        joined = ", ".join(f"{package}@{version}" for package, version in missing)
        raise PackageError(f"{label} is missing package locks for {joined}")


def _validate_lab_lock_for_dir(manifest: Mapping[str, Any], lab_dir: Path) -> None:
    if not _package_children(manifest):
        return
    lock_path = lab_dir / "biosimulant.lock"
    if not lock_path.is_file():
        raise PackageError(f"Package-backed lab children require {lock_path}")
    _validate_lab_lock(manifest, _safe_yaml_load(lock_path.read_bytes()), label=str(lock_path))


def _validate_lab_lock_for_archive(
    manifest: Mapping[str, Any], entries: Mapping[str, bytes], entry_manifest: str
) -> None:
    if not _package_children(manifest):
        return
    lock_path = f"{posixpath.dirname(entry_manifest)}/biosimulant.lock"
    if lock_path not in entries:
        raise PackageError("Package-backed lab children require payload/biosimulant.lock")
    _validate_lab_lock(manifest, _safe_yaml_load(entries[lock_path]), label=lock_path)


def _validate_lab_manifest(manifest: Mapping[str, Any]) -> None:
    models = manifest.get("models")
    children = manifest.get("children")
    has_children = isinstance(children, list) and len(children) > 0
    if not isinstance(models, list):
        if has_children:
            models = []
        else:
            raise PackageError("Lab manifest must contain a non-empty models list")
    if not models and not has_children:
        raise PackageError(
            "Lab manifest must contain a non-empty models list or children list"
        )
    aliases: set[str] = set()
    for entry in models:
        if not isinstance(entry, Mapping):
            raise PackageError("Lab model entries must be mappings")
        alias = entry.get("alias")
        if not isinstance(alias, str) or not alias.strip():
            raise PackageError("Lab model entries must define alias")
        if alias in aliases:
            raise PackageError(f"Duplicate lab model alias: {alias}")
        aliases.add(alias)
        if entry.get("repo") is not None or entry.get("manifest_path") is not None:
            raise PackageError(
                f"Lab model '{alias}' must not use repo or manifest_path"
            )
        if entry.get("package") is not None or entry.get("version") is not None:
            raise PackageError(f"Lab model '{alias}' must use path references only")
        has_path_ref = isinstance(entry.get("path"), str)
        if not has_path_ref:
            raise PackageError(f"Lab model '{alias}' must use a path reference")

    child_aliases: set[str] = set()
    if children is not None:
        if not isinstance(children, list):
            raise PackageError("Lab children entries must be a list")
        for entry in children:
            if not isinstance(entry, Mapping):
                raise PackageError("Lab child entries must be mappings")
            alias = entry.get("alias")
            if not isinstance(alias, str) or not alias.strip():
                raise PackageError("Lab child entries must define alias")
            if alias in child_aliases:
                raise PackageError(f"Duplicate lab child alias: {alias}")
            child_aliases.add(alias)
            if entry.get("repo") is not None or entry.get("manifest_path") is not None:
                raise PackageError(
                    f"Lab child '{alias}' must not use repo or manifest_path"
                )
            if entry.get("lab_id") is not None:
                raise PackageError(f"Lab child '{alias}' must not use lab_id")
            has_path_ref = isinstance(entry.get("path"), str)
            has_package_key = entry.get("package") is not None or entry.get("version") is not None
            has_package_ref = isinstance(entry.get("package"), str) and isinstance(entry.get("version"), str)
            if has_package_key and not has_package_ref:
                raise PackageError(f"Lab child '{alias}' must use path references only or package and version together")
            if has_path_ref and has_package_ref:
                raise PackageError(f"Lab child '{alias}' must use either path or package/version, not both")
            if not has_path_ref and not has_package_ref:
                raise PackageError(f"Lab child '{alias}' must use a path reference or package/version reference")

    wiring = manifest.get("wiring")
    if not isinstance(wiring, list):
        raise PackageError("Lab manifest must contain a wiring list")
    runtime = manifest.get("runtime")
    if not isinstance(runtime, Mapping):
        raise PackageError("Lab manifest must contain a runtime mapping")
    _declared_lab_python_version(manifest)
    if "tick_dt" in runtime:
        raise PackageError("Lab manifest runtime.tick_dt is not supported")
    communication_step = runtime.get("communication_step")
    if communication_step is None:
        raise PackageError("Lab manifest must contain runtime.communication_step")
    try:
        communication_step_value = float(communication_step)
    except (TypeError, ValueError) as exc:
        raise PackageError(
            "Lab manifest runtime.communication_step must be numeric"
        ) from exc
    if communication_step_value <= 0:
        raise PackageError("Lab manifest runtime.communication_step must be positive")
    extract_settle_steps(None, runtime, error_cls=PackageError)


__all__ = [
    "PACKAGE_EXTENSION",
    "PACKAGE_EXTENSIONS",
    "LabPackageRuntime",
    "PackageError",
    "PackageValidationResult",
    "build_package",
    "export_lab_package",
    "fetch_package",
    "inspect_lab_execution",
    "prepare_lab_package",
    "publish_package",
    "run_package",
    "unpack_package",
    "validate_lab_source",
    "validate_package",
]
