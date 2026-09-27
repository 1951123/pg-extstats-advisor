# Design decisions

## DD-001 — Native CE is authoritative

Avoid incomplete duplication of PostgreSQL estimator semantics. The advisor
controls hypothetical state; PostgreSQL computes CE.

## DD-002 — Upstream PostgreSQL source is immutable

`/root/projects/extended-stats-optim/postgresql-16.14.tar.bz2` is the sole upstream
source of truth. All project modifications are Git-tracked patches in this repo.

## DD-003 — The reference tree is read-only

`/root/projects/postgresql-reference/postgresql-16.14` exists only for inspection
and navigation. It is never patched or used as an authoritative build checkout.

## DD-004 — Build trees are disposable

Every clean build begins with the verified tarball, applies the current tracked
patch to a freshly extracted `.build/postgresql-16.14-src`, and installs under
`.build/postgresql-16.14-install`.

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
