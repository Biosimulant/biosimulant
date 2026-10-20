"""Explicit local qualification of checksum-pinned Tellurium packaging artifacts.

This module selects already-built artifacts. It never patches installed metadata
or downloads/rebuilds wheels. The candidate supports only the listed targets;
core Biosimulant Python support is a separate contract.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from ._errors import PackageError

WHEELHOUSE_ENV = "BIOSIMULANT_TELLURIUM_WHEELHOUSE"
_ARTIFACTS = {
    "3.11": (
        (
            "antimony",
            "antimony-3.2.0-1bsim1-cp311-none-linux_x86_64.whl",
            "be89044a920f214fc12116231e1b9bdf1aa425f9d415b1c2eefc1ff813a4bf7c",
        ),
        (
            "phrasedml",
            "phrasedml-1.3.0-1bsim1-cp311-none-linux_x86_64.whl",
            "9cc9dd27aeb200e8e10d958a3b2950762a2f6237e5ecffdc53b9d3ea3c150574",
        ),
    ),
    "3.12": (
        (
            "antimony",
            "antimony-3.2.0-1bsim1-cp312-none-linux_x86_64.whl",
            "f18810ac338187717c5f04044d1690a339a68efdd3af7cec9ff98c6793b413a9",
        ),
        (
            "phrasedml",
            "phrasedml-1.3.0-1bsim1-cp312-none-linux_x86_64.whl",
            "df24c730d99ed44325be791231760a7c047aa223d6b6a602ebf2b9e2a52a5f3d",
        ),
    ),
}


def requests_candidate(
    requirements: list[str], *, environment: dict[str, str] | None = None
) -> bool:
    """Limit the opt-in correction to the qualified Tellurium release."""
    return any(
        canonicalize_name(req.name) == "tellurium"
        and str(req.specifier) == "==2.2.11.2"
        and req.url is None
        and (environment is None or req.marker is None or req.marker.evaluate(environment))
        for req in map(Requirement, requirements)
    )


def artifact_requirements(
    requirements: list[str],
    wheelhouse: Path,
    *,
    python_version: str,
    sys_platform: str = "linux",
    machine: str = "x86_64",
    implementation: str = "cpython",
    python_full_version: str | None = None,
) -> list[str]:
    """Verify trusted candidates and return exact file requirements with hashes.

    These accompany the original requirements in one solve. Conflicting direct
    pins remain resolver errors; no package requirement is silently replaced.
    """
    if not requests_candidate(requirements):
        return []
    candidates = [
        req
        for req in map(Requirement, requirements)
        if canonicalize_name(req.name) == "tellurium" and str(req.specifier) == "==2.2.11.2"
    ]
    if any(
        req.marker
        and any(key in str(req.marker) for key in ("platform_release", "platform_version"))
        for req in candidates
    ):
        raise PackageError("Tellurium artifact selection does not support OS-release markers")
    environment = default_environment()
    environment.update(
        python_version=python_version,
        sys_platform=sys_platform,
        platform_machine=machine,
        implementation_name=implementation,
        platform_python_implementation="CPython" if implementation == "cpython" else implementation,
        os_name="posix" if sys_platform != "win32" else "nt",
        platform_system={"linux": "Linux", "darwin": "Darwin", "win32": "Windows"}.get(
            sys_platform, sys_platform
        ),
    )
    if python_full_version is not None:
        environment.update(
            python_full_version=python_full_version, implementation_version=python_full_version
        )
    elif any(
        req.marker
        and any(key in str(req.marker) for key in ("python_full_version", "implementation_version"))
        for req in candidates
    ):
        raise PackageError(
            "Tellurium artifact markers require the exact target Python patch version"
        )
    if not requests_candidate(requirements, environment=environment):
        return []
    if (sys_platform, machine, implementation) != (
        "linux",
        "x86_64",
        "cpython",
    ) or python_version not in _ARTIFACTS:
        raise PackageError(
            "Tellurium wheelhouse candidate supports only CPython 3.11/3.12 Linux x86_64"
        )
    result = []
    for name, filename, expected in _ARTIFACTS[python_version]:
        path = wheelhouse.expanduser().resolve() / filename
        if not path.is_file():
            raise PackageError(f"Missing verified Tellurium artifact: {filename}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise PackageError(f"Tellurium artifact checksum mismatch: {filename}")
        result.append(f"{name} @ {path.as_uri()}#sha256={expected}")
    return result
