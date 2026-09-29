# M2.17b PostgreSQL 16.14 source audit

The audit is against the pristine source tree at
`/root/projects/postgresql-reference/postgresql-16.14`; the working-tree
patch only adds the frozen-sample acquisition branch and its three GUCs.

## One sample feeds ordinary and extended statistics

`analyze_rel()` selects `acquire_sample_rows` (`src/backend/commands/analyze.c:205`)
and `do_analyze_rel()` computes the target sample size, calls the acquire
function, then passes the same `rows`, `numrows`, and `totalrows` to every
ordinary `VacAttrStats.compute_stats` call and to
`BuildRelationExtStatistics` (`analyze.c:291-326, 450-615`).  The final
`vac_update_relstats` call records the relation-level row estimate
(`analyze.c:644-679`).

The stock sampler is `acquire_sample_rows` (`analyze.c:1104-1370`).  It uses
the native block sampler and Vitter reservoir, then sorts the retained tuples
by physical TID before returning them.  M2.17b captures after this sort and
replays those tuples in the same order; it does not replace PostgreSQL's
ordinary or extended-statistics builders.

`BuildRelationExtStatistics` obtains the relation's statistics entries and
constructs one `StatsBuildData` object from the sampled tuples
(`src/backend/statistics/extended_stats.c:115-213, 2497+`).  The native MCV
builder is `statext_mcv_build` (`src/backend/statistics/mcv.c:184`), and the
functional-dependency builder is `statext_dependencies_build`
(`src/backend/statistics/dependencies.c:350`).  Both therefore consume the
same sample that ordinary column statistics consume.

For MCV, `statext_mcv_build` returns `NULL` when the sorted sample cannot
produce any group above PostgreSQL's minimum-count threshold
(`mcv.c:205-267`); this is a legitimate no-MCV outcome, not a serialization
failure.  For FD, `statext_dependencies_build` only allocates a result for
non-zero dependency degrees and returns `NULL` if every generated dependency
is rejected (`dependencies.c:350-449`).  Thus MCV and FD can independently
produce a NULL native field even though the `pg_statistic_ext` definition is
present.

## NULL/absence semantics

PostgreSQL may legitimately leave one requested extended-statistics payload
absent when the native builder has no realizable object for that sample (for
example, a degenerate dependency).  `pg_statistic_ext_data` is still updated
by the normal catalog write path; the absence is represented by a NULL field
or an absent data row, rather than by a fabricated serialized blob.  The
M2.17b repository retains the existing `ABSENT_NATIVE` state and does not
silently drop candidates.

## Frozen branch semantics

The patched `acquire_sample_rows` branch is opt-in through
`pg_extstats.frozen_sample_mode`.  `off` follows the stock sampler, `capture`
executes the stock sampler and persists the returned tuples, and `replay`
loads a checksummed, schema-validated auxiliary sample relation and returns
its tuples plus the captured `totalrows`.  Missing relations, wrong column
count/type, empty samples, and invalid row limits raise errors.  No target
relation rows are sampled in replay mode, and no catalog statistics are
fabricated.  The default is `off`, preserving ordinary PostgreSQL behavior.
