# M2.16 DMV maintenance-cost calibration

## Scope and authority

M2.16 calibrates an independent DMV maintenance model; it does not transfer
the Census coefficients and does not run a DMV budget search. The protocol uses
the frozen M2.15 raw arity-two catalog (36 MCV and 36 functional-dependency
candidates), target 100, and source-built PostgreSQL 16.14 on `public.dmv`.
The M2.15 candidate catalog and unpriced singleton profile remain immutable.
Native `ABSENT_NATIVE` payloads do not become zero-cost objects here: timing is
for requested mechanism objects, independent of singleton payload availability.

The calibration database is isolated and the relation is left without
statistics objects after every configuration. One untimed warmup precedes each
configuration. The frozen run has 64 configurations, two measured repetitions
for the fitting and held-out sets, and three repetitions for the three
same-count stability configurations. The fitting set contains empty plus pure
MCV/FD configurations; mixed configurations are held out. No search,
screening, CE-semantic, evaluator, or PostgreSQL-patch change is part of this
milestone.

## Model and acceptance

The fitted aggregate model is:

```text
T = alpha + beta_mcv * n_mcv + beta_fd * n_fd + epsilon
design resource = beta_mcv * n_mcv + beta_fd * n_fd
```

The DMV run accepted `empirical-mechanism-count-v1` with:

| quantity | value |
|---|---:|
| intercept alpha | 123.389415 ms |
| MCV slope | 4.151578829 ms/object |
| FD slope | 6.183028745 ms/object |
| FD/MCV slope ratio | 1.489319847 |
| fitting R-squared | 0.988802401 |
| maximum within-configuration CV | 6.2678% |
| maximum held-out relative error | 5.0521% |
| maximum same-count subset CV | 5.4437% |

All preregistered gates pass: CV at most 10%, R-squared at least .95,
held-out relative error at most 15%, same-count subset CV at most 10%, and
positive mechanism slopes. The intercept is reported for validation but is not
budgeted; only the mechanism slopes are the additive design resource.

The accepted artifact is
`calibration/dmv-pg16.14-m2-16-r1/maintenance-model.json`. The complete
protocol, measurements, fit, held-out checks, and stability details are in the
same directory. `priced-candidate-catalog.json` is a derived convenience
artifact that attaches these costs to unchanged candidate IDs and provides a
descriptive cost-aware singleton ranking. It does not select a screening
fraction or run optimization.

## Portability boundary

These are environment-, relation-, PostgreSQL-version-, target-, and
arity-specific empirical estimates. They are not universal PostgreSQL costs
and do not claim per-candidate ANALYZE accuracy. Hardware provenance is
explicitly incomplete. A future DMV full-universe ADD-only feasibility run may
use this accepted model, subject to its recorded provenance and budget
interpretation; no such search is part of M2.16.
