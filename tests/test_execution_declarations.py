from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

import biosim
from biosim.execution import (
    bind_manifest_execution_policy,
    declared_execution_policy,
    describe_lab_execution,
    execution_phase_findings,
    module_edges_from_wiring,
    read_source_execution_policy,
    source_declaration_findings,
)
from biosim.pack import (
    PackageError,
    build_package,
    inspect_lab_execution,
    run_package,
    validate_lab_source,
)

P = biosim.ExecutionPolicy
SCALAR = biosim.SignalSpec.scalar(dtype="float64")


# --- Manifest declarations -------------------------------------------------


def test_declared_policy_is_optional_and_validated() -> None:
    assert declared_execution_policy({"biosim": {"entrypoint": "src.m:M"}}) is None
    assert declared_execution_policy({"biosim": {"execution_policy": None}}) is None
    assert (
        declared_execution_policy({"biosim": {"execution_policy": "once_before_run"}})
        is P.ONCE_BEFORE_RUN
    )
    with pytest.raises(ValueError, match="biosim.execution_policy must be one of"):
        declared_execution_policy({"biosim": {"execution_policy": "sometimes"}})


class _Once(biosim.BioModule):
    execution_policy = P.ONCE_BEFORE_RUN

    def outputs(self):
        return {"score": SCALAR}

    def execute(self, inputs, *, context):
        return {"score": 1.0}


class _Inherits(biosim.BioModule):
    def outputs(self):
        return {"score": SCALAR}

    def execute(self, inputs, *, context):
        return {"score": 1.0}


class _Temporal(biosim.BioModule):
    def outputs(self):
        return {"score": SCALAR}

    def advance_window(self, start, end):
        return None


class _ByMode(biosim.BioModule):
    execution_policy = P.ONCE_BEFORE_RUN

    def __init__(self, mode: str = "once") -> None:
        if mode == "stream":
            self.execution_policy = P.EACH_WINDOW

    def outputs(self):
        return {"score": SCALAR}

    def execute(self, inputs, *, context):
        return {"score": 1.0}


def _manifest(policy: str | None) -> dict:
    block = {"entrypoint": "src.m:M", "communication_step": 1.0}
    if policy is not None:
        block["execution_policy"] = policy
    return {"biosim": block}


def test_binding_accepts_matching_declarations() -> None:
    assert bind_manifest_execution_policy(_Once(), _manifest("once_before_run")) is P.ONCE_BEFORE_RUN
    assert bind_manifest_execution_policy(_Temporal(), _manifest("each_window")) is P.EACH_WINDOW
    assert bind_manifest_execution_policy(_Once(), _manifest(None)) is None


@pytest.mark.parametrize(
    ("module", "declared", "resolved"),
    [
        (_Once(), "each_window", "once_before_run"),
        (_Inherits(), "once_before_run", "each_window"),
        (_Temporal(), "once_after_run", "each_window"),
        (_ByMode(mode="stream"), "once_before_run", "each_window"),
    ],
)
def test_binding_rejects_declarations_the_code_contradicts(module, declared, resolved) -> None:
    with pytest.raises(ValueError) as excinfo:
        bind_manifest_execution_policy(module, _manifest(declared))
    message = str(excinfo.value)
    assert f"declares biosim.execution_policy '{declared}'" in message
    assert f"resolves to '{resolved}'" in message


def test_each_window_declaration_confirms_an_inherited_policy() -> None:
    world = biosim.BioWorld(communication_step=0.1)
    module = _Inherits()
    bind_manifest_execution_policy(module, _manifest("each_window"))

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        world.add_biomodule("model", module)

    assert world.execution_policies == {"model": P.EACH_WINDOW}


# --- Profiles ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("policies", "timing", "undeclared"),
    [
        ({"a": "once_before_run", "b": "once_after_run"}, "finite", []),
        ({"a": "once_before_run", "b": "each_window"}, "temporal", []),
        ({"a": None, "b": "each_window"}, "temporal", ["a"]),
        ({"a": None, "b": "once_before_run"}, "unknown", ["a"]),
        ({}, "unknown", []),
    ],
)
def test_lab_profile_timing(policies, timing, undeclared) -> None:
    profile = describe_lab_execution(policies).to_dict()

    assert profile["timing"] == timing
    assert profile["undeclared"] == undeclared
    assert profile["policies"] == policies


# --- Phase rules -------------------------------------------------------------


def test_wiring_edges_use_the_last_dot_as_port_separator() -> None:
    wiring = [
        {"from": "child.prep.score", "to": ["report.score", "child.sim.x"]},
        ("sim.out", "report.y"),
        {"from": "bad", "to": ["report.z"]},
    ]

    assert module_edges_from_wiring(wiring) == [
        ("child.prep", "report"),
        ("child.prep", "child.sim"),
        ("sim", "report"),
    ]


def test_phase_findings_reject_backward_edges_and_once_cycles() -> None:
    policies = {
        "prep": "once_before_run",
        "sim": "each_window",
        "left": "once_after_run",
        "right": "once_after_run",
    }
    findings = execution_phase_findings(
        policies,
        [("sim", "prep"), ("left", "right"), ("right", "left"), ("prep", "sim")],
    )

    assert findings == [
        "invalid execution phase edge: sim (each_window) -> prep (once_before_run)",
        "once_after_run modules contain a dependency cycle: left, right",
    ]


def test_phase_findings_skip_undeclared_modules() -> None:
    assert execution_phase_findings({"sim": "each_window", "prep": None}, [("sim", "prep")]) == []


def test_static_phase_rules_match_bioworld() -> None:
    class After(_Once):
        execution_policy = P.ONCE_AFTER_RUN

    class Before(_Once):
        def inputs(self):
            return {"score": SCALAR}

    world = biosim.BioWorld(communication_step=0.1)
    world.add_biomodule("after", After())
    world.add_biomodule("before", Before())
    world.connect("after.score", "before.score")

    expected = execution_phase_findings(
        {name: policy.value for name, policy in world.execution_policies.items()},
        [("after", "before")],
    )
    with pytest.raises(ValueError) as excinfo:
        world.run(duration=0.1)
    assert [str(excinfo.value)] == expected


# --- Static source reading ---------------------------------------------------


@pytest.mark.parametrize(
    ("source", "status", "policy"),
    [
        (
            "from biosimulant import BioModule, ExecutionPolicy\n"
            "class M(BioModule):\n    execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN\n",
            "resolved",
            "once_before_run",
        ),
        (
            "import biosimulant\n"
            "class M(biosimulant.BioModule):\n    execution_policy = biosimulant.ExecutionPolicy.ONCE_AFTER_RUN\n",
            "resolved",
            "once_after_run",
        ),
        ("class M(BioModule):\n    execution_policy = 'each_window'\n", "resolved", "each_window"),
        (
            "class M(BioModule):\n    def execute(self, inputs, *, context):\n        return {}\n",
            "resolved",
            "each_window",
        ),
        (
            "class M(BioModule):\n    def advance_window(self, start, end):\n        return None\n",
            "resolved",
            "each_window",
        ),
        (
            "class Base(BioModule):\n    execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN\n"
            "class M(Base):\n    pass\n",
            "resolved",
            "once_before_run",
        ),
        (
            "class M(BioModule):\n    def __init__(self):\n"
            "        self.execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN\n",
            "resolved",
            "once_before_run",
        ),
        (
            "class M(BioModule):\n    execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN\n"
            "    def __init__(self, mode='once'):\n"
            "        if mode == 'stream':\n            self.execution_policy = ExecutionPolicy.EACH_WINDOW\n",
            "dynamic",
            None,
        ),
        ("class M(BioModule):\n    execution_policy = POLICY\n", "dynamic", None),
        ("class M(OnnxBioModule):\n    pass\n", "unknown", None),
        ("class Other(BioModule):\n    pass\n", "unknown", None),
        ("class M(:\n", "unknown", None),
    ],
)
def test_source_reading(source: str, status: str, policy: str | None) -> None:
    reading = read_source_execution_policy(source, "M")

    assert reading.status == status
    assert (reading.policy.value if reading.policy else None) == policy


def test_source_findings_error_on_proven_mismatch_and_warn_when_unverifiable() -> None:
    resolved = read_source_execution_policy(
        "class M(BioModule):\n    execution_policy = ExecutionPolicy.EACH_WINDOW\n", "M"
    )
    dynamic = read_source_execution_policy("class M(BioModule):\n    execution_policy = X\n", "M")

    errors, warns = source_declaration_findings(P.ONCE_BEFORE_RUN, resolved)
    assert errors and "resolves to 'each_window'" in errors[0]
    assert warns == []

    errors, warns = source_declaration_findings(P.ONCE_BEFORE_RUN, dynamic)
    assert errors == []
    assert warns and "can't be verified from source" in warns[0]

    assert source_declaration_findings(None, dynamic) == ([], [])


# --- Lab validation and runs -------------------------------------------------


def _write_model(
    path: Path,
    *,
    class_name: str,
    policy_source: str,
    declared: str | None,
    inputs: bool = False,
    outputs: bool = True,
    init: str = "",
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    lines = [
        'schema_version: "2.0"',
        f'title: "Test: {class_name}"',
        "standard: other",
        "biosim:",
        f'  entrypoint: "src.model:{class_name}"',
        "  communication_step: 0.1",
    ]
    if declared is not None:
        lines.append(f"  execution_policy: {declared}")
    (path / "model.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "src").mkdir(exist_ok=True)
    body = [
        "from biosimulant import BioModule, ExecutionPolicy, SignalSpec",
        "",
        "",
        f"class {class_name}(BioModule):",
        f"    {policy_source}",
    ]
    if init:
        body.append(init)
    if inputs:
        body += [
            "",
            "    def inputs(self):",
            '        return {"score": SignalSpec.scalar(dtype="float64")}',
        ]
    if outputs:
        body += [
            "",
            "    def outputs(self):",
            '        return {"score": SignalSpec.scalar(dtype="float64")}',
        ]
    body += [
        "",
        "    def execute(self, inputs, *, context):",
        '        return {"score": 1.0} if self.outputs() else {}',
        "",
    ]
    (path / "src" / "model.py").write_text("\n".join(body), encoding="utf-8")


def _write_finite_lab(path: Path, *, report_declared: str | None = "once_after_run") -> Path:
    _write_model(
        path / "models" / "prep",
        class_name="Prep",
        policy_source="execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN",
        declared="once_before_run",
    )
    _write_model(
        path / "models" / "report",
        class_name="Report",
        policy_source="execution_policy = ExecutionPolicy.ONCE_AFTER_RUN",
        declared=report_declared,
        inputs=True,
    )
    (path / "lab.yaml").write_text(
        "\n".join(
            [
                'schema_version: "2.0"',
                'title: "Test: Finite lab"',
                "package: tests/finite-lab",
                "version: 1.0.0",
                "models:",
                "  - path: models/prep",
                "    alias: prep",
                "  - path: models/report",
                "    alias: report",
                "wiring:",
                "  - from: prep.score",
                "    to: [report.score]",
                "runtime:",
                "  communication_step: 0.01",
                "  duration: 5",
                "  initial_inputs: {}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_validate_lab_source_reports_a_finite_profile(tmp_path: Path) -> None:
    lab = _write_finite_lab(tmp_path / "lab")

    result = validate_lab_source(lab)

    assert result.valid, result.errors
    assert result.metadata["execution"] == {
        "timing": "finite",
        "policies": {"prep": "once_before_run", "report": "once_after_run"},
        "undeclared": [],
    }
    assert result.warnings == []


def test_validate_lab_source_hints_undeclared_models(tmp_path: Path) -> None:
    lab = _write_finite_lab(tmp_path / "lab", report_declared=None)

    result = validate_lab_source(lab)

    assert result.valid, result.errors
    assert result.metadata["execution"]["timing"] == "unknown"
    assert result.metadata["execution"]["undeclared"] == ["report"]
    assert any("add `execution_policy: once_after_run`" in item for item in result.warnings)


def test_validate_lab_source_rejects_a_contradicted_declaration(tmp_path: Path) -> None:
    lab = _write_finite_lab(tmp_path / "lab", report_declared="each_window")

    result = validate_lab_source(lab)

    assert not result.valid
    assert "report: model.yaml declares biosim.execution_policy 'each_window'" in result.errors[0]


def test_validate_lab_source_rejects_backward_phase_wiring(tmp_path: Path) -> None:
    lab = _write_finite_lab(tmp_path / "lab")
    manifest = (lab / "lab.yaml").read_text(encoding="utf-8")
    (lab / "lab.yaml").write_text(
        manifest.replace("  - from: prep.score\n    to: [report.score]", "  - from: report.score\n    to: [prep.score]"),
        encoding="utf-8",
    )
    _write_model(
        lab / "models" / "prep",
        class_name="Prep",
        policy_source="execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN",
        declared="once_before_run",
        inputs=True,
    )

    result = validate_lab_source(lab)

    assert not result.valid
    assert result.errors == [
        "invalid execution phase edge: report (once_after_run) -> prep (once_before_run)"
    ]


def test_inspect_lab_execution_warns_for_parameter_dependent_declarations(tmp_path: Path) -> None:
    lab = _write_finite_lab(tmp_path / "lab")
    _write_model(
        lab / "models" / "prep",
        class_name="Prep",
        policy_source="execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN",
        declared="once_before_run",
        init=(
            "\n    def __init__(self, mode='once'):\n"
            "        if mode == 'stream':\n"
            "            self.execution_policy = ExecutionPolicy.EACH_WINDOW\n"
        ),
    )

    report = inspect_lab_execution(lab)

    assert report["errors"] == []
    assert report["models"]["prep"]["source"]["status"] == "dynamic"
    assert any("can't be verified from source" in item for item in report["warnings"])


def test_runtime_rejects_a_declaration_the_constructed_module_contradicts(tmp_path: Path) -> None:
    lab = _write_finite_lab(tmp_path / "lab")
    _write_model(
        lab / "models" / "prep",
        class_name="Prep",
        policy_source="execution_policy = ExecutionPolicy.ONCE_BEFORE_RUN",
        declared="once_before_run",
        init=(
            "\n    def __init__(self, mode='once'):\n"
            "        if mode == 'stream':\n"
            "            self.execution_policy = ExecutionPolicy.EACH_WINDOW\n"
        ),
    )
    manifest = (lab / "lab.yaml").read_text(encoding="utf-8")
    (lab / "lab.yaml").write_text(
        manifest.replace("    alias: prep\n", "    alias: prep\n    parameters:\n      mode: stream\n"),
        encoding="utf-8",
    )
    package = build_package(lab, output_path=tmp_path / "lab.bsilab")

    with pytest.raises(PackageError, match="model.yaml doesn't match the Python module"):
        run_package(package, install_deps=False)


def test_finite_lab_runs_once_in_a_single_step_and_reports_execution(tmp_path: Path) -> None:
    lab = _write_finite_lab(tmp_path / "lab")
    package = build_package(lab, output_path=tmp_path / "lab.bsilab")

    result = run_package(package, install_deps=False)

    assert result["execution"]["timing"] == "finite"
    assert result["outputs"]["report"]["score"]["value"] == 1.0


def test_world_without_each_window_modules_emits_one_step() -> None:
    world = biosim.BioWorld(communication_step=0.0001)
    world.add_biomodule("model", _Once())
    steps: list[dict] = []
    world.on(lambda event, payload: steps.append(payload) if event is biosim.WorldEvent.STEP else None)

    world.run(duration=100.0)

    assert len(steps) == 1
    assert steps[0]["window_start"] == 0.0
    assert steps[0]["window_end"] == 100.0
    assert steps[0]["progress_pct"] == 100.0
    assert world.get_outputs("model")["score"].value == 1.0


# --- Conformance fixture shared with ports of these rules ---------------------

_CONFORMANCE = json.loads(
    (Path(__file__).parent / "fixtures" / "execution_conformance.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", _CONFORMANCE["profiles"])
def test_conformance_profiles(case) -> None:
    profile = describe_lab_execution(case["policies"]).to_dict()
    assert {"timing": profile["timing"], "undeclared": profile["undeclared"]} == case["expected"]


@pytest.mark.parametrize("case", _CONFORMANCE["phase_findings"])
def test_conformance_phase_findings(case) -> None:
    edges = module_edges_from_wiring(case["wiring"])
    assert execution_phase_findings(case["policies"], edges) == case["expected_findings"]


@pytest.mark.parametrize("case", _CONFORMANCE["source_readings"])
def test_conformance_source_readings(case) -> None:
    reading = read_source_execution_policy(case["source"], case["class_name"])
    assert {
        "status": reading.status,
        "policy": reading.policy.value if reading.policy else None,
    } == case["expected"]


# --- Surfaces ----------------------------------------------------------------


def test_labs_serve_lab_payload_reports_execution(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from biosim.labs_serve.server import LabServeSession, create_app

    finite = _write_finite_lab(tmp_path / "finite")
    client = TestClient(create_app(LabServeSession(finite, install_deps=False)))

    lab = client.get("/api/lab").json()["data"]["lab"]

    assert lab["execution"]["timing"] == "finite"


def test_labs_serve_falls_back_to_unknown_when_execution_cannot_be_described(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from biosim.labs_serve import server
    from biosim.labs_serve.server import LabServeSession, create_app

    lab_dir = _write_finite_lab(tmp_path / "lab")

    def broken(_path):
        raise PackageError("unreadable")

    monkeypatch.setattr(server, "inspect_lab_execution", broken)
    client = TestClient(create_app(LabServeSession(lab_dir, install_deps=False)))

    lab = client.get("/api/lab").json()["data"]["lab"]

    assert lab["execution"] == {"timing": "unknown", "policies": {}, "undeclared": []}


def test_labs_validate_cli_prints_execution_timing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from biosim.__main__ import main

    lab = _write_finite_lab(tmp_path / "lab")

    main(["labs", "validate", str(lab)])

    output = capsys.readouterr().out
    assert "Execution: finite (every module runs once; duration and communication step don't apply)" in output


def test_inspect_lab_execution_leaves_package_children_undeclared(tmp_path: Path) -> None:
    lab = _write_finite_lab(tmp_path / "lab")
    manifest = (lab / "lab.yaml").read_text(encoding="utf-8")
    (lab / "lab.yaml").write_text(
        manifest.replace(
            "wiring:",
            "children:\n  - alias: docking\n    package: demi/docking-lab\n    version: 1.0.0\nwiring:",
        ),
        encoding="utf-8",
    )

    report = inspect_lab_execution(lab)

    assert report["profile"]["timing"] == "unknown"
    assert report["profile"]["undeclared"] == ["docking"]
    assert report["profile"]["policies"]["prep"] == "once_before_run"
