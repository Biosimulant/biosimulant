from __future__ import annotations

from pathlib import Path

import pytest

from biosim.pack import PackageError
from biosim.package_repo import load_package_repo_manifest
from tests.test_pack import _write_counter_model, _component_lab


def _write_manifest(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "biosimulant-packages.yaml"
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def test_package_repo_rejects_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match="Package manifest not found"):
        load_package_repo_manifest(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    "body, match",
    [
        ("- not-a-mapping", "YAML mapping"),
        ("schema_version: 2\npackages: []", "Unsupported schema_version"),
        ("default_visibility: hidden\npackages: []", "Invalid visibility"),
        ("schema_version: 1\npackages: []", "at least one package"),
        ("schema_version: 1\npackages:\n  - bad", "entry #1 must be a mapping"),
        (
            "schema_version: 1\npackages:\n  - package: Demo/Bad\n    version: 1.0.0\n    type: lab\n    path: model-lab",
            "Package names",
        ),
        (
            "schema_version: 1\npackages:\n  - package: demo/pkg\n    version: latest\n    type: lab\n    path: model-lab",
            "Invalid SemVer",
        ),
        (
            "schema_version: 1\npackages:\n  - package: demo/pkg\n    version: 1.0.0\n    type: space\n    path: model-lab",
            "type must be one of",
        ),
        (
            "schema_version: 1\npackages:\n  - package: demo/pkg\n    version: 1.0.0\n    type: lab\n    path: missing",
            "source path is missing",
        ),
    ],
)
def test_package_repo_manifest_validation_errors(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(PackageError, match=match):
        load_package_repo_manifest(_write_manifest(tmp_path, body))


def test_package_repo_rejects_missing_source_manifest(tmp_path: Path) -> None:
    (tmp_path / "model").mkdir()
    manifest = _write_manifest(
        tmp_path,
        """
schema_version: 1
packages:
  - package: demo/pkg
    version: 1.0.0
    type: lab
    path: model-lab
""",
    )

    with pytest.raises(PackageError, match="is missing"):
        load_package_repo_manifest(manifest)


def test_package_repo_rejects_duplicate_ids_and_package_versions(tmp_path: Path) -> None:
    _component_lab(_write_counter_model(tmp_path / "model"))
    duplicate_ids = _write_manifest(
        tmp_path,
        """
schema_version: 1
packages:
  - id: pkg
    package: demo/pkg-a
    version: 1.0.0
    type: lab
    path: model-lab
  - id: pkg
    package: demo/pkg-b
    version: 1.0.0
    type: lab
    path: model-lab
""",
    )
    with pytest.raises(PackageError, match="Duplicate package id"):
        load_package_repo_manifest(duplicate_ids)

    duplicate_versions = _write_manifest(
        tmp_path,
        """
schema_version: 1
packages:
  - package: demo/pkg
    version: 1.0.0
    type: lab
    path: model-lab
  - package: demo/pkg
    version: 1.0.0
    type: lab
    path: model-lab
""",
    )
    with pytest.raises(PackageError, match="Duplicate package entry"):
        load_package_repo_manifest(duplicate_versions)


def test_package_repo_rejects_bad_source_metadata_and_publish_type(tmp_path: Path) -> None:
    _component_lab(_write_counter_model(tmp_path / "model"))
    bad_source = _write_manifest(
        tmp_path,
        """
schema_version: 1
packages:
  - package: demo/pkg
    version: 1.0.0
    type: lab
    path: model-lab
    source: bad
""",
    )
    with pytest.raises(PackageError, match="source metadata"):
        load_package_repo_manifest(bad_source)

    bad_publish = _write_manifest(
        tmp_path,
        """
schema_version: 1
packages:
  - package: demo/pkg
    version: 1.0.0
    type: lab
    path: model-lab
    publish: maybe
""",
    )
    with pytest.raises(PackageError, match="publish must be true or false"):
        load_package_repo_manifest(bad_publish)
