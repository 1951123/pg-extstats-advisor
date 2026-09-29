# M2.17c — DMV frozen-sample singleton refresh

M2.17c refreshes the DMV singleton profile from the persisted M2.17b
acquisition sample. It is a validation artifact, not a new acquisition or
search campaign: random sampling, reacquisition, candidate screening, and
search are all disabled.

The authoritative sample is semantic digest
`59dc8dbe007a81cbd10a11894ff8abf9b4a8f0cac11523221dbd63e34dd4ca7f`, binary
SHA256 `c3b30ddfebf118cb9137bae122e2913e19e88d0cbee14b2c93e980c7693cf463`,
with 30,000 rows and frozen `totalrows` 11,687,702. The refresh uses the
M2.17b build-1 repository and verifies the candidate identity/order and
incidence/workload lineage against the prepared DMV catalog; it does not use
the historical M2.15 singleton profile as search input. The M2.16 accepted
maintenance model is recorded as provenance only and no replay timing is used
to refit it.

All 72 candidates (36 MCV and 36 FD) are evaluated twice from the empty design,
with the second pass in a fresh backend session. Both passes reproduce baseline
objective `42791.986480127205`, estimate-vector digest
`f3254350d068cbd625fffd5537cd25573961da71779977e73811c1d6acb4a93f`, and the
same semantic singleton profile digest. The realization split is 70
`PRESENT` and two `ABSENT_NATIVE`; the latter remain explicit zero-utility
diagnostic entries rather than being silently dropped. The persisted artifacts
are under `experiments/dmv-m2-17c-frozen-singletons/`.

After profiling, all temporary statistics and the replay sample relation are
dropped and the backend overlay is reset. The project-level invariant is that
no new authoritative CE experiment may depend on an unpersisted `ANALYZE`
sample: an experiment must replay a persisted, checksummed sample or declare a
new versioned acquisition campaign first.
