from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TEXT_ROOTS = (ROOT / "README.md", ROOT / "docs", ROOT / "examples")
COMPATIBILITY_DOCS = {
    ROOT / "README.md",
    ROOT / "docs" / "README.md",
    ROOT / "docs" / "releasing.md",
}
TEMPORAL_HOOK_EXAMPLE_ALLOWLIST = {ROOT / "docs" / "biomodule.md"}
TEMPORAL_COMPATIBILITY_REFERENCE_ALLOWLIST = {
    ROOT / "CHANGELOG.md",
    ROOT / "README.md",
    ROOT / "docs" / "biomodule.md",
    ROOT / "docs" / "kernel-1-5.md",
    ROOT / "docs" / "overview.md",
    ROOT / "docs" / "plugin-development.md",
    ROOT / "scripts" / "benchmark_execution_policy.py",
    ROOT / "src" / "biosim" / "contrib" / "cellml.py",
    ROOT / "src" / "biosim" / "contrib" / "sbml.py",
    ROOT / "src" / "biosim" / "execution.py",
    ROOT / "src" / "biosim" / "modules.py",
    ROOT / "src" / "biosim" / "world.py",
    ROOT / "tests" / "test_biomodules.py",
    ROOT / "tests" / "test_biosignals.py",
    ROOT / "tests" / "test_bioworld_flow.py",
    ROOT / "tests" / "test_cellml_contrib.py",
    ROOT / "tests" / "test_cli.py",
    ROOT / "tests" / "test_errors.py",
    ROOT / "tests" / "test_execution_declarations.py",
    ROOT / "tests" / "test_execution_policies.py",
    ROOT / "tests" / "test_listeners.py",
    ROOT / "tests" / "test_modules_coverage.py",
    ROOT / "tests" / "test_pack.py",
    ROOT / "tests" / "test_phase2_cli.py",
    ROOT / "tests" / "test_ports_validation.py",
    ROOT / "tests" / "test_sbml_contrib.py",
    ROOT / "tests" / "test_snapshot_biomodule_outputs.py",
    ROOT / "tests" / "test_visuals.py",
    ROOT / "tests" / "test_wiring_builder.py",
    ROOT / "tests" / "test_wiring_coverage.py",
    ROOT / "tests" / "test_world_coverage.py",
}


def _public_text_files() -> list[Path]:
    files: list[Path] = []
    for root in PUBLIC_TEXT_ROOTS:
        if root.is_file():
            files.append(root)
            continue
        files.extend(
            path
            for path in root.rglob("*")
            if path.suffix in {".md", ".py"} and "__pycache__" not in path.parts
        )
    return sorted(files)


def _repository_text_files() -> list[Path]:
    roots = (
        ROOT / "CHANGELOG.md",
        ROOT / "README.md",
        ROOT / "docs",
        ROOT / "examples",
        ROOT / "scripts",
        ROOT / "src",
        ROOT / "tests",
    )
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        files.extend(
            path
            for path in root.rglob("*")
            if path.suffix in {".md", ".py", ".toml", ".yaml", ".yml"}
            and "__pycache__" not in path.parts
        )
    return sorted(files)


def test_public_docs_lead_with_biosimulant_package_and_cli() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert readme.startswith("# biosimulant")
    assert "`biosimulant` is the primary package, import namespace, and CLI name" in readme
    assert "pip install biosimulant" in readme
    assert "biosimulant labs create" in readme
    assert "pip install biosimulant" in quickstart
    assert "local lab UI" in quickstart
    assert "--no-open" in quickstart
    assert "biosimulant labs create" in quickstart
    assert "## [Unreleased]" in changelog
    assert "Keep a Changelog" in changelog


def test_legacy_biosim_command_is_only_documented_as_compatibility() -> None:
    legacy_command = re.compile(r"python -m biosim(\s|$)")
    offenders: list[str] = []
    for path in _public_text_files():
        text = path.read_text(encoding="utf-8")
        if legacy_command.search(text) and path not in COMPATIBILITY_DOCS:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_public_examples_do_not_use_legacy_install_or_cli_names() -> None:
    forbidden = (
        re.compile(r"pip install ['\"]?biosim(\[|\s|$)"),
        re.compile(r"\bbiosim labs\b"),
        re.compile(r"\bbiosim packages\b"),
        re.compile(r"\bbiosim pack\b"),
    )
    offenders: list[str] = []

    for path in _public_text_files():
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)}: {pattern.pattern}")

    assert offenders == []


def test_public_python_examples_import_primary_namespace() -> None:
    offenders: list[str] = []
    for path in _public_text_files():
        if path == ROOT / "README.md":
            continue
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped == "import biosim" or stripped.startswith("from biosim "):
                offenders.append(f"{path.relative_to(ROOT)}: {stripped}")

    assert offenders == []


def test_public_authoring_docs_lead_with_canonical_execution() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
    starter = (ROOT / "src" / "biosim" / "_starter.py").read_text(encoding="utf-8")

    for text in (readme, quickstart, starter):
        assert "def execute(" in text
        assert "ExecutionPolicy.EACH_WINDOW" in text

    offenders: list[str] = []
    removed_hook = "advance" + "_to"
    compatibility_hook = "advance" + "_window"
    for path in _public_text_files():
        text = path.read_text(encoding="utf-8")
        if removed_hook in text:
            offenders.append(f"{path.relative_to(ROOT)}: {removed_hook}")
        if f"def {compatibility_hook}" in text and path not in TEMPORAL_HOOK_EXAMPLE_ALLOWLIST:
            offenders.append(f"{path.relative_to(ROOT)}: def {compatibility_hook}")

    assert offenders == []


def test_legacy_execution_references_are_allowlisted() -> None:
    removed_hook = "advance" + "_to"
    compatibility_hook = "advance" + "_window"
    removed_hook_offenders: list[str] = []
    compatibility_hook_offenders: list[str] = []

    for path in _repository_text_files():
        text = path.read_text(encoding="utf-8")
        if removed_hook in text:
            removed_hook_offenders.append(str(path.relative_to(ROOT)))
        if (
            compatibility_hook in text
            and path not in TEMPORAL_COMPATIBILITY_REFERENCE_ALLOWLIST
        ):
            compatibility_hook_offenders.append(str(path.relative_to(ROOT)))

    assert removed_hook_offenders == []
    assert compatibility_hook_offenders == []
