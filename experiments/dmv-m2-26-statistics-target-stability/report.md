# M2.26 — DMV Global Statistics-Target / Planner-State Stability

This is a characterization experiment only.  It performed no ADD/DROP/SWAP,
budget search, recommendation, calibration, or PostgreSQL patch change.

## Protocol and provenance

The exact source relation is `public.dmv` with 11,591,877 rows.  PostgreSQL
16.14's native block sampler and native reservoir were used through the patched
capture hook; no COPY/client-side reservoir was used.  Each T has three fresh
native ANALYZE realizations.  The same persisted sample is replayed into both
ordinary column statistics and all 72 frozen MCV/FD definitions.  Replay uses
the controlled population value `statistics_population_rows=11591877` for
every realization.  Binary samples and repositories are immutable, ignored
cache artifacts under `.build/artifact-cache/dmv-statistics-target-stability-v1`;
tracked manifests contain their SHA-256 lineage.

| target T | native sample rows | baseline objective mean | stdev | range | capture seconds (mean) |
|---:|---:|---:|---:|---:|---:|
| 100 | 30,000 | 41,710.803734 | 477.808522 | 915.501781 | 0.835 |
| 300 | 90,000 | 41,491.916731 | 131.918691 | 262.922004 | 2.038 |
| 1000 | 300,000 | 41,448.940398 | 213.542401 | 426.346386 | 7.760 |

The persisted rows exactly matched the source-derived native capacity observed
for this 72-object catalog (approximately `300*T`: 30k/90k/300k), rather than
being independently selected by the experiment.

## Stability evidence

- All 9 samples persisted; all 9 replayed successfully.
- The candidate catalog digest and all 72 logical candidate IDs were preserved.
- Fresh-backend replay of T100-A, T300-A, and T1000-A was exact for ordinary
  statistics digest, extended-statistics digest, baseline objective/vector, and
  all 72 singleton results.
- Ordinary-statistics, extended-statistics, baseline, and singleton details
  are in the companion JSON artifacts.  Singleton profiles include all 72
  candidates, top-5/10/20 sets, pairwise Spearman correlations, and sign flips;
  baseline stability includes per-query dispersion and the 20 most unstable
  queries.
- Same-target singleton rank correlation averaged 0.964 (T100), 0.965 (T300),
  and 0.934 (T1000) over the three within-target pairs.  Twenty-four candidates
  changed utility sign somewhere in the nine-realization matrix; this is a
  descriptive stability signal, not a design recommendation.

Ordinary and extended statistics consumed the same persisted rows in every
replay (`shared_sample_audit`); there was no secondary sampling or downsampling.
The extstats artifact records all 72 candidate states and pairwise state flips.
The ordinary artifact records per-column scalar/count dispersion, while the
baseline artifact records per-query estimate and q-error dispersion.

## Runtime and storage

Capture/replay time scaled with the native sample: mean capture times were
0.835 s, 2.038 s, and 7.760 s for T100/T300/T1000; mean replay times were
0.429 s, 1.361 s, and 4.944 s.  Mean singleton profiling times were 9.524 s,
9.586 s, and 9.668 s.  Sample COPY sizes were approximately 2.43 MiB,
7.30 MiB, and 23.2 MiB per realization.  All binary payloads remain in the
ignored immutable cache path and are recoverable by the SHA-256 values in the
tracked manifests.

## Historical context

M2.20 is referenced only as the earlier native-T100 multisample lineage;
M2.25 is referenced only as the reservoir/COPY lineage.  Their statistics are
not merged with this nine-sample target experiment, and neither is treated as
evidence for the target comparison.

## Answers to the seven frozen questions

1. `global_statistics_target` is now the single advisor-level target: the
   acquisition API accepts it explicitly (with the historical
   `statistics_target` spelling retained as a compatibility alias), applies it
   to ordinary statistics and all extstats, records it in provenance, and
   rejects conflicting values.
2. Yes.  The observed native sample capacities grew automatically with T and
   the sample persisted by the patched hook fed ordinary and extended builders
   together; no independent sample-size knob was used.
3. Yes.  T100 showed the largest baseline objective spread (stdev 477.809,
   range 915.502) and the greatest ordinary-state dispersion.
4. Yes for this workload and three-realization characterization: T300 reduced
   baseline stdev to 131.919 and range to 262.922, with stable singleton
   ranking (mean within-target Spearman 0.965).
5. T1000 did not further reduce baseline variance (stdev 213.542, range
   426.346), although its state is fully reproducible for a persisted sample.
   The evidence therefore suggests diminishing/non-monotone returns rather
   than a claim of monotonic improvement.
6. T300 is the most defensible next fixed target for controlled fidelity
   experiments: it materially improves stability over T100 without T1000's
   approximately 3.8x capture/replay sample-size cost.  This is a planning
   recommendation, not a search result.
7. Moving away from T100 requires rebuilding target-dependent acquisition
   samples, payload repositories/cache lineage, ordinary/extstats state
   summaries, singleton/baseline stability artifacts, and any target-specific
   maintenance/deployment calibration.  The correctness mechanisms that remain
   reusable are candidate identity/incidence, frozen-sample provenance,
   population-row control, native/replay semantic checks, cache validation,
   and deterministic evaluator behavior.  M2.16's T100 maintenance model is
   provenance only and was not refit here.
