# M0-B external evaluator

## Typed core

The external core uses stable string `QueryId` and `CandidateId` values; catalog
OIDs remain backend handles. Frozen dataclasses represent candidates, ordered
designs, ADD/DROP/SWAP moves, workload queries, per-query evaluations, and
evaluation states. A `CandidateCatalog` owns globally unique precedence ranks and
normalizes every design after a move. Consequently ADD inserts at its recorded
rank, while DROP preserves relative order and SWAP constructs and renormalizes
the complete counterfactual design.

An `EvaluationState` contains the ordered design, query evaluations in query-ID
order, aggregate objective, workload and repository digests, PostgreSQL version,
evaluator provenance, and affected/reused IDs. It contains no planner internals
or mutable global cache.

## Frozen repository v1

`PayloadRepository.load()` reads `manifest.json` plus
`payloads/<candidate-id>.<kind>.bin`. It validates format version, required
repository/candidate/provenance fields, stable candidate uniqueness, relative
blob paths, byte size, and SHA256. The repository digest is SHA256 of canonical
sorted compact JSON; each blob has an independently verified SHA256.

The manifest records PostgreSQL version, immutable-upstream digest, patch commit,
acquisition provenance, relation fingerprint, definition and interpretation
metadata, fixed rank, and backend OID. Native bytes remain authoritative.

During registration the adapter additionally reads `pg_statistic_ext.stxkind`
and verifies that each declared MCV or FD mechanism is present in its physical
definition. It also validates relation OID identity before calling the typed
backend registration function.

## Session lifecycle

One `PostgresAdapter` owns one connection and repository realization:

```text
connect -> reset -> validate/register all payloads once
        -> activate/replan many designs -> reset/close
```

Repeated evaluator calls do not register again. Reconnecting requires a new
adapter and registration. Adapter instrumentation records registration count,
activation count, and planner-called query IDs without changing PostgreSQL.

## Incidence and native CE boundary

`IncidenceIndex` stores `candidate_id -> frozenset(query_id)`, rejects unknown
query IDs and duplicate candidate entries, and defines ADD/DROP/SWAP affected
sets as the relevant endpoint set or endpoint union. False positives are valid;
false negatives violate the evaluator contract.

Every affected query enters `EXPLAIN (FORMAT JSON)`. Extraction recursively
requires exactly one plan node matching the query's target base relation and a
numeric `Plan Rows`; zero or multiple matches fail. No planner object or
query-internal semantic state is retained.

## Objective

Only positive-truth workload queries are accepted. For estimate `e` and truth
`t > 0`:

```text
e' = max(float(e), 1.0)
qerror = max(e' / t, t / e')
```

The workload loss is the unweighted `math.fsum` of contributions sorted by
stable query ID. Negative/non-finite estimates and non-positive truths fail.

## Evaluation operations

`evaluate_design(Y)` validates fixed order, activates all of `Y`, replans every
workload query, computes contributions, and returns the authoritative full state.

`evaluate_move(Y, move, state)` validates design and workload/repository lineage,
builds and activates the full counterfactual design, replans only the conservative
endpoint incidence, reuses the exact existing `QueryEvaluation` objects for all
other IDs, and deterministically reaggregates the objective.

## Correctness evidence

The integration fixture has five queries and six candidates: an unaffected
query, single-MCV query, overlapping-MCV query, FD query, and mixed MCV+FD query.
Fixture acquisition performs `ANALYZE` before repository construction; evaluator
hot-path operations perform none.

ADD and DROP each replan one query and reuse four; SWAP replans the union of two
queries and reuses three. For every move, query-local and full evaluation have
identical ordered designs, every estimate, every q-error contribution, and exact
aggregate loss. Planner-call instrumentation equals the affected ID tuple, while
reused entries retain Python object identity. Registration remains exactly six
calls across all designs and zero calls per move.

Physical/native spot checks cover empty, single MCV, catalog-order overlapping
MCV, FD-only, and mixed MCV+FD designs. Definition and payload fingerprints are
unchanged across evaluation.

## Limitations

- Workload ingestion and incidence derivation are fixture-provided, not general.
- Target extraction supports an unambiguous single base-relation node only.
- Native `Plan Rows` is the CE observation boundary; raw selectivity is not
  exposed.
- Repository acquisition is a fixture helper, not the deployment/acquisition
  workflow.
- There is no search, budget, batching, planner caching, or deployment logic.
Payload repositories record a realization state for every physical candidate:
`PRESENT` or `ABSENT_NATIVE`. Candidate identity, precedence, incidence, and
maintenance cost remain properties of the design definition. Missing or
unregistered repository entries remain correctness failures.
