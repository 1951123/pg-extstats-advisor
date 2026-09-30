# M2.32 deployment verification demonstration

This compact evidence uses the frozen M2.30/M2.31 recommendation against a
disposable stock PostgreSQL 16.14 instance. It does not rerun search, replay
CE, compare payload bytes, compare q-error/objective, or use the hypothetical
PostgreSQL patch.

The verification role is non-superuser and has `CONNECT`, schema `USAGE`,
relation `SELECT`, and catalog visibility required for the read-only checks,
including `SELECT` on `pg_catalog.pg_statistic_ext_data`. No credentials are
stored here. The catalog grant is fixture setup performed by the disposable
instance owner, not by the verifier.

Lifecycle evidence:

- `preflight-pass.json`: pre-mutation compatibility PASS.
- `before-analyze-fail.json`: no definitions yet, so deployment verification
  fails closed.
- `definition-without-analyze.json`: all 17 definitions exist, but all 17
  `pg_statistic_ext_data` rows are absent; exit code 3 with explicit
  `ANALYZE` guidance.
- `deployment-pass.json`: after four fixture rows and manual `ANALYZE`, all
  17 definitions and data rows are present; PASS.
- `partial-deployment-fail.json`: 10/17 definitions present; FAIL with 7
  missing.
- `wrong-kind-fail.json`: deterministic name exists with the wrong kind; FAIL.
- `wrong-columns-fail.json`: deterministic name exists on the wrong columns;
  FAIL.
- `partial-rollback-fail.json`: one of ten objects remains after a partial
  rollback; FAIL with one remaining object.
- `rollback-pass.json`: after applying the complete rollback script, all 17
  selected names are absent; PASS.

The source-grounded materialization rule is intentionally row-based:
PostgreSQL 16.14 `BuildRelationExtStatistics()` calls `statext_store()` and
replaces the `pg_statistic_ext_data` tuple during ANALYZE. The requested MCV or
dependency field can legitimately remain NULL when native build produces no
usable payload (`extended_stats.c:115-236, 791-861`; `mcv.c:184-205, 241-263`;
`dependencies.c:350-438`). Such a row is reported as native payload absent,
not as “ANALYZE did not run.” Raw payload bytes are never written to reports.
