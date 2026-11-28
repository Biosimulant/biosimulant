# Lab environment setup

`biosimulant` 0.0.43 introduces optional `lab.yaml` `runtime.setup`. It prepares
software before Lab/model dependency installation and model imports. No extra
file or Dockerfile is required:

```yaml
runtime:
  python_version: "3.12"
  setup:
    command:
      - python
      - -c
      - |
          import os
          from pathlib import Path
          prefix = Path(os.environ["BIOSIM_ENV_PREFIX"])
          (prefix / "bin").mkdir(parents=True, exist_ok=True)
          print("Preparing tools in", prefix, flush=True)
    timeout_seconds: 900
```

Use `[python, scripts/prepare.py]` for a file, or explicitly invoke a shell,
such as `[bash, -eu, scripts/install.sh]` or
`[powershell, -NonInteractive, -File, scripts/install.ps1]`. `command` must be a
nonempty argument list; there is no implicit shell, interpolation, or elevation.
The executable `python` selects the Lab interpreter, including on Windows.
Timeout defaults to 900 seconds, accepts 1–3600, and shares an overall one-hour
preparation deadline with dependency installation. Failure stops the run.

The working directory is the declaring Lab. `BIOSIM_LAB_ROOT` names it;
`BIOSIM_ENV_PREFIX` is the prepared environment's installation prefix. Install
user-space tools there (`bin` on POSIX, `Scripts` on Windows); its executable
directory is on the run's PATH. Use the platform's package manager only where
the execution environment permits it. Python orchestration does not make apt,
Homebrew, PowerShell, or their privileges available on every OS. Setup has no
GPU during hosted image construction.

Setup should use Python's standard library until dependencies are installed.
Keep Python package requirements in model manifests. Dataset downloads,
sample-specific indexes and work that depends on run inputs belong in model
execution, including `once_before_run` modules. Setup subprocess environment
exports do not change the runner. Relative build scratch files are temporary;
install persistent tools/assets into the environment prefix.

## Execution and composition

`labs run` and package execution prepare a fresh isolated environment even with
no Python requirements. `labs serve` defers setup until an explicit run and
executes it in an isolated child; merely viewing a Lab does not run setup.
Validation, capability inspection, packaging and previews do not execute setup.
Live-object APIs require a matching prepared environment and never install in
the caller's interpreter. `--no-install-deps` does not bypass this check.

Embedded child Labs run setup dependency-first, then their parent. Shared child
paths run once. For Hub/package-backed children, export with the existing
`--vendor-dependencies` option, then run the vendored package: setup needs all
source files in the immutable context. A setup plan with unresolved
package-backed children is rejected rather than skipped. All components still
share one compatible Python environment.

The source context includes the complete Lab source (up to 32 MiB/5000 files),
not just the directly named script. Helpers therefore invalidate preparation.
Operational state, caches, `.env*`, generated `environment.lock`, and bytecode
are excluded. Per-run runtime settings and instance parameter overrides do not
change software identity. Source symlinks are not allowed in setup context.
Write generated assets outside the source context so setup cannot invalidate
its own preparation. No background process may outlive setup.

Successful preparation is recorded with setup identity, interpreter, OS,
architecture, exit codes and durations. A boolean environment flag does not
prove preparation. Local results include `environment.setup`; cloud results
retain setup identity and execution in Lab environment provenance. Failures and
interrupted environments are never considered prepared. Scripts must tolerate
retry after partial failure; exactly-once execution across rebuilds is not
guaranteed.

## Hosted environments and locks

The platform builds setup inside the existing isolated Modal image preparation,
before Lab dependency resolution/installation, then reuses successful provider
image layers when available. Setup-only Labs also use this preparation path.
The selected hosted runtime must support 0.0.43; older runtimes/providers return
a blocker. Release availability and deployment are separate from source support.

`environment.lock` still exists. `environment.yaml` was retired. Setup-bearing
hosted environments use a version-2 Python lock bound to setup source and target
platform as well as pins/base/Python; legacy environments keep version-1 locks.
After adding/changing setup, regenerate and save the returned lock unchanged.
The lock covers Python distributions, not arbitrary apt packages or files fetched
by setup. Pin external versions and verify download checksums where practical;
identical script bytes alone do not guarantee identical remote downloads.

Local execution supports setup plus ordinary model package pins. The platform's
`runtime.environment` source/base/lock resolver remains a hosted capability;
local runs reject those declarations instead of silently ignoring them. Do not claim local/cloud parity for them. Preview the actual run
target and its supported capabilities before execution. Arbitrary setup remains
code execution and must be described accurately to users and plugin reviewers.
