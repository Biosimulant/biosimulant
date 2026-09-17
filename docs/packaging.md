# Packaging

`biosimulant` supports single-file package archives for both models and labs.
The `biosim` package/import name remains available for compatibility with
existing model code.

Package unit:
- one model package wraps one `model.yaml` into a `.bsimodel`
- one lab package wraps one `lab.yaml` into a portable `.bsilab`

Typical use cases:
- move a runnable model or lab without shipping a whole repository
- validate package structure before upload
- cache and fetch package-backed models locally
- export a portable lab archive, with an optional fully self-contained archival mode

## CLI

Initialize, validate, run, and serve a local lab without Desktop:

```bash
biosimulant labs create ./my-lab --name "My Lab"
biosimulant labs validate ./my-lab
biosimulant labs capabilities ./my-lab --json
biosimulant labs run ./my-lab --require-local-capability --no-install-deps
biosimulant labs serve ./my-lab
```

`labs capabilities` checks explicit CPU-core, memory, and accelerator
requirements against the current host. `--require-local-capability` makes a
local-first run fail before execution when those declared requirements are not
met. The probe does not replace model tests or establish framework-specific GPU
compatibility, and local runs do not create managed-run or Passport records.

Manage a local lab source tree without Desktop:

```bash
biosimulant labs list .
biosimulant labs get ./my-lab
biosimulant labs save ./my-lab
biosimulant labs add-model models/example --lab ./my-lab --alias example
biosimulant labs vendor-model ../model-source --lab ./my-lab --alias vendored
biosimulant labs inspect-owned ./my-lab
biosimulant labs package ./my-lab --out dist
```

Local workspace identity is stored in `.biosimulant/lab.json`; `lab.yaml`
remains the portable lab manifest embedded in exported `.bsilab` files.

Build all packages declared in a package repository manifest:

```bash
biosimulant labs release validate biosimulant-packages.yaml
biosimulant labs release build biosimulant-packages.yaml --out dist/biosimulant-packages
```

Discover and pull public registry labs:

```bash
biosimulant labs search immune
biosimulant labs info owner/lab-name@1.0.0
biosimulant labs versions owner/lab-name
biosimulant labs pull owner/lab-name@1.0.0 --target ./labs/lab-name
```

Build a lab archive:

```bash
biosimulant labs package path/to/lab --out dist
```

Build a fully self-contained archival package with every lockfile-pinned Hub
child embedded:

```bash
biosimulant labs package path/to/lab --out dist --vendor-dependencies
```

Validate or run a lab source tree or `.bsilab`:

```bash
biosimulant labs validate path/to/lab
biosimulant labs run path/to/lab.bsilab --no-install-deps

# Use a writable Lab-local state directory when the archive's folder is read-only.
biosimulant labs run path/to/lab.bsilab --dependency-root /tmp/my-lab-state
```

Use `--json` with `biosimulant labs` commands when you need machine-readable output.

For bash or zsh completion of commands, options, and paths, add this once to
`~/.zshrc` or `~/.bashrc`:

```bash
eval "$(register-python-argcomplete biosimulant)"
```

Restart the shell, or run `source ~/.zshrc` / `source ~/.bashrc`.

`biosimulant pack`, `biosimulant packages`, `biosimulant hub`, and standalone
`biosimulant models` are no longer public CLI surfaces. The library-level
package helpers remain available as Python APIs for internal tooling and
compatibility code.

Publishing and private downloads use the registry and authentication support
built into the headless Python CLI. Desktop state and interface actions are not
public CLI commands.

## Public Registry

```bash
biosimulant labs search [query]
biosimulant labs info namespace/name[@version]
biosimulant labs versions namespace/name
biosimulant labs pull namespace/name[@version] --target ./local-lab
```

Public reads are anonymous. Private pull and publishing use registry-scoped
credentials configured with `biosimulant auth login`.

## Package Repository Manifest

A package repository manifest declares one or more top-level packages to build:

```yaml
schema_version: "1"
default_visibility: public
packages:
  - id: example-lab
    package: demo/example-lab
    version: 1.0.0
    type: lab
    path: labs/example-lab
    visibility: public
```

`labs release validate` checks the manifest shape, package identity, SemVer versions,
source paths, package type, dependency pins, embedded lab/model paths, and archive
compatibility by building into a temporary directory.

## Runtime Compatibility

Package execution uses the provisional `biosimulant.runtime` helpers for behavior that must match other Biosimulant runtimes:

- entrypoints are loaded from the model directory with file-spec loading when possible, so multiple models can each use a `src/` namespace-style layout in one process
- run input payloads are coerced into typed `BioSignal` objects against each module's declared `inputs()`
- `communication_step` is resolved using the same precedence chain as platform executors: runtime override, simulation config, base runtime, then explicit fallback
- `settle_steps` is resolved with the same runtime precedence and defaults to `0`
- nested child labs are flattened with canonical alias scoping and `io.maps_to` remapping

The `biosimulant.runtime` API is public but provisional for this minor release. The `biosim.runtime` path remains available for compatibility.

## Source Layout

Model package source:

```text
my-model/
├── model.yaml
├── src/
├── artifacts/
├── data/
├── tests/
└── README.md
```

Lab package source:

```text
my-lab/
├── models/
├── labs/
├── lab.yaml
├── biosimulant.lock  # required for package-backed child Labs
├── tests/
└── README.md
```

## Package Contents

Each package archive is a ZIP with:
- `package.yaml`
- `payload/`
- `integrity/sha256sums.txt`

Model package payload usually includes:
- `payload/model.yaml`
- `payload/src/**`
- `payload/artifacts/**`
- `payload/data/**`

Lab package payload usually includes:
- `payload/lab.yaml`
- embedded model folders such as `payload/models/**`
- embedded child-lab folders such as `payload/labs/**`
- any additional helper files required by the lab

## Package Identity

If `model.yaml` or `lab.yaml` declares:

```yaml
package: biosimulant/example-counter
version: 1.2.0
```

then `biosimulant labs package` uses those values by default.

Local source-lab commands do not require release identity. For
`biosimulant labs validate`, `biosimulant labs run`, and
`biosimulant labs serve`, identity-free labs use a transient local identity:
- package: `local/<directory-slug>`
- version: `0.1.0`

Standalone `.bsilab` builds still need release identity. Add `package:` and
`version:` to `lab.yaml`, provide both at the CLI, or build through
`biosimulant-packages.yaml`:

```bash
biosimulant labs package path/to/lab --package biosimulant/example-lab --version 1.2.0
```

## Validation Rules

`biosimulant labs validate` checks source-tree labs and `.bsilab` archives:
- required archive files exist
- no invalid archive paths
- checksums match
- `package.yaml` points to a real manifest
- model manifests contain `biosim.entrypoint`
- `biosim.execution_policy`, when declared, is `once_before_run`, `each_window` or `once_after_run` and agrees with the model source; unverifiable declarations and undeclared models whose source resolves produce warnings
- wiring between declared modules only moves forward through execution phases, with no cycles among run-once modules
- lab manifests contain valid `models`, `wiring`, and `runtime`
- model dependencies use exact `==` pins only
- package-backed child Labs use exact `package` + `version` values and have a matching `biosimulant.lock` entry with an artifact SHA-256
- path-based nested models and child Labs stay inside the archive payload tree
- every embedded model or path-based child Lab manifest is valid

The command is meant to be operator-friendly:
- success prints a concise summary with package name, version, type and execution timing (`finite`, `temporal` or `unknown`)
- failure prints a concise error list and exits non-zero

## Intentional Runtime Differences

The open-source CLI and Biosimulant platform share package interpretation semantics, but they do not share every execution policy:

- `biosimulant labs run` installs exact-pinned manifest dependencies into the current Python environment when dependency installation is enabled
- platform and desktop executors install payload dependencies into isolated per-lock-hash environments with allow/deny policy
- `biosimulant labs run` preserves typed terminal signals under
  `outputs.<module-alias>.<port>` in JSON result and report files
- platform and desktop runs add durable artifact and run metadata for UI consumers

## Registries And Cache

The Python package still includes simple local package registry and cache helpers for API consumers:

- `BIOSIM_PACKAGE_REGISTRY_DIR`
- `BIOSIM_PACKAGE_CACHE_DIR`

Example:

```bash
export BIOSIM_PACKAGE_REGISTRY_DIR=/tmp/biosim-registry
export BIOSIM_PACKAGE_CACHE_DIR=/tmp/biosim-cache
```

Publish or fetch packages programmatically through the Python API when internal
tooling needs local cache behavior.

## Labs

```yaml
models:
  - path: models/example-counter
    alias: counter
```

`biosimulant labs package path/to/lab` preserves the runnable source tree under
`payload/`. Package-backed child Labs remain compact `package` + `version`
references, with their checksum provenance in `biosimulant.lock`. Runtime state
under `.biosimulant/` is excluded from normal package payloads.

Lab-local visualisation modules should remain inside each lab when portability is
the goal. If several labs intentionally carry byte-identical visualisation code,
keep those copies local and use a drift check in repository maintenance rather
than introducing a shared runtime import path.

Finite downstream report, export, or visualisation modules should implement
`execute(inputs, *, context)` with `ExecutionPolicy.ONCE_AFTER_RUN`. BioWorld drains chains of
those modules automatically after the final atomic window commit, so authors do
not calculate a settle depth. `runtime.settle_steps` remains unchanged for
legacy temporal modules that deliberately use zero-time propagation. Settling
does not extend simulated time, and canonical modules are not invoked by it.

Nested `models[]` use relative `path` refs. A child Lab in `children[]` may use
either a relative `path` or exact `package` + `version`; the latter must have a
matching package/version/checksum entry in the sibling `biosimulant.lock`.
Nested package references are resolved only into the parent Lab's local state.

If a lab depends on another model or child lab, that dependency must already exist inside
the lab directory before packaging when it is path-based. Package-backed child
Labs resolve from Hub at runtime. Use `--vendor-dependencies` for a fully
self-contained archival package.
