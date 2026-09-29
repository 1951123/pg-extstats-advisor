# M2.17a source audit

Pristine PostgreSQL 16.14 `src/backend/optimizer/util/plancat.c:1363-1374`,
`get_relation_statistics_worker()`, performs
`SearchSysCache2(STATEXTDATASTXOID, statOid, inh)` and returns immediately when
the `pg_statistic_ext_data` tuple is absent. Consequently no `StatisticExtInfo`
is created and MCV/FD loaders are not reached. The tracked patch adds a
backend-local check at that return point, but synthesizes metadata only for an
active registered PRESENT payload. This is the only case in which the frozen
native bytes need to be made visible without a physical data row.

`statext_mcv_load()` (`src/backend/statistics/mcv.c:562-588`) and
`statext_dependencies_load()` (`src/backend/statistics/dependencies.c:621-650`) both return a decoded
object for PRESENT payloads. Their ABSENT_NATIVE branches return NULL; the
upstream callers (`mcv.c:2067` and `dependencies.c:1631`) dereference those
objects, so an absent-native definition-only
case must remain unbuilt rather than manufacture a `StatisticExtInfo` that
would invoke a NULL loader. With a physical data row, PostgreSQL's
`statext_is_kind_built()` already omits a NULL requested field, so the
ABSENT_NATIVE row-present path remains unchanged.

The registry helper is `hypothetical_extstats_registered()` in the patched
`src/backend/statistics/hypothetical.c:98-113`; the planner hook is the
additional return-path block in patched `plancat.c` at the worker above.

Ordinary PostgreSQL behavior is preserved: no registration means a missing
`pg_statistic_ext_data` row still returns before constructing planner metadata.
