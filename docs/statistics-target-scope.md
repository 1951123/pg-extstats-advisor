# Fixed global statistics-target scope

The advisor treats PostgreSQL's global statistics target as an externally
supplied production/DBA configuration. The default is PostgreSQL's standard
value, `global_statistics_target = 100`. A caller may provide another positive
integer, but the core workflow does not select, sweep, or optimize the target.
The search problem is therefore:

```text
given T, choose the extended-statistics design Y
```

or, equivalently, `Y*_T = argmin_Y F_T(Y)`. There is no outer optimization over
`T`, no target-grid field in the core `AdvisorProblem`, and no per-column or
per-relation target decision. The target is immutable for one prepared
run/search and is reported as the evaluated configuration, never as a
recommended target.

## Identity and compatibility

`T` is part of the immutable problem identity, preparation summary, search
configuration, recommendation provenance, acquisition provenance, and payload
cache/realization identity. Logical candidate identity remains only relation,
columns, and mechanism; the same logical candidate at another `T` is a
different realized payload. Production capture compatibility checks compare the
recorded effective target with the advisor target when both are supplied.
Explicit `pg_attribute.attstattarget` and `pg_statistic_ext.stxstattarget`
overrides remain fail-closed under the database-wide-target contract.

Deployment emits ordinary extended-statistics DDL and does not alter a
database or role's default target. Production is expected to be configured
consistently with the evaluated `T`.

## Correctness, robustness, and non-requirement

For one persisted frozen acquisition realization, replay must be exact:
ordinary-statistics semantics, extstats payloads, planner estimates, q-error
vectors, aggregate objective, and deterministic search behavior must agree.
Independent `ANALYZE`/sample realizations are robustness evidence. Different
statistics, payloads, estimates, or selected designs across independent
realizations are expected and accepted; a frozen advisor sample is not
required to predict a future production `ANALYZE` sample exactly.

## Maintenance models

The Census and DMV calibrated maintenance models are valid for their recorded
`T=100` calibration. A budgeted run at another target must fail closed unless a
target-specific calibrated model is supplied; calibrated T100 coefficients are
never silently reused for another target. This is a compatibility check, not a
claim that PostgreSQL maintenance cost is independent of `T`.

## Historical scope evidence

The M2.26--M2.28 artifacts remain unchanged and are historical scope evidence:

- M2.26 showed that `T` materially affects statistics state and CE stability,
  so it belongs in provenance and cache identity, not in the design search.
- M2.27a showed that same-snapshot replicated multi-target capture is feasible;
  its target-grid path remains experimental rather than core production flow.
- M2.27b showed that the current production-readable reservoir does not retain
  native target-selection decision structure.
- M2.28 ruled out a simple reconstruction, one-MCV, source-population,
  histogram, or few-extreme-query explanation for the discrepancy. Automatic
  target selection therefore remains outside the core scope.

Future work may add a target-selection/acquisition-fidelity subsystem, but it
must be an explicit extension rather than an implicit change to this fixed-T
contract.
