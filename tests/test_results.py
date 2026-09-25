from __future__ import annotations

import json
import math
from pathlib import Path

from biosim.pack import run_package
from biosim.results import NON_FINITE_KEY, strict_results
from tests.test_pack import _build_component_lab


def _strict_loads(text: str):
    def reject(token: str):
        raise ValueError(f"non-standard JSON token {token}")

    return json.loads(text, parse_constant=reject)


def test_finite_results_pass_through_unchanged():
    result = {"outputs": {"growth": {"biomass": {"value": 3.2, "spec": {"emitted_unit": "g/L"}}}}}

    assert strict_results(result) == result
    assert NON_FINITE_KEY not in strict_results(result)


def test_non_finite_numbers_become_null_and_are_recorded():
    result = {
        "outputs": {"growth": {"rate": {"value": math.nan}}},
        "visuals": [{"data": {"series": [{"points": [[0.0, 1.0], [1.0, math.inf]]}]}}],
        "duration": -math.inf,
    }

    cleaned = strict_results(result)

    assert cleaned["outputs"]["growth"]["rate"]["value"] is None
    assert cleaned["visuals"][0]["data"]["series"][0]["points"][1] == [1.0, None]
    assert cleaned["duration"] is None
    assert cleaned[NON_FINITE_KEY] == {
        "count": 3,
        "truncated": False,
        "items": [
            {"path": "outputs.growth.rate.value", "value": "NaN"},
            {"path": "visuals[0].data.series[0].points[1][1]", "value": "Infinity"},
            {"path": "duration", "value": "-Infinity"},
        ],
    }
    _strict_loads(json.dumps(cleaned))
    # The input is left as it was.
    assert math.isnan(result["outputs"]["growth"]["rate"]["value"])


def test_a_long_run_of_non_finite_values_is_counted_not_listed():
    cleaned = strict_results({"trace": [math.nan] * 250})

    record = cleaned[NON_FINITE_KEY]
    assert record["count"] == 250
    assert record["truncated"] is True
    assert len(record["items"]) == 100


def _write_diverging_model(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "model.yaml").write_text(
        "\n".join(
            [
                'schema_version: "2.0"',
                'title: "Test: Diverging"',
                'description: "Emits values that are not finite"',
                "standard: other",
                "tags: [test]",
                'authors: ["Tests"]',
                "biosim:",
                '  entrypoint: "src.diverging:Diverging"',
                "  communication_step: 0.1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (path / "src").mkdir(exist_ok=True)
    (path / "src" / "diverging.py").write_text(
        """
from biosim import BioModule, ExecutionPolicy, SignalSpec


class Diverging(BioModule):
    execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN

    def outputs(self):
        return {
            "ratio": SignalSpec.scalar(dtype="float64"),
            "growth": SignalSpec.scalar(dtype="float64"),
        }

    def execute(self, inputs, *, context):
        return {"ratio": float("nan"), "growth": float("inf")}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def test_a_diverging_lab_run_still_produces_strict_json(tmp_path: Path):
    package_path = _build_component_lab(
        _write_diverging_model(tmp_path / "diverging"),
        package_name="local/diverging",
        version="1.0.0",
    )

    result = run_package(package_path, install_deps=False)

    parsed = _strict_loads(json.dumps(result))
    assert parsed["outputs"]["main"]["ratio"]["value"] is None
    assert parsed["outputs"]["main"]["growth"]["value"] is None
    paths = {item["path"]: item["value"] for item in parsed[NON_FINITE_KEY]["items"]}
    assert paths["outputs.main.ratio.value"] == "NaN"
    assert paths["outputs.main.growth.value"] == "Infinity"
