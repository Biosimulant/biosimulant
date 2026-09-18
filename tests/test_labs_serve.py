from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

from fastapi.testclient import TestClient

from biosim.labs_serve import server
from biosim.labs_serve.server import LabServeSession, RunRecord, create_app
from biosim.pack import _safe_yaml_dump, _safe_yaml_load
from tests.test_pack import _write_lab


def _client(lab: Path) -> tuple[TestClient, LabServeSession]:
    session = LabServeSession(lab, install_deps=False)
    return TestClient(create_app(session)), session


def _mark_runtime_metadata_ready(session: LabServeSession) -> None:
    with session._lock:
        session._runtime_metadata_status = "ready"
        session._runtime_metadata_error = None
        session._runtime_port_payloads = {}


class _RuntimeSpec:
    def __init__(self, description: str, *, units: list[str] | None = None) -> None:
        self.description = description
        self.units = units or []

    def to_dict(self) -> dict[str, object]:
        return {
            "description": self.description,
            "accepted_profiles": [{"accepted_units": self.units}] if self.units else [],
        }


class _RuntimeModule:
    input_specs = {"runtime_in": _RuntimeSpec("runtime input", units=["mM"])}
    output_specs = {"runtime_out": _RuntimeSpec("runtime output")}


class _RuntimeCompatibility:
    def to_dict(self) -> dict[str, object]:
        return {"connections": []}


class _RuntimeWorld:
    _modules = {"counter": _RuntimeModule()}
    # The real world carries these too, and serve records both in run results.
    module_names = ("counter",)
    compatibility = _RuntimeCompatibility()

    def __init__(self) -> None:
        self._listeners: list[object] = []

    def get_outputs(self, module_name: str) -> dict[str, object]:
        return {}

    def on(self, listener) -> None:
        self._listeners.append(listener)

    def off(self, listener) -> None:
        self._listeners.remove(listener)

    def run(self, *, duration: float) -> None:
        return None

    def settle(self, steps: int) -> None:
        return None

    def collect_visuals(self) -> list[dict[str, object]]:
        return []


class _PreparedRuntime:
    world = _RuntimeWorld()
    duration = 0.1
    communication_step = 0.01
    settle_steps = 0
    modules: list[dict[str, object]] = []


def _set_model_parameters(lab: Path, alias: str, parameters: dict[str, object]) -> None:
    manifest_path = lab / "lab.yaml"
    manifest = _safe_yaml_load(manifest_path.read_bytes())
    for entry in manifest["models"]:
        if entry["alias"] == alias:
            entry["parameters"] = parameters
            break
    else:  # pragma: no cover - defensive test helper
        raise AssertionError(f"model alias not found: {alias}")
    manifest_path.write_bytes(_safe_yaml_dump(manifest))


def test_root_html_static_assets_and_legacy_ui_redirect(tmp_path: Path, monkeypatch) -> None:
    lab = _write_lab(tmp_path / "lab")
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>labs serve</html>", encoding="utf-8")
    (static / "assets" / "app.js").write_text("console.log('ok')", encoding="utf-8")
    monkeypatch.setattr(server, "STATIC_DIR", static)

    client, _session = _client(lab)

    root = client.get("/")
    assert root.status_code == 200
    assert "labs serve" in root.text
    assert client.get("/assets/app.js").status_code == 200
    redirected = client.get("/ui/", follow_redirects=False)
    assert redirected.status_code == 307
    assert redirected.headers["location"] == "/"


def test_lab_api_enriches_payload_and_persists_edits(tmp_path: Path) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)

    payload = client.get("/api/lab").json()
    assert payload["ok"] is True
    lab_payload = payload["data"]["lab"]
    assert lab_payload["title"] == "Test: Lab"
    counter = lab_payload["manifest"]["models"][0]
    assert counter["alias"] == "counter"
    assert counter["resolved_model"]["title"] == "Test: Counter"
    if session._runtime_metadata_thread is not None:
        session._runtime_metadata_thread.join(timeout=2)
    payload = client.get("/api/lab").json()
    counter = payload["data"]["lab"]["manifest"]["models"][0]
    assert counter["resolved_model"]["io"]["outputs"][0]["name"] == "count"

    renamed = client.put("/api/lab/models/counter", json={"alias": "source"}).json()
    assert renamed["ok"] is True
    manifest = _safe_yaml_load((lab / "lab.yaml").read_bytes())
    assert manifest["models"][0]["alias"] == "source"
    assert manifest["wiring"][0]["from"] == "source.count"

    saved = client.put(
        "/api/lab/layout",
        json={"nodes": [{"id": "source", "position": {"x": 10, "y": 20}}]},
    ).json()
    assert saved["ok"] is True
    layout = json.loads((lab / "wiring-layout.json").read_text(encoding="utf-8"))
    assert layout["nodes"][0]["position"] == {"x": 10, "y": 20}

    world_saved = client.put(
        "/api/lab/world",
        json={"wiring": [{"from": "source.count", "to": "accumulator.value"}]},
    ).json()
    assert world_saved["ok"] is True
    manifest = _safe_yaml_load((lab / "lab.yaml").read_bytes())
    assert manifest["wiring"] == [{"from": "source.count", "to": "accumulator.value"}]


def test_compute_warning_detector_reports_gpu_accelerators() -> None:
    manifest = {
        "models": [
            {
                "alias": "boltz",
                "parameters": {"accelerator": "gpu", "devices": 1},
            }
        ]
    }

    warnings = server._compute_warnings_for_manifest(manifest)

    assert len(warnings) == 1
    assert warnings[0]["code"] == "gpu-accelerator-requested"
    assert warnings[0]["model_alias"] == "boltz"
    assert warnings[0]["parameter"] == "accelerator"
    assert warnings[0]["value"] == "gpu"
    assert "will continue with the lab's configured accelerator" in warnings[0]["message"]


def test_compute_warning_detector_ignores_cpu_and_missing_accelerators() -> None:
    assert (
        server._compute_warnings_for_manifest(
            {"models": [{"alias": "cpu", "parameters": {"accelerator": "cpu"}}]}
        )
        == []
    )
    assert server._compute_warnings_for_manifest({"models": [{"alias": "plain"}]}) == []


def test_lab_payload_includes_compute_warnings_for_gpu_accelerators(tmp_path: Path) -> None:
    lab = _write_lab(tmp_path / "lab")
    _set_model_parameters(lab, "counter", {"accelerator": "gpu"})
    client, session = _client(lab)
    _mark_runtime_metadata_ready(session)

    payload = client.get("/api/lab").json()["data"]["lab"]

    warnings = payload["compute_warnings"]
    assert len(warnings) == 1
    assert warnings[0]["code"] == "gpu-accelerator-requested"
    assert warnings[0]["model_alias"] == "counter"


def test_lab_api_returns_manifest_payload_before_runtime_metadata_is_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)
    started = threading.Event()
    release = threading.Event()

    def slow_prepare(*_args, **_kwargs):
        started.set()
        release.wait(timeout=5)
        return _PreparedRuntime()

    monkeypatch.setattr(server, "prepare_lab_package", slow_prepare)

    try:
        began = time.monotonic()
        payload = client.get("/api/lab").json()
        elapsed = time.monotonic() - began

        assert elapsed < 0.5
        assert payload["ok"] is True
        lab_payload = payload["data"]["lab"]
        assert lab_payload["runtime_metadata_status"] == "running"
        counter = lab_payload["manifest"]["models"][0]
        assert counter["resolved_model"]["title"] == "Test: Counter"
        assert started.wait(timeout=1)
    finally:
        release.set()
        if session._runtime_metadata_thread is not None:
            session._runtime_metadata_thread.join(timeout=2)


def test_lab_api_uses_cached_runtime_metadata_when_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)

    monkeypatch.setattr(server, "prepare_lab_package", lambda *_args, **_kwargs: _PreparedRuntime())

    first = client.get("/api/lab").json()["data"]["lab"]
    assert first["runtime_metadata_status"] in {"running", "ready"}
    assert session._runtime_metadata_thread is not None
    session._runtime_metadata_thread.join(timeout=2)

    payload = client.get("/api/lab").json()

    assert payload["ok"] is True
    lab_payload = payload["data"]["lab"]
    assert lab_payload["runtime_metadata_status"] == "ready"
    counter = lab_payload["manifest"]["models"][0]
    assert counter["resolved_model"]["io"]["inputs"][0]["name"] == "runtime_in"
    assert counter["resolved_model"]["io"]["inputs"][0]["accepted_units"] == ["mM"]
    assert counter["resolved_model"]["io"]["outputs"][0]["name"] == "runtime_out"


def test_run_runtime_preparation_waits_for_active_metadata_enrichment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)
    background_started = threading.Event()
    release_background = threading.Event()
    calls: list[float] = []

    def prepare(*_args, **kwargs):
        calls.append(time.monotonic())
        if len(calls) == 1:
            assert kwargs["install_deps"] is False
            background_started.set()
            release_background.wait(timeout=5)
        elif kwargs.get("dependency_logger"):
            kwargs["dependency_logger"]("Installing demo dependency")
        return _PreparedRuntime()

    monkeypatch.setattr(server, "prepare_lab_package", prepare)

    try:
        client.get("/api/lab")
        assert background_started.wait(timeout=1)

        created = client.post("/api/runs", json={}).json()
        assert created["ok"] is True
        run = session.get_run(created["data"]["run"]["id"])

        for _ in range(20):
            messages = [entry["message"] for entry in run.logs]
            if "Preparing runtime/dependencies..." in messages:
                break
            time.sleep(0.05)
        assert "Preparing runtime/dependencies..." in [entry["message"] for entry in run.logs]
        time.sleep(0.1)
        assert len(calls) == 1

        release_background.set()
        if session._runtime_metadata_thread is not None:
            session._runtime_metadata_thread.join(timeout=2)
        assert run.thread is not None
        run.thread.join(timeout=2)

        assert len(calls) == 2
        assert run.status == "completed"
        messages = [entry["message"] for entry in run.logs]
        assert "Runtime prepared" in messages
        assert "Waiting for existing runtime preparation..." in messages
        pip_logs = [entry for entry in run.logs if entry["source"] == "pip"]
        assert pip_logs[0]["message"] == "Installing demo dependency"
    finally:
        release_background.set()


def test_run_streams_dependency_logs_to_run_logs_and_terminal(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)

    def prepare(*_args, **kwargs):
        logger = kwargs.get("dependency_logger")
        if logger:
            logger("Collecting demo==1.0.0")
            logger("Successfully installed demo")
        return _PreparedRuntime()

    monkeypatch.setattr(server, "prepare_lab_package", prepare)

    created = client.post("/api/runs", json={}).json()
    assert created["ok"] is True
    run = session.get_run(created["data"]["run"]["id"])
    assert run.thread is not None
    run.thread.join(timeout=2)

    pip_logs = [entry for entry in run.logs if entry["source"] == "pip"]
    assert [entry["message"] for entry in pip_logs] == [
        "Collecting demo==1.0.0",
        "Successfully installed demo",
    ]
    terminal = capsys.readouterr().out
    assert "pip: Collecting demo==1.0.0" in terminal
    assert "runtime: Runtime prepared" in terminal


def test_run_streams_structured_model_progress_to_run_logs(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)

    class ProgressWorld(_RuntimeWorld):
        def run(self, *, duration: float) -> None:
            print("raw model output")
            print(
                'BSIM_PROGRESS:{"message":"Boltz-2 prediction is still running",'
                '"phase":"inference","duration":30.0}'
            )

    class PreparedProgressRuntime(_PreparedRuntime):
        world = ProgressWorld()

    monkeypatch.setattr(
        server,
        "prepare_lab_package",
        lambda *_args, **_kwargs: PreparedProgressRuntime(),
    )

    created = client.post("/api/runs", json={}).json()
    assert created["ok"] is True
    run = session.get_run(created["data"]["run"]["id"])
    assert run.thread is not None
    run.thread.join(timeout=2)

    model_logs = [entry for entry in run.logs if entry["source"] == "inference"]
    assert [entry["message"] for entry in model_logs] == [
        "Boltz-2 prediction is still running (30s elapsed)"
    ]
    assert not any(entry["message"] == "raw model output" for entry in run.logs)
    terminal = capsys.readouterr().out
    assert "raw model output" in terminal
    assert "BSIM_PROGRESS:" in terminal


def test_run_logs_compute_warning_without_blocking_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    _set_model_parameters(lab, "counter", {"accelerator": "gpu"})
    client, session = _client(lab)
    _mark_runtime_metadata_ready(session)

    monkeypatch.setattr(server, "prepare_lab_package", lambda *_args, **_kwargs: _PreparedRuntime())

    created = client.post("/api/runs", json={}).json()
    assert created["ok"] is True
    run = session.get_run(created["data"]["run"]["id"])
    assert run.thread is not None
    run.thread.join(timeout=2)

    assert run.status == "completed"
    warning_logs = [
        entry
        for entry in run.logs
        if entry["level"] == "warning" and entry["source"] == "compute"
    ]
    assert len(warning_logs) == 1
    assert "Model 'counter' requests GPU acceleration" in warning_logs[0]["message"]
    messages = [entry["message"] for entry in run.logs]
    assert messages.index(warning_logs[0]["message"]) < messages.index("Preparing runtime/dependencies...")


def test_cancel_while_waiting_for_runtime_prepare_lock_does_not_prepare(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)
    _mark_runtime_metadata_ready(session)
    calls = 0

    def prepare(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _PreparedRuntime()

    monkeypatch.setattr(server, "prepare_lab_package", prepare)
    session._runtime_prepare_lock.acquire()
    try:
        created = client.post("/api/runs", json={}).json()
        run = session.get_run(created["data"]["run"]["id"])
        for _ in range(30):
            if any(entry["message"] == "Waiting for existing runtime preparation..." for entry in run.logs):
                break
            time.sleep(0.05)

        response = client.post(f"/api/runs/{run.id}/cancel")

        assert response.status_code == 200
        assert response.json()["data"]["run"]["status"] == "cancelling"
        assert run.thread is not None
        run.thread.join(timeout=2)
        assert run.status == "cancelled"
        assert calls == 0
    finally:
        session._runtime_prepare_lock.release()


def test_create_run_rejects_overlapping_active_run_and_allows_later_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)
    _mark_runtime_metadata_ready(session)
    started = threading.Event()
    release = threading.Event()

    def prepare(*_args, **_kwargs):
        started.set()
        release.wait(timeout=5)
        return _PreparedRuntime()

    monkeypatch.setattr(server, "prepare_lab_package", prepare)
    try:
        first = client.post("/api/runs", json={})
        assert first.status_code == 201
        first_run = session.get_run(first.json()["data"]["run"]["id"])
        assert started.wait(timeout=1)

        second = client.post("/api/runs", json={})

        assert second.status_code == 409
        assert "already running" in second.json()["error"]["message"]

        release.set()
        assert first_run.thread is not None
        first_run.thread.join(timeout=2)
        assert first_run.status == "completed"

        third = client.post("/api/runs", json={})
        assert third.status_code == 201
        third_run = session.get_run(third.json()["data"]["run"]["id"])
        assert third_run.thread is not None
        third_run.thread.join(timeout=2)
        assert third_run.status == "completed"
    finally:
        release.set()


def test_cancel_during_dependency_preparation_kills_tracked_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    session = LabServeSession(lab, install_deps=True)
    _mark_runtime_metadata_ready(session)
    client = TestClient(create_app(session))
    started = threading.Event()
    process_holder: dict[str, subprocess.Popen[str]] = {}

    def prepare(*_args, **kwargs):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        process_holder["process"] = process
        started.set()
        with kwargs["dependency_process_tracker"](process):
            while process.poll() is None:
                kwargs["cancel_checker"]()
                time.sleep(0.02)
        kwargs["cancel_checker"]()
        return _PreparedRuntime()

    monkeypatch.setattr(server, "prepare_lab_package", prepare)

    created = client.post("/api/runs", json={}).json()
    run = session.get_run(created["data"]["run"]["id"])
    assert started.wait(timeout=1)

    first_cancel = client.post(f"/api/runs/{run.id}/cancel")
    second_cancel = client.post(f"/api/runs/{run.id}/cancel")

    assert first_cancel.status_code == 200
    assert second_cancel.status_code == 200
    assert run.thread is not None
    run.thread.join(timeout=3)
    process = process_holder["process"]
    assert process.poll() is not None
    assert run.status == "cancelled"
    assert [entry["message"] for entry in run.logs].count("Cancellation requested") == 1


def test_cancel_during_settle_kills_world_module_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)
    _mark_runtime_metadata_ready(session)
    started = threading.Event()

    class BlockingSettleWorld(_RuntimeWorld):
        def __init__(self) -> None:
            super().__init__()
            self.process: subprocess.Popen[str] | None = None

        def settle(self, steps: int) -> None:
            self.process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            started.set()
            while self.process.poll() is None:
                time.sleep(0.02)

        def request_stop(self) -> None:
            if self.process is not None:
                server._terminate_process_tree(self.process)

    class PreparedBlockingRuntime(_PreparedRuntime):
        def __init__(self) -> None:
            self.world = BlockingSettleWorld()
            self.duration = 0.1
            self.communication_step = 0.01
            self.settle_steps = 1
            self.modules = []

    monkeypatch.setattr(
        server,
        "prepare_lab_package",
        lambda *_args, **_kwargs: PreparedBlockingRuntime(),
    )

    created = client.post("/api/runs", json={}).json()
    run = session.get_run(created["data"]["run"]["id"])
    assert started.wait(timeout=1)

    response = client.post(f"/api/runs/{run.id}/cancel")

    assert response.status_code == 200
    assert run.thread is not None
    run.thread.join(timeout=3)
    assert run.status == "cancelled"


def test_serve_lab_disables_uvicorn_access_logs(tmp_path: Path, monkeypatch) -> None:
    lab = _write_lab(tmp_path / "lab")
    configs: list[dict[str, object]] = []

    class FakeConfig:
        def __init__(self, *_args, **kwargs) -> None:
            configs.append(kwargs)

    class FakeServer:
        def __init__(self, _config) -> None:
            return None

        def run(self, *, sockets) -> None:
            for sock in sockets:
                sock.close()

    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        types.SimpleNamespace(Config=FakeConfig, Server=FakeServer),
    )

    server.serve_lab(
        lab,
        host="127.0.0.1",
        port=0,
        open_browser=False,
        install_deps=False,
    )

    assert configs[0]["access_log"] is False


def test_serve_lab_treats_keyboard_interrupt_as_clean_shutdown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _write_lab(tmp_path / "lab")

    class FakeConfig:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

    class FakeServer:
        def __init__(self, _config) -> None:
            return None

        def run(self, *, sockets) -> None:
            for sock in sockets:
                sock.close()
            raise KeyboardInterrupt

    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        types.SimpleNamespace(Config=FakeConfig, Server=FakeServer),
    )

    server.serve_lab(
        lab,
        host="127.0.0.1",
        port=0,
        open_browser=False,
        install_deps=False,
    )


def test_run_overrides_map_world_inputs_to_alias_nested_shape() -> None:
    manifest = {
        "models": [{"alias": "accumulator"}],
        "io": {"inputs": [{"name": "seed", "maps_to": "accumulator.value"}]},
    }

    overlay = server._map_initial_inputs(
        manifest,
        {
            "seed": 4,
            "accumulator.extra": 2,
            "accumulator": {"other": 1},
        },
    )

    assert overlay == {"accumulator": {"value": 4, "extra": 2, "other": 1}}


def test_run_lifecycle_maps_world_inputs_and_returns_visuals(tmp_path: Path) -> None:
    lab = _write_lab(tmp_path / "lab")
    manifest = _safe_yaml_load((lab / "lab.yaml").read_bytes())
    manifest["io"] = {"inputs": [{"name": "seed", "maps_to": "accumulator.value"}], "outputs": []}
    manifest["wiring"] = []
    (lab / "lab.yaml").write_bytes(_safe_yaml_dump(manifest))
    client, session = _client(lab)

    created = client.post(
        "/api/runs",
        json={
            "parameters": {
                "initial_inputs": {"seed": 4},
                "per_model": {"counter": {"step": 2}},
            },
            "simulation_config": {"duration": 0.1, "settle_steps": 1},
        },
    ).json()
    assert created["ok"] is True
    run_id = created["data"]["run"]["id"]
    run = session.get_run(run_id)
    assert run.thread is not None
    run.thread.join(timeout=5)

    for _ in range(20):
        run_payload = client.get(f"/api/runs/{run_id}").json()["data"]["run"]
        status = run_payload["status"]
        if status != "running":
            break
        time.sleep(0.05)
    assert status == "completed"
    assert run_payload["progress"]["progress_pct"] == 100.0

    results = client.get(f"/api/runs/{run_id}/results").json()
    assert results["ok"] is True
    visuals = results["data"]["results"]["visuals"]
    assert visuals[0]["module"] == "accumulator"
    assert visuals[0]["visuals"][0]["render"] == "table"
    assert visuals[0]["visuals"][0]["data"]["rows"][0]["total"] == 4.0
    logs = client.get(f"/api/runs/{run_id}/logs").json()
    assert logs["data"]["logs"]
    assert any("progress" in entry["message"] for entry in logs["data"]["logs"])


def test_run_overrides_normalize_world_inputs_and_deep_merge() -> None:
    manifest = {
        "models": [{"alias": "accumulator"}],
        "io": {
            "inputs": [
                {"name": "seed", "maps_to": "accumulator.value"},
                {"name": "label", "maps_to": "accumulator.label"},
            ]
        },
        "runtime": {
            "initial_inputs": {
                "accumulator": {"value": 1, "other": 2},
            }
        },
    }

    server._apply_run_overrides(
        manifest,
        parameters={"initial_inputs": {"seed": 4, "label": "run"}},
        simulation_config={},
    )

    assert manifest["runtime"]["initial_inputs"] == {
        "accumulator": {"value": 4, "other": 2, "label": "run"}
    }


def test_run_overrides_preserve_dotted_alias_input_refs() -> None:
    manifest = {
        "models": [{"alias": "nested.counter"}],
        "runtime": {"initial_inputs": {}},
    }

    server._apply_run_overrides(
        manifest,
        parameters={"initial_inputs": {"nested.counter.value": 5}},
        simulation_config={},
    )

    assert manifest["runtime"]["initial_inputs"] == {
        "nested.counter": {"value": 5}
    }


def test_run_history_is_empty_without_persisted_runs(tmp_path: Path) -> None:
    client, _session = _client(_write_lab(tmp_path / "lab"))

    response = client.get("/api/runs")

    assert response.status_code == 200
    assert response.json()["data"]["runs"] == []


def test_completed_run_history_persists_across_session_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)
    _mark_runtime_metadata_ready(session)
    monkeypatch.setattr(
        server,
        "prepare_lab_package",
        lambda *_args, **_kwargs: _PreparedRuntime(),
    )

    created = client.post("/api/runs", json={}).json()
    run_id = created["data"]["run"]["id"]
    run = session.get_run(run_id)
    assert run.thread is not None
    run.thread.join(timeout=2)
    assert run.status == "completed"

    restarted = LabServeSession(lab, install_deps=False)
    restarted_client = TestClient(create_app(restarted))

    runs = restarted_client.get("/api/runs").json()["data"]["runs"]
    assert [item["id"] for item in runs] == [run_id]
    assert runs[0]["status"] == "completed"
    results = restarted_client.get(f"/api/runs/{run_id}/results").json()
    assert results["data"]["results"]["duration"] == 0.1
    logs = restarted_client.get(f"/api/runs/{run_id}/logs").json()["data"]["logs"]
    assert any(entry["message"] == "Run completed" for entry in logs)


def test_failed_run_history_persists_across_session_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)
    _mark_runtime_metadata_ready(session)

    def fail_prepare(*_args, **_kwargs):
        raise RuntimeError("dependency setup failed")

    monkeypatch.setattr(server, "prepare_lab_package", fail_prepare)

    created = client.post("/api/runs", json={}).json()
    run_id = created["data"]["run"]["id"]
    run = session.get_run(run_id)
    assert run.thread is not None
    run.thread.join(timeout=2)
    assert run.status == "failed"

    restarted = LabServeSession(lab, install_deps=False)
    restarted_client = TestClient(create_app(restarted))

    payload = restarted_client.get(f"/api/runs/{run_id}").json()["data"]["run"]
    assert payload["status"] == "failed"
    assert payload["error_message"] == "dependency setup failed"
    logs = restarted_client.get(f"/api/runs/{run_id}/logs").json()["data"]["logs"]
    assert any(entry["message"] == "dependency setup failed" for entry in logs)


def test_active_persisted_run_reloads_as_interrupted(tmp_path: Path) -> None:
    lab = _write_lab(tmp_path / "lab")
    session = LabServeSession(lab, install_deps=False)
    run = RunRecord(id="run-active", lab_id="lab", parameters=None, simulation_config=None)
    run.status = "running"
    run.started_at = "2026-06-05T00:00:00Z"
    run.add_log("info", "Run started")
    session._run_store.save_run(run)
    session._run_store.replace_logs(run)

    restarted = LabServeSession(lab, install_deps=False)
    restarted_client = TestClient(create_app(restarted))

    payload = restarted_client.get("/api/runs/run-active").json()["data"]["run"]
    assert payload["status"] == "interrupted"
    assert payload["error_message"] == server.RUN_INTERRUPTED_MESSAGE
    assert payload["completed_at"]
    logs = restarted_client.get("/api/runs/run-active/logs").json()["data"]["logs"]
    assert logs[-1]["message"] == server.RUN_INTERRUPTED_MESSAGE
    stored = json.loads(
        (lab / ".biosimulant" / "runs" / "run-active" / "run.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["run"]["status"] == "interrupted"


def test_malformed_persisted_run_history_is_ignored(tmp_path: Path) -> None:
    lab = _write_lab(tmp_path / "lab")
    bad_dir = lab / ".biosimulant" / "runs" / "bad-run"
    bad_dir.mkdir(parents=True)
    (bad_dir / "run.json").write_text("{not-json", encoding="utf-8")

    client = TestClient(create_app(LabServeSession(lab, install_deps=False)))

    assert client.get("/api/runs").json()["data"]["runs"] == []


def test_structure3d_run_artifact_persists_across_session_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lab = _write_lab(tmp_path / "lab")
    source_artifact = tmp_path / "complex.cif"
    source_artifact.write_bytes(b"data_complex\n")

    class StructureWorld(_RuntimeWorld):
        def collect_visuals(self) -> list[dict[str, object]]:
            return [
                {
                    "module": "visualisation",
                    "visuals": [
                        {
                            "render": "structure3d",
                            "data": {
                                "title": "Predicted Complex Structure",
                                "format": "mmcif",
                                "source": {
                                    "kind": "artifact",
                                    "artifact_id": "complex-artifact",
                                    "path": str(source_artifact),
                                },
                            },
                        }
                    ],
                }
            ]

    class PreparedStructureRuntime(_PreparedRuntime):
        world = StructureWorld()

    client, session = _client(lab)
    _mark_runtime_metadata_ready(session)
    monkeypatch.setattr(
        server,
        "prepare_lab_package",
        lambda *_args, **_kwargs: PreparedStructureRuntime(),
    )

    created = client.post("/api/runs", json={}).json()
    run_id = created["data"]["run"]["id"]
    run = session.get_run(run_id)
    assert run.thread is not None
    run.thread.join(timeout=2)
    assert run.status == "completed"
    source_artifact.unlink()

    restarted = LabServeSession(lab, install_deps=False)
    restarted_client = TestClient(create_app(restarted))
    results = restarted_client.get(f"/api/runs/{run_id}/results").json()

    source = results["data"]["results"]["visuals"][0]["visuals"][0]["data"]["source"]
    assert source["artifact_id"] == "complex-artifact"
    assert source["url"] == f"/api/runs/{run_id}/artifacts/complex-artifact"
    assert ".biosimulant/runs" in source["path"].replace("\\", "/")
    response = restarted_client.get(source["url"])
    assert response.status_code == 200
    assert response.text == "data_complex\n"


def test_active_run_results_remain_compatible_empty_payload(tmp_path: Path) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)
    run = RunRecord(id="run-active", lab_id="lab", parameters=None, simulation_config=None)
    run.status = "running"
    session._runs[run.id] = run

    response = client.get(f"/api/runs/{run.id}/results")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "data": {"results": {}}, "error": None}


def test_structure3d_visuals_register_artifact_url_and_serve_file(tmp_path: Path) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)
    artifact = tmp_path / "complex.cif"
    artifact.write_bytes(b"data_complex\n")
    run = RunRecord(id="run-structure", lab_id="lab", parameters=None, simulation_config=None)
    run.status = "completed"
    run.results = {
        "visuals": server._sanitize_visuals(
            [
                {
                    "module": "visualisation",
                    "visuals": [
                        {
                            "render": "structure3d",
                            "data": {
                                "title": "Predicted Complex Structure",
                                "format": "mmcif",
                                "source": {
                                    "kind": "artifact",
                                    "artifact_id": "complex-artifact",
                                    "path": str(artifact),
                                },
                            },
                        }
                    ],
                }
            ],
            run_id=run.id,
            artifacts=run.artifacts,
        )
    }
    session._runs[run.id] = run

    results = client.get(f"/api/runs/{run.id}/results").json()

    source = results["data"]["results"]["visuals"][0]["visuals"][0]["data"]["source"]
    assert source["artifact_id"] == "complex-artifact"
    assert source["path"] == str(artifact.resolve())
    assert source["url"] == f"/api/runs/{run.id}/artifacts/complex-artifact"
    response = client.get(source["url"])
    assert response.status_code == 200
    assert response.text == "data_complex\n"


def test_run_artifact_route_rejects_unknown_run_and_artifact(tmp_path: Path) -> None:
    lab = _write_lab(tmp_path / "lab")
    client, session = _client(lab)
    run = RunRecord(id="run-empty", lab_id="lab", parameters=None, simulation_config=None)
    run.status = "completed"
    session._runs[run.id] = run

    missing_artifact = client.get(f"/api/runs/{run.id}/artifacts/missing")
    missing_run = client.get("/api/runs/missing/artifacts/anything")

    assert missing_artifact.status_code == 400
    assert missing_artifact.json()["error"]["message"] == "Artifact not found for run run-empty: missing"
    assert missing_run.status_code == 400
    assert missing_run.json()["error"]["message"] == "Run not found: missing"


def test_api_error_envelope_for_missing_run(tmp_path: Path) -> None:
    client, _session = _client(_write_lab(tmp_path / "lab"))

    response = client.get("/api/runs/missing")

    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"]["message"] == "Run not found: missing"
