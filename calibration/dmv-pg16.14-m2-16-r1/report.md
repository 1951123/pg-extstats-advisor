# DMV M2.16 maintenance-cost calibration

Status: **accepted** (`authoritative`).

This is an independent DMV calibration on source-built PostgreSQL 16.14. It
uses the frozen M2.15 raw catalog (36 arity-two MCV candidates and 36 arity-two
FD candidates), target 100, and the fixed
relation `public.dmv`. The frozen catalog and M2.15
singleton profile are not rewritten. `ABSENT_NATIVE` realization is irrelevant
to this maintenance timing protocol: calibration measures requested mechanism
objects, not singleton payload availability.

## Protocol

The dependent variable is full-relation `ANALYZE` wall-clock seconds. Each of
64 configurations received one untimed warmup
and 2 measured repetitions (the three
predeclared stability configurations used 3
repetitions). The fitting set has 55 configs,
the mixed held-out set has 6, and the
same-count stability set has 3.
The run recorded 195 total `ANALYZE` executions
(64 warmups and
131 measured runs), with
27.605078 seconds summed over measured runs
and 40.856806 seconds including warmups.
The protocol performs no search, singleton screening, or CE-semantic change.

The pilot completed before the frozen run:

| configuration | seconds |
|---|---:|
| empty | 0.133983964000 |
| 18 MCV | 0.196499521000 |
| 18 FD | 0.232960397000 |
| 18 MCV + 18 FD | 0.307252923000 |

## Fitted model and gates

The fitted model is
`T = alpha + beta_mcv * n_mcv + beta_fd * n_fd + epsilon`.
The measured intercept is 123.389415 ms and is
reported for validation but is not included in the design budget. The accepted
mechanism weights are:

| mechanism | slope (ms/object) |
|---|---:|
| MCV | 4.151578829 |
| FD | 6.183028745 |

FD is 1.489319847x the MCV slope in this environment.
Fit R-squared is 0.988802401; maximum within-configuration CV is
6.2678%;
maximum held-out relative error is 5.0521%; and maximum
same-count subset CV is
5.4437%.
All preregistered gates pass, so `maintenance-model.json` is accepted.

## Derived pricing artifact

`priced-candidate-catalog.json` attaches the accepted mechanism slopes to the
same 72 candidate IDs. Its cost-aware singleton ranking is descriptive only;
no budget search or screening decision is made here. The source M2.15
singleton profile remains unpriced and unchanged.

The dataset integrity check retained logical fingerprint
`7df509386693daab173476700bc361e9dd22e2c7fd8010a2758f6b6ada37253d` before and after calibration,
schema signature `003f35c74401ac72aad3c6190d32ee7f132bdd25654b3c2403b2e47138115b3c`, row count
11,591,877, persistence `u`, and
total relation size 1,981,145,088 bytes. No calibration
statistics objects remained and the default target remained
100. The PostgreSQL patch SHA256 was unchanged.

## Portability and integrity boundary

The coefficients are environment-, relation-, PostgreSQL-version-, target-,
and arity-specific empirical estimates. They are not universal PostgreSQL
costs and do not claim per-candidate ANALYZE accuracy. The authoritative input
relation has 11,591,877 rows, persistence `u`,
1,981,145,088 bytes, schema signature
`003f35c74401ac72aad3c6190d32ee7f132bdd25654b3c2403b2e47138115b3c`, and source SHA256
`ae310972b7ac08629d135a1da7c580e3fe603bfc2e665e1969005bd481d4605a`. The source-built PG
patch and build recipe are recorded in the calibration provenance.
