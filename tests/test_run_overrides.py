from __future__ import annotations

import json
from pathlib import Path

import yaml

from biosim.__main__ import _lab_path_with_run_inputs
from biosim.run_overrides import apply_run_overrides, map_initial_inputs


def test_run_overrides_map_public_inputs_and_runtime() -> None:
    manifest = {
        "models": [{"alias": "cell", "parameters": {"baseline": 1}}],
        "io": {
            "inputs": [
                {"name": "dose", "maps_to": "cell.drug.dose"},
            ]
        },
        "runtime": {
            "duration": 10,
            "communication_step": 1,
            "initial_inputs": {"cell": {"existing": 2}},
        },
    }

    apply_run_overrides(
        manifest,
        parameters={
            "initial_inputs": {"dose": 5, "cell.direct": 3},
            "per_model": {"cell": {"baseline": 9}},
        },
        simulation_config={"duration": 20, "settle_steps": 2},
    )

    assert manifest["runtime"] == {
        "duration": 20,
        "communication_step": 1,
        "settle_steps": 2,
        "initial_inputs": {
            "cell": {
                "existing": 2,
                "drug.dose": 5,
                "direct": 3,
            }
        },
    }
    assert manifest["models"][0]["parameters"] == {"baseline": 9}


def test_map_initial_inputs_preserves_unknown_public_keys() -> None:
    assert map_initial_inputs({"models": []}, {"temperature": 37}) == {
        "temperature": 37
    }


def test_run_input_staging_does_not_mutate_source(tmp_path: Path) -> None:
    lab_dir = tmp_path / "lab"
    lab_dir.mkdir()
    manifest_path = lab_dir / "lab.yaml"
    manifest_path.write_text(
        """\
schema_version: "2.0"
title: Test
package: tests/test
version: 1.0.0
models: []
wiring: []
runtime:
  duration: 10
  communication_step: 1
""",
        encoding="utf-8",
    )
    original = manifest_path.read_bytes()
    run_inputs = tmp_path / "run-inputs.json"
    run_inputs.write_text(
        json.dumps(
            {
                "parameters": {},
                "simulation_config": {"duration": 25},
            }
        ),
        encoding="utf-8",
    )

    with _lab_path_with_run_inputs(lab_dir, run_inputs) as staged:
        assert staged != lab_dir
        rendered = yaml.safe_load((staged / "lab.yaml").read_text(encoding="utf-8"))
        assert rendered["runtime"]["duration"] == 25

    assert manifest_path.read_bytes() == original


def _manifest_with_inputs() -> dict:
    return {
        "models": [
            {"alias": "cell", "parameters": {"baseline": 1, "rate": 0.5}},
            {"alias": "reporter"},
        ],
        "io": {"inputs": [{"name": "dose", "maps_to": "cell.dose"}]},
        "runtime": {
            "duration": 10,
            "communication_step": 1,
            "settle_steps": 0,
            "initial_inputs": {"cell": {"dose": 1}},
        },
    }


def test_studio_run_shape_applies_nested_runtime_inputs_and_alias_parameters() -> None:
    manifest = _manifest_with_inputs()

    apply_run_overrides(
        manifest,
        parameters={"cell": {"baseline": 7}},
        simulation_config={
            "initial_inputs": {"dose": 5},
            # Studio echoes the whole lab runtime, including its default inputs.
            "runtime": {
                "duration": 2,
                "communication_step": 0.5,
                "settle_steps": 1,
                "initial_inputs": {"cell": {"dose": 1}},
                "python_version": "3.12",
            },
        },
    )

    assert manifest["runtime"]["duration"] == 2
    assert manifest["runtime"]["communication_step"] == 0.5
    assert manifest["runtime"]["settle_steps"] == 1
    assert manifest["runtime"]["initial_inputs"] == {"cell": {"dose": 5}}
    assert manifest["models"][0]["parameters"] == {"baseline": 7, "rate": 0.5}
    assert "parameters" not in manifest["models"][1]


def test_desktop_run_shape_applies_top_level_runtime_and_alias_nested_inputs() -> None:
    manifest = _manifest_with_inputs()

    apply_run_overrides(
        manifest,
        parameters={"cell": {"rate": 0.9}},
        simulation_config={
            "duration": 4,
            "communication_step": 0.25,
            "initial_inputs": {"cell": {"dose": 3}},
        },
    )

    assert manifest["runtime"]["duration"] == 4
    assert manifest["runtime"]["communication_step"] == 0.25
    assert manifest["runtime"]["initial_inputs"] == {"cell": {"dose": 3}}
    assert manifest["models"][0]["parameters"] == {"baseline": 1, "rate": 0.9}


def test_nested_runtime_wins_over_top_level_runtime_keys() -> None:
    manifest = _manifest_with_inputs()

    apply_run_overrides(
        manifest,
        parameters=None,
        simulation_config={"duration": 3, "runtime": {"duration": 6, "communication_step": None}},
    )

    assert manifest["runtime"]["duration"] == 6
    assert manifest["runtime"]["communication_step"] == 1


def test_explicit_parameter_inputs_win_over_simulation_config_inputs() -> None:
    manifest = _manifest_with_inputs()

    apply_run_overrides(
        manifest,
        parameters={"initial_inputs": {"dose": 9}},
        simulation_config={"initial_inputs": {"dose": 5}},
    )

    assert manifest["runtime"]["initial_inputs"] == {"cell": {"dose": 9}}


def test_omitted_run_inputs_keep_lab_values() -> None:
    manifest = _manifest_with_inputs()
    expected = yaml.safe_load(yaml.safe_dump(manifest))

    apply_run_overrides(manifest, parameters={}, simulation_config={"runtime": {}})

    assert manifest == expected


def test_studio_shaped_run_input_file_reaches_staged_lab(tmp_path: Path) -> None:
    lab_dir = tmp_path / "lab"
    lab_dir.mkdir()
    (lab_dir / "lab.yaml").write_text(
        """\
schema_version: "2.0"
title: Test
package: tests/test
version: 1.0.0
models: []
wiring: []
runtime:
  duration: 10
  communication_step: 1
""",
        encoding="utf-8",
    )
    run_inputs = tmp_path / "run_inputs.json"
    run_inputs.write_text(
        json.dumps(
            {
                "parameters": None,
                "simulation_config": {
                    "initial_inputs": {},
                    "runtime": {"duration": 2, "communication_step": 0.5},
                },
            }
        ),
        encoding="utf-8",
    )

    with _lab_path_with_run_inputs(lab_dir, run_inputs) as staged:
        rendered = yaml.safe_load((staged / "lab.yaml").read_text(encoding="utf-8"))
        assert rendered["runtime"]["duration"] == 2
        assert rendered["runtime"]["communication_step"] == 0.5
