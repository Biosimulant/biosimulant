"""A caller-owned extraction must outlive the CLI that produced its files."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from biosim import __main__ as cli
from biosim import managed_runtime, pack
from tests.test_pack import _write_lab


def _lab_with_file_output(root: Path) -> Path:
    lab = _write_lab(root)
    source = lab / "models/counter/src/counter.py"
    text = source.read_text()
    text = text.replace(
        "from biosim import BioModule, SignalSpec, ScalarSignal",
        "from pathlib import Path\nfrom biosim import BioModule, SignalSpec, ScalarSignal, RecordSignal",
    ).replace(
        'return {"count": SignalSpec.scalar(dtype="float64")}',
        'return {"count": SignalSpec.scalar(dtype="float64"), '
        '"files": SignalSpec.record(schema={"report_file": "string"})}',
    ).replace(
        "    def get_outputs(self):\n        return {",
        '    def get_outputs(self):\n'
        '        path = Path(__file__).resolve().parent / "outputs" / "report.csv"\n'
        '        path.parent.mkdir(exist_ok=True)\n'
        '        path.write_text("count\\n" + str(self.value) + "\\n")\n'
        '        return {"files": RecordSignal(source="counter", name="files", '
        'value={"report_file": str(path)}, emitted_at=0.1, spec=self.outputs()["files"]), ',
    )
    source.write_text(text)
    return lab


@pytest.mark.parametrize("preserve", [False, True])
def test_cli_model_relative_file_lifetime(tmp_path: Path, preserve: bool, capsys) -> None:
    lab = _lab_with_file_output(tmp_path / "lab")
    results = tmp_path / "results.json"
    extraction = tmp_path / "run" / "runtime"
    args = ["run", str(lab), "--no-install-deps", "--results-file", str(results), "--json"]
    if preserve:
        args.extend(["--unpack-root", str(extraction)])

    cli._main_labs(args)
    capsys.readouterr()

    document = json.loads(results.read_text())
    path = Path(document["outputs"]["counter"]["files"]["value"]["report_file"])
    # The default records the original failure; the opt-in path must remain
    # readable after CLI return so Studio can retain it before caller cleanup.
    assert path.is_file() is preserve
    if preserve:
        assert extraction.resolve() in path.parents
        assert path.read_text() == "count\n2.0\n"


def test_managed_python_forwards_caller_owned_extraction(monkeypatch, tmp_path: Path) -> None:
    extraction = tmp_path / "runtime"
    observed = {}
    monkeypatch.setattr(managed_runtime, "requested_package_python_version", lambda _: "3.11")
    monkeypatch.setattr(managed_runtime, "_current_python_minor", lambda: "3.12")
    monkeypatch.setattr(managed_runtime, "ensure_executor_python", lambda _: Path("/python"))

    def child(_python, _package, **kwargs):
        observed.update(kwargs)
        return {"outputs": {}}

    monkeypatch.setattr(managed_runtime, "run_child_package", child)
    managed_runtime.run_package_with_managed_python(
        "lab.bsilab", install_deps=False, unpack_root=extraction,
    )
    assert observed == {"install_deps": False, "unpack_root": extraction}


def test_dependency_child_preserves_the_same_extraction(monkeypatch, tmp_path: Path) -> None:
    from contextlib import nullcontext
    from biosim import dependency_runtime

    lab = _lab_with_file_output(tmp_path / "lab")
    package = pack.build_package(
        lab, output_path=tmp_path / "lab.bsilab", package_name="local/files", version="1.0.0"
    )
    extraction = tmp_path / "run" / "runtime"
    observed = {}
    monkeypatch.setattr(pack, "_package_requirements", lambda *_: ["demo==1.0"])
    monkeypatch.setattr(dependency_runtime, "isolated_environment", lambda *_a, **_k: nullcontext(Path("/python")))

    def child(_python, _package, **kwargs):
        observed.update(kwargs)
        return {"outputs": {}}

    monkeypatch.setattr(managed_runtime, "run_child_package", child)
    pack.run_package(package, unpack_root=extraction)
    assert observed["unpack_root"] == extraction.resolve()
    assert observed["install_deps"] is False


def test_real_child_keeps_model_relative_output(tmp_path: Path) -> None:
    lab = _lab_with_file_output(tmp_path / "lab")
    package = pack.build_package(
        lab, output_path=tmp_path / "lab.bsilab", package_name="local/files", version="1.0.0"
    )
    extraction = tmp_path / "run" / "runtime"
    # run_child_package removes PYTHONPATH and uses the interpreter's installed
    # runtime, as a managed handoff does. The child API already supports this
    # option; a top-level CLI help assertion alone would not exercise it.
    document = managed_runtime.run_child_package(
        Path(sys.executable), package, install_deps=False, unpack_root=extraction,
    )
    path = Path(document["outputs"]["counter"]["files"]["value"]["report_file"])
    assert extraction.resolve() in path.parents
    assert path.read_text() == "count\n2.0\n"
