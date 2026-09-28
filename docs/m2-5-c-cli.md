# M2.5-C restartable CLI

The `pg-extstats-advisor` console entry point exposes `prepare`, `search`,
`recommend`, `validate`, `run`, and `cleanup-acquisition`. Commands are
non-interactive and use persisted artifacts as their only cross-process state.
The reusable Python boundary consists of `load_prepared_run`,
`execute_search_stage`, `load_search_result`,
`execute_recommendation_stage`, `execute_validation_stage`, and
`cleanup_acquisition_stage`; argparse contains no estimator or search logic.

`prepare` writes versioned workload, candidate, incidence, native repository,
frozen preset maintenance-model, summary, and run-manifest artifacts. Reloaders
recompute workload, full catalog, incidence, repository, maintenance-model, and
search-result digests and validate their lineage. The run manifest is only a
mutable stage index; individual validated artifacts remain authoritative.

Acquisition statistics definitions must remain in the acquisition database
until search finishes because their OIDs are PG16 overlay registration handles.
Every restarted search validates OID, relation, and mechanism through the
existing adapter. `cleanup-acquisition` deletes only names recorded by this run.
After cleanup, a new search fails loudly; persisted recommendation remains
renderable. Cleanup is idempotent through `DROP STATISTICS IF EXISTS`.

DSNs are resolved from explicit command options or `PGEXT_SOURCE_DSN`,
`PGEXT_ACQUISITION_DSN`, and `PGEXT_VALIDATION_DSN`. Config may use corresponding
`*_dsn_env` keys. Persisted config redacts keyword and URI passwords; no runtime
secret is copied to results. Search budget is parsed as finite nonnegative
`Decimal` in the frozen model's exact unit.

Search persists selected design/state, per-query estimates/truth/q-error,
objective, Decimal costs and budget as strings, full move trajectory, counts,
configuration, and all lineage digests. Recommendation never searches or
connects to a database: it renders structured candidate/provenance JSON and
reuses M2 SQL planning for `deployment-plan.json` and `deployment.sql`.

Validation uses the persisted selected state as A, explicitly connects to an
isolated validation DSN, physically creates the selected definitions, applies
the uniform target, performs fresh ANALYZE, obtains C through native EXPLAIN,
and persists full and compact drift reports. Same-realization B remains M2
integration evidence and is not exposed by the CLI.

`run` performs prepare, search, and recommend; physical validation occurs only
with `--validate`. Existing output directories fail rather than overwrite.
There is no resume/overwrite mode, general secret manager, remote service,
maintenance fitting, autonomous deployment, or M3 runtime/plan analysis.
