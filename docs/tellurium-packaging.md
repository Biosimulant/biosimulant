# Tellurium packaging candidate and local qualification

This change prepares an **explicit local wheelhouse candidate** for
Tellurium 2.2.11.2. Default installations and the normal cloud submission path
remain unchanged. It is not a published package correction or a staging or
production deployment. Production remains on Modal.

## Evidence and root cause

The reported run `3e5e2aa4-641f-4016-a0ba-a35520e4a007` failed at dependency
validation before scientific execution. Its retained evidence does not establish
the original resolved transitive versions or installer version.

Independent checksum verification on 2026-10-20 confirmed these current PyPI
artifacts:

| Artifact | Source SHA-256 | Internal WHEEL tag |
| --- | --- | --- |
| `antimony-3.2.0-py3-none-manylinux_2_28_x86_64.whl` | `2036c1dcf699a8f1dedf35d26007e0060ddbd91f9f4bb9f4d05fa8a79cc7c2fd` | `cp313-cp313-linux_x86_64` |
| `phrasedml-1.3.0-py3-none-manylinux2014_x86_64.whl` | `b0e7e58cb84377d9f021c335dd2bd44a6a1e608c8f3b779a1b511d6ea2961ce6` | `cp39-cp39-linux_x86_64` |

Their filenames advertise general Python 3 compatibility, conflicting with
installed metadata. The current upstream [Antimony workflow](https://github.com/sys-bio/antimony/blob/develop/.github/workflows/main.yml)
and [phrasedml workflow](https://github.com/sys-bio/phrasedml/blob/master/.github/workflows/main.yml)
rename wheel filenames. The wrapper sources load their native libraries through
`ctypes`; this alone does not establish Python or Linux compatibility.

The local qualification harness records an independent replay with exact package
origins and a resolved inventory. That replay must not be described as the
unknown original environment.

## Artifact identity and selection

`scripts/prepare_tellurium_wheelhouse.py` downloads checksum-pinned source wheels
and deterministically creates derivative wheels. Every Python source, native
library, license, `METADATA` file and original ZIP member permission is retained.
Only `WHEEL` and `RECORD` change. ZIP member order/timestamps are normalized;
uncompressed ZIP storage makes output independent of zlib versions.

The distinct build number is `1bsim1`. Distribution versions remain 3.2.0 and
1.3.0 so imported and installed version checks retain their existing meaning.
The Python-specific tags are `cp311-none-linux_x86_64` and
`cp312-none-linux_x86_64`. They make no manylinux or other-Python claim.

| Candidate | SHA-256 |
| --- | --- |
| `antimony-3.2.0-1bsim1-cp311-none-linux_x86_64.whl` | `be89044a920f214fc12116231e1b9bdf1aa425f9d415b1c2eefc1ff813a4bf7c` |
| `antimony-3.2.0-1bsim1-cp312-none-linux_x86_64.whl` | `f18810ac338187717c5f04044d1690a339a68efdd3af7cec9ff98c6793b413a9` |
| `phrasedml-1.3.0-1bsim1-cp311-none-linux_x86_64.whl` | `9cc9dd27aeb200e8e10d958a3b2950762a2f6237e5ecffdc53b9d3ea3c150574` |
| `phrasedml-1.3.0-1bsim1-cp312-none-linux_x86_64.whl` | `df24c730d99ed44325be791231760a7c047aa223d6b6a602ebf2b9e2a52a5f3d` |

The builder writes `provenance.json`, including upstream URLs/hashes and every
unchanged member hash. The runtime catalog independently pins candidate hashes;
an untrusted wheelhouse manifest cannot override the catalog.

Set `BIOSIMULANT_TELLURIUM_WHEELHOUSE` to the prepared directory to opt in.
When the target requests exactly Tellurium 2.2.11.2, the runtime verifies hashes
and adds exact file references with hash fragments to the **same solve** as the
original requirements. Explicit incompatible pins still fail. Other package
requirements, manifest exact-pin rules and `uv pip check` remain unchanged.
No installed metadata is rewritten. Environment markers use target Python/OS/
architecture; patch-version markers require the exact target patch version, and
OS-release markers fail closed because target release metadata is not supplied.

The backend adds only a callable-level `_build_image(tellurium_wheelhouse=...)`
qualification recipe. It transfers verified files individually, uses exact
in-image references, and checks the resulting environment. It is not enabled by
settings, model payloads, compute assignments, or normal submission. Provider
contract tests are not evidence that a cloud image was built or deployed.

## Reproduce locally

From the runtime staging checkout, choose a new disposable output directory.
The following example uses `/tmp/bsim-tellurium-local-qualification`:

```sh
python scripts/prepare_tellurium_wheelhouse.py /tmp/bsim-tellurium-local-qualification/wheelhouse
python -m build --wheel --outdir /tmp/bsim-tellurium-local-qualification/dist
docker build --platform linux/amd64 -t bsim-tellurium-qualification:local \
  -f scripts/tellurium-qualification.Dockerfile .
docker run --rm --platform linux/amd64 \
  -v "$PWD":/work/biosim:ro \
  -v /tmp/bsim-tellurium-local-qualification:/qualification \
  -e UV_CACHE_DIR=/qualification/uv-cache \
  bsim-tellurium-qualification:local sh -c \
  'python -m uv pip install --system /qualification/dist/biosimulant-0.0.37-py3-none-any.whl && python scripts/qualify_tellurium_wheelhouse.py --wheelhouse /qualification/wheelhouse --runtime-wheel /qualification/dist/biosimulant-0.0.37-py3-none-any.whl --source-root /work/biosim --evidence-dir /qualification/evidence-py312'
```

The driver separates original preparation, negative checks, corrected managed
preparation, installed-wheel science, source science, runtime regressions and
the preserved RoadRunner reproduction tests. It records source checksums,
native ELF architecture/GLIBC symbols/shared-library requirements, exact commands,
resolved distributions, interpreter and installer versions, and environment
cleanup. All failed checks remain visible; it never updates baselines or widens
scientific tolerances.

## Qualification limits and rollback

The candidate targets CPython 3.12/Linux x86_64 first. Generated CPython 3.11
artifacts are not qualified merely by generating their hashes. Other Python
versions, macOS, Windows, aarch64, musl, and broad manylinux portability are
untested. Core Biosimulant support is independent of this Tellurium matrix.

The Koo2013 snapshot JSON is present, but its referenced
`models-done/models-biomechanics/labs/koo2013-integrated` source is absent from
this workspace. Its exact comparison is blocked until the original source is
available. Do not reconstruct it or regenerate the baseline to obtain a pass.

The canonical Tellurium adapter currently has empty snapshot/no-op restore
behavior. Tests characterize this existing limitation; they do not establish
checkpoint continuation. The independent RoadRunner reproduction and its
checkpoint tests remain unchanged. Its qualified conclusion remains a partial
reproduction.

Rollback consists of removing the local opt-in, retaining the preceding runtime
wheel/environment, and reverting the focused staging commits if needed.
Containers use `--rm`; remove only the dedicated local image and disposable
test caches after retaining evidence. No cloud resources are created.

Before pushing, inspect actual deployment triggers; repository workflow YAML
does not establish provider-side autodeploy behavior. Artifact publication or
hosting, isolated Modal builds/jobs, staging deployment and end-to-end staging
qualification each need separate explicit approval identifying the environment,
commits, hashes, cost ceiling, cleanup and rollback. No production action is
included.
