# M2.13 formal workflow smoke report

This is a development-only protocol smoke, not a replacement for the
authoritative M2.7 Census search. It formalizes the previously exercised
singleton-screened ADD-only path without changing CE semantics, candidate
generation, or search behavior.

## Provenance

- Baseline repository commit: `48c5c9b425f0e80d31d541afe2a8a620ab2b9507`.
- Singleton source: frozen M2.9 `singleton-results.csv`.
- Native singleton evaluations in this smoke: `0`.
- Raw catalog: `4,506` candidates.
- Screen: explicit `singleton_top_fraction`, fraction `0.05`, `ceil`, retaining `226`.
- Screened catalog digest: `c0c544c703682a13e7447a514f0610d251b4694cb92f2c8eeead3f3ee50880a8`.
- Candidate-set artifact digest: `344c64191a6a6605026ad78db8fc41565f4eb2fc9d796b4c08842889e5f2a64a`.

## Reproduction

The formal search was executed exactly once with `ADD-only` and
`candidate-set-total` budget semantics. It reproduced the M2.11 retained
membership, ranking order, selected design, and accepted candidate order.
The result selected `110` candidates with objective
`936.3584041825216`, modeled cost `195.4027612476062514`, and reached
`add-local-optimum` after `111` deterministic ADD rounds (110 accepted
moves plus the terminating no-improvement round). The resulting search and
recommendation digests are recorded in `protocol.json` and
`reproduction-summary.json`.

The `candidate-set-total` budget was `414.0398034077412444`; the selected
design consumed `195.4027612476062514` of that budget.

The observed profile-import plus screen wall-clock interval was `4.31 s`;
the one formal search process took `361.68 s`, for `365.99 s` excluding the
historical singleton profiling run. The search made `16,644` evaluator calls
and `87,877` planner calls (the latter is the baseline query pass plus the
affected-query counts recorded in the trajectory).

The candidate-set artifact is copied into the run before search. Search,
recommendation, and validation reload it and verify its raw-catalog,
workload, incidence, repository, and maintenance-model lineage. The
recommendation records the screened artifact digest, screened catalog digest,
singleton profile digest, visible candidate count, search mode, and its
development-only role. No fresh physical validation was run in this smoke.

## Interface and compatibility gate

Full-catalog search remains the default when no candidate-set artifact is
provided. Existing persisted search results load with the new `SearchConfig`
defaults. Screened recommendations and validation resolve the visible catalog
from the validated candidate-set artifact, while selected payloads remain
looked up in the frozen repository. A screened result is therefore
restartable and cannot silently fall back to the raw catalog.

## Interpretation

This milestone demonstrates a reproducible engineering workflow for
development-time singleton screening. It does not establish five percent as
a general threshold, prove that screening preserves the full-universe
optimum, or complete M2.7.
