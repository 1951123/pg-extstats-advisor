# Fixed-T end-to-end advisor workflow

The M2.29 MVP treats PostgreSQL's global statistics target as an external,
fixed configuration.  The default is `T=100`; the advisor chooses only the
extended-statistics design `Y`.  Target tuning, target sweeps, and
per-candidate target variants are outside this scope.

The supported production input is Bundle v1 profile
`fixed_t_single_snapshot`; Bundle v2 target-grid artifacts remain experimental.
The production boundary is a single read-only `REPEATABLE READ` snapshot.
The capture phase computes the canonical DMV truth, metadata, and one
deterministic reservoir sample while that snapshot is open, then seals a
Bundle v1 directory.  Production is stopped and its endpoint is checked
inaccessible before any preparation, payload reconstruction, search, or
recommendation work begins.  The sample is authoritative for replay of this
bundle, but is explicitly not claimed to be PostgreSQL `ANALYZE`-equivalent.

The offline phase reconstructs the frozen 72-candidate (36 MCV + 36 FD)
realization in the patched advisor cluster, runs the existing deterministic
ADD-only search with the historical DMV maintenance budget, and emits a
recommendation bundle plus ordinary PostgreSQL `CREATE STATISTICS`,
`ALTER STATISTICS`, `ANALYZE`, and reverse-order `DROP STATISTICS` statements.
The generated deployment statements are stock-PostgreSQL DDL; the patched
cluster is required only for native payload replay during evaluation.

Cold and warm runs must agree exactly on repository semantic digest, baseline
estimate vector/objective, selected design, objective, and modeled cost.  A
cache hit is a derived-artifact optimization, never a replacement for the
sealed capture provenance.
