# M2.21 — Census Frozen-Sample Authoritative Rerun

This is the authoritative reproducible Census CE lineage. One native PostgreSQL 16.14 sample was captured once from the canonical `public.climate` relation and persisted; all ordinary statistics and all 4,506 candidate payload states were reconstructed from that sample. Historical Census artifacts remain preserved as exploratory evidence and are not exact-continuation targets.

The sample contains 30000 rows from 2458285 physical rows. Semantic sample digest is `1cb881fa32920c1edc9473117ceacffe5f10f946204d8eb726066fb7b14649a9` and binary digest is `84de07e3f60cb539bf75f7a8829501cdaee75d502063e61fa1850921268e20c6` (16620021 bytes); frozen totalrows is 2458288.0. Three clean replay builds matched ordinary statistics, semantic repository state, baseline vector, and baseline objective exactly. New baseline objective is `5577.425518330484` (historical baseline `5586.930692724469` is descriptive only).

Repository realization on this sample is {'ABSENT_NATIVE': 1105, 'PRESENT': 3401}; candidate catalog and incidence remain the frozen 4,506/19,996 identities. The 4,506 singleton profile was repeated exactly at the semantic level (runtime metadata excluded from the semantic digest): {'negative': 1379, 'positive': 1560, 'zero': 1567}. Positive utility concentration is recorded in `singleton/summary.json`. Historical M2.9 comparison is explicitly descriptive: Spearman 0.953454, top-50/100/200 overlap 48/97/193.

The formal top-5% screen retains exactly 226 candidates (215 MCV, 11 FD; 226 PRESENT, 0 ABSENT_NATIVE) and is bound to the new sample and singleton digests.

The screened deterministic ADD-only search reached an add-local optimum after 113 rounds in 340.995s, selecting 112 candidates (110 MCV, 2 FD) at objective `928.597323483826` from baseline `5577.425518330484`. Cost is `198.8975850452002434` under total screened budget `412.4460772177728987`. The repeat matched sequence, design, objective, cost, rounds, and termination exactly; no full-4,506 search, DROP, SWAP, or fresh-sample robustness run was performed.

Same-sample physical validation matched hypothetical replay for all 112 selected payloads, all 468/468 query estimates, and all 468/468 q-errors. H/P objectives and ordinary-statistics digests were exact across two clean validation runs. Cleanup returned exactly to the new empty baseline and left no experiment statistics or sample table.

The only environment repair was registering the already compiled `pg_hypothetical_extstats_register_absent` internal function in the pre-existing Census database; the PostgreSQL source patch, binary, and build provenance were unchanged.

## Detailed gates and comparisons

The replay ordinary-statistics digest was
`c96595463fa8d242a6fc491391b0179e9682aff3f6574702ea96e17dcf571233` in all
three builds. Their repository semantic digest was
`7e42ba7dbeb9a0a3a2539b1d6e72ab3fa04c5db31e931a6bca3485181bf6df85`, baseline
vector digest was
`3752f91098d2375b464395ec9ac6f78323113ddf8aa2d1e5a372f64af940b192`, and
baseline objective was `5577.425518330484` in every build. The raw repository
directories are about 4.996, 4.996, and 5.001 MB (raw manifests retain runtime
metadata); the semantic state is exact. The new baseline is `9.505174393984817`
below historical baseline `5586.930692724469`; this difference is descriptive
and reflects the changed sample lineage.

The new realization counts are 2,253/0 MCV PRESENT/ABSENT_NATIVE and
1,148/1,105 FD PRESENT/ABSENT_NATIVE, for 3,401/1,105 total. Historical M2.7
counts were 2,253 MCV PRESENT and 1,144/1,109 FD PRESENT/ABSENT_NATIVE; that
comparison is historical only.

The singleton sign breakdown is 1,560 positive, 1,567 zero, and 1,379 negative
overall; MCV is 1,089/53/1,111 and FD is 471/1,514/268 (positive/zero/negative).
By realization state, PRESENT is 1,560/462/1,379 and ABSENT_NATIVE is
0/1,105/0. Positive-utility shares for the top 1/5/10/20/50 percent are
84.994995%, 94.435172%, 96.698968%, 98.706780%, and 99.904653%.
Against historical M2.9, sign counts were 1,571/1,577/1,358, Spearman rank
correlation was `0.9534544847`, and top-50/100/200 overlaps were 48/97/193.
These are descriptive historical comparisons only.

Search efficiency was 19,210 conceptual and feasible moves, zero
budget-infeasible moves, 2,441 exact-bound-pruned moves (12.706923%), 16,769
native evaluations, and 88,654 planner calls. Mean/median/p90/max round time
was 3.017600/3.362163/4.078330/4.481274 seconds. The selected design has 112
PRESENT objects (110 MCV, 2 FD) and no ABSENT_NATIVE object. Every accepted
candidate was singleton-positive; accepted singleton ranks have min/median/p90/max
`1/123/202/224`, so there were no singleton-nonpositive contextual rescues in
this run.

The physical validation emitted 112 `CREATE STATISTICS` statements and 112
corresponding `DROP STATISTICS` statements. Both validation runs had exact
hypothetical/physical payloads, estimates, q-errors, and ordinary statistics;
cleanup had zero residual statistics rows, zero residual data rows, and removed
the sample relation. No unpersisted authoritative sample, random post-capture
sampling, candidate/catalog/workload change, maintenance refit, DROP/SWAP
search, or patch change was used.
