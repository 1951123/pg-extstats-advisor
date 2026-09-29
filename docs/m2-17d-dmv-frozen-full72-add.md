# M2.17d — DMV frozen-sample full-72 ADD-only search

M2.17d is the first DMV full-catalog search on the persisted M2.17b
acquisition lineage. It uses all 72 candidates (36 MCV and 36 FD), the
accepted M2.16 aggregate mechanism-count maintenance model, deterministic
best-improvement ADD-only semantics, and the existing M2.8 exact q-error
lower-bound pruning. The historical M2.15 singleton artifact is not search
input; the refreshed M2.17c profile is used only for interaction evidence.

The full-catalog-total budget is the exact sum of every candidate cost,
`372.045872636249472` milliseconds-per-analyze. The persisted sample has
semantic digest
`59dc8dbe007a81cbd10a11894ff8abf9b4a8f0cac11523221dbd63e34dd4ca7f`, binary
SHA256 `c3b30ddfebf118cb9137bae122e2913e19e88d0cbee14b2c93e980c7693cf463`,
and frozen `totalrows` 11,687,702. A pre-search replay derived ordinary
statistics once; before search, all 72 physical definitions were recreated
with zero `pg_statistic_ext_data` rows. The search hot path performed no
sampling, acquisition, repository writes, calibration, statistics DDL, or
`ANALYZE`.

The run reached an ADD local optimum after 32 rounds and 226.178 seconds.
Objective decreased from `42791.986480127205` to `22014.061316846422`, an
absolute improvement of `20777.925163280783` (48.555645%). The selected design
has 31 candidates: eight MCV and 23 FD, all `PRESENT`, at modeled maintenance
cost `175.422291756478746` milliseconds-per-analyze. The final workload
distribution is mean `11.214498887848407`, median `2.370412682262538`, p90
`2.5222221658360744`, and maximum `8723.666666666666`; 1,562 queries improve,
84 are unchanged, and 317 worsen under the fixed positive-truth workload.

An independent fresh-backend repeat matched the accepted candidate,
mechanism, and realization sequences, objective/cost trajectory, final design,
all search counters, and termination reason exactly. The two
`ABSENT_NATIVE` candidates were evaluated by the native search path, had no
positive contextual effect, and were not selected. The full artifact package
is under `experiments/dmv-m2-17d-frozen-full72-add/`.

This freezes the DMV development profile as persisted frozen acquisition
sample plus full candidate universe plus ADD-only search. It does not reopen
DROP/SWAP or M3 work, and it does not claim that the M2.16 aggregate model is a
per-candidate ANALYZE cost oracle.
