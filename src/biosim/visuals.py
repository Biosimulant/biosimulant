from __future__ import annotations

import json
import math

from .visual_contract import validate_payload
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple, TypedDict, Union


class VisualSpec(TypedDict, total=False):
    """Renderer-agnostic visual specification for browser clients.

    Required keys:
    - render: the visual type (e.g., 'timeseries', 'bar', 'graph', 'table', 'image', 'text', 'structure3d', 'custom:...')
    - data: JSON-serializable data payload interpreted by the client renderer for the given render type
    """

    schema_version: str
    render: str
    data: Dict[str, Any]
    description: str


Visuals = Union[VisualSpec, List[VisualSpec]]
VisualCapability = Literal["3d-capable", "non-3d", "no-visuals", "conditional"]


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _label(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("-", "_").split("_") if part)


def derive_timeseries_visuals(signals: Mapping[str, Any]) -> List[VisualSpec]:
    """Derive conservative charts from typed record trajectory outputs.

    A record qualifies only when it contains a non-empty ``rows`` list whose
    rows expose a numeric time coordinate and numeric value.  This keeps the
    fallback generic while avoiding guesses for arbitrary record payloads.
    Explicit ``BioModule.visualize`` output remains authoritative.
    """

    visuals: List[VisualSpec] = []
    for port_name, signal in signals.items():
        value = getattr(signal, "value", None)
        if not isinstance(value, Mapping) or not isinstance(value.get("rows"), list):
            continue
        rows = [row for row in value["rows"] if isinstance(row, Mapping)]
        if not rows:
            continue
        first = rows[0]
        time_key = next(
            (
                key
                for key in first
                if isinstance(key, str)
                and (key.startswith("time_") or key in {"time", "t"})
                and _finite_number(first.get(key)) is not None
            ),
            None,
        )
        if time_key is None:
            continue
        value_key = "value" if _finite_number(first.get("value")) is not None else None
        if value_key is None:
            candidates = [
                key
                for key in first
                if key != time_key and _finite_number(first.get(key)) is not None
            ]
            value_key = candidates[0] if len(candidates) == 1 else None
        if value_key is None:
            continue
        points = []
        for row in rows:
            time_value = _finite_number(row.get(time_key))
            output_value = _finite_number(row.get(value_key))
            if time_value is not None and output_value is not None:
                points.append([time_value, output_value])
        if not points:
            continue
        quantity = str(value.get("quantity") or value_key or port_name)
        time_unit = value.get("time_unit")
        value_unit = value.get("value_unit")
        spec = getattr(signal, "spec", None)
        if not isinstance(value_unit, str) and spec is not None:
            emitted_unit = getattr(spec, "emitted_unit", None)
            value_unit = emitted_unit if isinstance(emitted_unit, str) else None
        data: Dict[str, Any] = {
            "title": _label(quantity),
            "x_label": "Time",
            "y_label": _label(quantity),
            "series": [{"name": _label(quantity), "points": points}],
        }
        if isinstance(time_unit, str) and time_unit:
            data["x_unit"] = time_unit
        elif time_key.startswith("time_"):
            data["x_unit"] = time_key[len("time_") :]
        if isinstance(value_unit, str) and value_unit:
            data["y_unit"] = value_unit
        visuals.append(
            {
                "render": "timeseries",
                "description": f"Derived from typed trajectory output {port_name}.",
                "data": data,
            }
        )
    return visuals


def validate_visual_spec(spec: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Validate that a dict conforms to the VisualSpec shape and is JSON-serializable.

    Returns (ok, error_message). When ok is False, error_message contains a brief reason.
    """
    if not isinstance(spec, dict):
        return False, "visual must be a dict"
    if "render" not in spec:
        return False, "missing 'render' key"
    if "data" not in spec:
        return False, "missing 'data' key"
    render = spec["render"]
    if not isinstance(render, str) or not render:
        return False, "'render' must be a non-empty string"
    data = spec["data"]
    if not isinstance(data, dict):
        return False, "'data' must be a dict"
    if "description" in spec and not isinstance(spec["description"], str):
        return False, "'description' must be a string"
    if "schema_version" in spec:
        error = validate_payload(spec)
        if error:
            return False, error
    # Legacy specs retain their envelope compatibility.
    try:
        # Include optional fields that the UI may rely on.
        payload: Dict[str, Any] = {"render": render, "data": data}
        if "description" in spec:
            payload["description"] = spec["description"]
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        return False, f"data not JSON-serializable: {exc}"
    return True, None


def normalize_visuals(visuals: Visuals) -> List[VisualSpec]:
    """Normalize a single VisualSpec or list into a list of VisualSpec.

    Invalid entries are filtered out.
    """
    items: List[Dict[str, Any]]
    if isinstance(visuals, list):
        items = visuals  # type: ignore[assignment]
    else:
        items = [visuals]  # type: ignore[list-item]
    out: List[VisualSpec] = []
    for v in items:
        ok, _ = validate_visual_spec(v)
        if ok:
            normed: Dict[str, Any] = {"render": v["render"], "data": v["data"]}
            if "schema_version" in v:
                normed["schema_version"] = v["schema_version"]
            if "description" in v and isinstance(v["description"], str):
                normed["description"] = v["description"]
            out.append(normed)  # type: ignore[arg-type]
    return out


def classify_visual_capability(
    visuals: Visuals | None,
    *,
    conditional_when_empty: bool = False,
) -> VisualCapability:
    """Classify a module or lab visual payload for renderer coverage audits."""
    if not visuals:
        return "conditional" if conditional_when_empty else "no-visuals"
    normalized = normalize_visuals(visuals)
    if not normalized:
        return "no-visuals"
    if any(visual["render"].lower() == "structure3d" for visual in normalized):
        return "3d-capable"
    return "non-3d"
