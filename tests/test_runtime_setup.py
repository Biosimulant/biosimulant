from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
import yaml

from biosim import runtime_setup as runner
from biosim.runtime_setup_contract import SetupError, parse_setup, setup_plan


def files(command=None, **options):
    return {
        "lab.yaml": yaml.safe_dump(
            {
                "models": [],
                "runtime": {
                    "setup": {
                        "command": command or ["python", "-c", "print('prepared')"],
                        **options,
                    }
                },
            }
        ).encode()
    }


def plan(source):
    return setup_plan(source, load_yaml=yaml.safe_load)


def test_interpreter_identity_normalizes_parent_alias_but_keeps_venv(tmp_path):
    directory = tmp_path / "environment"
    (directory / "bin").mkdir(parents=True)
    interpreter = directory / "bin" / "python"
    alias = tmp_path / "alias"
    try:
        interpreter.symlink_to(sys.executable)
        alias.symlink_to(directory, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this host")
    setup = plan(files())
    expected = runner.identity(setup, interpreter)
    assert runner.identity(setup, alias / "bin" / "python") == expected
    assert expected != runner.identity(setup, sys.executable)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "echo hello",
        {},
        {"command": []},
        {"command": "python script.py"},
        {"command": [1]},
        {"command": ["python", "\0"]},
        {"command": ["python"], "timeout_seconds": True},
        {"command": ["python"], "timeout_seconds": 3601},
        {"command": ["../outside"]},
        {"command": ["python"], "shell": True},
    ],
)
def test_invalid_setup(value):
    with pytest.raises(SetupError):
        parse_setup({"setup": value})


def test_setup_contract_defaults_and_explicit_shell():
    assert parse_setup({}) is None
    assert (
        parse_setup({"setup": {"command": ["python", "-c", ""]}})["timeout_seconds"]
        == 900
    )
    assert (
        parse_setup({"setup": {"command": ["powershell", "-File", "prepare.ps1"]}})[
            "command"
        ][0]
        == "powershell"
    )


def test_identity_binds_helpers_but_not_run_overrides_or_saved_lock():
    source = {**files(), "helper.py": b"version=1"}
    original = plan(source)
    source["helper.py"] = b"version=2"
    assert plan(source)["setup_sha256"] != original["setup_sha256"]
    source["helper.py"] = b"version=1"
    lab = yaml.safe_load(source["lab.yaml"])
    lab["runtime"].update(duration=20, initial_inputs={"x": 5})
    source["lab.yaml"] = yaml.safe_dump(lab)
    source["environment.lock"] = "new generated lock"
    source[".env"] = "secret"
    assert plan(source) == original


def test_child_setups_are_dependency_first_and_shared_path_once():
    source = files()
    lab = yaml.safe_load(source["lab.yaml"])
    lab["children"] = [
        {"path": "child", "alias": "one"},
        {"path": "child", "alias": "two"},
    ]
    source["lab.yaml"] = yaml.safe_dump(lab)
    source["child/lab.yaml"] = files()["lab.yaml"]
    assert [step["root"] for step in plan(source)["steps"]] == ["child", ""]


def test_unmaterialized_children_never_silently_skip_setup():
    source = files()
    lab = yaml.safe_load(source["lab.yaml"])
    lab["children"] = [{"package": "org/lab", "version": "1.0.0"}]
    source["lab.yaml"] = yaml.safe_dump(lab)
    with pytest.raises(SetupError, match="Vendor"):
        plan(source)


def test_execution_uses_selected_python_cwd_prefix_and_logs(tmp_path, monkeypatch):
    source = files(
        [
            "python",
            "-c",
            "import os,pathlib,sys;print(os.getcwd());print(sys.executable);pathlib.Path(os.environ['BIOSIM_ENV_PREFIX'],'installed').write_text('ok')",
        ]
    )
    for name, content in source.items():
        (tmp_path / name).write_bytes(content)
    prefix = tmp_path.parent / (tmp_path.name + " prefix ü")
    logs = []
    record = runner.execute_setup(
        plan(source), tmp_path, python=sys.executable, prefix=prefix, logger=logs.append
    )
    assert (prefix / "installed").read_text() == "ok"
    assert str(tmp_path) in "".join(logs)
    assert record["steps"][0]["exit_code"] == 0
    monkeypatch.setenv(runner.PREPARED_ENV, str(prefix / "setup-record.json"))
    runner.require_prepared(plan(source))
    source["helper"] = b"changed"
    with pytest.raises(SetupError, match="not prepared"):
        runner.require_prepared(plan(source))


@pytest.mark.parametrize(
    "command,code",
    [
        (
            ["python", "-c", "print('specific failure');raise SystemExit(7)"],
            "runtime_setup_failed",
        ),
        (["biosimulant-missing-executable"], "runtime_setup_executable_unavailable"),
        (["python", "-c", "import time;time.sleep(60)"], "runtime_setup_timeout"),
    ],
)
def test_failure_leaves_no_success_record(tmp_path, command, code):
    source = files(command, timeout_seconds=1)
    prefix = tmp_path / "prefix"
    with pytest.raises(SetupError) as caught:
        runner.execute_setup(
            plan(source), tmp_path, python=sys.executable, prefix=prefix
        )
    assert caught.value.code == code
    assert not (prefix / "setup-record.json").exists()


def test_cancellation_reaps_process_tree(tmp_path):
    child_pid = tmp_path / "child.pid"
    code = (
        "import subprocess,sys,pathlib,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid));time.sleep(60)"
    )
    source = files(["python", "-c", code])
    started = time.monotonic()

    def cancel():
        if child_pid.exists():
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        runner.execute_setup(
            plan(source),
            tmp_path,
            python=sys.executable,
            prefix=tmp_path / "prefix",
            cancel_checker=cancel,
        )
    assert time.monotonic() - started < 8
    pid = int(child_pid.read_text())
    if os.name == "posix":
        # A reparented child can briefly be a zombie; neither state runs code.
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        )
        assert (
            result.returncode
            or not result.stdout.strip()
            or result.stdout.strip().startswith("Z")
        )


def test_validation_and_packaging_never_execute_setup(tmp_path, monkeypatch):
    from tests.test_dependency_runtime import make_lab
    from biosim.pack import build_package, validate_package, validate_lab_source

    make_lab(tmp_path, [])
    lab = yaml.safe_load((tmp_path / "lab.yaml").read_text())
    lab["runtime"]["setup"] = {
        "command": ["python", "-c", "raise RuntimeError('must not run')"]
    }
    (tmp_path / "lab.yaml").write_text(yaml.safe_dump(lab))
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("setup during validation")
    )
    assert validate_lab_source(tmp_path).valid
    assert validate_package(build_package(tmp_path)).valid


def test_prepared_flag_without_a_record_is_insufficient(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOSIM_MANAGED_RUNTIME_CHILD", "1")
    monkeypatch.delenv(runner.PREPARED_ENV, raising=False)
    with pytest.raises(SetupError):
        runner.require_prepared(plan(files()))


def test_package_setup_runs_once_before_model_import_in_child(tmp_path):
    from tests.test_dependency_runtime import make_lab
    from biosim.pack import build_package, run_package

    make_lab(tmp_path, [])
    counter = tmp_path.parent / (tmp_path.name + "-setup-count")
    lab = yaml.safe_load((tmp_path / "lab.yaml").read_text())
    lab["runtime"]["setup"] = {
        "command": [
            "python",
            "-c",
            "import os,pathlib;"
            f"p=pathlib.Path({str(counter)!r});p.write_text(p.read_text()+'x' if p.exists() else 'x');"
            "pathlib.Path(os.environ['BIOSIM_ENV_PREFIX'],'prepared-tool').write_text('ready')",
        ]
    }
    (tmp_path / "lab.yaml").write_text(yaml.safe_dump(lab))
    module = tmp_path / "models/counter/src/counter.py"
    module.write_text(
        "import os,pathlib\nassert pathlib.Path(os.environ['BIOSIM_ENV_PREFIX'],'prepared-tool').read_text() == 'ready'\n"
        + module.read_text()
    )
    result = run_package(build_package(tmp_path))
    assert counter.read_text() == "x"
    assert result["outputs"]["counter"]["count"]["value"] > 0
    assert result["environment"]["setup"]["status"] == "succeeded"


def test_local_serve_defers_setup_until_run_and_preserves_results(tmp_path):
    from tests.test_dependency_runtime import make_lab
    from biosim.labs_serve.server import LabServeSession

    make_lab(tmp_path, [])
    count = tmp_path.parent / (tmp_path.name + "-serve-count")
    lab = yaml.safe_load((tmp_path / "lab.yaml").read_text())
    lab["runtime"]["setup"] = {
        "command": [
            "python",
            "-c",
            f"from pathlib import Path;Path({str(count)!r}).write_text('ran')",
        ]
    }
    (tmp_path / "lab.yaml").write_text(yaml.safe_dump(lab))
    session = LabServeSession(tmp_path, install_deps=True)
    session.lab_payload()
    session._runtime_metadata_thread.join(timeout=5)
    assert not count.exists()
    run = session.create_run({"simulation_config": {"duration": 0.2}})
    run.thread.join(timeout=90)
    assert not run.thread.is_alive()
    assert run.status == "completed", run.error_message
    assert count.read_text() == "ran"
    assert run.results["outputs"]["counter"]["count"]["value"] > 0


def test_local_environment_recipe_is_never_silently_ignored(monkeypatch):
    monkeypatch.delenv(runner.PREPARED_ENV, raising=False)
    monkeypatch.delenv("BIOSIM_REMOTE_EXECUTION", raising=False)
    with pytest.raises(SetupError) as caught:
        runner.require_supported_environment(
            {"runtime": {"environment": {"base": "image@sha256:..."}}}
        )
    assert caught.value.code == "runtime_environment_requires_cloud"


def test_child_setup_cannot_silently_change_the_shared_python():
    source = files()
    lab = yaml.safe_load(source["lab.yaml"])
    lab["runtime"]["python_version"] = "3.12"
    lab["children"] = [{"path": "child", "alias": "child"}]
    source["lab.yaml"] = yaml.safe_dump(lab)
    source["child/lab.yaml"] = yaml.safe_dump({"models": [], "runtime": {"python_version": "3.11"}})
    with pytest.raises(SetupError) as error:
        plan(source)
    assert error.value.code == "runtime_setup_environment_conflict"
