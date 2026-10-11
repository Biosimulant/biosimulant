from __future__ import annotations

import importlib.metadata as metadata
import json
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from biosim import dependency_runtime as runtime
from biosim import managed_runtime, pack
from biosim.pack import PackageError
from tests.test_pack import _write_counter_model


def manifest(*packages):
    return {"runtime": {"dependencies": {"packages": list(packages)}}}


@pytest.mark.parametrize("invalid", ["numpy>=1", "numpy==1.*", "numpy", "numpy @ https://example.com/n.whl", 42])
def test_exact_pins_required(invalid):
    with pytest.raises(PackageError, match="dependency|pin"):
        runtime.collect_requirements([("cohort", manifest(invalid))])


def test_normalized_conflicts_name_both_models():
    with pytest.raises(PackageError, match=r"Conflicting dependency my-package.*cohort.*qa"):
        runtime.collect_requirements([
            ("cohort", manifest("my_package==1.0")),
            ("qa", manifest("My.Package==2.0")),
        ])


def test_merge_extras_and_ignore_inactive_markers():
    assert runtime.collect_requirements([
        ("cohort", manifest("demo[a]==1.0", "numpy==1.0; python_version<'2'")),
        ("qa", manifest("demo[b]==1.0")),
    ]) == ["demo[a,b]==1.0"]


def test_patch_version_marker_uses_actual_python():
    pin = f"demo==1.0; python_full_version=='{sys.version.split()[0]}'"
    assert runtime.collect_requirements([("model", manifest(pin))], python_version=f"{sys.version_info.major}.{sys.version_info.minor}") == ["demo==1.0"]


def test_foreign_python_markers_are_left_for_target_resolver():
    pin = "demo==1.0; python_full_version=='3.11.9'"
    assert runtime.collect_requirements([("model", manifest(pin))], python_version="3.11" if sys.version_info.minor != 11 else "3.10") == [str(runtime.Requirement(pin))]


def test_missing_or_wrong_environment_never_installs(monkeypatch):
    monkeypatch.setattr(runtime.metadata, "version", lambda _name: "2.0")
    monkeypatch.setattr(runtime.metadata, "requires", lambda _name: [])
    monkeypatch.setattr(runtime, "_loaded_version_conflicts", lambda _names: [])
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("must not install"))
    with pytest.raises(PackageError, match="installed 2.0"):
        runtime.require_current_environment(["demo==1.0"])


def test_transitive_extra_requirements_checked(monkeypatch):
    monkeypatch.setattr(runtime.metadata, "version", lambda name: "1.0" if name == "demo" else "2.0")
    monkeypatch.setattr(runtime.metadata, "requires", lambda name: ["other==1.0; extra=='plot'"] if name == "demo" else [])
    monkeypatch.setattr(runtime, "_loaded_version_conflicts", lambda _names: [])
    runtime.require_current_environment(["demo==1.0"])
    with pytest.raises(PackageError, match="other.*installed 2.0"):
        runtime.require_current_environment(["demo[plot]==1.0"])


def test_loaded_metadata_mismatch_caught(monkeypatch):
    monkeypatch.setattr(runtime.metadata, "packages_distributions", lambda: {"demo": ["demo"]})
    monkeypatch.setattr(runtime.metadata, "version", lambda _name: "1.0")
    monkeypatch.setattr(runtime.metadata, "requires", lambda _name: [])
    monkeypatch.setitem(sys.modules, "demo", SimpleNamespace(__version__="2.0"))
    with pytest.raises(PackageError, match="demo loaded 2.0, installed 1.0"):
        runtime.require_current_environment(["demo==1.0"])


def test_unrelated_loaded_package_does_not_block_lab(monkeypatch):
    monkeypatch.setattr(runtime.metadata, "packages_distributions", lambda: {"unrelated": ["unrelated"]})
    monkeypatch.setitem(sys.modules, "unrelated", SimpleNamespace(__version__="2.0"))
    runtime.require_current_environment([])


def test_dependency_failure_has_logs():
    with pytest.raises(PackageError, match="resolver conflict"):
        runtime.run_dependency_command([sys.executable, "-c", "print('resolver conflict');raise SystemExit(1)"])


def test_dependency_output_streams_and_tracks_process():
    from contextlib import contextmanager
    events = []

    @contextmanager
    def track(process):
        events.append(process.pid)
        yield
        events.append(process.returncode)

    assert runtime.run_dependency_command(
        [sys.executable, "-c", "print('first');print('second')"],
        dependency_logger=events.append, process_tracker=track,
    ) == "first\nsecond\n"
    assert events[1:3] == ["first", "second"]
    assert events[-1] == 0


def test_cancellation_interrupts_silent_install():
    start = time.monotonic()

    def cancel():
        if time.monotonic() - start > 0.3:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        runtime.run_dependency_command([sys.executable, "-c", "import time;time.sleep(60)"], cancel_checker=cancel)
    assert time.monotonic() - start < 5


def test_child_cancellation_interrupts_and_reaps(monkeypatch, tmp_path):
    original = subprocess.Popen
    children = []

    def start(_command, **kwargs):
        child = original([sys.executable, "-c", "import time;time.sleep(60)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(managed_runtime.subprocess, "Popen", start)
    start_time = time.monotonic()

    def cancel():
        if time.monotonic() - start_time > 0.3:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        managed_runtime.run_child_package(Path(sys.executable), tmp_path / "lab", install_deps=False, cancel_checker=cancel)
    assert all(child.poll() is not None for child in children)


def make_lab(root: Path, pins: list[str], *, child=False):
    model = _write_counter_model(root / "models" / "counter")
    data = yaml.safe_load((model / "model.yaml").read_text())
    data.update(manifest(*pins))
    (model / "model.yaml").write_text(yaml.safe_dump(data))
    (root / "lab.yaml").write_text(yaml.safe_dump({
        "schema_version": "2.0", "package": "env-test", "version": "0.1.0",
        "models": [{"alias": "counter", "path": "models/counter"}], "wiring": [],
        "runtime": {"duration": 0.1, "communication_step": 0.1},
    }))
    if child:
        nested = root / "nested"
        make_lab(nested, ["numpy==2.0.2"])
        data = yaml.safe_load((root / "lab.yaml").read_text())
        data["children"] = [{"alias": "nested", "path": "nested"}]
        (root / "lab.yaml").write_text(yaml.safe_dump(data))
    return pack.build_package(root, output_path=root.parent / (root.name + ".bsilab"))


def test_nested_conflict_rejected_before_install_or_import(tmp_path, monkeypatch):
    archive = make_lab(tmp_path / "lab", ["numpy==1.26.4"], child=True)
    monkeypatch.setattr(pack, "_instantiate_model_from_dir", lambda *_a, **_k: pytest.fail("no model imports"))
    monkeypatch.setattr(runtime, "isolated_environment", lambda *_a, **_k: pytest.fail("no install"))
    with pytest.raises(PackageError, match=r"counter.*nested/models/counter"):
        pack.run_package(archive)


def test_live_api_checks_without_installing(tmp_path, monkeypatch):
    archive = make_lab(tmp_path / "lab", ["nonexistent-biosimulant-test-package==1.0"])
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("no install"))
    with pytest.raises(PackageError, match="is not installed"):
        pack.prepare_lab_package(archive)


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    if sys.version_info[:2] != (3, 12):
        pytest.skip("real dependency fixtures cover NumPy 1.26–2.5 on Python 3.12")
    destination = tmp_path_factory.mktemp("dependency-wheel")
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(destination)], check=True, capture_output=True)
    return next(destination.glob("biosimulant-*.whl"))


@pytest.mark.integration
@pytest.mark.parametrize("version", ["1.26.4", "2.0.2", "2.5.2"])
def test_real_numpy_run_isolated_from_imported_parent(tmp_path, monkeypatch, built_wheel, version):
    import numpy
    loaded_version, loaded_path = numpy.__version__, numpy.__file__
    installed = metadata.version("numpy")
    archive = make_lab(tmp_path / "lab", [f"numpy=={version}"])
    monkeypatch.setenv("BIOSIMULANT_MANAGED_SOURCE_SPEC", str(built_wheel))
    monkeypatch.setenv("PYTHONPATH", "/invalid/parent/path")
    result = pack.run_package(archive)
    assert result["environment"]["packages"]["numpy"] == version
    imported = result["environment"]["imported"]["numpy"]
    assert imported["version"] == version
    assert "biosimulant-lab-env-" in imported["path"]
    assert numpy.__version__ == loaded_version and numpy.__file__ == loaded_path
    assert metadata.version("numpy") == installed
    assert result["outputs"]["counter"]["count"]["value"] == 1
    assert not Path(result["environment"]["imported"]["numpy"]["path"]).exists()


@pytest.mark.integration
def test_real_transitive_conflict_and_cleanup(monkeypatch, built_wheel):
    from contextlib import contextmanager

    original = runtime.tempfile.TemporaryDirectory
    directories = []

    @contextmanager
    def temporary_directory(*args, **kwargs):
        with original(*args, **kwargs) as directory:
            directories.append(Path(directory))
            yield directory

    monkeypatch.setattr(runtime.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setenv("BIOSIMULANT_MANAGED_SOURCE_SPEC", str(built_wheel))
    with pytest.raises(PackageError, match="lifelines|numpy"):
        with runtime.isolated_environment(["numpy==2.1.3", "lifelines==0.29.0"]):
            pytest.fail("conflicting graph must not execute")
    assert directories and all(not directory.exists() for directory in directories)


@pytest.mark.integration
def test_failed_model_removes_environment(tmp_path, monkeypatch, built_wheel):
    from contextlib import contextmanager

    root = tmp_path / "lab"
    make_lab(root, ["numpy==2.0.2"])
    (root / "models/counter/src/counter.py").write_text("raise RuntimeError('deliberate model failure')\n")
    archive = pack.build_package(root, output_path=tmp_path / "failed.bsilab")
    monkeypatch.setenv("BIOSIMULANT_MANAGED_SOURCE_SPEC", str(built_wheel))
    environments = []

    @contextmanager
    def track(process):
        executable = Path(process.args[0])
        if "biosimulant-lab-env-" in str(executable):
            environments.append(executable.parent.parent)
        yield

    with pytest.raises(PackageError, match="deliberate model failure"):
        pack.run_package(archive, dependency_process_tracker=track)
    assert environments and all(not environment.exists() for environment in environments)


@pytest.mark.integration
def test_concurrent_environments_do_not_share_packages(tmp_path, monkeypatch, built_wheel):
    monkeypatch.setenv("BIOSIMULANT_MANAGED_SOURCE_SPEC", str(built_wheel))
    archives = [make_lab(tmp_path / name, [f"numpy=={version}"]) for name, version in [("one", "1.26.4"), ("two", "2.0.2")]]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(pack.run_package, archives))
    assert [r["environment"]["packages"]["numpy"] for r in results] == ["1.26.4", "2.0.2"]
    assert results[0]["environment"]["imported"]["numpy"]["path"] != results[1]["environment"]["imported"]["numpy"]["path"]


@pytest.mark.integration
@pytest.mark.skipif(os.name != "posix", reason="uses process-group SIGINT for server shutdown")
def test_cli_serve_prepares_environment_before_model_imports(tmp_path, built_wheel):
    import httpx

    root = tmp_path / "lab"
    make_lab(root, ["numpy==2.0.2"])
    model = root / "models/counter/src/counter.py"
    model.write_text("import numpy\nassert numpy.__version__ == '2.0.2'\n" + model.read_text())
    with socket.socket() as socket_:
        socket_.bind(("127.0.0.1", 0))
        port = socket_.getsockname()[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    env["BIOSIMULANT_MANAGED_SOURCE_SPEC"] = str(built_wheel)
    env.pop(managed_runtime.BIOSIM_MANAGED_RUNTIME_CHILD_ENV, None)
    with (tmp_path / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "biosim", "labs", "serve", str(root), "--no-open", "--port", str(port)],
            env=env, stdout=log, stderr=log, start_new_session=True,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2) as client:
                deadline = time.monotonic() + 60
                while True:
                    try:
                        response = client.get("/api/lab")
                        if response.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert process.poll() is None and time.monotonic() < deadline
                    time.sleep(0.1)
                response = client.post("/api/runs", json={})
                response.raise_for_status()
                run_id = response.json()["data"]["run"]["id"]
                while True:
                    run = client.get(f"/api/runs/{run_id}").json()["data"]["run"]
                    if run["status"] not in {"queued", "running"}:
                        break
                    assert time.monotonic() < deadline
                    time.sleep(0.1)
                assert run["status"] == "completed", run
                result = client.get(f"/api/runs/{run_id}/results").json()["data"]["results"]
                assert result["outputs"]["counter"]["count"]["value"] == 1
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            log.seek(0)
            assert "Incompatible Python environment" not in log.read()
