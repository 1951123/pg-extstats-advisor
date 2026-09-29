# M2.21 — Census frozen-sample authoritative rerun

M2.21 replaces the earlier Census search lineage with one persisted native
PostgreSQL 16.14 acquisition sample. It is an authoritative frozen-sample
replay and screened-search result; it is not a fresh-sample robustness study
and it does not complete the previously blocked full 4,506-candidate search.

## Frozen inputs

- Source relation: `public.climate`, 2,458,285 rows, 69 nullable integer
  columns, persistent relation.
- Canonical source CSV SHA256:
  `3be576490f4dc1cae9fc6c04a23f27633b0b09fac00bd3d0a63d011958252e33`.
- Workload: 468 parsed Census queries from the legacy `query.sql`; workload
  digest `796ab606e825969ad91801c9795cd830ef40686568fe857d856921a8d42215`.
- Candidate catalog: 4,506 candidates (2,253 MCV and 2,253 FD), catalog
  digest `74727a871b3a601977885cbf96d25cda6e5ac38ef448d1815561bdc5988a425a`.
- Incidence digest:
  `0cd8466f654e21080914cf8444ad3ce3a093d9a7700d370b5ac6a4b9a28f66b4`.
- Maintenance model digest:
  `dbad23611afa778a7ff6aab6ad39e519f7bfc51387874aa07dd33e37a5d3a11e`.

The native sample contains 30,000 rows. Its semantic digest is
`1cb881fa32920c1edc9473117ceacffe5f10f946204d8eb726066fb7b14649a9`; the
persisted PostgreSQL binary sample is 16,620,021 bytes with SHA256
`84de07e3f60cb539bf75f7a8829501cdaee75d502063e61fa1850921268e20c6`.
The builder's frozen `totalrows` is `2458288.0`. The sample and its manifest
are under `datasets/census-frozen-acquisition-sample-v1/`.

Three clean replay builds produced identical ordinary-statistics digests,
semantic payload-repository digest
(`7e42ba7dbeb9a0a3a2539b1d6e72ab3fa04c5db31e931a6bca3485181bf6df85`),
baseline estimate vector digest
(`3752f91098d2375b464395ec9ac6f78323113ddf8aa2d1e5a372f64af940b192`), and
baseline objective `5577.425518330484`. Runtime and raw repository digests are
retained for provenance; runtime metadata is excluded from the semantic
singleton-profile digest
`d9d6e8ef95bff5bb6d4459c919ed52479b1b5db99ebafd1892874eb18ce6d3b9`.

## Singleton screening and search

The complete 4,506-candidate singleton profile was evaluated twice from the
same repository. It contains 1,560 positive, 1,567 zero, and 1,379 negative
singleton improvements. The deterministic raw top-five-percent screen retains
exactly 226 candidates (215 MCV and 11 FD); all 226 are `PRESENT` in the frozen
sample. This screen is an explicitly bounded M2.21 protocol step, not a claim
that five percent is a universal policy.

The existing deterministic screened ADD-only search was then run twice. It
selected 112 candidates (110 MCV and 2 FD), reached `add-local-optimum` after
113 rounds, and reduced the objective from `5577.425518330484` to
`928.5973234838256` (relative improvement `0.8335078934838404`). The final
maintenance cost is `198.8975850452002434` under the screened-catalog budget
`412.4460772177728987`. The repeat matched the accepted sequence, design,
objective, cost, and termination exactly. No full 4,506-candidate search,
DROP/SWAP search, fresh-sample robustness run, or M3 plan/runtime experiment
was performed.

## Physical validation

The selected 112 payloads were materialized from the same persisted sample in
the normal physical deployment path and compared with hypothetical replay.
Two clean validation runs matched all 112 selected payloads, all 468 estimate
vectors, all 468 q-error vectors, aggregate objectives, and ordinary-statistics
digests exactly. Cleanup returned the database to the empty baseline. This is
same-sample mechanism/realization fidelity only; it is not evidence of
fresh-sample robustness or maintenance-model validity.

The only environment repair was registering the already compiled
`pg_hypothetical_extstats_register_absent` internal function in the pre-existing
Census database. The tracked PostgreSQL source patch, binary, and build input
provenance were unchanged. The registration is recorded in
`experiments/census-m2-21-frozen-authoritative/{protocol,lineage}.json`.

## Artifact boundary

The persisted sample is the authoritative source.  The former three full
payload repositories are replaced by the compact tracked
`repository-summary.json`; a cache miss reconstructs the repository under
`.build/artifact-cache/` and validates semantic digest
`7e42ba7dbeb9a0a3a2539b1d6e72ab3fa04c5db31e931a6bca3485181bf6df85` before use.

The complete machine-readable protocol, lineage, replay, singleton, screening,
search, and physical-validation artifacts are under
`experiments/census-m2-21-frozen-authoritative/`. Earlier Census experiments
remain preserved as historical or development evidence and are not overwritten
or silently relabeled. The persisted sample is the exact-continuation boundary
for this M2.21 result.
