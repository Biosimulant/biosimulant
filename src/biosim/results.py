"""Run results as strict JSON.

JSON has no NaN or Infinity. Python's encoder writes them anyway, as bare
tokens that browsers, PostgreSQL, ``jq`` and most other readers reject, so one
diverging value would make a whole results file unreadable. Results replace
every non-finite number with ``null`` and record where each one was, so the
loss is visible rather than silent.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

NON_FINITE_KEY = "non_finite_values"
_MAX_RECORDED_PATHS = 100


def strict_results(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``result`` that serializes as strict JSON.

    When anything was replaced, the copy gains a ``non_finite_values`` record::

        {"count": 2, "truncated": false,
         "items": [{"path": "outputs.growth.rate.value", "value": "NaN"}, ...]}

    ``path`` joins mapping keys with ``.`` and list positions as ``[i]``.
    """

    replaced: list[dict[str, str]] = []
    cleaned = _replace_non_finite(result, "", replaced)
    if replaced:
        cleaned[NON_FINITE_KEY] = {
            "count": len(replaced),
            "truncated": len(replaced) > _MAX_RECORDED_PATHS,
            "items": replaced[:_MAX_RECORDED_PATHS],
        }
    return cleaned


def _replace_non_finite(value: Any, path: str, replaced: list[dict[str, str]]) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        label = "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
        replaced.append({"path": path, "value": label})
        return None
    if isinstance(value, Mapping):
        return {
            key: _replace_non_finite(item, f"{path}.{key}" if path else str(key), replaced)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _replace_non_finite(item, f"{path}[{index}]", replaced)
            for index, item in enumerate(value)
        ]
    return value
