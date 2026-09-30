# M2.31 DBA preflight demonstration

This compact demonstration exercises the read-only deployment preflight on a
disposable stock PostgreSQL 16.14 instance. The recommendation is the frozen
M2.30 fixed-T design with only a new portable capture-time schema binding; no
search, CE, sampling, target-selection, or maintenance-model result changed.

The disposable relation is an empty `public.dmv` fixture with the captured
11 text columns. The validation role is non-superuser and has only CONNECT,
schema USAGE, and relation SELECT. No DSN, password, or host credential is
stored in these artifacts.

Reports:

- `pass-report.json`: exact 16.14, target 100, schema and permissions match,
  no overrides, no collisions, and no equivalent extstats.
- `target-mismatch-report.json`: controlled session target 300; fails with
  compatibility exit code 3.
- `schema-drift-report.json`: controlled column rename; fails the schema
  check with exit code 3, then the fixture was restored.
- `collision-report.json`: controlled deterministic-name collision; fails with
  exit code 3, then the object was removed.
- `equivalent-report.json`: controlled same-kind/same-column object under a
  different name; fails closed with exit code 3, then the object was removed.
- `permission-report.json`: role without schema/relation visibility; fails
  with permission exit code 4, then the role was removed.

All preflight connections used a read-only transaction. Fixture setup and
cleanup were performed separately by the disposable-instance owner; the
preflight itself issued no CREATE, DROP, ALTER, INSERT, or ANALYZE.
