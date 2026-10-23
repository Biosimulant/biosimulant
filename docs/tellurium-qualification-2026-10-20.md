# Local Tellurium qualification — 2026-10-20

The opt-in local packaging candidate passed the managed dependency path and
scientific checks below. Default cloud installations remain unchanged. No
provider build/job, deployment, push, artifact publication or production action
was performed by this work.

Implementation commits on staging:

- Runtime: `5f5fa09e7d02b799fd0257c794a5b796663764a6`, based on
  `56ed685dd060013e58d4a2dba99f3dcb5ea64811`.
- Backend recipe: `3cbf2229560667a2d8a939760af3bc81ee3497fb`, based on
  `76262ee56f87de20c23f06a22db72db07a0b6a7e`.

Both staging baselines contained current remote main when work began; syncing
was a no-op and preserved staging-only commits. During work, backend
`origin/staging` advanced externally to
`f33faf79a89cc43d6d71332f2b3eefbe47e2e6a2`. The local backend is one commit ahead
and one behind that reference. This work neither merged nor pushed the external
commit. Integrate and retest those changes before any future approved push.

## Results

Environment: Docker Linux x86_64 under OrbStack emulation on macOS arm64;
CPython 3.12.3, glibc 2.36, uv 0.13.4. Base manifest:
`sha256:afc139a0a640942491ec481ad8dda10f2c5b753f5c969393b12480155fe15a63`.
The exact local image built from the checked-in Dockerfile was
`sha256:ee214ca8ed584296e2250d6b5d763c1462b37b220d68581dee56116a44750956`.

| Check | Result |
| --- | --- |
| Original checksum-pinned wheels | Install succeeds; actual uv check rejects both package platform tags |
| Wrong architecture, conflicting exact pin, missing dependencies | All rejected |
| Corrected artifacts through actual isolated environment | Install/check/import pass |
| Installed runtime wheel, real engine/science | 5 passed |
| Source SBML and science tests | 20 passed |
| Linux runtime/isolation regressions, including built-wheel fixtures | 57 passed |
| Unchanged RoadRunner reproduction tests, including checkpoint continuation | 7 passed |
| Backend provider/recipe contracts | 67 passed; no provider execution |
| Required host commit hook, full suite | Passed; 82.75% coverage exceeds required 82% |

The full host suite collected 805 tests. The successful hook suppresses pytest
output, so its final passed/skipped split was not retained; no split is inferred.
The first hook attempt found a missing explicit allowlist entry for the new
adapter lifecycle test. Adding that single test-file entry resolved the policy
failure; no scientific tolerances, source SBML or adapter behavior changed.

Native ELF architecture and shared-library inspection passed; neither library
had unresolved dependencies in this environment. This is no manylinux claim.
Each derivative rebuilt byte-for-byte, preserving every Python/native payload
and ZIP member permission. Only WHEEL/RECORD contents changed.

The final runtime wheel is `biosimulant-0.0.37-py3-none-any.whl`, SHA-256
`416e90d566180b1e89ce2bdd258d920174fb37612c05bcd6b38965388149072d`.
Its changed product modules exactly match staging source. Separate scientific
reports verify installed-wheel and source imports and record CVODE settings.
All frozen finite/nonnegative/sustained/numerical/reference checks passed. The
scientific conclusion remains **partial reproduction**, with figure comparison
uncertainty preserved.

## Limits, evidence and rollback

CPython 3.12.3/Linux x86_64/glibc 2.36 is locally qualified for this candidate.
Generated CPython 3.11 wheels are untested. Other platforms/Python versions,
provider images, deployment and end-to-end staging execution are unqualified.
The Koo2013 source lab is absent, so its exact snapshot comparison remains
blocked. Canonical Tellurium checkpoint continuation remains unsupported;
the tests explicitly characterize that existing limitation.

See [reproduction and artifact instructions](tellurium-packaging.md), the
[machine-readable summary](evidence/tellurium-2026-10-20/summary.json), and
[exact commands/environment cleanup](evidence/tellurium-2026-10-20/qualification.json).
The evidence directory also retains source/candidate hashes, deterministic
rebuild verification, resolved inventories, native inspection and separate
scientific reports.

The artifact bundle is retained outside the repositories at workspace-relative
`.local-artifacts/tellurium-2026-10-20`. All three disposable Python environments,
both named containers, dedicated images and temporary installer caches were
removed. Ordinary shared Docker build cache was retained; no global prune was
run. No cloud resources were created.

Prior scientific/deployed environments were not replaced. Roll back by removing
the local opt-in and reverting the focused staging commits while retaining the
baseline commits and original wheels. Further artifact distribution, provider
qualification, staging deployment and any deployment-triggering push require
their separate environment-specific approvals. Production-related external
changes were observed; this report makes no claim about global production state.
