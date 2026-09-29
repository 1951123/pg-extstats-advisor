# M2.17a.1 — ABSENT_NATIVE row-presence equivalence and build-recipe audit

This follow-up did not modify the PostgreSQL patch, resume M2.17 search, run
screening, run M3, reacquire the DMV repository, or change the CE objective.

The current patch has three relevant branches. At
`src/backend/optimizer/util/plancat.c:get_relation_statistics_worker()` a
missing `pg_statistic_ext_data` row synthesizes a `StatisticExtInfo` only for an
active registered PRESENT payload. An active ABSENT_NATIVE entry does not
synthesize an info node when the row is missing. With a physical row whose
requested field is SQL NULL, upstream `statext_is_kind_built()` also omits that
kind, so the row-present and row-absent ABSENT_NATIVE states both have no
`StatisticExtInfo` in this scope. Ordinary inactive/unregistered behavior is
unchanged.

The generic fixture passed exact ABSENT-alone equality (50 versus 50) using the
same `st_fd_nodata` definition with its native-NULL shell row inserted versus
removed. It also passed same-definition mixed PRESENT-MCV plus ABSENT-FD
equality (50 versus 50), and the same mixed design under reverse precedence
(50 versus 50). The existing inactive negative control and unregistered
activation failure also passed.

The targeted DMV smoke used the frozen PRESENT MCV candidate
`cand_f94acad130c4ac5b3553` (`record_type, body_type`) and the sole frozen
ABSENT_NATIVE FD candidate `cand_6b0e7e9b7d7eb883405b` (`record_type, county`).
The query included all three columns. ABSENT-alone was 579549 rows in both
states; mixed and reversed-precedence states were 566175 rows in both states.
The row-present comparison used an isolated `pg_statistic_ext_data` shell with
SQL NULL payload fields, then removed it. No ANALYZE was used and the DMV
database ended with zero data rows. This is targeted semantic smoke, not a
search or calibration result.

The recipe digest audit found that `scripts/build_postgres16.sh` hashes the
upstream tarball SHA, tracked patch SHA, configure arguments, compiler path and
version, user CFLAGS, and effective `pg_config` CFLAGS. It excludes timestamps,
the binary SHA, and the repository commit. The old and new compiler,
configuration, flags, assertions/debug, LLVM/JIT, and library settings are
identical. The digest changed solely because the tracked patch SHA changed from
`aab7...` to `5f29...`; the binary SHA changed as a consequence. The field is
therefore a composite build-input identity, and this is documented without
renaming historical fields.

The patch does not touch ANALYZE/statistics-build code. Census M2.6 and DMV
M2.16 maintenance-calibration semantics remain applicable; frozen repositories
and historical search/calibration artifacts do not require recomputation.

No M2.17 search was resumed.
