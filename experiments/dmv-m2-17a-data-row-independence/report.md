# M2.17a — Hypothetical extstats data-row independence

The upstream failure is exact: PostgreSQL 16.14 returns from
`get_relation_statistics_worker()` when `pg_statistic_ext_data` has no tuple,
so no planner `StatisticExtInfo` exists. The patch now admits only active
registered PRESENT payloads at that return point. ABSENT_NATIVE remains
unbuilt when the physical data row is absent because PostgreSQL's native
loaders return NULL and their upstream callers require a decoded object.

The controlled integration matrix passed exact row-presence equality (50 rows)
for PRESENT MCV plus ABSENT_NATIVE FD, including physical data-row present and
definition-only variants. Definition-only unregistered statistics remained
ignored (1 row), registration without activation was unchanged, and
unregistered activation still failed loudly.

The frozen DMV repository was not reacquired and no ANALYZE was run. All 72
definitions (36 MCV, 36 FD) were recreated in the disposable clone with zero
`pg_statistic_ext_data` rows. Three singleton smoke evaluations completed
without a backend crash, including the formerly failing ABSENT_NATIVE
candidate. Their observed objectives are retained diagnostically; they are not
an authoritative M2.15 rerun because the no-ANALYZE disposable relation state
does not reproduce the prior acquisition objective exactly.

No M2.17 budget search was resumed.
