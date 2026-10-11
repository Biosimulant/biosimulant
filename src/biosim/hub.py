"""Hub-backed, lab-local composition helpers.

This module deliberately exposes an explicit composition API instead of an
import hook: resolving a Hub dependency can perform network I/O and must never
be a side effect of importing user code.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .pack import (
    PackageError,
    _instantiate_model_from_dir,
    _load_lab_manifest_from_dir,
    _load_model_manifest_from_dir,
    _resolve_embedded_dir,
    _safe_yaml_dump,
    unpack_package,
    validate_package,
)
from .registry import PublicRegistryClient, cached_lab_destination_for_reference, parse_package_reference
from .runtime import LabTree, LabTreeChild, LabTreeModel, LabTreeWire, flatten_lab_tree, lab_io_from_mapping
from .wiring import WiringBuilder
from .world import BioWorld


LOCK_FILE_NAME = "biosimulant.lock"
STATE_DIRECTORY_NAME = ".biosimulant"
DEPENDENCY_DIRECTORY_NAME = "dependencies"


def dependency_directory(lab_root: str | Path) -> Path:
    """Return the disposable, lab-owned dependency directory."""

    return Path(lab_root).expanduser().resolve() / STATE_DIRECTORY_NAME / DEPENDENCY_DIRECTORY_NAME


def materialize_vendored_lab(
    source_root: str | Path,
    destination: str | Path,
    *,
    registry_url: str | None = None,
) -> Path:
    """Copy a Lab source tree and replace locked Hub children with owned paths.

    The source Lab and its ``.biosimulant`` operational state are left untouched.
    The returned tree is suitable for archival packaging and contains every
    transitive Hub child under ``labs/<alias>``.
    """

    source = Path(source_root).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if not source.is_dir():
        raise PackageError(f"Lab root not found: {source}")
    if target.exists():
        raise PackageError(f"Vendored lab destination already exists: {target}")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(STATE_DIRECTORY_NAME))
    resolver = _LabDependencyResolver(lab_root=source, registry_url=registry_url)

    def materialize(lab_dir: Path, references: set[tuple[str, str]]) -> None:
        manifest = _load_lab_manifest_from_dir(lab_dir)
        changed = False
        for child in manifest.get("children", []) or []:
            if not isinstance(child, dict):
                raise PackageError("Lab child entries must be mappings")
            alias = child.get("alias")
            if not isinstance(alias, str) or not alias:
                raise PackageError("Lab child entries require alias")
            package, version = child.get("package"), child.get("version")
            path = child.get("path")
            if isinstance(package, str) or isinstance(version, str):
                if not isinstance(package, str) or not isinstance(version, str):
                    raise PackageError(f"Lab child '{alias}' requires package and exact version")
                reference = (package, version)
                if reference in references:
                    raise PackageError(f"Circular Hub lab reference detected at {package}@{version}")
                dependency_dir = resolver.resolve(
                    package=package, version=version, declaring_lab_dir=lab_dir
                )
                relative_path = Path("labs") / alias
                child_target = lab_dir / relative_path
                if child_target.exists():
                    raise PackageError(f"Vendored dependency target already exists: {child_target}")
                shutil.copytree(dependency_dir, child_target, ignore=shutil.ignore_patterns(STATE_DIRECTORY_NAME))
                child.clear()
                child.update({"alias": alias, "path": relative_path.as_posix()})
                materialize(child_target, {*references, reference})
                changed = True
            elif isinstance(path, str) and path:
                materialize(_resolve_embedded_dir(lab_dir, lab_dir, path), references)
            else:
                raise PackageError(f"Lab child '{alias}' requires path or package and version")
        if changed:
            (lab_dir / "lab.yaml").write_bytes(_safe_yaml_dump(manifest))

    materialize(target, set())
    return target


def _lock_entries(lab_dir: Path) -> dict[tuple[str, str], str]:
    lock_path = lab_dir / LOCK_FILE_NAME
    if not lock_path.is_file():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - package dependency guard
        raise PackageError("Hub composition requires PyYAML") from exc
    value = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, Mapping) or value.get("lock_version") != 1:
        raise PackageError(f"{lock_path} must declare lock_version: 1")
    dependencies = value.get("dependencies")
    if not isinstance(dependencies, list):
        raise PackageError(f"{lock_path} must contain a dependencies list")
    entries: dict[tuple[str, str], str] = {}
    for item in dependencies:
        if not isinstance(item, Mapping):
            raise PackageError(f"{lock_path} contains an invalid dependency")
        package = item.get("package")
        version = item.get("version")
        sha256 = item.get("artifact_sha256")
        if not all(isinstance(value, str) and value for value in (package, version, sha256)):
            raise PackageError(f"{lock_path} dependency entries require package, version, and artifact_sha256")
        key = (package, version)
        if key in entries:
            raise PackageError(f"{lock_path} contains duplicate dependency {package}@{version}")
        entries[key] = sha256.lower()
    return entries


class _LabDependencyResolver:
    def __init__(
        self,
        *,
        lab_root: Path,
        registry_url: str | None = None,
        dependency_root: Path | None = None,
    ) -> None:
        self.lab_root = lab_root.resolve()
        self.dependency_root = (dependency_root or dependency_directory(self.lab_root)).resolve()
        self.client = PublicRegistryClient(registry_url)

    def resolve(self, *, package: str, version: str, declaring_lab_dir: Path) -> Path:
        reference = f"{package}@{version}"
        parsed = parse_package_reference(reference)
        if parsed is None:
            raise PackageError(f"Invalid Hub lab reference: {reference}")
        expected_sha = _lock_entries(declaring_lab_dir).get((package, version))
        if expected_sha is None:
            raise PackageError(
                f"{declaring_lab_dir / LOCK_FILE_NAME} is missing a lock entry for {reference}"
            )

        artifact = self.client.resolve_package(package, version)
        if artifact.get("package_type") != "lab":
            raise PackageError(f"Package {reference} is not a lab")
        registry_sha = str(artifact.get("sha256") or "").lower()
        if not registry_sha:
            raise PackageError(f"Registry did not provide an immutable SHA-256 for {reference}")
        if registry_sha != expected_sha:
            raise PackageError(f"Locked checksum does not match Hub artifact for {reference}")

        destination = cached_lab_destination_for_reference(
            reference, artifact, cache_dir=self.dependency_root
        )
        if (destination / "lab.yaml").is_file() or (destination / "lab.yml").is_file():
            return destination

        self.dependency_root.mkdir(parents=True, exist_ok=True)
        if getattr(self.client, "_v1", False):
            archive = self.client.download_package(
                str(artifact.get("id") or ""),
                package_name=package,
                version=version,
            )
        else:
            archive = self.client.download_package(str(artifact["id"]))
        if hashlib.sha256(archive).hexdigest().lower() != expected_sha:
            raise PackageError(f"Downloaded checksum does not match lockfile for {reference}")
        with tempfile.TemporaryDirectory(prefix="hub-dependency-", dir=self.dependency_root) as temp_dir:
            temp_root = Path(temp_dir)
            archive_path = temp_root / "dependency.bsilab"
            archive_path.write_bytes(archive)
            validation = validate_package(archive_path)
            if not validation.valid:
                raise PackageError("; ".join(validation.errors))
            unpacked = unpack_package(archive_path, dest=temp_root / "unpacked")
            payload = unpacked / "payload"
            if not (payload / "lab.yaml").is_file() and not (payload / "lab.yml").is_file():
                raise PackageError(f"Hub package {reference} is missing a lab manifest")
            staged = temp_root / "staged"
            shutil.copytree(payload, staged)
            if destination.exists():
                shutil.rmtree(destination)
            staged.replace(destination)
        return destination


def _tree_from_lab_dir(
    *,
    lab_dir: Path,
    resolver: _LabDependencyResolver,
    visited: set[Path] | None = None,
    depth: int = 0,
    dependency_manifests: list[tuple[str, Mapping[str, Any]]] | None = None,
) -> tuple[LabTree, dict[str, Any]]:
    if depth > 5:
        raise PackageError("Lab nesting exceeds maximum depth of 5")
    key = lab_dir.resolve()
    seen = set(visited or set())
    if key in seen:
        raise PackageError(f"Circular child lab reference detected at {lab_dir}")
    seen.add(key)
    manifest = _load_lab_manifest_from_dir(lab_dir)
    if dependency_manifests is not None:
        dependency_manifests.append((str(lab_dir / "lab.yaml"), manifest))
    tree = LabTree(io=lab_io_from_mapping(manifest.get("io")))
    for entry in manifest.get("models", []):
        if not isinstance(entry, Mapping):
            raise PackageError("Lab model entries must be mappings")
        alias, path = entry.get("alias"), entry.get("path")
        if not isinstance(alias, str) or not isinstance(path, str):
            raise PackageError("Lab model entries require alias and path")
        model_dir = _resolve_embedded_dir(lab_dir, lab_dir, path)
        _load_model_manifest_from_dir(model_dir)
        tree.models = [*tree.models, LabTreeModel(alias=alias, ref={"model_dir": str(model_dir)}, parameters=entry.get("parameters") if isinstance(entry.get("parameters"), Mapping) else None)]
    for entry in manifest.get("children", []) or []:
        if not isinstance(entry, Mapping):
            raise PackageError("Lab child entries must be mappings")
        alias = entry.get("alias")
        if not isinstance(alias, str) or not alias:
            raise PackageError("Lab child entries require alias")
        path = entry.get("path")
        if isinstance(path, str) and path:
            child_dir = _resolve_embedded_dir(lab_dir, lab_dir, path)
        else:
            package, version = entry.get("package"), entry.get("version")
            if not isinstance(package, str) or not isinstance(version, str):
                raise PackageError(f"Lab child '{alias}' requires path or package and version")
            child_dir = resolver.resolve(package=package, version=version, declaring_lab_dir=lab_dir)
        child_tree, child_manifest = _tree_from_lab_dir(
            lab_dir=child_dir, resolver=resolver, visited=seen, depth=depth + 1,
            dependency_manifests=dependency_manifests,
        )
        tree.children.append(LabTreeChild(alias=alias, tree=child_tree, io=lab_io_from_mapping(child_manifest.get("io"))))
    for entry in manifest.get("wiring", []):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("from"), str) or not isinstance(entry.get("to"), list):
            raise PackageError("Lab wiring entries require from/to")
        tree.wiring = [*tree.wiring, LabTreeWire(from_ref=entry["from"], to_refs=entry["to"])]
    return tree, manifest


class HubComposition:
    """Explicitly resolve Hub Labs into one caller-owned :class:`BioWorld`."""

    def __init__(
        self,
        world: BioWorld,
        lab_root: str | Path,
        registry_url: str | None = None,
        dependency_root: str | Path | None = None,
    ) -> None:
        self.world = world
        self.lab_root = Path(lab_root).expanduser().resolve()
        if not self.lab_root.is_dir():
            raise PackageError(f"Lab root not found: {self.lab_root}")
        self._resolver = _LabDependencyResolver(
            lab_root=self.lab_root,
            registry_url=registry_url,
            dependency_root=Path(dependency_root).expanduser() if dependency_root else None,
        )
        self._tree = LabTree()
        self._dependency_manifests: list[tuple[str, Mapping[str, Any]]] = []
        self._setup_config: dict[str, dict[str, Any]] = {}

    @property
    def dependency_root(self) -> Path:
        return self._resolver.dependency_root

    def add(self, alias: str, reference: str) -> "HubComposition":
        parsed = parse_package_reference(reference)
        if parsed is None or parsed.version is None:
            raise PackageError("Hub components require namespace/name@exact-version")
        if any(child.alias == alias for child in self._tree.children):
            raise PackageError(f"Duplicate Hub component alias: {alias}")
        lab_dir = self._resolver.resolve(package=parsed.package_name, version=parsed.version, declaring_lab_dir=self.lab_root)
        child_tree, manifest = _tree_from_lab_dir(
            lab_dir=lab_dir, resolver=self._resolver,
            dependency_manifests=self._dependency_manifests,
        )
        self._tree.children.append(LabTreeChild(alias=alias, tree=child_tree, io=lab_io_from_mapping(manifest.get("io"))))
        return self

    def connect(self, source: str, targets: list[str]) -> "HubComposition":
        self._tree.wiring = [*self._tree.wiring, LabTreeWire(from_ref=source, to_refs=targets)]
        return self

    def apply(self, *, install_deps: bool = True) -> None:
        self._setup_config, _modules, _module_instances = _apply_tree_to_world(
            world=self.world, tree=self._tree, install_deps=install_deps,
            dependency_manifests=self._dependency_manifests,
        )

    def setup(self) -> None:
        """Initialize the world with model setup declarations from Hub Labs."""

        self.world.setup(self._setup_config)


def _apply_tree_to_world(
    *,
    world: BioWorld,
    tree: LabTree,
    install_deps: bool,
    dependency_logger: Any = None,
    dependency_process_tracker: Any = None,
    cancel_checker: Any = None,
    dependency_manifests: list[tuple[str, Mapping[str, Any]]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    flattened = flatten_lab_tree(tree, error_cls=PackageError)
    from .dependency_runtime import collect_requirements, require_current_environment

    manifests = list(dependency_manifests or [])
    manifests.extend(
        (entry["alias"], _load_model_manifest_from_dir(Path(entry["model_dir"])))
        for entry in flattened.models
    )
    require_current_environment(collect_requirements(manifests))
    builder = WiringBuilder(world)
    setup_config: dict[str, dict[str, Any]] = {}
    resolved: list[dict[str, Any]] = []
    modules_by_alias: dict[str, Any] = {}
    for entry in flattened.models:
        alias = entry["alias"]
        model_dir = Path(str(entry["model_dir"]))
        manifest = _load_model_manifest_from_dir(model_dir)
        module, meta = _instantiate_model_from_dir(
            model_dir, manifest=manifest, parameters=entry.get("parameters") or {}
        )
        builder.add(alias, module)
        modules_by_alias[alias] = module
        if meta.get("setup"):
            setup_config[alias] = dict(meta["setup"])
        resolved.append({"alias": alias, "path": str(model_dir)})
    for edge in flattened.wiring:
        builder.connect(edge["from"], edge["to"])
    builder.apply()
    return setup_config, resolved, modules_by_alias


def _prepare_package_backed_lab(
    *,
    world: BioWorld,
    lab_root: Path,
    dependency_root: Path,
    registry_url: str | None,
    install_deps: bool,
    dependency_logger: Any = None,
    dependency_process_tracker: Any = None,
    cancel_checker: Any = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    resolver = _LabDependencyResolver(
        lab_root=lab_root, registry_url=registry_url, dependency_root=dependency_root
    )
    manifests: list[tuple[str, Mapping[str, Any]]] = []
    tree, manifest = _tree_from_lab_dir(
        lab_dir=lab_root, resolver=resolver, dependency_manifests=manifests,
    )
    setup_config, modules, modules_by_alias = _apply_tree_to_world(
        world=world,
        tree=tree,
        install_deps=install_deps,
        dependency_manifests=manifests,
        dependency_logger=dependency_logger,
        dependency_process_tracker=dependency_process_tracker,
        cancel_checker=cancel_checker,
    )
    return manifest, setup_config, modules, modules_by_alias


__all__ = [
    "HubComposition",
    "LOCK_FILE_NAME",
    "dependency_directory",
    "materialize_vendored_lab",
]
