# SPDX-FileCopyrightText: 2026-present Biosimulant Team
#
# SPDX-License-Identifier: MIT
"""Shared parameter and runtime overlays for local lab runs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _initial_input_ref_parts(
    ref: str,
    aliases: set[str] | None = None,
) -> tuple[str, str] | None:
    if aliases:
        matching_aliases = [
            alias
            for alias in aliases
            if ref.startswith(f"{alias}.") and len(ref) > len(alias) + 1
        ]
        if matching_aliases:
            alias = max(matching_aliases, key=len)
            return alias, ref[len(alias) + 1 :]
    if ref.count(".") != 1:
        return None
    alias, port = ref.split(".", 1)
    if not alias or not port:
        return None
    return alias, port


def _merge_nested_input(
    output: dict[str, Any],
    alias: str,
    values: Mapping[str, Any],
) -> None:
    current = output.get(alias)
    if isinstance(current, dict):
        current.update(dict(values))
    else:
        output[alias] = dict(values)


def map_initial_inputs(
    manifest: Mapping[str, Any],
    value: Any,
) -> dict[str, Any]:
    """Map public or dotted input names to their model-local input structure."""

    if not isinstance(value, Mapping):
        return {}
    name_to_ref: dict[str, str] = {}
    model_aliases: set[str] = set()
    models = manifest.get("models")
    if isinstance(models, list):
        for entry in models:
            if isinstance(entry, Mapping) and isinstance(entry.get("alias"), str):
                model_aliases.add(str(entry["alias"]))
    io = manifest.get("io")
    if isinstance(io, Mapping):
        inputs = io.get("inputs")
        if isinstance(inputs, list):
            for port in inputs:
                if not isinstance(port, Mapping):
                    continue
                name = port.get("name")
                maps_to = port.get("maps_to")
                if isinstance(name, str) and isinstance(maps_to, str):
                    name_to_ref[name] = maps_to
    output: dict[str, Any] = {}
    for key, raw in value.items():
        text_key = str(key)
        mapped_ref = name_to_ref.get(text_key)
        if mapped_ref:
            parts = _initial_input_ref_parts(mapped_ref, model_aliases)
            if parts:
                alias, port = parts
                _merge_nested_input(output, alias, {port: raw})
            else:
                output[mapped_ref] = raw
            continue
        if text_key in model_aliases and isinstance(raw, Mapping):
            _merge_nested_input(output, text_key, raw)
            continue
        parts = _initial_input_ref_parts(text_key, model_aliases)
        if parts:
            alias, port = parts
            _merge_nested_input(output, alias, {port: raw})
            continue
        output[text_key] = raw
    return output


def _merge_initial_inputs(
    current: dict[str, Any],
    overlay: Mapping[str, Any],
) -> None:
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(current.get(key), dict):
            current[key].update(dict(value))
        elif isinstance(value, Mapping):
            current[key] = dict(value)
        else:
            current[key] = value


_RUNTIME_OVERRIDE_KEYS = ("duration", "communication_step", "settle_steps")
_RESERVED_PARAMETER_KEYS = frozenset({"initial_inputs", "per_model"})


def _per_model_overlays(
    parameters: Mapping[str, Any],
    aliases: set[str],
) -> dict[str, dict[str, Any]]:
    overlays: dict[str, dict[str, Any]] = {}
    # Hosted runs, Studio and Desktop key parameter overrides by model alias.
    for alias in aliases:
        if alias in _RESERVED_PARAMETER_KEYS:
            continue
        value = parameters.get(alias)
        if isinstance(value, Mapping):
            overlays.setdefault(alias, {}).update(dict(value))
    per_model = parameters.get("per_model")
    if isinstance(per_model, Mapping):
        for alias, value in per_model.items():
            if isinstance(alias, str) and isinstance(value, Mapping):
                overlays.setdefault(alias, {}).update(dict(value))
    return overlays


def apply_run_overrides(
    manifest: dict[str, Any],
    *,
    parameters: Any,
    simulation_config: Any,
) -> None:
    """Apply Desktop/Studio run inputs without mutating the saved lab.

    Accepts every run-input shape the product surfaces send:

    - runtime values at ``simulation_config.runtime.<key>`` (preferred, as the
      hosted executor resolves them) or ``simulation_config.<key>``;
    - initial inputs at ``simulation_config.runtime.initial_inputs``,
      ``simulation_config.initial_inputs`` and ``parameters.initial_inputs``,
      merged in that order so explicit input values win over an echoed runtime;
    - model parameters keyed by alias at ``parameters.<alias>`` or
      ``parameters.per_model.<alias>``, merged onto the lab's parameters.
    """

    runtime = manifest.setdefault("runtime", {})
    if not isinstance(runtime, dict):
        runtime = {}
        manifest["runtime"] = runtime
    sim_cfg = simulation_config if isinstance(simulation_config, Mapping) else {}
    nested_runtime = sim_cfg.get("runtime") if isinstance(sim_cfg.get("runtime"), Mapping) else {}
    for key in _RUNTIME_OVERRIDE_KEYS:
        value = nested_runtime.get(key)
        if value is None:
            value = sim_cfg.get(key)
        if value is not None:
            runtime[key] = value

    params = parameters if isinstance(parameters, Mapping) else {}
    for raw_inputs in (
        nested_runtime.get("initial_inputs"),
        sim_cfg.get("initial_inputs"),
        params.get("initial_inputs"),
    ):
        initial_overlay = map_initial_inputs(manifest, raw_inputs)
        if not initial_overlay:
            continue
        current = runtime.get("initial_inputs")
        if not isinstance(current, dict):
            current = {}
            runtime["initial_inputs"] = current
        _merge_initial_inputs(current, initial_overlay)

    models = manifest.get("models")
    if not params or not isinstance(models, list):
        return
    aliases = {
        str(entry["alias"])
        for entry in models
        if isinstance(entry, Mapping) and isinstance(entry.get("alias"), str)
    }
    overlays = _per_model_overlays(params, aliases)
    for entry in models:
        if not isinstance(entry, dict):
            continue
        alias = entry.get("alias")
        overlay = overlays.get(alias) if isinstance(alias, str) else None
        if not overlay:
            continue
        current_parameters = entry.get("parameters")
        merged = dict(current_parameters) if isinstance(current_parameters, Mapping) else {}
        merged.update(overlay)
        entry["parameters"] = merged
