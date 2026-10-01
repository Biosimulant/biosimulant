from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from biosim.execution_recorder import record_execution, verify_execution_record


def test_records_actual_execution_and_rejects_changed_bytes(tmp_path: Path):
    (tmp_path / "input.txt").write_text("plate-1")
    script = "from pathlib import Path; Path('result.txt').write_text(Path('input.txt').read_text()+' counted')"
    record = record_execution(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        record_path=tmp_path / "record.json",
        inputs={"plate": Path("input.txt")},
        outputs={"prediction": Path("result.txt")},
        timeout_seconds=5,
        environment={"PRIVATE_TOKEN": "not-in-record"},
    )
    assert record["status"] == "completed" and record["exit_code"] == 0
    assert record["evidence_origin"] == "imported"
    assert "not-in-record" not in json.dumps(record)
    verify_execution_record(record, root=tmp_path)
    (tmp_path / "result.txt").write_text("different")
    with pytest.raises(ValueError, match="Changed outputs"):
        verify_execution_record(record, root=tmp_path)
    with pytest.raises(FileExistsError):
        record_execution(
            [sys.executable, "-c", "raise Exception()"],
            cwd=tmp_path,
            record_path=tmp_path / "record.json",
            inputs={},
            outputs={},
            timeout_seconds=5,
        )


@pytest.mark.parametrize(
    "script,status",
    [("raise SystemExit(3)", "failed"), ("import time; time.sleep(10)", "timed_out")],
)
def test_failed_and_timed_out_attempts_are_preserved(tmp_path: Path, script, status):
    record = record_execution(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        record_path=tmp_path / "record.json",
        inputs={},
        outputs={},
        timeout_seconds=0.2,
    )
    assert record["status"] == status
    assert json.loads((tmp_path / "record.json").read_text()) == record
    verify_execution_record(record, root=tmp_path)


def test_missing_output_and_mutated_input_never_claim_success(tmp_path: Path):
    record = record_execution(
        [sys.executable, "-c", "pass"],
        cwd=tmp_path,
        record_path=tmp_path / "missing.json",
        inputs={},
        outputs={"model": Path("model.pt")},
        timeout_seconds=5,
    )
    assert (
        record["status"] == "failed"
        and record["error_code"] == "required_output_missing"
    )
    (tmp_path / "input.txt").write_text("original")
    record = record_execution(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('input.txt').write_text('changed')",
        ],
        cwd=tmp_path,
        record_path=tmp_path / "changed.json",
        inputs={"data": Path("input.txt")},
        outputs={},
        timeout_seconds=5,
    )
    assert (
        record["status"] == "failed"
        and record["error_code"] == "input_changed_during_execution"
    )


def test_paths_outside_root_and_forged_managed_origin_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        record_execution(
            [sys.executable, "-c", "pass"],
            cwd=tmp_path,
            record_path=tmp_path / "record.json",
            inputs={},
            outputs={"model": Path("../model.pt")},
            timeout_seconds=5,
        )
    with pytest.raises(ValueError, match="Unsupported"):
        verify_execution_record(
            {"schema_version": 1, "evidence_origin": "managed"}, root=tmp_path
        )


def test_credentials_are_removed_without_changing_execution(tmp_path: Path):
    import biosimulant.execution_recorder as public_recorder

    script = "import os; from pathlib import Path; Path('result').write_text(os.environ['API_TOKEN'])"
    secret = "private-fixture-token"
    record = public_recorder.record_execution(
        [
            sys.executable,
            "-c",
            script,
            "--password",
            "flag-password",
            "--api-key=inline-key",
        ],
        cwd=tmp_path,
        record_path=tmp_path / "record.json",
        inputs={},
        outputs={"result": Path("result")},
        configuration={
            "nested": {"password": "config-password", "api_key": "config-key"},
            "description": "value " + secret,
        },
        environment={"API_TOKEN": secret},
        timeout_seconds=5,
    )
    serialized = (tmp_path / "record.json").read_text()
    for value in (
        secret,
        "flag-password",
        "inline-key",
        "config-password",
        "config-key",
    ):
        assert value not in serialized
    assert (tmp_path / "result").read_text() == secret
    assert record["status"] == "completed"
    verify_execution_record(record, root=tmp_path)
