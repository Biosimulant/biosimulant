"""Portable delivery through real composition and durable local reopening."""

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from biosim import BioModule, BioWorld, ExecutionPolicy, SignalSpec
from biosim.pack import build_package, run_package, PackageError
from biosim.visual_contract import (
    audit_visualizations,
    visualization_catalog,
    validate_payload,
)
from biosim.labs_serve.server import LabServeSession, create_app

CATALOG = visualization_catalog()["renderers"]


@pytest.mark.parametrize("kind", CATALOG)
def test_portable_catalog_and_payload_validation(kind):
    assert validate_payload(CATALOG[kind]["example"]) is None
    assert validate_payload({"schema_version": "1", "render": kind, "data": {}})


def _gallery_lab(path):
    path.mkdir()
    model = path / "models" / "result"
    (model / "src").mkdir(parents=True)
    (model / "model.yaml").write_text("""schema_version: "2.0"
title: Delivery fixture
standard: other
biosim:
  entrypoint: src.result:Result
  execution_policy: once_before_run
""")
    (
        model / "src" / "result.py"
    ).write_text("""from biosim import BioModule, ExecutionPolicy, SignalSpec, visualization_catalog
from pathlib import Path
import base64
class Result(BioModule):
    execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN
    def outputs(self):
        return {"count": SignalSpec.scalar(dtype="float64", emitted_unit="1")}
    def execute(self, inputs, *, context):
        self.count = 12.0
        self.image = Path(__file__).parent / "overlay.png"
        self.image.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1cAAAAASUVORK5CYII="))
        self.structure = Path(__file__).parent / "structure.pdb"
        self.structure.write_text("ATOM      1  N   GLY A   1       0.000   0.000   0.000  1.00 20.00           N\\nATOM      2  CA  GLY A   1       1.450   0.000   0.000  1.00 20.00           C\\nATOM      3  C   GLY A   1       2.000   1.400   0.000  1.00 20.00           C\\nEND\\n")
        return {"count": self.count}
    def visualize(self):
        visuals = [v["example"] for v in visualization_catalog()["renderers"].values()]
        for visual in visuals:
            if visual["render"] == "bar":
                visual["data"]["items"][0]["value"] = self.count
            elif visual["render"] == "timeseries":
                visual["data"]["series"][0]["points"] = [[0, 0], [1, self.count]]
            elif visual["render"] == "image":
                visual["data"]["source"]["path"] = str(self.image)
            elif visual["render"] == "structure3d":
                visual["data"]["source"]["path"] = str(self.structure)
        return visuals
""")
    manifest = {
        "schema_version": "2.0",
        "title": "Delivery fixture",
        "models": [{"alias": "result", "path": "models/result"}],
        "runtime": {"communication_step": 0.1, "duration": 0.1},
        "wiring": [],
        "visualization": {
            "schema_version": "1",
            "required": [{"module": "result", "render": kind} for kind in CATALOG],
        },
    }
    (path / "lab.yaml").write_text(yaml.safe_dump(manifest))
    return path


def test_packaged_run_delivers_all_types_from_actual_state(tmp_path):
    lab = _gallery_lab(tmp_path / "lab")
    archive = build_package(lab, package_name="local/delivery", version="1.0.0")
    results = run_package(archive, install_deps=False)
    assert results["outputs"]["result"]["count"]["value"] == 12
    assert results["visualization"]["status"] == "passed"
    by_type = {v["render"]: v["data"] for v in results["visuals"][0]["visuals"]}
    assert set(by_type) == set(CATALOG)
    assert (
        by_type["bar"]["items"][0]["value"]
        == results["outputs"]["result"]["count"]["value"]
    )
    assert (
        by_type["timeseries"]["series"][0]["points"][-1][1]
        == results["outputs"]["result"]["count"]["value"]
    )
    json.dumps(results, allow_nan=False)


def test_local_assets_survive_reopening_and_source_removal(tmp_path):
    lab = _gallery_lab(tmp_path / "lab")
    session = LabServeSession(lab, install_deps=False)
    client = TestClient(create_app(session))
    created = client.post("/api/runs", json={}).json()
    run_id = created["data"]["run"]["id"]
    run = session.get_run(run_id)
    run.thread.join(timeout=10)
    assert run.status == "completed", run.error
    assert run.results["visualization"]["status"] == "passed"
    sources = [
        v["data"]["source"]
        for v in run.results["visuals"][0]["visuals"]
        if v["render"] in {"image", "structure3d"}
    ]
    # The stored copies remain when the model's original output files disappear.
    for name in ["overlay.png", "structure.pdb"]:
        (lab / "models/result/src" / name).unlink(missing_ok=True)
    reopened = TestClient(create_app(LabServeSession(lab, install_deps=False)))
    result = reopened.get(f"/api/runs/{run_id}/results").json()["data"]["results"]
    assert len(result["visuals"][0]["visuals"]) == 9
    for source in sources:
        assert reopened.get(source["url"]).status_code == 200


def test_bad_visual_is_diagnosed_without_losing_science():
    class Bad(BioModule):
        execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN

        def outputs(self):
            return {"count": SignalSpec.scalar(dtype="float64")}

        def execute(self, inputs, *, context):
            return {"count": 7.0}

        def visualize(self):
            raise RuntimeError("renderer failed")

    world = BioWorld(communication_step=0.1)
    world.add_biomodule("bad", Bad())
    world.run(duration=0.1)
    assert world.collect_visuals() == []
    assert world.visual_diagnostics[0]["code"] == "visualize_exception"
    assert world.get_outputs("bad")["count"].value == 7
    assert (
        audit_visualizations(
            [],
            {"schema_version": "1", "required": [{"module": "bad", "render": "bar"}]},
            diagnostics=world.visual_diagnostics,
        )["status"]
        == "failed"
    )


def test_invalid_manifest_requirement_fails_before_execution(tmp_path):
    lab = _gallery_lab(tmp_path / "lab")
    manifest = yaml.safe_load((lab / "lab.yaml").read_text())
    manifest["visualization"] = {"schema_version": "1"}
    (lab / "lab.yaml").write_text(yaml.safe_dump(manifest))
    with pytest.raises(PackageError, match="required visual"):
        build_package(lab, package_name="local/delivery", version="1.0.0")
