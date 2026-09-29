# M2.19 — DMV fixed-design fresh-sample robustness

The immutable 31-stat design from M2.17d (8 MCV, 23 FD; digest `c196630393e536612b600cd1abbabf025961ed1e3c977477d0c0e0a8f0ef99a9`) was deployed for 10 independent native PostgreSQL 16.14 ANALYZE realizations on the canonical 11,591,877-row DMV relation. No frozen-sample replay or search was used.

Each run produced ordinary statistics and all selected extended-statistics payloads in one native ANALYZE. The empty-design objective was measured by transactional removal of only the 31 extended-statistics definitions and rollback, preserving the same ordinary-statistics realization; the design objective was then measured after rollback.

Design objective ranged from 21100.834339 to 23786.399813; relative improvement ranged from 43.751352% to 48.623259%. Sample A relative improvement was 48.555645%; the weakest fresh run retained 90.11% of that benefit. The preregistered engineering classification is **strongly robust**.

Selected realization states were stable: 28 candidates were PRESENT in every run, 3 flipped state, and 0 was always ABSENT_NATIVE. Flipped candidates: cand_5e3a909effbf6f7d6e21, cand_5400d6d245a511006593, cand_d102d0d6903dbc44935f.

Across the 1,963 queries: 1508 were always improved, 77 always unchanged, 275 always worsened, and 103 mixed across fresh runs. 15/20 sample-A hardest queries improved in every fresh run.

This measures ANALYZE sampling-noise robustness for a fixed recommendation from the same data distribution. It does not test design-selection stability, temporal/workload/schema/version drift, maintenance calibration, or M3.

Cleanup after every run removed experiment statistics and reset the hypothetical registry; the canonical DMV relation remained unchanged.
