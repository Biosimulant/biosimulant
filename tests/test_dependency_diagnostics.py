from __future__ import annotations

from pathlib import Path

import pytest

from biosim import dependency_runtime as runtime
from biosim import managed_runtime
from biosim._errors import PackageError


@pytest.fixture
def failed_validation(monkeypatch):
    """Exercise failed environment preparation without an external installer."""
    monkeypatch.delenv("BIOSIMULANT_TELLURIUM_WHEELHOUSE", raising=False)
    monkeypatch.setattr(managed_runtime, "_uv_command", lambda: ["test-uv"])
    monkeypatch.setattr(managed_runtime, "_local_source_spec", lambda: "biosimulant==0.0.37")
    roots = []
    diagnostic_calls = []
    outputs = {
        "installer": "uv 0.9.4\n",
        "interpreter": "3.12.3\nLinux x86_64\n",
        "resolved packages": "padding" * 2000 + "\n"
        "antimony @ https://user:password@example.test/wheel.whl?token=private\n"
        "tellurium==2.2.11.2\n",
        "wheel tags": "antimony==3.2.0 Tag: cp313-cp313-linux_x86_64\n",
    }

    def run(command, **callbacks):
        if command[1] == "venv":
            roots.append(Path(command[-1]))
            assert roots[-1].exists()
            return ""
        if command[1:3] == ["pip", "install"]:
            return ""
        if command[1:3] == ["pip", "check"]:
            raise PackageError("antimony was built for a different platform")
        if command[1:] == ["--version"]:
            label = "installer"
        elif command[1:3] == ["pip", "freeze"]:
            label = "resolved packages"
        elif command[1] == "-c":
            label = "wheel tags" if "importlib.metadata" in command[2] else "interpreter"
        else:
            pytest.fail(f"Unexpected dependency command: {command}")
        diagnostic_calls.append((label, callbacks))
        output = outputs[label]
        if isinstance(output, Exception):
            raise output
        if callbacks.get("dependency_logger"):
            callbacks["dependency_logger"](output)
        return output

    monkeypatch.setattr(runtime, "run_dependency_command", run)
    return roots, diagnostic_calls, outputs


def test_validation_failure_retains_bounded_redacted_evidence_and_cleans_up(failed_validation):
    roots, calls, _ = failed_validation
    streamed = []
    with pytest.raises(PackageError) as caught:
        with runtime.isolated_environment(
            ["tellurium==2.2.11.2"], dependency_logger=streamed.append
        ):
            pytest.fail("An invalid environment must never reach execution")
    message = str(caught.value)
    for evidence in (
        "validation failed after installation",
        "different platform",
        "uv 0.9.4",
        "3.12.3",
        "Linux x86_64",
        "tellurium==2.2.11.2",
        "cp313-cp313-linux_x86_64",
    ):
        assert evidence in message
    assert "password" not in message and "private" not in message
    assert "example.test" in message
    assert len(message) < 25000
    assert streamed == []
    assert len(calls) == 4 and all(call[1]["dependency_logger"] is None for call in calls)
    assert roots and all(not root.exists() for root in roots)


@pytest.mark.parametrize(
    ("url_prefix", "url_suffix"),
    [
        ("https://user:credential-prefix-", "@example.test/wheel.whl\n"),
        ("https://example.test/wheel.whl?token=credential-prefix-", "\n"),
    ],
)
def test_diagnostic_truncation_cannot_expose_partial_credentials(
    failed_validation, url_prefix, url_suffix
):
    roots, _, outputs = failed_validation
    secret = "boundary-secret"
    final_package = "\ntellurium==2.2.11.2\n"
    retained = secret + url_suffix
    retained += "x" * (6000 - len(retained) - len(final_package)) + final_package
    outputs["resolved packages"] = "padding\n" * 1000 + url_prefix + retained
    assert outputs["resolved packages"][-6000:].startswith(secret)

    with pytest.raises(PackageError) as caught:
        with runtime.isolated_environment(["tellurium==2.2.11.2"]):
            pytest.fail("An invalid environment must never reach execution")
    message = str(caught.value)
    assert secret not in message
    assert "different platform" in message and "tellurium==2.2.11.2" in message
    assert len(message) < 25000
    assert roots and all(not root.exists() for root in roots)


def test_unavailable_diagnostic_does_not_hide_validation_failure(failed_validation):
    roots, _, outputs = failed_validation
    outputs["installer"] = PackageError("installer introspection failed")
    with pytest.raises(PackageError) as caught:
        with runtime.isolated_environment(["tellurium==2.2.11.2"]):
            pytest.fail("An invalid environment must never reach execution")
    message = str(caught.value)
    assert "different platform" in message and "installer: unavailable" in message
    assert "tellurium==2.2.11.2" in message and "cp313-cp313-linux_x86_64" in message
    assert all(not root.exists() for root in roots)


def test_diagnostic_collection_preserves_cancellation_and_process_tracking(failed_validation):
    roots, calls, outputs = failed_validation
    cancellation = RuntimeError("cancelled during diagnostic collection")
    outputs["resolved packages"] = cancellation

    def cancel():
        return None

    def track(process):
        raise AssertionError("The mocked runner must not start subprocesses")

    with pytest.raises(RuntimeError) as caught:
        with runtime.isolated_environment(
            ["tellurium==2.2.11.2"],
            cancel_checker=cancel,
            process_tracker=track,
        ):
            pytest.fail("An invalid environment must never reach execution")
    assert caught.value is cancellation
    assert all(callbacks["cancel_checker"] is cancel for _, callbacks in calls)
    assert all(callbacks["process_tracker"] is track for _, callbacks in calls)
    assert all(not root.exists() for root in roots)
