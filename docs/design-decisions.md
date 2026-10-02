# Design decisions

## DD-001 — Native CE is authoritative

Avoid incomplete duplication of PostgreSQL estimator semantics. The advisor
controls hypothetical state; PostgreSQL computes CE.

## DD-002 — PostgreSQL authority is a Git commit

Official PostgreSQL `REL_16_14` at
`0d1c00c624fa7367d4a895f44381887757289682` is the immutable base. The modified
experimental source of truth is the frozen commit in
`postgresql-pgextadv`; advisor patch files are generated derivatives.

## DD-003 — Source and build trees are separate

The clean `postgresql-pgextadv` checkout is read-only build input. Build and
install directories are separate generated paths. The preferred flow builds
directly from the authoritative commit; applying the generated aggregate patch
to official PostgreSQL is an equivalent alternate path.

## DD-004 — Derived patch output is reproducible

`pg/patches/postgresql-16.14-pgextadv.patch` is generated only by
`scripts/export_postgres_patch.sh` from the upstream base and a clean frozen
authoritative commit. It is not an independent implementation unit.

## DD-005 — Payloads are acquired once and frozen

Search compares fixed payload realizations without repeated sampling noise.
Authoritative execution payloads use PostgreSQL-native binary representation.

## DD-006 — Incrementality stops at query granularity

Prior wall-clock evidence rejected query-internal semantic incremental replay.
Only conservative candidate-to-query reuse remains.

## DD-007 — Affected queries are replanned natively

The initial implementation uses the simplest complete native planner boundary.
Planner-state caching and fixed CE skeletons require separate later evidence.

## DD-008 — Search is a client

Evaluator architecture and correctness do not depend on a local-search algorithm.

## DD-009 — Legacy CE-Replay remains historical

The old research repository is evidence and implementation reference only. Full
CE-Replay and semantic incremental machinery are not migrated.

## DD-010 — Python dependencies are repository-local

Development uses `.venv/` inside this repository. Dependency constraints live in
`pyproject.toml`; project packages are not installed globally or shared with the
legacy research environment.
