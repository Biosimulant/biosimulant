#!/usr/bin/env python3
"""Local Linux qualification, with separate preparation and scientific evidence.

Run in the disposable Docker harness documented in docs/tellurium-packaging.md.
The caller must install the candidate runtime wheel before invoking this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PINS = ["tellurium==2.2.11.2", "libroadrunner==2.8.0", "scipy==1.15.3", "numpy==2.2.6"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--runtime-wheel", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    import biosim
    from biosim.dependency_runtime import _redact_dependency_line, isolated_environment
    from biosim.tellurium_artifacts import WHEELHOUSE_ENV, artifact_requirements

    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    uv = [sys.executable, "-m", "uv"]
    results = {
        "scope": "local Docker; no cloud or deployment qualification",
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "runtime_import": biosim.__file__,
        "runtime_wheel_sha256": hashlib.sha256(args.runtime_wheel.read_bytes()).hexdigest(),
        "declared_requirements": PINS,
        "stages": {},
    }

    def command(label: str, cmd: list[str], *, expected: int | None = 0, env=None):
        completed = subprocess.run(
            cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env
        )
        output = _redact_dependency_line(completed.stdout)
        (evidence / f"{label}.log").write_text(output)
        results["stages"][label] = {"exit_code": completed.returncode, "command": cmd}
        print(f"{label}: exit={completed.returncode}", flush=True)
        (evidence / "qualification.json").write_text(
            json.dumps(results, indent=2, sort_keys=True) + "\n"
        )
        if expected is not None and completed.returncode != expected:
            raise RuntimeError(f"{label} failed; see {evidence / (label + '.log')}")
        return completed

    command("uv-version", [*uv, "--version"])
    command("system-packages", ["dpkg-query", "-W"])
    native = {}
    for name, version in (("antimony", "3.2.0"), ("phrasedml", "1.3.0")):
        source = next(args.wheelhouse.glob(f"{name}-{version}-py3-*.whl"))
        with zipfile.ZipFile(source) as archive:
            library = archive.read(f"{name}/lib{name}.so")
        assert library[:4] == b"\x7fELF" and library[4] == 2 and library[5] == 1
        assert struct.unpack_from("<H", library, 18)[0] == 62, "Native library is not ELF x86_64"
        native[name] = {
            "elf_machine": "x86_64",
            "sha256": hashlib.sha256(library).hexdigest(),
            "glibc_symbol_versions": sorted(
                {m.decode() for m in re.findall(rb"GLIBC_\d+\.\d+(?:\.\d+)?", library)}
            ),
            "portability_claim": "linux_x86_64 only; no manylinux certification",
        }
        with tempfile.TemporaryDirectory(prefix=f"bsim-{name}-native-") as native_directory:
            library_path = Path(native_directory) / f"lib{name}.so"
            library_path.write_bytes(library)
            dependencies = command(f"native-{name}-ldd", ["ldd", str(library_path)])
            assert "not found" not in dependencies.stdout, f"Unresolved shared library for {name}"
    (evidence / "native-inspection.json").write_text(
        json.dumps(native, indent=2, sort_keys=True) + "\n"
    )

    with tempfile.TemporaryDirectory(prefix="bsim-tellurium-original-") as directory:
        baseline = Path(directory) / "env"
        command("original-venv", [*uv, "venv", "--python", sys.executable, str(baseline)])
        python = baseline / "bin/python"
        originals = [
            str(next(args.wheelhouse.glob(f"{name}-{version}-py3-*.whl")))
            for name, version in (("antimony", "3.2.0"), ("phrasedml", "1.3.0"))
        ]
        command(
            "original-install",
            [
                *uv,
                "pip",
                "install",
                "--python",
                str(python),
                str(args.runtime_wheel),
                *PINS,
                *originals,
            ],
        )
        command("original-resolved", [*uv, "pip", "freeze", "--python", str(python)])
        check = command(
            "original-check", [*uv, "pip", "check", "--python", str(python)], expected=None
        )
        assert check.returncode != 0 and "antimony" in check.stdout and "phrasedml" in check.stdout
        assert "platform" in check.stdout.lower(), (
            "Original failure did not reproduce platform validation"
        )
    results["original_environment_removed"] = not baseline.exists()

    os.environ["BIOSIMULANT_MANAGED_SOURCE_SPEC"] = str(args.runtime_wheel.resolve())
    os.environ[WHEELHOUSE_ENV] = str(args.wheelhouse.resolve())
    refs = artifact_requirements(
        PINS,
        args.wheelhouse,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        python_full_version=platform.python_version(),
    )
    with tempfile.TemporaryDirectory(prefix="bsim-tellurium-negative-") as directory:
        negative = Path(directory) / "env"
        command("negative-venv", [*uv, "venv", "--python", sys.executable, str(negative)])
        python = negative / "bin/python"
        wrong_arch = command(
            "wrong-architecture",
            [
                *uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--python-platform",
                "aarch64-unknown-linux-gnu",
                *refs,
            ],
            expected=None,
        )
        assert wrong_arch.returncode != 0
        conflict = command(
            "conflicting-version",
            [*uv, "pip", "install", "--python", str(python), "antimony==3.1.0", *refs],
            expected=None,
        )
        assert conflict.returncode != 0 and "antimony" in conflict.stdout
        command(
            "missing-dependencies-install",
            [*uv, "pip", "install", "--no-deps", "--python", str(python), str(args.runtime_wheel)],
        )
        missing = command(
            "missing-dependencies-check",
            [*uv, "pip", "check", "--python", str(python)],
            expected=None,
        )
        assert missing.returncode != 0 and "numpy" in missing.stdout
    results["negative_environment_removed"] = not negative.exists()
    with isolated_environment([*PINS, "pytest==9.0.2", "build==1.6.1"]) as python:
        results["candidate_artifacts"] = refs
        command("candidate-check", [*uv, "pip", "check", "--python", str(python)])
        command("candidate-resolved", [*uv, "pip", "freeze", "--python", str(python)])
        command(
            "candidate-import",
            [
                str(python),
                "-c",
                "import biosim,tellurium;print(biosim.__file__);"
                "assert 'site-packages' in biosim.__file__;print(tellurium.__version__)",
            ],
        )
        with tempfile.TemporaryDirectory(prefix="bsim-tellurium-tests-") as test_directory:
            copied = Path(test_directory) / "test_tellurium_qualification.py"
            shutil.copyfile(args.source_root / "tests/test_tellurium_qualification.py", copied)
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            environment["BIOSIMULANT_TELLURIUM_QUALIFICATION_REQUIRED"] = "1"
            environment["BIOSIMULANT_TELLURIUM_QUALIFICATION_SOURCE_ROOT"] = str(args.source_root)
            pytest_command = [
                str(python),
                "-m",
                "pytest",
                "-o",
                "addopts=",
                "-p",
                "no:cacheprovider",
                "-q",
            ]
            environment["BIOSIMULANT_TELLURIUM_QUALIFICATION_REPORT"] = str(
                evidence / "scientific-report-built-wheel.json"
            )
            command(
                "candidate-built-wheel-science",
                [
                    *pytest_command,
                    "-o",
                    "markers=integration:real engine qualification",
                    str(copied),
                ],
                env=environment,
            )
            built_report = json.loads((evidence / "scientific-report-built-wheel.json").read_text())
            assert "site-packages" in built_report["qualification"]["runtime_import"]
            environment["BIOSIMULANT_TELLURIUM_QUALIFICATION_REPORT"] = str(
                evidence / "scientific-report-source.json"
            )
            command(
                "candidate-source-science",
                [
                    *pytest_command,
                    str(args.source_root / "tests/test_sbml_contrib.py"),
                    str(args.source_root / "tests/test_tellurium_qualification.py"),
                ],
                env=environment,
            )
            source_report = json.loads((evidence / "scientific-report-source.json").read_text())
            assert (
                Path(source_report["qualification"]["runtime_import"])
                .resolve()
                .is_relative_to((args.source_root / "src").resolve())
            )
            command(
                "runtime-regressions",
                [
                    *pytest_command,
                    *[
                        str(args.source_root / "tests" / name)
                        for name in (
                            "test_dependency_runtime.py",
                            "test_dependency_diagnostics.py",
                            "test_tellurium_artifacts.py",
                            "test_managed_runtime.py",
                        )
                    ],
                ],
                env=environment,
            )
            command(
                "preserved-roadrunner-reference",
                [
                    str(python),
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(args.source_root / "examples/labs/repressilator-reproduction/tests"),
                    "-v",
                ],
                env=environment,
            )
    results["candidate_environment_removed"] = not python.parent.parent.exists()
    (evidence / "qualification.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main()
