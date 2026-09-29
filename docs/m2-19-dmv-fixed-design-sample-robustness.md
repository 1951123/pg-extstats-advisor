# M2.19 — DMV fixed-design fresh-sample robustness

M2.19 keeps the 31-stat design selected on frozen sample A immutable and
measures it on ten independent native PostgreSQL 16.14 `ANALYZE` realizations
of the canonical 11,591,877-row DMV relation. The workload, truth vector,
candidate catalog, maintenance cost, PostgreSQL build, and patch are fixed;
no search, screening, candidate refresh, or maintenance refit is performed.

For every realization, ordinary statistics and all selected extended-statistics
payloads are produced by the same native `ANALYZE`. The paired empty baseline
is measured by transactionally dropping only the selected extended-statistics
definitions and rolling back, so the ordinary `pg_statistic` realization is
unchanged before the fixed design is evaluated. Fresh runs never use frozen
sample replay.

The fixed design has 8 MCV and 23 FD objects (digest
`c196630393e536612b600cd1abbabf025961ed1e3c977477d0c0e0a8f0ef99a9`). Every
fresh realization has positive improvement. Relative improvement ranges from
43.751352% to 48.623259%, versus 48.555645% on sample A; the weakest run
retains 90.11% of sample-A benefit and therefore passes the preregistered
strong-robustness engineering gate (positive improvement everywhere and at
least 80% of sample-A relative improvement).

Twenty-eight selected candidates are `PRESENT` in all ten runs and three
flip between `PRESENT` and `ABSENT_NATIVE`; none is always absent. The
candidate-state and payload-digest table records those transitions. Across
1,963 queries, 1,508 are always improved, 77 always unchanged, 275 always
worsened, and 103 mixed across runs; 15 of the 20 hardest sample-A baseline
queries improve in every fresh run.

This is robustness to native `ANALYZE` sampling noise under one canonical data
distribution. It does not test design-selection stability, temporal or
workload drift, schema/version drift, maintenance-cost validity, or M3.
Artifacts are in `experiments/dmv-m2-19-fixed-design-fresh-sample-robustness/`.
