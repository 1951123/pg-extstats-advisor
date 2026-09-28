# SQL analysis

Preparation parses workload SQL with the PostgreSQL-compatible `pglast` parser (currently 7.18), then applies the project's deliberately narrow MVP policy. The AST supplies relation, boolean structure, and predicate metadata; parser capability does not expand supported query scope.

The precise subset is one base relation and an AND conjunction of plain column comparisons (`=`, `<`, `<=`, `>`, `>=`), constant-list `IN`, and `IS NULL`. OR, NOT, unsupported expressions, and unresolved columns produce conservative fallback metadata. Joins, subqueries, CTEs, aggregates, grouping, windows, and set operations are rejected. Fallback queries do not create candidates and conservatively receive all same-relation incidence edges.

Candidate generation and incidence consume this structured analysis rather than SQL text. Preparation provenance records parser and analysis versions; full ASTs are not persisted.
