# Model Requirements Specification: Deterministic Repressilator Reproduction

## 1. Document Control
| Field | Value |
|---|---|
| MRS identifier | REPR-MRS-001 |
| Version | 1.0.0 |
| Status | Approved |
| Last revised | 2026-10-20 |
| Selected type or types | Mechanistic |
| Model owner | Codex, implementing for demi |
| Intended-use owner | demi |
| Validation owner | demi; scientific acceptance requires human review |

Approval basis: demi authorized the four-stage approach in Codex on 2026-10-20. These detailed requirements are governed by session contract 1f71b00c-de96-4f8c-a344-4c3fbbe01901, digest e9db5ad41d61b17e1e6a869dd0c94c2bcb06eda422559ff0a36f1c6bc0fb9bfa, approved automatically under the user's Hand off policy and applied through Biosimulant. This is approval of execution requirements, not a human scientific acceptance decision or permission for infrastructure changes.

### Revision History
| Version | Date | Change |
|---|---|---|
| 1.0.0 | 2026-10-20 | Initial requirements transcribed from the approved session contract before simulation. |

## 2. Executive Summary
Determine whether a source-faithful implementation of the six deterministic repressilator ODEs and reported parameters reproduces Figure 1c, left, of Elowitz and Leibler (2000), with comparison to the exact curated BioModels reference.
The deliverable may report reproduction, partial reproduction or non-reproduction. Do not tune parameters to obtain the desired answer.

## 3. Biological Objective and Intended Use
A reproducible computational reproduction study and reusable private Biosimulant Lab. The conclusion may be reproduced, partially reproduced or not reproduced; successful software execution is not biological validation.
The decision is whether the deterministic published mathematical behavior can be reproduced with a traceable implementation. It does not establish biological validity in living bacteria.

## 4. Model Definition and Scope
Deterministic symmetric three-gene circuit only. Excludes stochastic simulations, experimental bacterial measurements, parameter fitting to force agreement, public sharing, publication, hosting, deployment, provider changes and infrastructure configuration. Preserve reported and derived parameters separately; keep physical minutes/copies per cell distinct from Box 1 rescaled time and states. Missing figure initial conditions or machine-readable trajectories must be disclosed and may limit the conclusion.
Each scientific invocation is a finite evaluation of a complete deterministic time course, followed by comparisons. The reference is an evolving six-state system. No data assimilation, training or identification is requested.

## 5. Inputs
Immutable reference: BioModels BIOMD0000000012 revision-3, SHA-256 d535c84bc5fe68b01728cac5c53f8fed971176e3f0f2367a9ba1e9c043b11a2d, 43901 bytes.
Paper Box 1: fully induced/repressed transcription 0.5/0.0005 transcripts per second, translation efficiency 20 proteins per transcript, Hill coefficient 2, mRNA half-life 2 min, protein half-life 10 min, repression scale 40 monomers per cell.
Reference initial state in order X,Y,Z,PX,PY,PZ: 0,20,0,0,0,0 molecules per cell. This is a curated-reference initialization; the paper does not establish that this was its figure initialization.
Reject missing or changed authoritative bytes and nonfinite or negative initial states. Do not impute missing figure data.

## 6. Outputs
All six trajectories with physical minutes and molecules per cell; protein-only plots labelled copies per cell; per-protein periods, peak-to-trough amplitudes and relative peak phases; numerical/reference errors and boolean checks derived from outputs; figure comparison with stated uncertainty; run/environment/settings provenance.
A useful reusable time-series view must plot all three actual computed protein trajectories and agree with retained typed outputs. Numerical uncertainty comes from solver comparison; figure uncertainty comes from source resolution. No probabilistic biological confidence interval is claimed.

## 7. Data, Assay, and Evidence Specification
There is no wet-lab dataset, replication design or train/test split: this is deterministic model reproduction, with no fitting. The exact unmodified SBML is the executable comparator. The paper is copyrighted; retain attribution and a private evidence copy, and do not publicly redistribute its figure. The SBML's own notes explicitly dedicate the encoded model under CC0.
Use executable SBML rules over contradictory rounded parameter values or prose, preserving all discrepancies. No prospective biological validation is in scope.

## 8. Ground Truth or Estimand
The estimands are deterministic period, peak-to-trough amplitude and relative timing of the three proteins. The curated model is a computational reference, not experimental ground truth. Figure 1c left is the reproduction target; lack of raw figure data limits exact waveform matching.

## 9. Generalization Domain
Only the reported symmetric repressilator parameter set and curated initial state, 0–2000 physical minutes. No claims about perturbations, parameter identification, stochastic behavior, bacteria, clinical use or other genetic circuits.

## 10. Performance Requirements
All tolerances below are project requirements frozen before computed outcomes:
| MRS-001 | Source fidelity | Preserve paper identity and locations of Figure 1c/Box 1, exact BioModels accession/revision/artifact checksum, retrieval provenance and reuse terms. Map all six equations, repression direction, parameters, units and initial conditions to sources or explicit assumptions. Independently derive and document physical-to-rescaled conversions, and report rather than silently reconcile discrepancies. |
| MRS-002 | Frozen comparison protocol | Before evaluating simulation outputs, freeze the MRS/MTS, code revision, source artifacts, parameters, initial state, solver/dependency versions, sampling grid, transient window, metric definitions and failure handling. Report first 0–1000 min to match the figure; evaluate sustained behavior over 1000–2000 min, extending only by a recorded approved protocol revision if fewer than 3 complete cycles are available. Define amplitude as peak-to-trough and period as median same-species peak interval; define relative phase by circular peak-time differences divided by period. |
| MRS-003 | Numerical robustness | All six states must be finite with no values below -1e-8 in their reported physical units. Execute both the primary solver and an independently configured tighter/different solver; require protein-trajectory disagreement <=0.1% of primary post-transient peak-to-trough range and period/amplitude differences <=0.1%. These are project-selected verification tolerances, not paper claims. Failures must remain visible. |
| MRS-004 | Curated reference comparison | Execute the exact unmodified curated SBML reference and source-faithful six-ODE implementation using matched physical parameters and initial conditions where supported. Compare all proteins: period and peak-to-trough amplitude within 1%, relative phase within 0.01 cycle, and trajectory normalized RMSE <=1% of reference peak-to-trough range. Permit at most one explicitly reported common time-origin shift when source initial conditions differ; never independently align each protein or fit parameters to obtain agreement. Thresholds are project-selected. |
| MRS-005 | Figure agreement assessment | Assess period, protein peak-to-trough amplitude and relative ordering/timing against Figure 1c left with documented figure-reading/digitization uncertainty. Project-selected figure thresholds: period/amplitude discrepancies <=10% and phase discrepancy <=0.05 cycle, only if figure resolution supports those comparisons. At least 3 complete tail cycles with <=1% amplitude drift support sustained-oscillation classification. Absent exact figure initial conditions or raw numerical figure data, explicitly limit waveform/phase claims; reference agreement alone cannot establish exact figure reproduction. A documented partial or negative outcome is valid study reporting. |
| MRS-006 | Reproducible evidence package | Retain a private immutable Lab with six-state/protein time-series and metric outputs, computed protein time-series visualization labelled in minutes and copies per cell, executable verification checks, environment/settings/source identities, exact run/revision/artifact provenance, and a report with classification, discrepancy explanations, limitations and open questions. Verify saved output bytes against size and SHA-256 before interpretation. Distinguish local evidence, managed runs and signed Passports; neither run completion nor a Passport self-certifies scientific validity. Keep cumulative compute within the existing USD 3 limit and obtain any action-specific approvals. |

## 11. Validation Plan
Lock code, dependencies, source bytes, settings and thresholds before managed execution. Reference calculations use unmodified SBML, while the independent six-ODE implementation uses the physical rate law derived from Box 1. Evaluate every protein; retain negative outcomes and failures without post-selection.
Figure comparison must declare uncertainty and missing initial conditions. Do not manufacture figure readings or claim a quantitative figure pass when source resolution is insufficient. If fewer than three complete cycles occur in the tail, report insufficient evidence rather than extending the protocol silently.
Require independent solver comparison and a nonzero-time reference snapshot/restore continuation check. No mass-conservation requirement applies to this open synthesis/degradation network.

## 12. Acceptance and Traceability
| Requirement | Verification | Evidence | Owner | Acceptance status |
|---|---|---|---|---|
| MRS-001 | Source fidelity inspection or executable comparison | Immutable Lab and retained comparison output | Codex prepares; demi reviews | Not evaluated |
| MRS-002 | Frozen comparison protocol inspection or executable comparison | Immutable Lab and retained comparison output | Codex prepares; demi reviews | Not evaluated |
| MRS-003 | Numerical robustness inspection or executable comparison | Immutable Lab and retained comparison output | Codex prepares; demi reviews | Not evaluated |
| MRS-004 | Curated reference comparison inspection or executable comparison | Immutable Lab and retained comparison output | Codex prepares; demi reviews | Not evaluated |
| MRS-005 | Figure agreement assessment inspection or executable comparison | Immutable Lab and retained comparison output | Codex prepares; demi reviews | Not evaluated |
| MRS-006 | Reproducible evidence package inspection or executable comparison | Immutable Lab and retained comparison output | Codex prepares; demi reviews | Not evaluated |
A scientific criterion is accepted only through human review in the session. Agent progress, successful execution and technical Passports do not establish it. A partial/negative report can fulfill faithful study reporting without being a successful reproduction.

## 13. Constraints, Risks, and Governance
Cumulative session compute limit USD 3.00, revision-1 policy agent_decides/Hand off; recheck before execution. Obtain action-specific approval where required. No deployment, infrastructure configuration, public release or standing permissions are authorized.
Risks: rescaling errors, half-life/lifetime confusion, reference prose inconsistencies, unspecified paper initialization, low-resolution figure, solver phase drift and dependency unavailability. Preserve source discrepancies and exact settings; do not hide a failed tolerance.
Requirements changes require a versioned session contract change, never an unrecorded MTS edit.

## 14. Reproducibility, Deliverables, and Handoff
Deliver immutable source artifacts, source mapping, this MRS, the MTS, code/manifests/tests, settings and environment versions, reusable visualization and typed results, run and artifact identities and a qualified report. Store progress and results in the existing session. Local evidence remains separate from managed Runs and Passports.
Retain failed versions; no deployment or publication is part of handoff. Source and scientific code are prepared on staging, synchronized with main baseline 28f572be47db030a3894d6efe4e0c2289e2dd8fe; preserve exact source commit/revision at run freeze.

## 15. Mechanistic Module
Three mRNAs and three repressor proteins in one effective cell. Repression cycle: cI suppresses lacI, LacI suppresses tetR, TetR suppresses cI.
Box 1: dm_i/dtau=-m_i+alpha/(1+p_j^n)+alpha0 and dp_i/dtau=beta*(m_i-p_i).
Physical rates: dM_i/dt=r0+(rmax-r0)/(1+(P_j/KM)^n)-gamma_m*M_i; dP_i/dt=eff*gamma_m*M_i-gamma_p*P_i.
gamma_m=ln(2)/2 min^-1; gamma_p=ln(2)/10 min^-1; r0=0.03, rmax=30 transcripts/min. Conversion: tau=gamma_m*t; p=P/KM; m=eff*gamma_m*M/(gamma_p*KM). Thus beta=0.2; alpha=(rmax-r0)*eff/(gamma_p*KM); alpha0=r0*eff/(gamma_p*KM). This transformation follows algebra, not the inconsistent mRNA-scaling prose in the curated notes.
No parameter fitting, identifiability claim, stochastic terms or interventions. Nonnegativity, accuracy, and source-faithful numerical comparison govern verification.

## 16. Milestones and Responsibilities
Codex verifies sources, prepares design/code and produces evidence. demi owns scientific review and protected approvals. No independent reviewer is invented. Order: source readiness, design freeze, verified execution, report and human review.

## 17. Open Questions and Decisions
Paper figure initial conditions and raw trajectories are unavailable in the inspected paper; demi reviews the resulting limit on exact figure claims. This does not authorize replacing the stated target.
Decision: use the exact SBML executable assignment/kinetic rules rather than obsolete initial alpha values or explanatory-note arithmetic. Capture this in SOURCE_REVIEW.md.

## 18. References and Glossary
- Elowitz & Leibler (2000), DOI 10.1038/35002125, Figure 1c left and Box 1, https://www.dna.caltech.edu/courses/cs191/paperscs191/ElowitzRepressilator2000.pdf
- BioModels BIOMD0000000012 revision-3, https://www.biomodels.org/BIOMD0000000012
- Existing session https://studio.biosimulant.com/c/4001d817-7a6d-4ab6-8d15-a75685519d17
Lifetime means inverse decay rate; half-life means ln(2) divided by decay rate. Phase is a fraction of one oscillation cycle.
