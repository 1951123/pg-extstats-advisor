# M2.18 — Frozen-sample hypothetical versus physical validation

M2.18 validates the realization boundary, not a new search. The final DMV
design is loaded directly from the authoritative M2.17d artifact and evaluated
twice using the same persisted 30,000-row acquisition sample, ordinary
statistics, PostgreSQL 16.14 build, workload, and truth vector.

The hypothetical state (H) keeps 72 shell definitions with no physical data
rows and activates the selected 31 payloads through the backend-local overlay.
The physical state (P) creates the selected 31 statistics from the identical
frozen sample and evaluates them in a fresh backend with no hypothetical
overlay. The selected design contains 8 MCV and 23 FD objects, with modeled
cost `175.422291756478746`.

H and P are exactly equal for the aggregate objective
`22014.061316846422`, the 1,963-query estimate vector and q-errors, and the
31 selected payload byte digests. Their ordinary-statistics digest and relation
metadata also match. Relative to the empty-design baseline, both have 1,562
improved, 84 unchanged, and 317 worsened query contributions. A second clean
H/P run reproduced the same objectives, vector digest, and payload digests.

The result is a fixed-sample semantic-fidelity validation. It does not claim
fresh-sample robustness, per-candidate maintenance-cost accuracy, or a new
optimization result. No random/native heap sampling, candidate regeneration,
search, screening, or maintenance calibration was run in M2.18. Cleanup
restored the empty baseline and removed all experiment statistics, data rows,
and the temporary sample relation.

The generated protocol, exact estimate comparison, selected-payload audit,
repeatability record, and executable DDL are in
`experiments/dmv-m2-18-frozen-hyp-vs-physical/`.
