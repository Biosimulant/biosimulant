"""Machine-readable lab runs must show model output and progress while they run."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import biosim
from biosim.progress_stream import PROGRESS_PREFIX, attach_progress_stream
from biosimulant.__main__ import _LiveCaptureStream, main


def _write_printing_lab(root: Path, *, windows: int = 3) -> Path:
    model_dir = root / "models" / "ticker"
    (model_dir / "src").mkdir(parents=True)
    (model_dir / "src" / "__init__.py").write_text("", encoding="utf-8")
    (model_dir / "src" / "ticker.py").write_text(
        "import sys\n"
        "from biosim import BioModule, ExecutionContext, ExecutionPolicy, SignalSpec\n\n"
        "class Ticker(BioModule):\n"
        "    execution_policy = ExecutionPolicy.EACH_WINDOW\n\n"
        "    def __init__(self):\n"
        "        self.count = 0.0\n\n"
        "    def outputs(self):\n"
        "        return {'count': SignalSpec.scalar(dtype='float64')}\n\n"
        "    def execute(self, inputs, *, context: ExecutionContext):\n"
        "        self.count += 1\n"
        "        print(f'ticker stdout window {self.count:.0f}')\n"
        "        print(f'ticker stderr window {self.count:.0f}', file=sys.stderr)\n"
        "        return {'count': self.count}\n",
        encoding="utf-8",
    )
    (model_dir / "model.yaml").write_text(
        'schema_version: "2.0"\n'
        'title: "Ticker"\n'
        'description: "Prints each window."\n'
        "standard: other\n"
        "biosim:\n"
        '  entrypoint: "src.ticker:Ticker"\n'
        "  communication_step: 1.0\n"
        "  execution_policy: each_window\n"
        "io:\n"
        "  inputs: []\n"
        "  outputs:\n"
        "    - name: count\n"
        "      signal_type: scalar\n"
        "      dtype: float64\n",
        encoding="utf-8",
    )
    (root / "lab.yaml").write_text(
        'schema_version: "2.0"\n'
        'title: "Printing lab"\n'
        'description: "Prints from model code."\n'
        "models:\n"
        "  - alias: ticker\n"
        "    path: models/ticker\n"
        "runtime:\n"
        "  communication_step: 1.0\n"
        f"  duration: {float(windows)}\n"
        "  initial_inputs: {}\n"
        "wiring: []\n",
        encoding="utf-8",
    )
    return root


def _progress_lines(text: str) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix(PROGRESS_PREFIX))
        for line in text.splitlines()
        if line.startswith(PROGRESS_PREFIX)
    ]


def test_live_capture_stream_forwards_lines_and_keeps_json_result() -> None:
    forward = io.StringIO()
    stream = _LiveCaptureStream(forward, skip_json_result=True)

    stream.write("model says hi\n{\"ok\": tr")
    assert forward.getvalue() == "model says hi\n"
    stream.write("ue}\npartial line")
    stream.finish()

    assert forward.getvalue() == "model says hi\npartial line\n"
    assert stream.getvalue() == 'model says hi\n{"ok": true}\npartial line'


def test_json_lab_run_streams_model_output_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BIOSIM_PROGRESS_EVENTS", raising=False)
    lab = _write_printing_lab(tmp_path / "lab")

    main(["--json", "--no-open", "labs", "run", str(lab), "--no-install-deps"])
    captured = capsys.readouterr()

    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert "ticker stdout window" not in captured.out
    for window in (1, 2, 3):
        assert f"ticker stdout window {window}" in captured.err
        assert f"ticker stderr window {window}" in captured.err
    assert '"ok"' not in captured.err
    assert PROGRESS_PREFIX not in captured.err


def test_json_lab_run_emits_progress_when_enabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIOSIM_PROGRESS_EVENTS", "1")
    lab = _write_printing_lab(tmp_path / "lab", windows=4)

    main(["--json", "--no-open", "labs", "run", str(lab), "--no-install-deps"])
    captured = capsys.readouterr()

    assert json.loads(captured.out)["ok"] is True
    events = _progress_lines(captured.err)
    assert events[0]["message"] == "Running simulation..."
    assert events[-1]["message"] == "Finalizing simulation..."
    pcts = [event["progress_pct"] for event in events[1:-1]]
    assert pcts == [25.0, 50.0, 75.0, 100.0]
    assert [event["step_count"] for event in events[1:-1]] == [1, 2, 3, 4]
    assert all(event["phase"] == "running" for event in events)


def test_progress_stream_throttles_to_whole_percent_or_interval() -> None:
    world = biosim.BioWorld(communication_step=1.0)
    out = io.StringIO()
    now = [0.0]
    listener = attach_progress_stream(world, stream=out, clock=lambda: now[0])

    listener(biosim.WorldEvent.STARTED, {"t": 0.0, "progress_pct": 0.0})
    for pct in (0.2, 0.4, 0.6):
        listener(biosim.WorldEvent.STEP, {"t": pct, "progress_pct": pct})
    now[0] = 6.0
    listener(biosim.WorldEvent.STEP, {"t": 0.8, "progress_pct": 0.8})
    listener(biosim.WorldEvent.STEP, {"t": 1.0, "progress_pct": 1.0})
    listener(biosim.WorldEvent.ERROR, {"t": 1.0})

    events = _progress_lines(out.getvalue())
    # Start, the first step into bucket 0, the interval-due step, then bucket 1.
    assert [event.get("progress_pct") for event in events] == [0.0, 0.2, 0.8, 1.0]
    assert events[-1]["step_count"] == 5
