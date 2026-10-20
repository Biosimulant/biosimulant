# Modeling Technical Specification: Deterministic Repressilator Reproduction

## 1. Document Control
| Field | Value |
|---|---|
| MTS identifier | REPR-MTS-001 |
| Version | 1.1.0 |
| Status | In review |
| Last revised | 2026-10-20 |
| Source MRS | REPR-MRS-001 version 1.0.0, Approved for execution requirements under the recorded session contract |
| Biosimulant fit | Adapter fit |
| Model owner | Codex for demi |
| Scientific acceptance | demi, after evidence review |

### Revision History
1.1.0, 2026-10-20: user explicitly approved the RoadRunner-only adapter and Modal rerun within the remaining budget after the first run failed on Tellurium transitive-wheel platform metadata. The exact source SBML and MRS acceptance requirements are unchanged.
1.0.0, 2026-10-20: design frozen before executing scientific calculations. The installed MTS starter rejected its protected template checksum; the template was not changed. This independently authored specification follows the required sections and is checked with validate_mts.py. Its status remains In review, rather than claiming human design approval.

## 2. Technical Decision Summary
MTS-001: retain unmodified SBML behind a RoadRunner-only BioModule temporal adapter; independently solve the physical six-ODE equations with SciPy DOP853 and Radau. A finite after-run analysis consumes the reference trajectory through typed wiring and emits trajectories, metrics, source/environment identities and computed timeseries visuals.
The session contract approves bounded work under Hand off; scientific criteria remain human-reviewed. The user explicitly authorized Modal execution of this RoadRunner-only revision within the remaining session budget. Any server-required authenticated action review still applies.

## 3. Scope, Boundaries, and Assumptions
Exact MRS intended use and scope apply. Native time is minutes and states are molecules/cell, not nM. The curated initial state is not asserted to be the paper's figure initial state. No parameter or source mutation, fitting, stochasticity, hosting or deployment.
Policy preference agent_decides permits cloud; local CLI lacks its required capability command and local scientific dependencies are unavailable. Select managed compute with a prepared exact profile; no silent local/cloud fallback.

## 4. MRS Traceability
| MRS requirement | Implementation | Test/protocol | Evidence | Accountable owner |
|---|---|---|---|---|
| MRS-001: Source fidelity | Source hash and rule/unit inspection | Source review and SHA-256 test | SOURCE_REVIEW.md and results comparison payload | Codex prepares; demi accepts |
| MRS-002: Frozen comparison protocol | Locked settings, code, MRS and MTS | Immutable revision + environment capture | SOURCE_REVIEW.md and results comparison payload | Codex prepares; demi accepts |
| MRS-003: Numerical robustness | Independent DOP853/Radau trajectories and numerical checks | NUM-001 checks from computed values | SOURCE_REVIEW.md and results comparison payload | Codex prepares; demi accepts |
| MRS-004: Curated reference comparison | Canonical SBML trajectory against independent equations | REF-001 comparisons for all proteins | SOURCE_REVIEW.md and results comparison payload | Codex prepares; demi accepts |
| MRS-005: Figure agreement assessment | Frozen approximate figure evidence and explicit limits | FIG-001 bounded comparison; no exact waveform claim | SOURCE_REVIEW.md and results comparison payload | Codex prepares; demi accepts |
| MRS-006: Reproducible evidence package | Typed terminals, executable tests and computed visuals | DEL-001 artifact verification + suite result | SOURCE_REVIEW.md and results comparison payload | Codex prepares; demi accepts |
The unchanged numeric criteria are: -1e-8 lower state floor; <=0.1% numerical trajectory/period/amplitude disagreement; <=1% reference period/amplitude/normalized RMSE, <=0.01-cycle reference phase; figure <=10% period/amplitude and <=0.05-cycle phase if resolution supports them; >=3 complete tail cycles with <=1% amplitude drift. No threshold is changed here.

## 5. Architecture and Data Flow
reference (EACH_WINDOW via execute(inputs, *, context)) -> comparison (ONCE_AFTER_RUN).
Lab duration=2000 min, communication_step=2000 min: one temporal window, with internal native output sampling every 0.5 min (4001 observations). The after-run dependency layer sees the completed reference trajectory without a one-window lag. No settle steps or events are needed.
Comparison independently computes two full-horizon physical trajectories once. It does not evolve state or solve equations during visualize().
Preserve source original at evidence/BIOMD0000000012_url.xml and copy identical bytes into models/reference/artifacts/model.xml because adapter relative paths resolve from its model root. Hash both copies.

## 6. Data Implementation
Reference initial X,Y,Z,PX,PY,PZ=(0,20,0,0,0,0). Source artifact SHA-256 d535c84bc5fe68b01728cac5c53f8fed971176e3f0f2367a9ba1e9c043b11a2d, 43901 bytes.
No data partitions or learned preprocessing: no fitting. Derived rates are analytic from Box 1 and checked against executable SBML rules. Preserve paper PDF privately and record figure-reading uncertainty before computing.
SOURCE_REVIEW.md owns the source discrepancies; executable SBML assignment targets override initial value fields.

## 7. Input, Output, and Error Contracts
reference.trajectory: record schema {payload:json}, primary quantity copies/cell, nested time_unit=min and six named series. Declare explicit emitted units on trajectory/state ports and field units on the mixed summary; preserve existing Lab wiring.
comparison.reference: identical record schema accepting copies/cell.
comparison.trajectories: record {payload:json}, primary quantity copies/cell, with time_min and six-state arrays for primary/tighter/reference. comparison.report: mixed terminal record {payload:json} emitted unit 1, field units and definitions included in payload.
All arrays must have 4001 finite rows with times 0 through 2000. Missing inputs, checksum mismatch, wrong time grid, wrong species order and malformed trajectories raise an explicit execution failure; no fake result. Scientific tolerance failures are retained booleans and discrepancies, not erased.

## 8. Model Method and Uncertainty
Physical equations and rescaling are exactly those in the MRS. Primary DOP853: rtol=1e-9, atol=1e-11, max_step=0.5 min. Tighter independent Radau: rtol=1e-11, atol=1e-13, max_step=0.25 min. Native CVODE: rtol=1e-9, atol=1e-11, maximum_time_step=0.5 min, no source changes.
Evaluate 1000–2000 min. Locate local maxima and minima using scipy.signal.find_peaks; refine peak times and peak/trough values by local quadratic interpolation. Require at least 3 full cycles; each cycle amplitude uses its bracketing peaks and trough. Period is median peak interval and amplitude is median complete-cycle peak-to-trough range. Amplitude drift=(max-min)/median of complete-cycle amplitudes.
Phase is the circular difference of nearest cyclic peak times normalized by period, anchored to LacI; compare circular differences. No time-origin shift is applied for matched initial conditions. Numerical error uses maximum pointwise absolute difference divided by primary tail amplitude; reference RMSE uses reference amplitude.
Figure manual estimates are approximate: 140 ±10 min period, 2300 ±200 copies/cell peak-to-trough and roughly one-third-cycle separations. Do not interpret coarse agreement as exact waveform reproduction. Figure uncertainty overlapping a threshold yields indeterminate rather than a pass. Missing figure initialization prevents exact absolute-phase claims.

## 9. Biosimulant Fit Assessment
Outcome: Adapter fit
Immutable SBML remains the authoritative comparator; the RoadRunner-only wrapper supplies reusable temporal state and typed trajectories. The custom comparison component adds an independent implementation, metrics and reusable visualization. Provenance and durable managed outputs serve the requested reproducible Lab; a notebook alone would not fulfill that delivery.

## 10. Biosimulant Realization
reference subclasses BioModule with ExecutionPolicy.EACH_WINDOW and execute(inputs, *, context). setup() checks the original SHA-256 and constructs roadrunner.RoadRunner from that unchanged file, explicitly selecting CVODE and the existing tolerances. simulate_to() calls the native engine on the fixed 0.5-min grid and preserves all six native species amounts. It never rewrites SBML, substitutes hand-coded reference equations, disables dependency checks or imports Tellurium. Its typed state/summary/trajectory outputs preserve the existing scientific interface. Snapshot/restore captures native species amounts, time and retained trajectory for this event-free model; tests restore at 1000 min and continue to 2000 min against uninterrupted native execution. This user-authorized design replaces the earlier canonical Tellurium integration; it is not represented as that canonical adapter.
comparison subclasses BioModule, execution_policy=ExecutionPolicy.ONCE_AFTER_RUN; repeat once_after_run in model.yaml. inputs() and outputs() use SignalSpec.record and matching canonical IO. visualize() reads retained computed trajectories and returns portable schema_version=1 timeseries for all three proteins, labelled Time/min and Protein/copies per cell; downsample visuals to 2 min while retaining full 0.5 min typed data. Lab visualization requires a comparison timeseries.
Pin model dependencies: biosimulant==0.0.37, libroadrunner==2.8.0, scipy==1.15.3, numpy==2.2.6. Retain full resolved importlib.metadata versions in the scientific report. Both model manifests carry identical direct dependency pins; the environment resolver owns transitive resolution.

## 11. Verification and Test Design
Source tests verify exact bytes and initial state. Equation tests verify rate conversion and zero/basal production cases from actual functions. Contract tests instantiate typed ports, check phase policy and visual payload equality with computed arrays. Reference tests use direct native RoadRunner execution and checkpoint/restore at nonzero time. Numerical tests run both declared solvers and compute errors, not hardcoded outcomes.
Component tests are discoverable unittest files. The comparison component executes the suite in the managed dependency interpreter with python -m unittest discover -s tests -v and retains actual counts/status and diagnostic output. The suite tests helper computations and reference lifecycle, not comparison.execute(), avoiding recursive test invocation.
At this adapter-revision freeze, scientific tests are unexecuted: the host lacks a capability-verified local runtime and scientific dependencies. Static document validation is not scientific acceptance evidence. The exact managed execution is also the first acceptance-test execution and is described accordingly when requesting approval.

## 12. Reproducibility, Security, Performance, and Cost
Frozen snapshot is the immutable workspace revision used by run_prepare. Staging baseline commit 28f572be47db030a3894d6efe4e0c2289e2dd8fe; include new source commit when available. Record Python/platform and all installed distribution versions.
No stochastic seeds or network calls in scientific code. Read-only pinned files only. Provider discovered live as Modal; select free CPU profile if it meets resolved requirements, timeout <=300s, aggregate CPU >=0.5/RAM>=512MiB. Use prepare estimate and actual session remaining budget, never assume unknown cost is zero. The existing session USD 3 cap governs all retries. At this revision start USD 0.001122 was spent, nothing held, and USD 2.998878 remained; refresh before every execution. No hosting/deployment/provider configuration is changed.

## 13. Model Lock, Release, and Operations
Lock code/settings before comparing outcomes; failures can motivate a new implementation revision with unchanged MRS thresholds and retained failed evidence. No publication/release/deployment authorized. Verify workspace-results bytes/size/SHA-256 and actual terminal outputs/visuals before reporting. Reopen data through authorized run access. Leave the session unfinished where human scientific review is required.

## 14. Mechanistic Technical Module
State order and repression mappings are explicit in the MRS and source review. No conservation invariant is imposed on this open synthesis/degradation system. Finite/nonnegative behavior, tolerance sensitivity, native-reference parity and snapshot continuation are separate checks.
A different numerical outcome may represent an implementation error, numerical failure or source discrepancy; classify it before changing code. No optimizer or fitted parameter path exists.

## 15. Risks, Decisions, and Open Questions
Figure raw data and exact initialization are not available; no invented values will fill that gap. The coarse readings support bounded comparison only. Full independent biological validation remains outside scope.
The earlier MTS starter's integrity rejection affected scaffolding only; no protected skill file is modified, and this authored document must pass structural and semantic review before saved execution design is used.

## 16. References and Glossary
REPR-MRS-001 v1.0.0, SOURCE_REVIEW.md, exact BioModels artifact and Elowitz & Leibler (2000), DOI 10.1038/35002125.
DOP853 and Radau are independent integration methods; CVODE is the native reference solver. A Passport is technical evidence rather than universal scientific validity.
