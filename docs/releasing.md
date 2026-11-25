# Releasing biosimulant to PyPI

This repository publishes to PyPI using GitHub Trusted Publishing.

The distribution name is `biosimulant`. The wheel still includes the `biosim`
import path and `python -m biosim` compatibility command for existing code, but
new installs should use `pip install biosimulant`.

## Release workflow

1. Confirm the PyPI Trusted Publisher is configured for both the `biosimulant` and `biosimulant-model-sdk` projects.
2. Set the release version in `src/biosim/__about__.py`, update the exact SDK requirement in the root `pyproject.toml`, and refresh `uv.lock`.
3. Move the matching notes in `CHANGELOG.md` from `Unreleased` into a dated
   release section.
4. Commit and push the version/changelog bump to `main`.
5. Create and push a matching tag (`v0.0.15`).
6. Verify GitHub Actions `Publish to PyPI` finishes successfully and produces
   the `biosimulant-checksums` artifact plus its build-provenance attestation.
7. Verify a fresh install from PyPI:

```bash
python -m venv /tmp/biosimulant-release-check
/tmp/biosimulant-release-check/bin/python -m pip install biosimulant
/tmp/biosimulant-release-check/bin/biosimulant --help
/tmp/biosimulant-release-check/bin/python -m biosim --help
```

## Forwarding-wrapper note

The Python package installs the canonical `biosimulant` console script. During
the one-release migration window, an older native installation may leave a
forwarding wrapper earlier on `PATH`. Use `python -m biosimulant ...` to
explicitly verify the installed Python package.

## Manual commands

```bash
git add src/biosim/__about__.py pyproject.toml uv.lock
git add CHANGELOG.md
git commit -m "Bump version to 0.0.15"
git push origin main
git tag v0.0.15
git push origin v0.0.15
```

## Automation script

Use the helper script:

```bash
bash scripts/release_pypi.sh
```

Behavior:

- Reads the version from `src/biosim/__about__.py`.
- Requires a clean git tree (including untracked files).
- Pushes `main`/`master`, then creates and pushes `v<version>`.

Options:

- Explicit version: `bash scripts/release_pypi.sh 0.0.15`
- Local tag only (no push): `bash scripts/release_pypi.sh --no-push`

### Model SDK and executor distributions

The model SDK owns the shared authoring modules under both public import names.
The full executor requires the matching SDK version and owns the runner and CLI.
Both distributions read the version in `src/biosim/__about__.py`. Register the
trusted PyPI publisher for both project names before tagging the first paired
release. The tag workflow publishes the SDK first. Test source checkouts with
`python -m pip install ./src -e '.[dev]' build`.

`biosimulant labs validate PATH --check-dependencies` checks published wheels
for the declared interpreter on the current platform using the full executor's
requirements. It does not reserve compute or start the Lab. GPU drivers, native
imports and availability on another target need separate verification.
