# Changelog

All notable changes to `biosimulant` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.0.31] - 2026-09-17

### Fixed

- Apply each model's `runtime.remote.init_kwargs` when `BIOSIM_REMOTE_EXECUTION=1`,
  expanding `${REMOTE_EXECUTION_MOUNT_ROOT}` from
  `BIOSIM_REMOTE_EXECUTION_MOUNT_ROOT`, so hosted runs use installed runtime
  dependencies and persistent caches as the hosted executor does.
- Put the interpreter's scripts directory on `PATH` before installing declared
  dependencies so models can invoke dependency console scripts such as `boltz`.

## [0.0.30] - 2026-09-17

### Changed

- Allow structurally valid connections when only one port declares a
  compatibility profile, reporting `PROFILE_PARTIAL` instead of blocking the
  wire. Live values are still checked against whichever profile is declared.
- Report a partial-profile warning once when a connection is created instead
  of repeating it at every communication boundary.

## [0.0.28] - 2026-09-13

### Added

- Add `biosimulant labs capabilities` and
  `labs run --require-local-capability` for conservative, hardware-aware
  local-first execution on declared CPU, memory, CUDA, or Apple MPS resources.

### Changed

- Generate canonical `execute(inputs, *, context)` starter models with an
  explicit `ExecutionPolicy.EACH_WINDOW` from both Lab creation commands.
- Route BioModule regression snapshots through `BioWorld` so canonical
  invocation policies, typed output normalization, and compatibility modules
  use the same execution path as Labs.
- Lead active documentation and first-party examples with the canonical
  execution contract while retaining one explicit temporal compatibility path.

### Removed

- Remove obsolete audit and V1-to-V1.5 migration documents that described
  superseded execution signatures.

## [0.0.27] - 2026-09-12

### Changed

- Preserve every module's typed terminal output signals in Lab run JSON and
  report results so managed and local runs retain inspectable scientific data.

## [0.0.26] - 2026-09-10

### Added

- Add `ExecutionPolicy`, immutable `ExecutionContext`, and the canonical
  `execute(inputs, *, context)` BioModule authoring contract for finite or
  temporal computation before, during, or after communication windows.
- Add deterministic dependency draining for once-before and once-after modules.

### Changed

- Route standalone model-package execution through BioWorld so invocation policy,
  signal timestamps, and output validation match Lab execution.
- Preserve existing temporal `advance_window()` behavior as the default while
  excluding canonical modules from zero-duration settle calls.
- Validate canonical signatures at registration, warn when `EACH_WINDOW` is only
  inherited, and commit normalized canonical output caches atomically with world
  signals.

## [0.0.22] - 2026-07-29

### Added

- Generate a standalone HTML report for `labs run --report-file`, completing
  headless report parity for Studio, Desktop, local terminals, and CI.

## [0.0.21] - 2026-07-29

### Added

- Add the canonical headless CLI contract, schema-v1 JSON/JSONL output,
  standardized exit codes, portable doctor/runtime commands, and top-level
  `validate` and `run` aliases.
- Add registry-qualified references, Registry API v1 discovery and publishing,
  registry-scoped credentials, and short-lived workspace token exchange.
- Add headless runs, remote-runs, jobs capability reporting, lab publishing,
  sync status, and release publishing workflows.

### Changed

- Make the MIT PyPI package the sole public CLI implementation used by local,
  Desktop, Studio, CI, and server environments.

### Removed

- Remove the proprietary extension contract and Desktop-binary CLI delegation.

## [0.0.20] - 2026-07-19

### Added

- Add explicit `biosimulant.hub.HubComposition` for resolving exact,
  lockfile-pinned public Hub Labs into a caller-owned `BioWorld`.
- Store composed Hub Lab payloads under the parent Lab's
  `.biosimulant/dependencies/` state instead of a shared machine cache.
- Add `biosimulant.lock` validation for package-backed child Labs and
  `--vendor-dependencies` for fully self-contained archival `.bsilab` releases.
- Add `--dependency-root` to `biosimulant labs run` for package-backed archives
  in read-only locations.

### Changed

- Keep ordinary Lab package payloads compact by preserving locked Hub package
  references; archive runtime state remains disposable and excluded from normal
  package output.

## [0.0.19] - 2026-07-19

### Added

- Add the open `Client` and `AsyncClient` interfaces for explicit managed,
  durable execution through the private Biosimulant developer API.
- Add typed run, result, artifact, error, timeout, and webhook verification
  contracts while leaving `BioWorld.run()` local and unchanged.

## [0.0.18] - 2026-06-05

### Added

- Add managed runtime support for local lab execution and serving.
- Add 3D Labs Serve visual support.
- Add GPU accelerator warning detection.

### Changed

- Improve lab command progress messages during package resolution, dependency
  installation, and runtime preparation.
- Add Python version validation and warnings for lab manifests.
- Improve PublicRegistryClient request headers and Cloudflare error handling.
- Disable uvicorn access logs for the Labs Serve API.

## [0.0.17] - 2026-06-03

### Changed

- Remove normal UI support for persistent manual input defaults while keeping
  runtime compatibility for existing `runtime.initial_inputs`.
- Consolidate lab wiring authoring into the World inspector and simplify model
  inspectors to read-only interface summaries.
- Keep run-modal input overrides ephemeral and prevent `Save & Run` from
  persisting input defaults.

## [0.0.16] - 2026-06-03

### Added

- Add root `biosimulant --version` output.
- Add Labs Serve run progress metadata for active local runs.

### Changed

- Update Labs Serve desktop download links to
  `https://www.biosimulant.com/download/desktop`.
- Avoid polling run results from the Labs Serve UI until a run reaches a
  terminal state.
- Accept `biosimulant labs run --no-open` as a compatibility no-op flag.

## [0.0.15] - 2026-06-02

### Added

- Add the embedded open-source Labs Serve UI as the implementation behind
  `biosimulant labs serve`.
- Add Labs Serve API endpoints for lab metadata, local run lifecycle, logs,
  results, cancellation, manifest edits, and layout persistence.
- Add `--no-open` to suppress the default browser launch.

### Changed

- Serve the Labs Serve UI at the root URL (`/`) and report the root URL in JSON
  output.
- Redirect `/ui` and `/ui/` to `/` for one release.
- Build the private React/Vite Labs Serve UI into the Python wheel so users do
  not install a separate UI package.

### Removed

- Remove the old Python-declared SimUI implementation and public
  `biosim.simui` / `biosimulant.simui` modules.
- Remove the top-level `--simui` runner flag and the `biosimulant[ui]` extra.

## [0.0.14] - 2026-06-02

### Changed

- Move SimUI runtime dependencies into the default package install so
  `pipx install biosimulant` supports `biosimulant labs serve` without requiring
  the `biosimulant[ui]` extra.
- Keep `biosimulant[ui]` as a backwards-compatible install extra while updating
  docs and stale-environment error messages to lead with the default install.
- Update backend, desktop, sandbox, and web surfaces to target the next
  `biosimulant` runtime release (`0.0.14`).

## [0.0.13] - 2026-06-02

### Added

- Add shell completion support for the `biosimulant` CLI.
- Add local lab identity handling improvements for source-tree lab workflows.

### Changed

- Focus release instructions and packaging language on the `biosimulant`
  distribution and CLI.
- Split PyPI publishing workflows for clearer release automation.
- Add archive messaging for the legacy `biosim` package.

## [0.0.12] - 2026-06-02

### Added

- Add product extension contracts and command routing for product-owned CLI
  surfaces.
- Add broader registry, workspace, and package-management test coverage.

### Changed

- Rename public package and documentation language from BioSim/Biosim toward
  Biosimulant while preserving compatibility imports and commands.
- Improve package validation and lab manifest package formatting.
- Tighten the coverage gate and tidy wiring builder demo coverage.

## [0.0.11] - 2026-05-30

### Added

- Add the lab-scoped CLI surface for local lab initialization, validation,
  running, serving, packaging, registry lookup, and package repository release
  workflows.
- Add package repository manifest validation/build support under
  `biosimulant labs release`.
- Add input value type handling for `SignalSpec`.

### Changed

- Move public CLI guidance toward lab-scoped commands such as
  `biosimulant labs package`, `biosimulant labs run`, and
  `biosimulant labs serve`.

## [0.0.10] - 2026-05-30

### Changed

- Rename the Python distribution and primary CLI entrypoint to `biosimulant`.
- Preserve the legacy `biosim` import path and `python -m biosim` compatibility
  command for existing model packages.

[Unreleased]: https://github.com/Biosimulant/biosimulant/compare/v0.0.19...HEAD
[0.0.19]: https://github.com/Biosimulant/biosimulant/compare/v0.0.18...v0.0.19
[0.0.18]: https://github.com/Biosimulant/biosimulant/compare/v0.0.17...v0.0.18
[0.0.17]: https://github.com/Biosimulant/biosimulant/compare/v0.0.16...v0.0.17
[0.0.16]: https://github.com/Biosimulant/biosimulant/compare/v0.0.15...v0.0.16
[0.0.15]: https://github.com/Biosimulant/biosimulant/compare/v0.0.14...v0.0.15
[0.0.14]: https://github.com/Biosimulant/biosimulant/compare/v0.0.13...v0.0.14
[0.0.13]: https://github.com/Biosimulant/biosimulant/compare/v0.0.12...v0.0.13
[0.0.12]: https://github.com/Biosimulant/biosimulant/compare/v0.0.11...v0.0.12
[0.0.11]: https://github.com/Biosimulant/biosimulant/compare/v0.0.10...v0.0.11
[0.0.10]: https://github.com/Biosimulant/biosimulant/releases/tag/v0.0.10
