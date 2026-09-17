from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from biosim.__main__ import main
from biosim.pack import validate_package
from biosim.workspace import create_lab
from tests.test_pack import _write_lab, _write_lab_release_identity


def _assert_canonical_starter(lab_dir: Path) -> None:
    source = (lab_dir / "models" / "hello" / "src" / "hello.py").read_text(
        encoding="utf-8"
    )
    assert "from biosimulant import" in source
    assert "execution_policy = ExecutionPolicy.EACH_WINDOW" in source
    assert "def execute(self, inputs, *, context: ExecutionContext):" in source
    assert "def advance_window" not in source
    assert "def get_outputs" not in source


def test_labs_init_validate_and_run_without_desktop(tmp_path: Path, capsys) -> None:
    lab_dir = tmp_path / "starter-lab"

    main(["labs", "init", str(lab_dir), "--name", "Starter Lab"], prog="biosimulant")
    init_output = capsys.readouterr().out
    assert "Biosimulant lab initialized." in init_output
    assert (lab_dir / "lab.yaml").is_file()
    assert (lab_dir / "models" / "hello" / "model.yaml").is_file()
    _assert_canonical_starter(lab_dir)

    main(["labs", "validate", str(lab_dir), "--json"], prog="biosimulant")
    validate_payload = json.loads(capsys.readouterr().out)
    assert validate_payload["valid"] is True
    assert validate_payload["package"] == "local/starter-lab"

    main(["labs", "capabilities", str(lab_dir), "--json"], prog="biosimulant")
    capability_payload = json.loads(capsys.readouterr().out)
    assert capability_payload["local_supported"] is True
    assert capability_payload["selected_backend"] == "cpu"

    main(
        ["labs", "run", str(lab_dir), "--no-install-deps", "--json"],
        prog="biosimulant",
    )
    run_payload = json.loads(capsys.readouterr().out)
    assert run_payload["package"] == "local/starter-lab"
    assert run_payload["duration"] == 1.0
    assert run_payload["modules"][0]["alias"] == "hello"

    main(
        [
            "labs",
            "run",
            str(lab_dir),
            "--no-install-deps",
            "--require-local-capability",
            "--json",
            "--no-open",
        ],
        prog="biosimulant",
    )
    run_no_open_payload = json.loads(capsys.readouterr().out)
    assert run_no_open_payload["package"] == "local/starter-lab"
    assert run_no_open_payload["local_execution"]["local_supported"] is True


def test_labs_create_uses_the_same_canonical_starter(tmp_path: Path) -> None:
    lab_dir = tmp_path / "managed-starter-lab"

    result = create_lab(lab_dir, name="Managed Starter Lab")

    assert result["created"] is True
    _assert_canonical_starter(lab_dir)


def test_root_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"], prog="biosimulant")

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "biosimulant 0.0.33"


def test_labs_serve_uses_local_lab_ui_without_desktop(tmp_path: Path) -> None:
    lab_dir = tmp_path / "served-lab"
    main(["labs", "init", str(lab_dir), "--name", "Served Lab"], prog="biosimulant")

    with patch("biosim.__main__.serve_lab") as serve_lab:
        main(
            ["labs", "serve", str(lab_dir), "--port", "9999", "--no-open", "--no-install-deps"],
            prog="biosimulant",
        )

    serve_lab.assert_called_once()
    assert serve_lab.call_args.args == (lab_dir.resolve(),)
    assert serve_lab.call_args.kwargs["port"] == 9999
    assert serve_lab.call_args.kwargs["open_browser"] is False


def test_identity_free_labs_validate_run_serve_and_package_with_flags(
    tmp_path: Path,
    capsys,
) -> None:
    lab_dir = _write_lab(tmp_path / "identity-free-lab")

    main(["labs", "validate", str(lab_dir), "--json"], prog="biosimulant")
    validate_payload = json.loads(capsys.readouterr().out)
    assert validate_payload["valid"] is True
    assert validate_payload["package"] == "local/identity-free-lab"
    assert validate_payload["version"] == "0.1.0"

    main(
        ["labs", "run", str(lab_dir), "--no-install-deps", "--json"],
        prog="biosimulant",
    )
    run_payload = json.loads(capsys.readouterr().out)
    assert run_payload["package"] == "local/identity-free-lab"
    assert run_payload["version"] == "0.1.0"
    assert run_payload["modules"][0]["alias"] == "counter"

    with patch("biosim.__main__.serve_lab") as serve_lab:
        main(
            ["labs", "serve", str(lab_dir), "--no-install-deps"],
            prog="biosimulant",
        )
    serve_lab.assert_called_once()
    assert serve_lab.call_args.args == (lab_dir.resolve(),)
    assert serve_lab.call_args.kwargs["open_browser"] is True

    with pytest.raises(SystemExit) as exc_info:
        main(["labs", "package", str(lab_dir)], prog="biosimulant")
    assert exc_info.value.code == 1
    assert "pass --package" in capsys.readouterr().err

    main(
        [
            "labs",
            "package",
            str(lab_dir),
            "--package",
            "demo/identity-free-lab",
            "--version",
            "1.0.0",
            "--out",
            str(tmp_path / "dist"),
            "--json",
        ],
        prog="biosimulant",
    )
    package_payload = json.loads(capsys.readouterr().out)
    assert package_payload["package"] == "demo/identity-free-lab"
    assert package_payload["version"] == "1.0.0"
    assert Path(package_payload["package_file"]).is_file()


def test_labs_release_validate_build_and_run_repo_manifest(tmp_path: Path, capsys) -> None:
    lab_dir = tmp_path / "lab"
    main(["labs", "init", str(lab_dir), "--name", "Package Lab"], prog="biosimulant")
    capsys.readouterr()
    _write_lab_release_identity(lab_dir, "demo/package-lab", "1.0.0")

    manifest = tmp_path / "biosimulant-packages.yaml"
    manifest.write_text(
        """
schema_version: "1"
namespace: demo
default_visibility: public
packages:
  - id: package-lab
    package: demo/package-lab
    version: 1.0.0
    type: lab
    path: lab
    visibility: public
""".strip()
        + "\n",
        encoding="utf-8",
    )

    main(["labs", "release", "validate", str(manifest), "--json"], prog="biosimulant")
    validate_payload = json.loads(capsys.readouterr().out)
    assert validate_payload["valid"] is True
    assert validate_payload["package_count"] == 1

    out_dir = tmp_path / "dist" / "packages"
    main(
        ["labs", "release", "build", str(manifest), "--out", str(out_dir), "--json"],
        prog="biosimulant",
    )
    build_payload = json.loads(capsys.readouterr().out)
    built_path = Path(build_payload["built"][0]["path"])
    assert built_path.name == "demo__package-lab-1.0.0.bsilab"
    assert validate_package(built_path).valid

    main(
        ["labs", "run", str(built_path), "--no-install-deps", "--json"],
        prog="biosimulant",
    )
    run_payload = json.loads(capsys.readouterr().out)
    assert run_payload["package"] == "demo/package-lab"
    assert run_payload["modules"][0]["alias"] == "hello"


def test_labs_release_manifest_supplies_identity_for_identity_free_lab(
    tmp_path: Path,
    capsys,
) -> None:
    _write_lab(tmp_path / "lab")
    manifest = tmp_path / "biosimulant-packages.yaml"
    manifest.write_text(
        """
schema_version: "1"
namespace: demo
default_visibility: public
packages:
  - id: identity-free-lab
    package: demo/identity-free-lab
    version: 1.0.0
    type: lab
    path: lab
    visibility: public
""".strip()
        + "\n",
        encoding="utf-8",
    )

    main(["labs", "release", "validate", str(manifest), "--json"], prog="biosimulant")
    validate_payload = json.loads(capsys.readouterr().out)
    assert validate_payload["valid"] is True
    assert validate_payload["packages"][0]["package"] == "demo/identity-free-lab"

    main(
        ["labs", "release", "build", str(manifest), "--out", str(tmp_path / "dist"), "--json"],
        prog="biosimulant",
    )
    build_payload = json.loads(capsys.readouterr().out)
    built_path = Path(build_payload["built"][0]["path"])
    assert built_path.name == "demo__identity-free-lab-1.0.0.bsilab"
    assert validate_package(built_path).valid


def test_package_archive_validate_under_labs(tmp_path: Path, capsys) -> None:
    lab_dir = tmp_path / "lab"
    main(["labs", "init", str(lab_dir), "--name", "Archive Lab"], prog="biosimulant")
    capsys.readouterr()
    _write_lab_release_identity(lab_dir, "demo/archive-lab", "1.0.0")
    main(
        ["labs", "release", "build", _write_single_package_manifest(tmp_path, lab_dir), "--json"],
        prog="biosimulant",
    )
    built_path = Path(json.loads(capsys.readouterr().out)["built"][0]["path"])

    main(["labs", "validate", str(built_path), "--json"], prog="biosimulant")
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["package"] == "demo/archive-lab"


def _write_single_package_manifest(tmp_path: Path, lab_dir: Path) -> str:
    manifest = tmp_path / "packages.yaml"
    manifest.write_text(
        f"""
schema_version: "1"
packages:
  - package: demo/archive-lab
    version: 1.0.0
    type: lab
    path: {lab_dir.relative_to(tmp_path).as_posix()}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return str(manifest)
