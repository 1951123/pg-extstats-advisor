# M2.28 — Acquisition-fidelity root-cause study

M2.27b established that the deterministic full-stream row reservoir does not
preserve the native PostgreSQL target-selection structure on DMV
(`native_analyze_equivalent = false` and
`target_selection_fidelity_qualified = false`). M2.28 asks why, without
starting another search or introducing a sampler.

## Protocol

The study reuses the frozen M2.26 native samples, the sealed M2.27a reservoir
Bundle v2, and the M2.27b canonical-A vectors and top-contributor ranking. It
replays only those persisted samples in the isolated source-built PostgreSQL
16.14 advisor cluster. No production relation, production capture, TABLESAMPLE,
extstats, deployment, search, join, or source change is used.

The primary set is the frozen reservoir T300-A → T1000-A top 20. The full
workload contains 1,963 positive-truth queries. The top-20 q-error reduction is
34,690.895, while the full canonical-A reservoir objective reduction is
33,374.166; the resulting share is 103.95%. This is above 100% because the
remaining queries have a net offsetting regression. The top 1, 3, and 10
shares are 65.91%, 97.65%, and 103.85%, respectively.

## What the ordinary statistics show

The top queries are conjunction-heavy single-table selections. Across the top
20, the most frequent columns are `body_type` (17 queries),
`registration_class` (16), `county` (15), and `record_type` (14). The
membership artifact records 670/707/787 exact-MCV predicate constants for
native T100/T300/T1000 and 656/729/795 for reservoir T100/T300/T1000. Thus the
reservoir T300→T1000 improvement is not explained by a simple story in which
the critical constants for dmv.1037, dmv.179, or dmv.1100 are absent at T300
and enter the MCV list at T1000: those constants are already represented in
the relevant states. The dominant changes are instead MCV frequencies, MCV
set composition for non-critical values, and `n_distinct`/histogram state.

For the three largest contributors:

* **dmv.1037** changes from reservoir estimate 333,979 (q-error
  55,663.17) at T300 to 201,988 (33,664.67) at T1000. Its six predicate
  columns remain MCV-represented; the largest ordinary-stat changes include
  `body_type` MCV count 33→41 and `n_distinct` 51→55, and
  `registration_class` 36→44 and 51→62. There is no single membership
  transition that uniquely explains the estimate change.
* **dmv.179** changes from estimate 233,831 (q-error 19,485.92) to estimate
  136,254 (q-error 11,354.5). Its predicate constants remain MCV-represented. It
  shares the `body_type`, `record_type`, `registration_class`, and `state`
  ordinary-statistics changes with dmv.1037, but also has its own suspension
  predicate; this is shared-state sensitivity, not proof of one causal field.
* **dmv.1100** changes from 5,927 (q-error 5,927) to 3,467 (3,467). Its
  county values are represented in both states (all 63 county values are MCVs);
  its distinctive evidence is the county-frequency change together with the
  shared body/registration/record-type changes. It should not be collapsed
  into a claim about a missing rare-value MCV.

The values above are planner estimates and q-errors from the frozen canonical-A
replay. Detailed field-level values, predicate membership, MCV overlap,
frequency deltas, and query decomposition are in the tracked CSV/JSON
artifacts in `experiments/dmv-m2-28-acquisition-root-cause/`.

## Global and trimmed comparisons

Native and reservoir MCV sets are not identical. Queried-constant
representation as `(both, native-only, reservoir-only, neither)` is
`(142,3,2,59)` at T100, `(155,2,4,45)` at T300, and `(173,4,3,26)` at
T1000. MCV frequency differences shrink in the aggregate at larger targets,
but target-specific column-set and `n_distinct` differences remain. The
workload is dominated by equality/IN predicates, and the PostgreSQL source
path checks per-column MCVs before using the non-MCV distinct-value fallback;
histograms are therefore not the leading explanation for the three primary
queries.

Removing the same fixed reservoir-T300→T1000 top-20 query IDs from both
mechanisms does not make the canonical-A structures agree: native trimmed
ordering is `300 < 1000 < 100`, while reservoir trimmed ordering is
`300 < 100 < 1000`. The extreme queries therefore explain most of the
aggregate movement, but not all of the target-order disagreement.

For context, the M2.27b mean-across-A/B/C ordering remains native
`1000 < 300 < 100` versus reservoir `1000 < 100 < 300`; the trimmed result is
reported separately because this root-cause replay deliberately follows the
canonical-A T300→T1000 contrast.

## Source-grounded interpretation

PostgreSQL 16.14 `acquire_sample_rows` in
`src/backend/commands/analyze.c:1104–1345` first samples blocks with
`BlockSampler`, scans accepted blocks, then applies Vitter row-reservoir
selection and sorts the selected tuples by physical position. The same source
documents that the block representation is not perfectly uniform. Ordinary
scalar statistics use `compute_scalar_stats` with a `300 * target` minimum
sample at `:1883–1950`; per-column MCV tracking is in
`compute_distinct_stats` at `:2038–2051`. Equality selectivity in
`src/backend/utils/adt/selfuncs.c:290–448` first searches MCV values and then
uses remaining mass, null fraction, and estimated other distinct values.

This establishes a plausible block-sensitive variable and a concrete
ordinary-statistics path, but M2.28 does not isolate block layout from
stream-order/RNG effects, target-dependent MCV thresholds, or objective
concentration. Accordingly, it does **not** justify implementing a block-aware
prototype as the next step. A future prototype should be considered only
after an experiment that separates those alternatives using the existing
fidelity framework.

## Scope and status

Established before M2.28: reservoir target-selection fidelity fails. M2.28:
ordinary-statistics divergence is demonstrated and the primary extreme-query
mechanism is narrowed to frequency/set/`n_distinct` changes rather than a
single missing-MCV event. Not established: that physical block structure is
the cause. No new sampling mechanism was implemented.
