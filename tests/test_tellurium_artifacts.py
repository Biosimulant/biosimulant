from __future__ import annotations

import hashlib
import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

from biosim import dependency_runtime as runtime
from biosim import tellurium_artifacts as artifacts
from biosim._errors import PackageError


def test_candidate_does_not_intercept_other_dependencies(tmp_path):
    assert artifacts.artifact_requirements(["numpy==2.2.6"], tmp_path, python_version="3.12") == []
    assert not artifacts.requests_candidate(["tellurium==2.2.11"])


@pytest.mark.parametrize(
    "marker", ["python_version < '3'", "sys_platform == 'darwin'", "platform_machine == 'aarch64'"]
)
def test_inactive_target_marker_never_selects_artifacts(tmp_path, marker):
    assert (
        artifacts.artifact_requirements(
            [f"tellurium==2.2.11.2; {marker}"], tmp_path, python_version="3.12"
        )
        == []
    )


def test_patch_marker_uses_target_not_host(tmp_path):
    assert (
        artifacts.artifact_requirements(
            ["tellurium==2.2.11.2; python_full_version == '3.11.9'"],
            tmp_path,
            python_version="3.12",
            python_full_version="3.12.3",
        )
        == []
    )


@pytest.mark.parametrize(
    "target",
    [
        {"python_version": "3.13"},
        {"python_version": "3.12", "machine": "aarch64"},
        {"python_version": "3.12", "sys_platform": "darwin"},
        {"python_version": "3.12", "implementation": "pypy"},
    ],
)
def test_unqualified_target_fails_closed(tmp_path, target):
    with pytest.raises(PackageError, match="only CPython"):
        artifacts.artifact_requirements(["tellurium==2.2.11.2"], tmp_path, **target)


def test_os_release_marker_cannot_use_host_metadata(tmp_path):
    with pytest.raises(PackageError, match="OS-release markers"):
        artifacts.artifact_requirements(
            ["tellurium==2.2.11.2; platform_release == 'host-value'"],
            tmp_path,
            python_version="3.12",
        )


def test_missing_and_tampered_artifacts_are_blocked(tmp_path):
    requirements = ["tellurium==2.2.11.2"]
    with pytest.raises(PackageError, match="Missing verified"):
        artifacts.artifact_requirements(requirements, tmp_path, python_version="3.12")
    filename = artifacts._ARTIFACTS["3.12"][0][1]
    (tmp_path / filename).write_bytes(b"untrusted replacement")
    with pytest.raises(PackageError, match="checksum mismatch"):
        artifacts.artifact_requirements(requirements, tmp_path, python_version="3.12")


def test_artifact_resolution_uses_trusted_content_not_directory_order(tmp_path, monkeypatch):
    trusted = b"trusted test artifact"
    digest = hashlib.sha256(trusted).hexdigest()
    filename = "antimony-3.2.0-1bsim1-cp312-none-linux_x86_64.whl"
    monkeypatch.setitem(artifacts._ARTIFACTS, "3.12", (("antimony", filename, digest),))
    (tmp_path / filename).write_bytes(trusted)
    (tmp_path / "antimony-3.2.0-9untrusted-cp312-none-linux_x86_64.whl").write_bytes(b"wrong")
    refs = artifacts.artifact_requirements(["tellurium==2.2.11.2"], tmp_path, python_version="3.12")
    assert refs == [f"antimony @ {(tmp_path / filename).as_uri()}#sha256={digest}"]


def test_builder_preserves_payloads_modes_and_deterministic_identity():
    script = Path(__file__).resolve().parents[1] / "scripts/prepare_tellurium_wheelhouse.py"
    spec = importlib.util.spec_from_file_location("build_tellurium_test", script)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    payloads = {
        "demo/demo.py": b"# wrapper\n",
        "demo/libdemo.so": b"native payload",
        "demo-1.0.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp39-cp39-linux_x86_64\n"
        ),
        "demo-1.0.dist-info/METADATA": b"Name: demo\nVersion: 1.0\n",
        "demo-1.0.dist-info/RECORD": b"original record",
    }
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        for path, data in payloads.items():
            info = zipfile.ZipInfo(path)
            info.external_attr = 0o100755 << 16
            archive.writestr(info, data)
    first = builder.corrected_wheel(source.getvalue(), name="demo", version="1.0", python="3.12")
    assert first == builder.corrected_wheel(
        source.getvalue(), name="demo", version="1.0", python="3.12"
    )
    filename, blob, provenance = first
    assert filename == "demo-1.0-1bsim1-cp312-none-linux_x86_64.whl"
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for path, data in payloads.items():
            if path not in provenance["modified_members"]:
                assert archive.read(path) == data
            assert archive.getinfo(path).external_attr == 0o100755 << 16
        assert b"Tag: cp312-none-linux_x86_64" in archive.read("demo-1.0.dist-info/WHEEL")


def test_dependency_errors_and_streams_redact_url_credentials():
    import sys

    logs = []
    secret = "https://user:password@example.test/wheel.whl?token=private#other"
    with pytest.raises(PackageError) as caught:
        runtime.run_dependency_command(
            [sys.executable, "-c", f"print({secret!r});raise SystemExit(1)"],
            dependency_logger=logs.append,
        )
    for text in [str(caught.value), *logs]:
        assert "password" not in text and "private" not in text
        assert "example.test" in text
