# M2.6 aggregate maintenance calibration

## Frozen MVP scope

The MVP admits only two-column (`arity = 2`) MCV and functional-dependency
candidates. Every candidate in a run uses one explicit, uniform statistics target.
Target tuning, target sweeps, candidate-specific timing, arity extrapolation, and
relation-size extrapolation are outside this scope.

The production empirical model is `empirical-mechanism-count-v1`:

```text
full ANALYZE seconds = alpha + beta_mcv * n_mcv + beta_fd * n_fd + error
search resource(Y)  = beta_mcv * n_mcv(Y) + beta_fd * n_fd(Y)
```

`alpha` predicts the shared part of a complete relation `ANALYZE`; it is retained
for validation but excluded from design cost. The slopes are mechanism-specific
aggregate marginal maintenance weights, not direct measurements of individual
statistics objects. Search budgets use `milliseconds-per-analyze` for the predicted
mechanism-count-dependent incremental portion, not a complete-ANALYZE deadline.

## Why aggregate calibration

Legacy Census measurements found a strong mechanism-aware aggregate object-count
signal and useful mixed-design prediction. Legacy DMV paired single-object timing
was too noisy to justify candidate-specific latency estimates. M2.6 therefore uses
many-object pure-mechanism configurations for fitting and unseen mixed configurations
for additive validation.

Calibration is an explicit offline command and is never invoked by preparation or
search:

```bash
pg-extstats-advisor calibrate-maintenance calibration-config.json
```

The calibration database must be isolated and the relation must initially contain
no extended-statistics objects. Definitions use a calibration-owned prefix and are
removed after the run, including on failure. `CREATE`, `ALTER`, and `DROP STATISTICS`
are outside the timer. Each configuration receives one untimed warmup followed by
repeated `time.perf_counter()` measurements of full relation `ANALYZE`. Configuration
order is a deterministic seeded shuffle; the long-lived server is not restarted and
caches are not flushed.

## Fitting, validation, and gates

EMPTY plus pure MCV and pure FD count levels form the fitting set. Mixed MCV/FD
configurations are held out. The implementation records every timing, configuration
summaries, ordinary OLS coefficients and errors, held-out full-ANALYZE predictions,
environment and relation provenance, and explicit gate outcomes.

The frozen default gates are:

- maximum within-configuration CV at most 10%;
- fitting R-squared at least 0.95;
- maximum held-out mixed relative error at most 15%;
- both fitted slopes nonnegative.

One deterministic subset is used per count, so no same-count subset gate applies.
Ordinary OLS confidence intervals are descriptive normal approximations; repeated
measurements are not claimed to be independent experimental runs. A rejected run
retains its complete report but does not emit `maintenance-model.json`.

## Current Census calibration

The current-environment run is stored under `calibration/census-m2-6/`. It used an
isolated clone of the fixed 2,458,285-row Census relation, PostgreSQL 16.15, target
100, 136 MCV and 136 FD candidates, count levels 16/32/64/128, and nine measured
repetitions per configuration.

The run was correctly rejected. Maximum CV was 10.921% and fitting R-squared was
0.943833, failing their predeclared gates. Both slopes were positive and all held-out
mixed predictions passed, with maximum relative error 5.408%. No empirical model
artifact was emitted, and these rejected coefficients must not be used by search.

## Portability boundary

An accepted artifact is specific to its PostgreSQL environment, relation, target,
arity, and mechanisms. It is not a universal PostgreSQL cost model. Hardware
provenance in the current run is explicitly incomplete. The development preset
remains available for tests and fixtures, but its base-plus-per-column form is not an
empirical claim.
