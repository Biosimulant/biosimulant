# Source review — 2026-10-20

## Exact sources
Paper: Elowitz & Leibler, Nature 403, 335–338 (2000), DOI 10.1038/35002125. The supplied Oregon State URL returned 404. The Caltech copy was downloaded from https://www.dna.caltech.edu/courses/cs191/paperscs191/ElowitzRepressilator2000.pdf ; 367593 bytes, SHA-256 b0c5133ec17296b92e47715027a31bdee19272953d85014937709154c58c3ace. Figure 1c left is on printed p336; Box 1 on p337. Copyright Macmillan Magazines Ltd; private research evidence only.
BioModels BIOMD0000000012 revision-3, retained through biology_fetch as resource 99d21f02-4e83-4d61-b817-5a5b5578ea6d. Artifact 43901 bytes; SHA-256 d535c84bc5fe68b01728cac5c53f8fed971176e3f0f2367a9ba1e9c043b11a2d. An independent download from https://www.ebi.ac.uk/biomodels/model/download/BIOMD0000000012?filename=BIOMD0000000012_url.xml matches exactly. The artifact notes explicitly dedicate the encoded model under CC0.

## Inspection
Native time is minute (SBML time unit multiplier 60 second); substance is item and cell volume is 1 cubic micrometre. Species use hasOnlySubstanceUnits=true. Thus values are molecule amounts in an effective single cell, reported as copies per cell, not molar concentrations.
Order X,Y,Z,PX,PY,PZ = LacI mRNA,TetR mRNA,cI mRNA,LacI,TetR,cI proteins. Native initial amounts 0,20,0,0,0,0. The paper does not specify exact figure initial conditions or raw time-course values.
All assignment rules and kinetic laws were read from the checksum-matched XML. Repression: PZ inhibits X; PX inhibits Y; PY inhibits Z. Production/degradation match the physical six-ODE equations in MRS.md. No events, parameter fitting or interventions are introduced.

## Executable rules override inconsistent notes
a_tr = (ps_a-ps_0)*60 = 29.97 transcripts/min, a0_tr = ps_0*60 = 0.03 transcripts/min. The notes display an inconsistent 'ps_a*60 = 29.97'; their executable subtraction is authoritative.
kd_mRNA=ln(2)/2; kd_prot=ln(2)/10; k_tl=20*kd_mRNA. alpha and alpha0 are assignment-rule targets: their XML initial value fields 216.404 and 0.2164 are not authoritative runtime values. Read the evaluated rules at runtime.
Dimensionless m must be k_tl*M/(kd_prot*KM)=2.5*M for Box 1 dp/dtau=beta*(m-p). The reference explanatory note m=M/eff is inconsistent with the executable physical model; do not use it. Dimensionless p=P/40; tau=kd_mRNA*t; beta=0.2.
Keep original XML unchanged.

## Figure evidence and limits
The complete relevant PDF pages were read as text and Figure 1c was visually inspected after rendering. It shows a 0–1000 min window, a 0–6000 copies/cell axis, three regular out-of-phase oscillations and a growing initial transient. Peak heights are far below the axis maximum; 6000 is not an oscillation amplitude.
Coarse manual readings, frozen before simulation: period about 140 min with roughly ±10 min reading uncertainty, mature peak-to-trough range about 2300 copies/cell with roughly ±200 uncertainty, and successive species peaks spaced about one-third cycle. These are approximate figure readings, not source numerical data. No exact pointwise figure comparison or absolute phase claim is supportable from this evidence alone. A numerical reference match does not remove that limitation.
