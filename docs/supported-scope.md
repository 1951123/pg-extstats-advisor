# Supported scope

This document is the canonical scope for the current research-prototype
advisor. It describes the supported fixed-target workflow; historical
experiments remain evidence but do not silently expand this scope.

## Supported configuration

- PostgreSQL **16.14 exactly**. The advisor-side PostgreSQL is source-built
  from the verified upstream tarball plus the tracked hypothetical-statistics
  patch. Production may remain stock PostgreSQL 16.14.
- The verified deployment path is Docker. Native/VM or bare-host deployment
  has not been release-qualified.
- One base relation and base-relation selection cardinality estimation.
- A supplied, fixed global statistics target, default `T=100`. The target is
  part of configuration identity and is fixed before capture/advice/search.
- Arity exactly two, with MCV and functional-dependency (`dependencies`)
  extended-statistics candidates.
- A frozen statistics realization supplied by a sealed capture bundle and its
  persisted payload repository.
- A fixed-workload q-error objective with contextual candidate evaluation and
  deterministic local search. The implementation's local optimum is not a
  global-optimality claim.
- Standard PostgreSQL extended-statistics DDL, reviewed and executed by a
  DBA; deployment is never automatic.
- Read-only capture, bundle validation, offline advice, read-only preflight,
  read-only deployment verification, and read-only rollback verification.

The product candidate representation is dataset-agnostic. Candidate counts
such as Census's 4,506 or DMV's 72 describe particular benchmark universes,
not a product invariant.

## Unsupported scope

The current core does **not** support:

- joins or multi-table execution;
- PostgreSQL versions other than 16.14;
- automatic statistics-target selection or per-candidate target tuning;
- automatic deployment, automatic rollback, automatic repair, or
  transactionally atomic deployment;
- extstats arity above two;
- mechanisms other than MCV and functional dependencies;
- latency, runtime-performance, or end-to-end speed guarantees;
- global search optimality;
- a claim that a frozen sample equals a future production `ANALYZE` sample;
- secret management, artifact encryption, centralized authentication, network
  security policy, or automatic retention/deletion.

## Semantics and boundaries

Correctness in this scope means exact replay under the **same persisted frozen
realization**. Robustness is a separate empirical question measured across
independent native realizations. `PRESENT` and `ABSENT_NATIVE` are payload
states of a registered candidate; `UNREGISTERED` is a system failure state.
Maintenance cost belongs to the candidate definition, not to whether its
frozen payload happens to be present.

Incrementality stops at query granularity: affected queries are replanned and
unaffected objective contributions are reused. The workflow does not reuse a
fixed internal `PlannerInfo` or claim planner-internal incremental mutation.

The production trust zone performs read-only capture and exports a sealed
bundle. The advisor zone receives that bundle, uses a patched private
PostgreSQL for offline replay, and has no production credentials. Capture
bundles may contain sampled real values, exact truth, SQL, and constants;
recommendations may contain schema metadata, SQL, provenance, and DDL. Treat
both as potentially sensitive and apply external encryption, access control,
transport protection, and retention policy.

## Historical and experimental material

Target grids, Bundle v2, reservoir/native comparisons, target-selection studies,
and acquisition-fidelity investigations are retained under `experiments/` and
their milestone documents. They are historical/experimental evidence, not the
current product workflow. The authoritative fixed-sample Census lineage is
M2.21; the Docker clean-room reproduction is M2.33.
