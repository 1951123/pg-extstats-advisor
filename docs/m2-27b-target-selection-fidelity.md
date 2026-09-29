# M2.27b: native-vs-reservoir target-selection fidelity

M2.27b compares the frozen M2.26 native PostgreSQL 16.14 characterization
with the M2.27a same-snapshot production-compatible reservoir characterization.
It reuses the existing samples and Bundle v2 cache; it does not recapture the
production table, change the workload/truth, create extended statistics, or run
any search.

## Metric semantics

The authoritative comparison metric is

```text
F(T,r) = sum of q_error over the fixed 1,963-query positive-truth DMV workload.
```

M2.26 already records this sum through `aggregate_objective()` and
`math.fsum`.  The historical M2.27a `target-sweep.json` field named
`objective` was calculated with `statistics.fmean`; it is therefore a
per-query mean q-error.  This is a label/normalization issue, not a CE
calculation error.  M2.27b preserves the raw M2.27a artifact and records
corrected aggregate sums (`mean_qerror = F / 1963`) in its comparison table.

## Input alignment

Both sides use 11,591,877 source rows and the same 1,963 positive-truth query
and truth-value population.  The stored workload digests use different
historical JSON envelopes, so the experiment verifies semantic equality after
normalizing relation spelling and binding query ID, SQL, truth, and target
relation.  Native A/B/C labels and reservoir A/B/C labels are mechanism-local;
they are not paired random streams.  Canonical A comparisons are descriptive
only and are explicitly not paired-sample inference.

The native baseline has 72 physical extstats shell objects in its replay
database, but the hypothetical overlay's active design is empty for the
baseline evaluation.  The reservoir replay has no extstats objects and no
active hypothetical design.  Thus the compared planner state is ordinary-only
for the baseline objective.  `native_analyze_equivalent=false` remains the
required reservoir capability declaration.

## Result

Native target ordering by mean aggregate objective is
`1000 < 300 < 100`; reservoir ordering is `1000 < 100 < 300`.  Native quality
gain is concentrated in `100 -> 300` (0.5248%) and is small for `300 -> 1000`
(0.1036%).  Reservoir quality slightly worsens from `100 -> 300` (-0.1495%)
and improves sharply from `300 -> 1000` (38.0031%).  Native normalized-range
and CV stability improve then worsen; reservoir stability improves at both
steps.  The resulting qualitative knee is native Pattern A (candidate 300)
versus reservoir Pattern B (candidate 1000).

The conservative five-condition gate therefore fails all five conditions:
quality ordering, adjacent quality-gain ordering, normalized-range trend, CV
trend, and knee structure.  The workload-specific conclusion is that the
current reservoir acquisition does not preserve DMV global-target decision
structure under this grid.

The canonical-A diagnostics show large aggregate gaps at T100 (117.59%), T300
(113.28%), and T1000 (33.71%).  The T300-A to T1000-A reservoir improvement is
dominated by a small set of extreme queries (the top ten contribute 93.25% of
the T1000-A aggregate), while the native top ten contribute 87.57%; the tracked
CSV lists the top 20 contributors and their predicate columns for both sides.
All 11 ordinary-column semantic records differ in the compact comparison;
the records include null fraction, distinct estimate, MCV/histogram counts and
value/frequency digests (some individual scalar fields can still agree).  This
is diagnostic evidence, not a claim of byte-level equivalence or a new
acquisition mechanism.

## Boundary

`target_selection_fidelity_qualified=false` is workload- and grid-specific.
The result does not establish a universal target, reservoir/native sampling
equivalence, extstats recommendation fidelity, Census generalization, or any
other PostgreSQL-version claim.  The next research question is how to obtain a
production-readable acquisition state whose target-dependent ordinary
statistics preserve native decision structure, without adding a privileged
ANALYZE hook in this milestone.
