from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .pack import PackageError, build_package, validate_package


_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"$"
)
_VALID_PACKAGE_SEGMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_VALID_VISIBILITIES = {"private", "public"}
_TYPE_EXTENSION = {"lab": ".bsilab"}
_TYPE_MANIFEST = {"lab": "lab.yaml"}


@dataclass(frozen=True)
class PackageRepoEntry:
    id: str | None
    package: str
    version: str
    package_type: str
    path: Path
    visibility: str
    publish: bool | None = None
    source: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PackageRepoManifest:
    path: Path
    root: Path
    schema_version: str | None
    namespace: str | None
    default_visibility: str
    packages: list[PackageRepoEntry]


def load_package_repo_manifest(path: str | Path) -> PackageRepoManifest:
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_file():
        raise PackageError(f"Package manifest not found: {manifest_path}")
    raw = _safe_yaml_load(manifest_path)
    if not isinstance(raw, Mapping):
        raise PackageError("Package manifest must be a YAML mapping")

    root = manifest_path.parent
    schema_version = raw.get("schema_version")
    if schema_version is not None:
        schema_version = str(schema_version)
        if schema_version != "1":
            raise PackageError(f"Unsupported schema_version {schema_version}; expected 1")

    namespace = _optional_str(raw, "namespace")
    default_visibility = _optional_str(raw, "default_visibility") or "private"
    _validate_visibility(default_visibility)

    packages_raw = raw.get("packages")
    if not isinstance(packages_raw, list) or not packages_raw:
        raise PackageError("Package manifest must declare at least one package")

    entries: list[PackageRepoEntry] = []
    seen_ids: set[str] = set()
    seen_package_versions: set[str] = set()
    for index, item in enumerate(packages_raw, start=1):
        if not isinstance(item, Mapping):
            raise PackageError(f"Package entry #{index} must be a mapping")
        entry_id = _optional_str(item, "id")
        if entry_id:
            if entry_id in seen_ids:
                raise PackageError(f"Duplicate package id: {entry_id}")
            seen_ids.add(entry_id)

        package = _required_str(item, "package", index)
        _validate_package_ref(package)
        version = _required_str(item, "version", index)
        _validate_semver(version)
        package_type = _required_str(item, "type", index)
        if package_type not in _TYPE_EXTENSION:
            raise PackageError(
                f"Package entry #{index} type must be one of: {', '.join(sorted(_TYPE_EXTENSION))}"
            )
        visibility = _optional_str(item, "visibility") or default_visibility
        _validate_visibility(visibility)

        key = f"{package}@{version}"
        if key in seen_package_versions:
            raise PackageError(f"Duplicate package entry: {key}")
        seen_package_versions.add(key)

        rel_path = _required_str(item, "path", index)
        source_dir = (root / rel_path).resolve()
        if not source_dir.is_dir():
            raise PackageError(
                f"Package {package} source path is missing or not a directory: {source_dir}"
            )
        source_manifest = source_dir / _TYPE_MANIFEST[package_type]
        if not source_manifest.is_file():
            raise PackageError(f"Package {package} is missing {source_manifest}")

        source = item.get("source") or {}
        if not isinstance(source, Mapping):
            raise PackageError(f"Package {package} source metadata must be a mapping")

        publish = item.get("publish")
        if publish is not None and not isinstance(publish, bool):
            raise PackageError(f"Package {package} publish must be true or false")

        entries.append(
            PackageRepoEntry(
                id=entry_id,
                package=package,
                version=version,
                package_type=package_type,
                path=Path(rel_path),
                visibility=visibility,
                publish=publish,
                source={str(key): value for key, value in source.items()},
            )
        )

    manifest = PackageRepoManifest(
        path=manifest_path,
        root=root,
        schema_version=schema_version,
        namespace=namespace,
        default_visibility=default_visibility,
        packages=entries,
    )
    _validate_builds_without_writing_to_source(manifest)
    return manifest


def validate_package_repo(path: str | Path) -> PackageRepoManifest:
    return load_package_repo_manifest(path)


def build_package_repo(path: str | Path, out_dir: str | Path) -> list[dict[str, Any]]:
    manifest = load_package_repo_manifest(path)
    target_dir = Path(out_dir).expanduser()
    if not target_dir.is_absolute():
        target_dir = manifest.root / target_dir
    target_dir = target_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    built: list[dict[str, Any]] = []
    for entry in manifest.packages:
        package_file = target_dir / _package_filename(entry.package, entry.version, entry.package_type)
        package_path = build_package(
            manifest.root / entry.path,
            output_path=package_file,
            package_name=entry.package,
            version=entry.version,
            visibility=entry.visibility,
            source=_source_metadata(manifest, entry),
        )
        validation = validate_package(package_path)
        if not validation.valid:
            raise PackageError("; ".join(validation.errors))
        data = package_path.read_bytes()
        built.append(
            {
                "package": entry.package,
                "version": entry.version,
                "package_type": entry.package_type,
                "visibility": entry.visibility,
                "path": str(package_path),
                "archive_sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "logical_sha256": validation.metadata.get("sha256")
                if validation.metadata
                else None,
            }
        )
    return built


def _validate_builds_without_writing_to_source(manifest: PackageRepoManifest) -> None:
    with tempfile.TemporaryDirectory(prefix="biosim-package-repo-validate-") as tmp:
        tmp_dir = Path(tmp)
        for entry in manifest.packages:
            target = tmp_dir / _package_filename(entry.package, entry.version, entry.package_type)
            package_path = build_package(
                manifest.root / entry.path,
                output_path=target,
                package_name=entry.package,
                version=entry.version,
                visibility=entry.visibility,
                source=_source_metadata(manifest, entry),
            )
            result = validate_package(package_path)
            if not result.valid:
                raise PackageError(
                    f"Package {entry.package}@{entry.version} is invalid: "
                    + "; ".join(result.errors)
                )


def _safe_yaml_load(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Package repository support requires PyYAML. Install with: pip install pyyaml"
        ) from exc
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _optional_str(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PackageError(f"{key} must be a string")
    value = value.strip()
    return value or None


def _required_str(mapping: Mapping[str, Any], key: str, index: int) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PackageError(f"Package entry #{index} must declare a non-empty {key}")
    return value.strip()


def _validate_visibility(value: str) -> None:
    if value not in _VALID_VISIBILITIES:
        raise PackageError(
            f"Invalid visibility {value}; expected one of: {', '.join(sorted(_VALID_VISIBILITIES))}"
        )


def _validate_semver(value: str) -> None:
    if not _SEMVER_RE.match(value):
        raise PackageError(f"Invalid SemVer version: {value}")


def _validate_package_ref(value: str) -> None:
    parts = value.split("/")
    if len(parts) not in {1, 2} or any(not part for part in parts):
        raise PackageError("Package names must be `name` or `namespace/name`")
    for part in parts:
        if not _VALID_PACKAGE_SEGMENT_RE.match(part):
            raise PackageError(
                "Package names must contain only lowercase letters, digits, and dashes, "
                "and must start and end with a lowercase letter or digit"
            )


def _package_filename(package: str, version: str, package_type: str) -> str:
    slug = "__".join(part for part in package.split("/") if part)
    return f"{slug}-{version}{_TYPE_EXTENSION[package_type]}"


def _source_metadata(manifest: PackageRepoManifest, entry: PackageRepoEntry) -> dict[str, Any]:
    data: dict[str, Any] = {
        "path": entry.path.as_posix(),
        "package_manifest_file": manifest.path.name,
    }
    data.update(entry.source)
    return data


__all__ = [
    "PackageRepoEntry",
    "PackageRepoManifest",
    "build_package_repo",
    "load_package_repo_manifest",
    "validate_package_repo",
]
