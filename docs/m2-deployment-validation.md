# M2 physical deployment and fresh-ANALYZE validation

## Semantic boundary

M2 keeps three states distinct:

- **A — hypothetical, frozen payload:** the native PostgreSQL estimate used by
  search for the selected design. `SearchResult.selected_state` retains this exact
  final `EvaluationState`; validation never reruns search to reconstruct it.
- **B — physical, same realization:** a controlled integration reference in which
  the definitions and payload realization used for acquisition remain physical.
  The fixture checks exact per-query Plan Rows, q-error, and aggregate equality
  with A. This is a semantic-equivalence control, not a fresh deployment result.
- **C — physical, fresh realization:** definitions are physically created in a
  disposable PostgreSQL cluster, the uniform target policy is applied, relations
  receive a real fresh `ANALYZE`, and the workload is replanned without the
  hypothetical overlay.

The difference between A and C is reported as **payload realization drift**. It
is not automatically classified as an overlay error or a system failure. M2
measures and attributes this difference; it does not average repeated ANALYZE,
model sampling noise, or perform robust optimization.

## Explicit workflow and isolation

`build_deployment_plan` validates the selected design against the frozen catalog
and emits deterministic `CREATE STATISTICS`, optional uniform `ALTER STATISTICS
... SET STATISTICS`, and `ANALYZE` statements. Search never deploys implicitly.
`PhysicalDeployer.deploy` executes the plan transactionally and records catalog
OIDs, relation identities, PostgreSQL version, environment identity, commands,
and timestamps. DDL or ANALYZE failure rolls back and propagates.

Integration uses a new temporary data directory and PostgreSQL cluster for every
run. It reconstructs the M0/M1 data distribution, explicitly drops acquisition
definitions before fresh deployment, and drops all created statistics and tables
before completion. The authoritative acquisition/search database is therefore
not mutated. Repeating an actual deployment without cleanup fails on PostgreSQL's
duplicate-object check; it is never silently accepted.

An empty design emits no `CREATE STATISTICS`, but callers supply validation
relations so fresh baseline `ANALYZE` still runs. The integration fixture covers
empty, single MCV, mixed MCV+FD, and the actual budget-4 M1-selected design.

## SQL and statistics-target policy

Object names use `pgextadv_<sanitized-candidate-prefix>_<sha256-prefix>`, bounded
to PostgreSQL's 63-byte identifier limit. Schema, object, relation, and attribute
identifiers are separately double-quoted; raw candidate metadata is never placed
into SQL as syntax. Candidate precedence determines statement order, and the full
statement sequence has a SHA256 digest.

The MVP fixes one statistics target before acquisition/search/validation. The
same target is applied to every deployed object; it is not a candidate variable,
cost feature, or per-candidate tuning dimension. This is an explicit MVP scope,
not a claim that PostgreSQL maintenance behavior is target-independent.

## Payloads, native CE, and report

Fresh MCV and dependency values are serialized with PostgreSQL's native send
functions and fingerprinted with SHA256; size, definition identity, catalog OID,
relation OID, and a relation fingerprint are recorded. OID equality is never used
as realization equality. Frozen and fresh hashes are compared candidate by
candidate.

Physical validation uses the same workload, target-plan-node extraction, q-error,
and deterministic aggregate functions as the evaluator, but does not activate
the hypothetical overlay. Each report records truth, frozen/fresh estimates and
q-errors, absolute/relative estimate differences, q-error difference, aggregate
objective drift, payload comparisons, deployment provenance, lineage digests,
budget/model/search identity, PostgreSQL/upstream/patch identity, row counts,
planner settings, and the uniform target. `write_validation_report` produces
canonical, human-inspectable JSON such as `validation-report.json`.

## Scope and limitations

M2 supports only MCV and functional-dependency candidates and base-relation CE.
It assumes compatible schema/data, PostgreSQL version, ordinary statistics, and
planner settings between acquisition and validation; provenance makes those
assumptions auditable. It does not restore arbitrary payload bytes into physical
catalogs, modify the PostgreSQL patch, fit maintenance costs, compare plan shapes,
measure execution runtime, deploy autonomously, or begin M3.
Payload validation distinguishes `PRESENT` and `ABSENT_NATIVE` realizations.
Fresh validation may therefore observe present-to-absent, absent-to-present,
or absent-to-absent transitions in addition to byte changes; a native SQL NULL
is not treated as a corrupt payload.
