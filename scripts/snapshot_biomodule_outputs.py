#!/usr/bin/env python3
"""Capture regression snapshots for BioModule model and lab source trees."""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from biosim.pack import (  # noqa: E402
    PackageError,
    _flatten_embedded_lab_dir,
    _instantiate_model_from_dir,
    _load_model_manifest_from_dir,
    _select_alias_override,
    coerce_typed_inputs,
    extract_communication_step,
)
from biosim.dependency_runtime import collect_requirements, require_current_environment
from biosim.wiring import WiringBuilder  # noqa: E402
from biosim.world import BioWorld  # noqa: E402


def _normalize(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return _normalize(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize(item) for item in value]
    return value


def _scrub_paths(value: Any, source_root: Path | None) -> Any:
    if source_root is None:
        return value
    root = source_root.expanduser().resolve().as_posix()
    if isinstance(value, Mapping):
        return {str(key): _scrub_paths(value[key], source_root) for key in sorted(value)}
    if isinstance(value, list):
        return [_scrub_paths(item, source_root) for item in value]
    if isinstance(value, str):
        return value.replace(root, "${SOURCE_ROOT}")
    return value


def _specs_to_dict(specs: Mapping[str, Any]) -> dict[str, Any]:
    return {name: _normalize(spec) for name, spec in sorted(specs.items())}


def _signals_to_dict(signals: Mapping[str, Any]) -> dict[str, Any]:
    return {name: _normalize(signal) for name, signal in sorted(signals.items())}


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise PackageError(f"Expected mapping in {path}")
    return loaded


def _with_resolved_visualisation_sources(
    manifest: Mapping[str, Any],
    parameters: Mapping[str, Any] | None,
    model_paths_by_alias: Mapping[str, Path],
) -> dict[str, Any]:
    """Fill lab-local visualisation source paths for models that need them."""

    bsim = manifest.get("biosim") if isinstance(manifest.get("biosim"), Mapping) else {}
    init_kwargs = dict(bsim.get("init_kwargs") or {}) if isinstance(bsim.get("init_kwargs"), Mapping) else {}
    if parameters:
        init_kwargs.update(dict(parameters))
    sources = init_kwargs.get("sources")
    if not isinstance(sources, list):
        return dict(parameters or {})

    resolved_sources: list[dict[str, Any]] = []
    changed = False
    for source in sources:
        if not isinstance(source, Mapping):
            resolved_sources.append(source)
            continue
        source_alias = source.get("alias")
        resolved = dict(source)
        if isinstance(source_alias, str) and "resolved_path" not in resolved:
            model_path = model_paths_by_alias.get(source_alias)
            if model_path is not None:
                resolved["resolved_path"] = str(model_path)
                changed = True
        resolved_sources.append(resolved)
    if not changed:
        return dict(parameters or {})
    return {**dict(parameters or {}), "sources": resolved_sources}


def _source_label(path: Path, source_root: Path | None) -> str:
    path = path.expanduser().resolve()
    if source_root is not None:
        root = source_root.expanduser().resolve()
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            pass
    return str(path)


def snapshot_model_dir(
    model_dir: Path,
    *,
    duration: float | None,
    install_deps: bool,
    source_root: Path | None,
) -> dict[str, Any]:
    manifest = _load_model_manifest_from_dir(model_dir)
    require_current_environment(collect_requirements([("model", manifest)]))
    module, meta = _instantiate_model_from_dir(model_dir, manifest=manifest)
    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), Mapping) else {}
    communication_step = extract_communication_step(
        None,
        runtime,
        fallback=meta["communication_step"],
        error_cls=PackageError,
    )
    run_duration = float(duration if duration is not None else communication_step)
    world = BioWorld(communication_step=communication_step)
    world.add_biomodule("model", module)
    world.setup({"model": dict(meta["setup"])})
    initial_inputs = runtime.get("initial_inputs") if isinstance(runtime, Mapping) else {}
    if isinstance(initial_inputs, Mapping) and initial_inputs:
        declared_inputs = module.inputs() if isinstance(module.inputs(), dict) else {}
        module.set_inputs(
            coerce_typed_inputs(
                initial_inputs,
                declared_inputs,
                source="snapshot",
                time_value=0.0,
                error_cls=PackageError,
            )
        )
    world.run(duration=run_duration)
    visuals = module.visualize()
    return {
        "kind": "model",
        "source": _source_label(model_dir, source_root),
        "duration": run_duration,
        "inputs": _specs_to_dict(module.inputs() if isinstance(module.inputs(), dict) else {}),
        "outputs": _specs_to_dict(module.outputs() if isinstance(module.outputs(), dict) else {}),
        "signals": _signals_to_dict(world.get_outputs("model")),
        "state": _normalize(module.snapshot()),
        "visuals": _normalize(visuals or []),
    }


def snapshot_lab_dir(
    lab_dir: Path,
    *,
    duration: float | None,
    install_deps: bool,
    source_root: Path | None,
) -> dict[str, Any]:
    parsed_lab = _load_yaml(lab_dir / "lab.yaml")
    runtime = parsed_lab.get("runtime") if isinstance(parsed_lab.get("runtime"), Mapping) else {}
    dependency_manifests = []
    models, wiring, _ = _flatten_embedded_lab_dir(
        payload_root=lab_dir, current_lab_dir=lab_dir,
        dependency_manifests=dependency_manifests,
    )
    dependency_manifests.extend(
        (entry["alias"], _load_model_manifest_from_dir(Path(entry["model_dir"])))
        for entry in models
    )
    require_current_environment(collect_requirements(dependency_manifests))
    communication_step = extract_communication_step(None, runtime, error_cls=PackageError)
    run_duration = float(duration if duration is not None else runtime.get("duration", 1.0))

    world = BioWorld(communication_step=communication_step)
    builder = WiringBuilder(world)
    modules_by_alias = {}
    setup_config: dict[str, dict[str, Any]] = {}
    manifests_by_alias: dict[str, Mapping[str, Any]] = {}
    model_paths_by_alias = {
        str(entry["alias"]): Path(entry["model_dir"]).resolve()
        for entry in models
    }

    for entry in models:
        alias = entry["alias"]
        model_dir = model_paths_by_alias[alias]
        manifest = _load_model_manifest_from_dir(model_dir)
        manifests_by_alias[alias] = manifest
        parameters = entry.get("parameters") if isinstance(entry.get("parameters"), Mapping) else {}
        module, meta = _instantiate_model_from_dir(
            model_dir,
            manifest=manifest,
            parameters=_with_resolved_visualisation_sources(manifest, parameters, model_paths_by_alias),
        )
        builder.add(alias, module)
        modules_by_alias[alias] = module
        if meta["setup"]:
            setup_config[alias] = dict(meta["setup"])

    for edge in wiring:
        builder.connect(edge["from"], edge["to"])
    builder.apply()
    world.setup(setup_config)

    initial_inputs = runtime.get("initial_inputs") if isinstance(runtime.get("initial_inputs"), Mapping) else {}
    for alias, module in modules_by_alias.items():
        alias_inputs = _select_alias_override(initial_inputs, alias)
        if not alias_inputs:
            continue
        declared_inputs = module.inputs() if isinstance(module.inputs(), dict) else {}
        module.set_inputs(
            coerce_typed_inputs(
                alias_inputs,
                declared_inputs,
                source="snapshot",
                time_value=0.0,
                error_cls=PackageError,
            )
        )

    world.run(duration=run_duration)
    return {
        "kind": "lab",
        "source": _source_label(lab_dir, source_root),
        "duration": run_duration,
        "modules": sorted(modules_by_alias),
        "inputs": {
            alias: _specs_to_dict(module.inputs() if isinstance(module.inputs(), dict) else {})
            for alias, module in sorted(modules_by_alias.items())
        },
        "outputs": {
            alias: _specs_to_dict(module.outputs() if isinstance(module.outputs(), dict) else {})
            for alias, module in sorted(modules_by_alias.items())
        },
        "signals": {
            alias: _signals_to_dict(world.get_outputs(alias))
            for alias in sorted(modules_by_alias)
        },
        "state": _normalize(world.snapshot()),
        "visuals": _normalize(world.collect_visuals()),
    }


def snapshot_target(
    path: Path,
    *,
    duration: float | None,
    install_deps: bool,
    source_root: Path | None,
) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if (path / "model.yaml").exists():
        return snapshot_model_dir(path, duration=duration, install_deps=install_deps, source_root=source_root)
    if (path / "lab.yaml").exists():
        return snapshot_lab_dir(path, duration=duration, install_deps=install_deps, source_root=source_root)
    raise PackageError(f"Expected model.yaml or lab.yaml under {path}")


def _diff_payloads(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> str:
    expected_text = json.dumps(_normalize(expected), indent=2, sort_keys=True).splitlines(keepends=True)
    actual_text = json.dumps(_normalize(actual), indent=2, sort_keys=True).splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            expected_text,
            actual_text,
            fromfile="baseline",
            tofile="current",
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="+", type=Path, help="Model or lab source directories to run.")
    parser.add_argument("--duration", type=float, help="Override run duration for every target.")
    parser.add_argument("--install-deps", action="store_true", help="Install manifest runtime dependencies before running.")
    parser.add_argument(
        "--source-root",
        type=Path,
        help="Store target source paths relative to this root for portable committed snapshots.",
    )
    parser.add_argument("--compare", type=Path, help="Fail if the generated snapshot differs from this baseline JSON.")
    parser.add_argument("--output", type=Path, help="Write snapshot JSON to this path.")
    args = parser.parse_args()

    snapshots = [
        snapshot_target(
            target,
            duration=args.duration,
            install_deps=args.install_deps,
            source_root=args.source_root,
        )
        for target in args.targets
    ]
    payload = _scrub_paths(_normalize({"snapshots": snapshots}), args.source_root)
    if args.compare:
        baseline = json.loads(args.compare.read_text(encoding="utf-8"))
        baseline = _normalize(baseline)
        if baseline != payload:
            sys.stderr.write(_diff_payloads(baseline, payload))
            return 1
    text = json.dumps(_normalize(payload), indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    elif not args.compare:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
