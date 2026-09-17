from __future__ import annotations

import io
import hashlib
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from biosim import pack as pack_module
from biosim import hub as hub_module
from biosim.pack import (
    PackageError,
    build_package,
    export_lab_package,
    fetch_package,
    prepare_lab_package,
    publish_package,
    run_package,
    unpack_package,
    validate_package,
)


def _write_counter_model(
    path: Path, *, package_name: str | None = None, version: str | None = None
) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    model_yaml = [
        'schema_version: "2.0"',
        'title: "Test: Counter"',
        'description: "Counter model"',
        "standard: other",
        "tags: [test]",
        'authors: ["Tests"]',
        "biosim:",
        '  entrypoint: "src.counter:Counter"',
        "  communication_step: 0.1",
    ]
    if package_name:
        model_yaml.append(f"package: {package_name}")
    if version:
        model_yaml.append(f"version: {version}")
    (path / "model.yaml").write_text("\n".join(model_yaml) + "\n", encoding="utf-8")
    src_dir = path / "src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "counter.py").write_text(
        """
from biosim import BioModule, SignalSpec, ScalarSignal


class Counter(BioModule):
    def __init__(self, step: float = 1.0):
        self.value = 0.0
        self.step = step

    def outputs(self):
        return {"count": SignalSpec.scalar(dtype="float64")}

    def advance_window(self, _start: float, t: float) -> None:
        self.value += self.step

    def get_outputs(self):
        return {"count": ScalarSignal(source="counter", name="count", value=self.value, emitted_at=0.1, spec=self.outputs()["count"])}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_accumulator_model(
    path: Path, *, package_name: str | None = None, version: str | None = None
) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    model_yaml = [
        'schema_version: "2.0"',
        'title: "Test: Accumulator"',
        'description: "Accumulator model"',
        "standard: other",
        "tags: [test]",
        'authors: ["Tests"]',
        "biosim:",
        '  entrypoint: "src.accumulator:Accumulator"',
        "  communication_step: 0.1",
    ]
    if package_name:
        model_yaml.append(f"package: {package_name}")
    if version:
        model_yaml.append(f"version: {version}")
    (path / "model.yaml").write_text("\n".join(model_yaml) + "\n", encoding="utf-8")
    src_dir = path / "src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "accumulator.py").write_text(
        """
from biosim import BioModule, SignalSpec, ScalarSignal


class Accumulator(BioModule):
    def __init__(self):
        self.total = 0.0

    def inputs(self):
        return {"value": SignalSpec.scalar(dtype="float64")}

    def outputs(self):
        return {"total": SignalSpec.scalar(dtype="float64")}

    def set_inputs(self, signals):
        signal = signals.get("value")
        if signal is not None:
            self.total += float(signal.value)

    def advance_window(self, _start: float, t: float) -> None:
        return

    def get_outputs(self):
        return {"total": ScalarSignal(source="acc", name="total", value=self.total, emitted_at=0.1, spec=self.outputs()["total"])}

    def visualize(self):
        if self.total <= 0:
            return None
        return {"render": "table", "data": {"rows": [{"total": self.total}]}}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_lab(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _write_counter_model(
        path / "models" / "counter",
        package_name="local/counter",
        version="1.0.0",
    )
    _write_accumulator_model(
        path / "models" / "accumulator",
        package_name="local/accumulator",
        version="1.0.0",
    )
    (path / "lab.yaml").write_text(
        """
schema_version: "2.0"
title: "Test: Lab"
description: "Self-contained source-tree lab"
models:
  - path: models/counter
    alias: counter
  - path: models/accumulator
    alias: accumulator
runtime:
  communication_step: 0.1
  duration: 0.2
  initial_inputs: {}
wiring:
  - from: counter.count
    to: [accumulator.value]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_lab_release_identity(path: Path, package_name: str, version: str) -> Path:
    manifest_path = path / "lab.yaml"
    manifest = pack_module._safe_yaml_load(manifest_path.read_bytes())
    manifest["package"] = package_name
    manifest["version"] = version
    manifest_path.write_bytes(pack_module._safe_yaml_dump(manifest))
    return path


def _write_path_manifest_lab(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _write_counter_model(path / "models" / "counter")
    (path / "lab.yaml").write_text(
        """
schema_version: "2.0"
title: "Test: Source Lab"
description: "Source-style lab refs"
models:
  - path: models/counter
    alias: counter
runtime:
  communication_step: 0.1
  duration: 0.2
  initial_inputs: {}
wiring: []
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_child_output_lab(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _write_counter_model(
        path / "models" / "counter",
        package_name="local/counter",
        version="1.0.0",
    )
    (path / "lab.yaml").write_text(
        """
schema_version: "2.0"
title: "Test: Child Lab"
description: "Embedded child lab"
models:
  - path: models/counter
    alias: counter
io:
  outputs:
    - name: count
      maps_to: counter.count
runtime:
  communication_step: 0.1
  duration: 0.2
  initial_inputs: {}
wiring: []
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_parent_lab_with_child(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _write_accumulator_model(
        path / "models" / "accumulator",
        package_name="local/accumulator",
        version="1.0.0",
    )
    _write_child_output_lab(path / "labs" / "child-lab")
    (path / "lab.yaml").write_text(
        """
schema_version: "2.0"
title: "Test: Parent Lab"
description: "Embedded child-lab refs"
models:
  - path: models/accumulator
    alias: accumulator
children:
  - path: labs/child-lab
    alias: nested
runtime:
  communication_step: 0.1
  duration: 0.2
  initial_inputs: {}
wiring:
  - from: nested.count
    to: [accumulator.value]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def test_build_validate_and_unpack_model_package(tmp_path: Path):
    model_dir = _write_counter_model(tmp_path / "counter")
    package_path = build_package(
        model_dir, package_name="local/counter", version="1.0.0"
    )

    validation = validate_package(package_path)
    assert validation.valid
    assert validation.metadata["package"] == "local/counter"
    assert validation.metadata["package_type"] == "model"

    unpacked = unpack_package(package_path, dest=tmp_path / "unpacked")
    assert (unpacked / "payload" / "model.yaml").exists()
    assert (unpacked / "payload" / "src" / "counter.py").exists()


def test_build_uses_manifest_declared_package_and_version(tmp_path: Path):
    model_dir = _write_counter_model(
        tmp_path / "counter",
        package_name="manifest/counter",
        version="2.3.4",
    )
    package_path = build_package(model_dir)

    validation = validate_package(package_path)
    assert validation.valid
    assert validation.metadata["package"] == "manifest/counter"
    assert validation.metadata["version"] == "2.3.4"


def test_model_package_run_smoke(tmp_path: Path):
    model_dir = _write_counter_model(tmp_path / "counter")
    package_path = build_package(
        model_dir, package_name="local/counter", version="1.0.0"
    )
    result = run_package(package_path, install_deps=False)
    assert result["package"] == "local/counter"
    assert "count" in result["outputs"]


def test_execute_only_model_package_uses_bioworld_policy(tmp_path: Path):
    model_dir = tmp_path / "execute-model"
    model_dir.mkdir()
    (model_dir / "model.yaml").write_text(
        """
schema_version: "2.0"
title: "Execute Model"
description: "Execute-only package"
standard: other
tags: [test]
authors: ["Tests"]
biosim:
  entrypoint: "src.execute_model:ExecuteModel"
  communication_step: 0.1
""".strip()
        + "\n",
        encoding="utf-8",
    )
    src_dir = model_dir / "src"
    src_dir.mkdir()
    (src_dir / "execute_model.py").write_text(
        """
from biosim import BioModule, ExecutionPolicy, SignalSpec


class ExecuteModel(BioModule):
    execution_policy = ExecutionPolicy.ONCE_AFTER_RUN

    def __init__(self):
        self.calls = 0

    def outputs(self):
        return {"value": SignalSpec.scalar(dtype="float64")}

    def execute(self, inputs, *, context):
        self.calls += 1
        return {"value": 7.0}

    def snapshot(self):
        return {"calls": self.calls}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    package_path = build_package(
        model_dir, package_name="local/execute-model", version="1.0.0"
    )
    result = run_package(package_path, install_deps=False)

    assert result["outputs"] == ["value"]
    assert result["state"] == {"calls": 1}


def test_execute_only_model_package_receives_runtime_initial_inputs(tmp_path: Path):
    model_dir = tmp_path / "execute-input-model"
    model_dir.mkdir()
    (model_dir / "model.yaml").write_text(
        """
schema_version: "2.0"
title: "Execute Input Model"
description: "Canonical package input"
standard: other
tags: [test]
authors: ["Tests"]
biosim:
  entrypoint: "src.execute_input_model:ExecuteInputModel"
  communication_step: 0.1
runtime:
  initial_inputs:
    value: 4.0
""".strip()
        + "\n",
        encoding="utf-8",
    )
    src_dir = model_dir / "src"
    src_dir.mkdir()
    (src_dir / "execute_input_model.py").write_text(
        """
from biosim import BioModule, ExecutionPolicy, SignalSpec


class ExecuteInputModel(BioModule):
    execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN

    def __init__(self):
        self.latest = None

    def inputs(self):
        return {"value": SignalSpec.scalar(dtype="float64")}

    def outputs(self):
        return {"value": SignalSpec.scalar(dtype="float64")}

    def execute(self, inputs, *, context):
        self.latest = inputs["value"].value * 2
        return {"value": self.latest}

    def snapshot(self):
        return {"latest": self.latest}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    package_path = build_package(
        model_dir, package_name="local/execute-input-model", version="1.0.0"
    )
    result = run_package(package_path, install_deps=False)

    assert result["outputs"] == ["value"]
    assert result["state"] == {"latest": 8.0}


def test_model_package_run_coerces_runtime_initial_inputs(tmp_path: Path):
    model_dir = tmp_path / "input-model"
    model_dir.mkdir()
    (model_dir / "model.yaml").write_text(
        """
schema_version: "2.0"
title: "Input Model"
description: "Input coercion model"
standard: other
tags: [test]
authors: ["Tests"]
biosim:
  entrypoint: "src.input_model:InputModel"
  communication_step: 0.1
runtime:
  initial_inputs:
    value: 4.0
""".strip()
        + "\n",
        encoding="utf-8",
    )
    src_dir = model_dir / "src"
    src_dir.mkdir()
    (src_dir / "input_model.py").write_text(
        """
from biosim import BioModule, SignalSpec, ScalarSignal


class InputModel(BioModule):
    def __init__(self):
        self.value = 0.0
        self.received_signal_type = None

    def inputs(self):
        return {"value": SignalSpec.scalar(dtype="float64")}

    def outputs(self):
        return {"value": SignalSpec.scalar(dtype="float64")}

    def set_inputs(self, signals):
        signal = signals["value"]
        self.received_signal_type = signal.__class__.__name__
        self.value = float(signal.value)

    def advance_window(self, _start, _end):
        return

    def get_outputs(self):
        return {"value": ScalarSignal(source="input", name="value", value=self.value, emitted_at=0.1, spec=self.outputs()["value"])}

    def snapshot(self):
        return {"value": self.value, "received_signal_type": self.received_signal_type}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    package_path = build_package(
        model_dir, package_name="local/input-model", version="1.0.0"
    )
    result = run_package(package_path, install_deps=False)

    assert result["state"] == {"value": 4.0, "received_signal_type": "ScalarSignal"}


def test_lab_build_embeds_models_and_runs_without_registry(tmp_path: Path):
    lab_dir = _write_lab_release_identity(
        _write_lab(tmp_path / "lab"),
        "local/source-lab",
        "1.0.0",
    )
    lab_pkg = build_package(lab_dir, package_name="local/source-lab", version="1.0.0")
    validation = validate_package(lab_pkg)
    assert validation.valid
    unpacked = unpack_package(lab_pkg, dest=tmp_path / "unpacked-lab")
    assert (unpacked / "payload" / "models" / "counter" / "model.yaml").exists()
    assert (unpacked / "payload" / "models" / "accumulator" / "model.yaml").exists()
    result = run_package(lab_pkg, install_deps=False)
    assert result["package"] == "local/source-lab"
    assert result["duration"] == pytest.approx(0.2)
    assert result["communication_step"] == pytest.approx(0.1)
    assert result["settle_steps"] == 0
    assert result["modules"] == [
        {
            "alias": "counter",
            "path": "models/counter",
            "package": "local/counter",
            "version": "1.0.0",
        },
        {
            "alias": "accumulator",
            "path": "models/accumulator",
            "package": "local/accumulator",
            "version": "1.0.0",
        },
    ]


def test_identity_free_lab_source_uses_transient_identity_for_validation(
    tmp_path: Path,
):
    lab_dir = _write_lab(tmp_path / "Identity Free Lab")

    validation = pack_module.validate_lab_source(lab_dir)

    assert validation.valid
    assert validation.metadata["package"] == "local/identity-free-lab"
    assert validation.metadata["version"] == "0.1.0"


def test_identity_free_lab_requires_release_identity_for_standalone_package(
    tmp_path: Path,
):
    lab_dir = _write_lab(tmp_path / "identity-free-lab")

    with pytest.raises(PackageError, match="pass --package"):
        build_package(lab_dir)

    package_path = build_package(
        lab_dir,
        package_name="demo/identity-free-lab",
        version="1.0.0",
    )
    validation = validate_package(package_path)

    assert validation.valid
    assert validation.metadata["package"] == "demo/identity-free-lab"
    assert validation.metadata["version"] == "1.0.0"


def test_lab_release_identity_overrides_must_match_declared_identity(
    tmp_path: Path,
):
    lab_dir = _write_lab_release_identity(
        _write_lab(tmp_path / "lab"),
        "demo/source-lab",
        "1.0.0",
    )

    with pytest.raises(PackageError, match="--package must match"):
        build_package(
            lab_dir,
            package_name="demo/other-lab",
            version="1.0.0",
        )
    with pytest.raises(PackageError, match="--version must match"):
        build_package(
            lab_dir,
            package_name="demo/source-lab",
            version="2.0.0",
        )


def test_export_lab_alias_embeds_models(tmp_path: Path):
    lab_dir = _write_lab_release_identity(
        _write_lab(tmp_path / "lab"),
        "local/source-lab",
        "1.0.0",
    )
    exported = export_lab_package(
        lab_dir, package_name="local/source-lab", version="1.0.0"
    )
    validation = validate_package(exported)
    assert validation.valid
    result = run_package(exported, install_deps=False)
    assert result["package"] == "local/source-lab"


def test_lab_package_run_preserves_typed_terminal_outputs(tmp_path: Path):
    lab_dir = _write_lab(tmp_path / "lab")
    package_path = build_package(
        lab_dir,
        package_name="local/source-lab",
        version="1.0.0",
    )

    result = run_package(package_path, install_deps=False)

    assert set(result["outputs"]) == {"counter", "accumulator"}
    assert result["outputs"]["counter"]["count"]["type"] == "scalar"
    assert result["outputs"]["counter"]["count"]["spec"]["dtype"] == "float64"
    assert result["outputs"]["counter"]["count"]["value"] == pytest.approx(2.0)
    assert result["outputs"]["accumulator"]["total"]["type"] == "scalar"
    assert result["outputs"]["accumulator"]["total"]["value"] == pytest.approx(1.0)


def test_export_lab_package_ignores_project_metadata_in_logical_hash(tmp_path: Path):
    lab_dir = _write_lab_release_identity(
        _write_lab(tmp_path / "lab"),
        "local/source-lab",
        "1.0.0",
    )
    metadata_path = lab_dir / ".biosimulant-project.json"
    metadata_path.write_text('{"hub_id":"old"}\n', encoding="utf-8")
    first = export_lab_package(
        lab_dir,
        output_path=tmp_path / "first.bsilab",
        package_name="local/source-lab",
        version="1.0.0",
    )
    first_validation = validate_package(first)
    assert first_validation.valid

    metadata_path.write_text(
        '{"hub_id":"new","last_synced_at":"2026-05-15T00:00:00Z"}\n',
        encoding="utf-8",
    )
    second = export_lab_package(
        lab_dir,
        output_path=tmp_path / "second.bsilab",
        package_name="local/source-lab",
        version="1.0.0",
    )
    second_validation = validate_package(second)
    assert second_validation.valid
    assert second_validation.metadata["sha256"] == first_validation.metadata["sha256"]


def test_lab_package_run_settles_final_visuals(tmp_path: Path):
    lab_dir = _write_lab(tmp_path / "lab")
    (lab_dir / "lab.yaml").write_text(
        """
schema_version: "2.0"
title: "Test: Settled Lab"
description: "Final propagation lab"
package: local/settled-lab
version: 1.0.0
models:
  - path: models/counter
    alias: counter
  - path: models/accumulator
    alias: accumulator
runtime:
  communication_step: 0.1
  duration: 0.1
  settle_steps: 1
  initial_inputs: {}
wiring:
  - from: counter.count
    to: [accumulator.value]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    package_path = build_package(
        lab_dir, package_name="local/settled-lab", version="1.0.0"
    )
    result = run_package(package_path, install_deps=False)

    assert result["communication_step"] == pytest.approx(0.1)
    assert result["settle_steps"] == 1
    assert result["visuals"][0]["module"] == "accumulator"


def test_lab_build_ignores_generated_files(tmp_path: Path):
    lab_dir = _write_lab_release_identity(
        _write_lab(tmp_path / "lab"),
        "local/source-lab",
        "1.0.0",
    )
    (lab_dir / ".DS_Store").write_text("junk", encoding="utf-8")
    (lab_dir / "dist").mkdir(exist_ok=True)
    (lab_dir / "dist" / "old.bsilab").write_text("junk", encoding="utf-8")
    experiment_dir = lab_dir / ".biosimulant" / "experiments" / "exp-test"
    experiment_dir.mkdir(parents=True, exist_ok=True)
    (experiment_dir / "experiment.json").write_text("{}", encoding="utf-8")
    pycache_dir = lab_dir / "models" / "counter" / "__pycache__"
    pycache_dir.mkdir(exist_ok=True)
    (pycache_dir / "counter.cpython-311.pyc").write_bytes(b"junk")

    package_path = build_package(
        lab_dir, package_name="local/source-lab", version="1.0.0"
    )
    unpacked = unpack_package(package_path, dest=tmp_path / "lab-unpacked")

    assert not (unpacked / "payload" / ".DS_Store").exists()
    assert not (unpacked / "payload" / ".biosimulant").exists()
    assert not (unpacked / "payload" / "dist").exists()
    assert not (unpacked / "payload" / "models" / "counter" / "__pycache__").exists()


def test_build_lab_from_source_path_refs(tmp_path: Path):
    lab_dir = _write_lab_release_identity(
        _write_path_manifest_lab(tmp_path / "source-lab"),
        "local/source-lab",
        "1.0.0",
    )

    package_path = build_package(
        lab_dir, package_name="local/source-lab", version="1.0.0"
    )

    validation = validate_package(package_path)
    assert validation.valid

    unpacked = unpack_package(package_path, dest=tmp_path / "source-lab-unpacked")
    assert (unpacked / "payload" / "models" / "counter" / "model.yaml").exists()
    result = run_package(package_path, install_deps=False)
    assert result["package"] == "local/source-lab"
    assert result["modules"] == [
        {
            "alias": "counter",
            "path": "models/counter",
        }
    ]


def test_build_lab_from_source_path_refs_does_not_require_nested_package_identity(
    tmp_path: Path,
):
    lab_dir = _write_lab_release_identity(
        _write_path_manifest_lab(tmp_path / "source-lab"),
        "local/source-lab",
        "1.0.0",
    )
    package_path = build_package(
        lab_dir, package_name="local/source-lab", version="1.0.0"
    )
    validation = validate_package(package_path)
    assert validation.valid


def test_lab_build_preserves_source_provenance(tmp_path: Path):
    source = {"path": "labs/demo/lab.yaml", "commit": "abc123"}
    lab_dir = _write_lab_release_identity(
        _write_lab(tmp_path / "lab"),
        "local/provenance-lab",
        "1.0.0",
    )
    lab_pkg = build_package(
        lab_dir,
        package_name="local/provenance-lab",
        version="1.0.0",
        source=source,
    )

    validation = validate_package(lab_pkg)
    assert validation.valid
    assert validation.metadata["source"] == source
    assert validation.metadata["provenance"] == source


def test_build_rejects_legacy_source_provenance(tmp_path: Path):
    model_dir = _write_counter_model(
        tmp_path / "counter", package_name="local/counter", version="1.0.0"
    )

    with pytest.raises(PackageError, match="must not include legacy source keys"):
        build_package(
            model_dir,
            package_name="local/counter",
            version="1.0.0",
            source={
                "repo": "Biosimulant/models-demo",
                "manifest_path": "models/counter/model.yaml",
            },
        )


def test_lab_build_embeds_child_spaces_and_runs_without_registry(tmp_path: Path):
    parent_lab = _write_lab_release_identity(
        _write_parent_lab_with_child(tmp_path / "parent-lab"),
        "local/parent-lab",
        "1.0.0",
    )
    parent_pkg = build_package(
        parent_lab,
        package_name="local/parent-lab",
        version="1.0.0",
    )

    validation = validate_package(parent_pkg)
    assert validation.valid
    unpacked = unpack_package(parent_pkg, dest=tmp_path / "parent-lab-unpacked")
    assert (unpacked / "payload" / "labs" / "child-lab" / "lab.yaml").exists()
    result = run_package(parent_pkg, install_deps=False)
    assert result["package"] == "local/parent-lab"
    assert result["modules"] == [
        {
            "alias": "accumulator",
            "path": "models/accumulator",
            "package": "local/accumulator",
            "version": "1.0.0",
        },
        {
            "alias": "nested.counter",
            "path": "labs/child-lab/models/counter",
            "package": "local/counter",
            "version": "1.0.0",
        },
    ]


def test_lab_package_run_coerces_alias_initial_inputs(tmp_path: Path):
    lab_dir = _write_path_manifest_lab(tmp_path / "source-lab")
    _write_accumulator_model(lab_dir / "models" / "accumulator")
    (lab_dir / "lab.yaml").write_text(
        """
schema_version: "2.0"
title: "Input Lab"
description: "Lab initial inputs"
package: local/input-lab
version: 1.0.0
models:
  - path: models/accumulator
    alias: accumulator
runtime:
  communication_step: 0.1
  duration: 0.1
  initial_inputs:
    accumulator:
      value: 7.0
wiring: []
""".strip()
        + "\n",
        encoding="utf-8",
    )

    package_path = build_package(
        lab_dir, package_name="local/input-lab", version="1.0.0"
    )
    result = run_package(package_path, install_deps=False)

    assert result["package"] == "local/input-lab"
    assert result["modules"] == [{"alias": "accumulator", "path": "models/accumulator"}]
    assert result["visuals"][0]["visuals"][0]["data"]["rows"][0]["total"] == 7.0


def test_lab_package_run_accepts_legacy_flat_initial_inputs(tmp_path: Path):
    lab_dir = _write_lab(tmp_path / "source-lab")
    manifest = pack_module._safe_yaml_load((lab_dir / "lab.yaml").read_bytes())
    manifest["wiring"] = []
    manifest["runtime"]["initial_inputs"] = {"accumulator.value": 7.0}
    (lab_dir / "lab.yaml").write_bytes(pack_module._safe_yaml_dump(manifest))

    package_path = build_package(
        lab_dir, package_name="local/flat-input-lab", version="1.0.0"
    )
    result = run_package(package_path, install_deps=False)

    assert result["package"] == "local/flat-input-lab"
    assert result["visuals"][0]["module"] == "accumulator"
    assert result["visuals"][0]["visuals"][0]["data"]["rows"][0]["total"] == 7.0


def test_lab_build_rejects_nested_package_refs(tmp_path: Path):
    path = tmp_path / "legacy-lab"
    path.mkdir(parents=True, exist_ok=True)
    (path / "lab.yaml").write_text(
        """
schema_version: "2.0"
title: "Legacy Lab"
models:
  - package: local/counter
    version: 1.0.0
    alias: counter
runtime:
  duration: 0.2
  initial_inputs: {}
wiring: []
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PackageError, match="must use path references only"):
        build_package(path, package_name="local/legacy-lab", version="1.0.0")


def test_lab_build_allows_locked_package_child_references(tmp_path: Path):
    path = tmp_path / "package-child-lab"
    path.mkdir()
    (path / "lab.yaml").write_text(
        """
schema_version: "2.0"
title: "Package child"
package: local/package-child
version: 1.0.0
models: []
children:
  - package: acme/nutrient-medium
    version: 1.1.0
    alias: medium
wiring: []
runtime:
  duration: 1
  communication_step: 1
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (path / "biosimulant.lock").write_text(
        """
lock_version: 1
dependencies:
  - package: acme/nutrient-medium
    version: 1.1.0
    artifact_sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
""".strip()
        + "\n",
        encoding="utf-8",
    )

    package_path = build_package(path)

    assert validate_package(package_path).valid


def test_package_child_resolves_into_parent_lab_local_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    child_source = _write_child_output_lab(tmp_path / "child")
    child_package = build_package(child_source, package_name="acme/child", version="1.0.0")
    child_bytes = child_package.read_bytes()
    child_sha = hashlib.sha256(child_bytes).hexdigest()
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "lab.yaml").write_text(
        "schema_version: \"2.0\"\n"
        "title: Parent\npackage: local/parent\nversion: 1.0.0\nmodels: []\n"
        "children:\n  - package: acme/child\n    version: 1.0.0\n    alias: child\n"
        "runtime:\n  communication_step: 0.1\n  duration: 0.2\nwiring: []\n",
        encoding="utf-8",
    )
    (parent / "biosimulant.lock").write_text(
        f"lock_version: 1\ndependencies:\n  - package: acme/child\n"
        f"    version: 1.0.0\n    artifact_sha256: {child_sha}\n",
        encoding="utf-8",
    )
    parent_package = build_package(parent)

    class FakeRegistry:
        _v1 = True

        def __init__(self, _base_url=None):
            pass

        def resolve_package(self, package, version):
            assert (package, version) == ("acme/child", "1.0.0")
            return {"id": "child-artifact", "package_type": "lab", "sha256": child_sha}

        def download_package(self, artifact_id, *, package_name=None, version=None):
            assert artifact_id == "child-artifact"
            assert package_name == "acme/child"
            assert version == "1.0.0"
            return child_bytes

    monkeypatch.setattr(hub_module, "PublicRegistryClient", FakeRegistry)
    prepared = prepare_lab_package(parent_package, install_deps=False)

    assert prepared.world.module_names == ["child.counter"]
    state = parent_package.parent / ".biosimulant" / f"{parent_package.stem}.dependencies"
    assert list(state.rglob("lab.yaml"))


def test_vendor_dependencies_embeds_locked_hub_labs_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    child_source = _write_child_output_lab(tmp_path / "child")
    child_package = build_package(child_source, package_name="acme/child", version="1.0.0")
    child_bytes = child_package.read_bytes()
    child_sha = hashlib.sha256(child_bytes).hexdigest()
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "lab.yaml").write_text(
        "schema_version: \"2.0\"\n"
        "title: Parent\npackage: local/parent\nversion: 1.0.0\nmodels: []\n"
        "children:\n  - package: acme/child\n    version: 1.0.0\n    alias: child\n"
        "runtime:\n  communication_step: 0.1\n  duration: 0.2\nwiring: []\n",
        encoding="utf-8",
    )
    (parent / "biosimulant.lock").write_text(
        f"lock_version: 1\ndependencies:\n  - package: acme/child\n"
        f"    version: 1.0.0\n    artifact_sha256: {child_sha}\n",
        encoding="utf-8",
    )

    class FakeRegistry:
        def __init__(self, _base_url=None):
            pass

        def resolve_package(self, package, version):
            assert (package, version) == ("acme/child", "1.0.0")
            return {"id": "child-artifact", "package_type": "lab", "sha256": child_sha}

        def download_package(self, artifact_id):
            assert artifact_id == "child-artifact"
            return child_bytes

    monkeypatch.setattr(hub_module, "PublicRegistryClient", FakeRegistry)
    archive = build_package(parent, vendor_dependencies=True)
    unpacked = unpack_package(archive, dest=tmp_path / "unpacked")
    manifest = pack_module._safe_yaml_load((unpacked / "payload" / "lab.yaml").read_bytes())
    assert manifest["children"] == [{"alias": "child", "path": "labs/child"}]
    assert (unpacked / "payload" / "labs" / "child" / "lab.yaml").is_file()

    monkeypatch.setattr(
        hub_module,
        "PublicRegistryClient",
        lambda *_args, **_kwargs: pytest.fail("vendored package must not resolve Hub dependencies"),
    )
    result = run_package(archive, install_deps=False)
    assert result["modules"][0]["alias"] == "child.counter"


def test_package_dependency_root_override_keeps_archive_parent_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    child_source = _write_child_output_lab(tmp_path / "child")
    child_package = build_package(child_source, package_name="acme/child", version="1.0.0")
    child_bytes = child_package.read_bytes()
    child_sha = hashlib.sha256(child_bytes).hexdigest()
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "lab.yaml").write_text(
        "schema_version: \"2.0\"\ntitle: Parent\npackage: local/parent\nversion: 1.0.0\nmodels: []\n"
        "children:\n  - package: acme/child\n    version: 1.0.0\n    alias: child\n"
        "runtime:\n  communication_step: 0.1\n  duration: 0.2\nwiring: []\n",
        encoding="utf-8",
    )
    (parent / "biosimulant.lock").write_text(
        f"lock_version: 1\ndependencies:\n  - package: acme/child\n"
        f"    version: 1.0.0\n    artifact_sha256: {child_sha}\n",
        encoding="utf-8",
    )
    parent_package = build_package(parent)

    class FakeRegistry:
        def __init__(self, _base_url=None):
            pass

        def resolve_package(self, *_args):
            return {"id": "child-artifact", "package_type": "lab", "sha256": child_sha}

        def download_package(self, *_args):
            return child_bytes

    monkeypatch.setattr(hub_module, "PublicRegistryClient", FakeRegistry)
    dependency_root = tmp_path / "writable-state"
    prepared = prepare_lab_package(
        parent_package, install_deps=False, dependency_root=dependency_root
    )

    assert prepared.world.module_names == ["child.counter"]
    assert list(dependency_root.rglob("lab.yaml"))
    assert not (parent_package.parent / ".biosimulant").exists()


def test_invalid_dependency_pin_fails(tmp_path: Path):
    model_dir = tmp_path / "bad-model"
    model_dir.mkdir()
    (model_dir / "model.yaml").write_text(
        """
schema_version: "2.0"
title: "Bad"
description: "Bad"
standard: other
tags: [test]
authors: ["Tests"]
biosim:
  entrypoint: "src.bad:Bad"
  communication_step: 0.1
runtime:
  dependencies:
    packages:
      - numpy>=1.0
""".strip()
        + "\n",
        encoding="utf-8",
    )
    src_dir = model_dir / "src"
    src_dir.mkdir()
    (src_dir / "bad.py").write_text(
        """
from biosim import BioModule


class Bad(BioModule):
    def advance_window(self, _start, t): return
    def get_outputs(self): return {}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PackageError):
        build_package(model_dir)


def test_build_rejects_missing_source_and_non_directory_tree(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match="Package source must be a directory"):
        build_package(tmp_path / "missing")

    model_dir = tmp_path / "bad-tree"
    _write_counter_model(model_dir)
    (model_dir / "data").write_text("not a directory", encoding="utf-8")

    with pytest.raises(PackageError, match="Expected directory"):
        build_package(model_dir)


def test_package_private_helpers_cover_source_and_alias_edges(tmp_path: Path) -> None:
    assert pack_module._package_slug("demo/pkg") == "demo__pkg"
    assert pack_module._sanitize_package_source(None) == {}
    assert pack_module._sanitize_package_source(
        {"path": "models/a", "empty": None}
    ) == {"path": "models/a"}

    with pytest.raises(PackageError, match="must not be empty"):
        pack_module._package_slug("")
    with pytest.raises(PackageError, match="source metadata must be a mapping"):
        pack_module._sanitize_package_source(["bad"])  # type: ignore[arg-type]

    assert pack_module._select_alias_override(None, "model") == {}
    assert pack_module._select_alias_override({"model": {"x": 1}}, "model") == {"x": 1}
    assert pack_module._select_alias_override({"model.x": 1}, "model") == {"x": 1}
    assert pack_module._select_alias_override(
        {"model.x": 1, "model": {"x": 2, "y": 3}},
        "model",
    ) == {"x": 2, "y": 3}
    assert pack_module._select_alias_override({"x": 1}, "model", allow_global=True) == {
        "x": 1
    }
    assert pack_module._select_alias_override(
        {"x": 1, "model.x": 2},
        "model",
        allow_global=True,
    ) == {"x": 2}
    assert (
        pack_module._select_alias_override(
            {"other.x": 9},
            "model",
            allow_global=True,
        )
        == {}
    )
    assert pack_module._select_alias_override(
        {"model": {"x": 2}, "x": 1},
        "model",
        allow_global=True,
    ) == {"x": 2}

    remap = pack_module._port_remap_for_child(
        prefix="outer.",
        child_alias="child",
        child_manifest={
            "io": {
                "inputs": [{"name": "in", "maps_to": "inner.in"}],
                "outputs": [{"name": "out", "maps_to": "inner.out"}],
            }
        },
    )
    assert remap == {
        "outer.child.in": "outer.child.inner.in",
        "outer.child.out": "outer.child.inner.out",
    }


def test_validate_package_reports_archive_structure_errors(tmp_path: Path) -> None:
    assert not validate_package(tmp_path / "not-a-package.txt").valid
    missing = validate_package(tmp_path / "missing.bsimodel")
    assert missing.errors and "not found" in missing.errors[0]

    no_manifest = tmp_path / "no-manifest.bsimodel"
    with ZipFile(no_manifest, "w") as zipf:
        zipf.writestr("payload/model.yaml", "biosim: {}\n")
    assert "missing package.yaml" in validate_package(no_manifest).errors[0]

    bad_path = tmp_path / "bad-path.bsimodel"
    with ZipFile(bad_path, "w") as zipf:
        zipf.writestr("../evil", "bad")
        zipf.writestr("package.yaml", "package_type: model\n")
        zipf.writestr("integrity/sha256sums.txt", "")
    assert "Invalid archive path" in validate_package(bad_path).errors[0]

    bad_checksum = tmp_path / "bad-checksum.bsimodel"
    with ZipFile(bad_checksum, "w") as zipf:
        zipf.writestr(
            "package.yaml",
            """
package_type: model
entry_manifest: payload/model.yaml
sha256: bad
""",
        )
        zipf.writestr(
            "payload/model.yaml", "biosim:\n  entrypoint: src.counter:Counter\n"
        )
        zipf.writestr("integrity/sha256sums.txt", "abc  missing.txt\n")
    assert "missing file" in validate_package(bad_checksum).errors[0]


def test_publish_fetch_and_prepare_error_paths(tmp_path: Path, monkeypatch) -> None:
    model_dir = _write_counter_model(tmp_path / "counter")
    package_path = build_package(
        model_dir, package_name="demo/counter", version="1.0.0"
    )

    monkeypatch.delenv("BIOSIM_PACKAGE_REGISTRY_DIR", raising=False)
    with pytest.raises(PackageError, match="Set BIOSIM_PACKAGE_REGISTRY_DIR"):
        publish_package(package_path)

    published = publish_package(package_path, registry_dir=tmp_path / "registry")
    assert published.exists()

    cached = fetch_package(
        "demo/counter",
        "1.0.0",
        registry_dir=tmp_path / "registry",
        cache_dir=tmp_path / "cache",
    )
    assert cached.exists()
    assert (
        fetch_package("demo/counter", "1.0.0", cache_dir=tmp_path / "cache") == cached
    )

    with pytest.raises(PackageError, match="no registry is configured"):
        fetch_package("demo/missing", "1.0.0", cache_dir=tmp_path / "empty-cache")
    with pytest.raises(PackageError, match="was not found"):
        fetch_package(
            "demo/missing",
            "1.0.0",
            registry_dir=tmp_path / "registry",
            cache_dir=tmp_path / "other-cache",
        )
    with pytest.raises(PackageError, match="Expected a lab package"):
        prepare_lab_package(package_path, install_deps=False)


def test_install_declared_dependencies_validation_and_command(monkeypatch) -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    pack_module._install_declared_dependencies({})
    pack_module._install_declared_dependencies(
        {"runtime": {"dependencies": {"packages": []}}}
    )

    with pytest.raises(PackageError, match="exact pins"):
        pack_module._install_declared_dependencies(
            {"runtime": {"dependencies": {"packages": ["numpy>=1"]}}}
        )

    monkeypatch.setattr(pack_module, "_uv_module_available", lambda: True)
    monkeypatch.setattr(pack_module.subprocess, "run", fake_run)
    pack_module._install_declared_dependencies(
        {"runtime": {"dependencies": {"packages": ["numpy==1.26.0"]}}}
    )
    assert calls[0][0] == [
        pack_module.sys.executable,
        "-m",
        "uv",
        "pip",
        "install",
        "--python",
        pack_module.sys.executable,
        "numpy==1.26.0",
    ]


def test_install_declared_dependencies_falls_back_to_pip_when_uv_unavailable(
    monkeypatch,
) -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pack_module, "_uv_module_available", lambda: False)
    monkeypatch.setattr(pack_module.subprocess, "run", fake_run)

    pack_module._install_declared_dependencies(
        {"runtime": {"dependencies": {"packages": ["demo==1.0.0"]}}}
    )

    assert calls[0][0] == [
        pack_module.sys.executable,
        "-m",
        "pip",
        "install",
        "demo==1.0.0",
    ]


def test_install_declared_dependencies_checks_uv_with_current_python(
    monkeypatch,
) -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pack_module.subprocess, "run", fake_run)

    assert pack_module._uv_module_available() is True
    assert calls[0][0] == [
        pack_module.sys.executable,
        "-m",
        "uv",
        "--version",
    ]
    assert calls[0][1]["stdout"] is pack_module.subprocess.DEVNULL
    assert calls[0][1]["stderr"] is pack_module.subprocess.DEVNULL


def test_install_declared_dependencies_no_logger_failure_reports_output(
    monkeypatch,
) -> None:
    def fake_run(args, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="stdout-line\n",
            stderr="stderr-line\n",
        )

    monkeypatch.setattr(pack_module, "_uv_module_available", lambda: False)
    monkeypatch.setattr(pack_module.subprocess, "run", fake_run)

    with pytest.raises(PackageError) as exc_info:
        pack_module._install_declared_dependencies(
            {"runtime": {"dependencies": {"packages": ["demo==1.0.0"]}}},
        )

    message = str(exc_info.value)
    assert "Dependency installation failed with exit code 1" in message
    assert "stdout-line" in message
    assert "stderr-line" in message


def test_install_declared_dependencies_managed_runtime_uses_uv_without_pip(
    monkeypatch,
) -> None:
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pack_module.sys, "executable", "/runtime/python")
    monkeypatch.setattr(pack_module, "_uv_module_available", lambda: True)
    monkeypatch.setattr(pack_module.subprocess, "run", fake_run)

    pack_module._install_declared_dependencies(
        {"runtime": {"dependencies": {"packages": ["demo==1.0.0"]}}},
    )

    assert calls[0][0] == [
        "/runtime/python",
        "-m",
        "uv",
        "pip",
        "install",
        "--python",
        "/runtime/python",
        "demo==1.0.0",
    ]


def test_install_declared_dependencies_streams_output(monkeypatch) -> None:
    calls = []

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.StringIO(
                "Collecting demo\nInstalling collected packages\n"
            )

        def wait(self) -> int:
            return 0

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    lines: list[str] = []
    monkeypatch.setattr(pack_module, "_uv_module_available", lambda: True)
    monkeypatch.setattr(pack_module.subprocess, "Popen", fake_popen)

    pack_module._install_declared_dependencies(
        {"runtime": {"dependencies": {"packages": ["demo==1.0.0"]}}},
        dependency_logger=lines.append,
    )

    assert calls[0][0] == [
        pack_module.sys.executable,
        "-m",
        "uv",
        "pip",
        "install",
        "--python",
        pack_module.sys.executable,
        "demo==1.0.0",
    ]
    assert calls[0][1]["stderr"] is pack_module.subprocess.STDOUT
    assert lines == [
        "Installing dependencies: demo==1.0.0",
        "Collecting demo",
        "Installing collected packages",
    ]


def test_install_declared_dependencies_streaming_failure_includes_tail(
    monkeypatch,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.StringIO("".join(f"line-{index}\n" for index in range(50)))

        def wait(self) -> int:
            return 2

    monkeypatch.setattr(
        pack_module.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess()
    )
    monkeypatch.setattr(pack_module, "_uv_module_available", lambda: True)
    lines: list[str] = []

    with pytest.raises(PackageError) as exc_info:
        pack_module._install_declared_dependencies(
            {"runtime": {"dependencies": {"packages": ["demo==1.0.0"]}}},
            dependency_logger=lines.append,
        )

    message = str(exc_info.value)
    assert "Dependency installation failed with exit code 2" in message
    assert "line-49" in message
    assert "line-0" not in message
    assert lines[-1] == "line-49"


def test_validate_lab_manifest_accepts_supported_python_version() -> None:
    for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        pack_module._validate_lab_manifest(
            {
                "models": [{"alias": "a", "path": "models/a"}],
                "wiring": [],
                "runtime": {"communication_step": 0.1, "python_version": version},
            }
        )


def test_validate_lab_manifest_rejects_unsupported_python_version() -> None:
    with pytest.raises(PackageError, match="runtime.python_version"):
        pack_module._validate_lab_manifest(
            {
                "models": [{"alias": "a", "path": "models/a"}],
                "wiring": [],
                "runtime": {"communication_step": 0.1, "python_version": "3.15"},
            }
        )


def test_validate_lab_manifest_rejects_invalid_python_version_type() -> None:
    with pytest.raises(PackageError, match="runtime.python_version"):
        pack_module._validate_lab_manifest(
            {
                "models": [{"alias": "a", "path": "models/a"}],
                "wiring": [],
                "runtime": {
                    "communication_step": 0.1,
                    "python_version": ["3.11"],
                },
            }
        )


def test_validate_lab_manifest_keeps_missing_python_version_optional() -> None:
    pack_module._validate_lab_manifest(
        {
            "models": [{"alias": "a", "path": "models/a"}],
            "wiring": [],
            "runtime": {"communication_step": 0.1},
        }
    )


def test_lab_python_version_runtime_check_reports_interpreter_mismatch(
    monkeypatch,
) -> None:
    monkeypatch.setattr(pack_module.sys, "version_info", (9, 9, 0, "final", 0))
    with pytest.raises(PackageError, match="requires Python 3.11"):
        pack_module._ensure_lab_python_version_matches_current(
            {"runtime": {"python_version": "3.11"}}
        )


@pytest.mark.parametrize(
    "manifest, match",
    [
        (
            {"models": [], "wiring": [], "runtime": {"communication_step": 0.1}},
            "non-empty models",
        ),
        (
            {"models": ["bad"], "wiring": [], "runtime": {"communication_step": 0.1}},
            "model entries",
        ),
        (
            {
                "models": [{"path": "models/a"}],
                "wiring": [],
                "runtime": {"communication_step": 0.1},
            },
            "define alias",
        ),
        (
            {
                "models": [{"alias": "a"}],
                "wiring": [],
                "runtime": {"communication_step": 0.1},
            },
            "path reference",
        ),
        (
            {
                "models": [
                    {"alias": "a", "path": "models/a"},
                    {"alias": "a", "path": "models/b"},
                ],
                "wiring": [],
                "runtime": {"communication_step": 0.1},
            },
            "Duplicate lab model alias",
        ),
        (
            {
                "models": [{"alias": "a", "repo": "old", "path": "models/a"}],
                "wiring": [],
                "runtime": {"communication_step": 0.1},
            },
            "repo",
        ),
        (
            {
                "models": [{"alias": "a", "package": "old", "path": "models/a"}],
                "wiring": [],
                "runtime": {"communication_step": 0.1},
            },
            "path references only",
        ),
        (
            {
                "children": "bad",
                "models": [{"alias": "a", "path": "models/a"}],
                "wiring": [],
                "runtime": {"communication_step": 0.1},
            },
            "children entries",
        ),
        (
            {
                "children": ["bad"],
                "models": [],
                "wiring": [],
                "runtime": {"communication_step": 0.1},
            },
            "child entries",
        ),
        (
            {
                "children": [{"path": "labs/a"}],
                "models": [],
                "wiring": [],
                "runtime": {"communication_step": 0.1},
            },
            "define alias",
        ),
        (
            {
                "children": [{"alias": "a"}],
                "models": [],
                "wiring": [],
                "runtime": {"communication_step": 0.1},
            },
            "path reference",
        ),
        (
            {
                "children": [
                    {"alias": "a", "path": "labs/a"},
                    {"alias": "a", "path": "labs/b"},
                ],
                "models": [],
                "wiring": [],
                "runtime": {"communication_step": 0.1},
            },
            "Duplicate lab child alias",
        ),
        (
            {
                "children": [{"alias": "a", "repo": "old", "path": "labs/a"}],
                "models": [],
                "wiring": [],
                "runtime": {"communication_step": 0.1},
            },
            "repo",
        ),
        (
            {
                "children": [{"alias": "a", "package": "old", "path": "labs/a"}],
                "models": [],
                "wiring": [],
                "runtime": {"communication_step": 0.1},
            },
            "path references only",
        ),
        (
            {
                "models": [{"alias": "a", "path": "models/a"}],
                "runtime": {"communication_step": 0.1},
            },
            "wiring list",
        ),
        (
            {
                "models": [{"alias": "a", "path": "models/a"}],
                "wiring": [],
                "runtime": "bad",
            },
            "runtime mapping",
        ),
        (
            {
                "models": [{"alias": "a", "path": "models/a"}],
                "wiring": [],
                "runtime": {"tick_dt": 0.1, "communication_step": 0.1},
            },
            "tick_dt",
        ),
        (
            {
                "models": [{"alias": "a", "path": "models/a"}],
                "wiring": [],
                "runtime": {},
            },
            "communication_step",
        ),
        (
            {
                "models": [{"alias": "a", "path": "models/a"}],
                "wiring": [],
                "runtime": {"communication_step": 0},
            },
            "positive",
        ),
        (
            {
                "models": [{"alias": "a", "path": "models/a"}],
                "wiring": [],
                "runtime": {"communication_step": "fast"},
            },
            "numeric",
        ),
    ],
)
def test_validate_lab_manifest_error_matrix(manifest, match: str) -> None:
    with pytest.raises(PackageError, match=match):
        pack_module._validate_lab_manifest(manifest)


def test_remote_execution_init_kwargs_override_parameters_only_on_remote_compute(
    tmp_path: Path, monkeypatch
) -> None:
    model_dir = _write_counter_model(tmp_path / "counter")
    manifest_path = model_dir / "model.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        + "runtime:\n"
        + "  remote:\n"
        + "    init_kwargs:\n"
        + "      step: 5.0\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(pack_module.BIOSIM_REMOTE_EXECUTION_ENV, raising=False)

    local_module, _ = pack_module._instantiate_model_from_dir(
        model_dir, parameters={"step": 2.0}
    )
    monkeypatch.setenv(pack_module.BIOSIM_REMOTE_EXECUTION_ENV, "1")
    remote_module, _ = pack_module._instantiate_model_from_dir(
        model_dir, parameters={"step": 2.0}
    )

    assert local_module.step == 2.0
    assert remote_module.step == 5.0


def test_remote_execution_init_kwargs_expand_the_mount_root(monkeypatch) -> None:
    manifest = {
        "runtime": {
            "remote": {
                "init_kwargs": {
                    "runtime_mode": "external",
                    "cache_dir": "${REMOTE_EXECUTION_MOUNT_ROOT}/runtime-cache/boltz",
                    "paths": ["${REMOTE_EXECUTION_MOUNT_ROOT}/a", "b"],
                }
            }
        }
    }
    monkeypatch.setenv(pack_module.BIOSIM_REMOTE_EXECUTION_ENV, "1")
    monkeypatch.delenv(pack_module.BIOSIM_REMOTE_EXECUTION_MOUNT_ROOT_ENV, raising=False)

    with pytest.raises(PackageError, match="BIOSIM_REMOTE_EXECUTION_MOUNT_ROOT"):
        pack_module._remote_execution_init_kwargs(manifest)

    monkeypatch.setenv(pack_module.BIOSIM_REMOTE_EXECUTION_MOUNT_ROOT_ENV, "/workspace/.bsimcache/")
    assert pack_module._remote_execution_init_kwargs(manifest) == {
        "runtime_mode": "external",
        "cache_dir": "/workspace/.bsimcache/runtime-cache/boltz",
        "paths": ["/workspace/.bsimcache/a", "b"],
    }
    assert pack_module._remote_execution_init_kwargs({"runtime": {"remote": {}}}) == {}


def test_install_declared_dependencies_exposes_installed_console_scripts(
    tmp_path: Path, monkeypatch
) -> None:
    scripts_dir = str(tmp_path / "venv" / "bin")
    monkeypatch.setattr(
        pack_module.sysconfig,
        "get_path",
        lambda name: scripts_dir if name == "scripts" else None,
    )
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(pack_module, "_uv_module_available", lambda: False)
    monkeypatch.setattr(
        pack_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    manifest = {"runtime": {"dependencies": {"packages": ["boltz==2.0.2"]}}}

    pack_module._install_declared_dependencies(manifest)
    pack_module._install_declared_dependencies(manifest)

    assert pack_module.os.environ["PATH"].split(pack_module.os.pathsep) == [
        scripts_dir,
        "/usr/bin",
    ]
