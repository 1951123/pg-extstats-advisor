# Architecture

## Frozen system question

Given an extended-statistics design `Y`, compute its fixed target-workload CE
objective quickly and faithfully:

```text
design Y
  -> backend-local hypothetical extstats state
  -> conservative affected-query lookup
  -> native replan of affected queries
  -> reuse unaffected objective contributions
  -> aggregate target-workload objective
```

## Principles

### 1. Native CE is authoritative

PostgreSQL owns GreedyCover, compatibility and clause consumption, MCV
matching/combination, FD semantics, MCV-to-FD interaction, and precedence
behavior. The advisor does not duplicate these rules.

### 2. Payload realization is offline

A candidate definition and a realized payload are different objects. Candidates
are materialized in an isolated acquisition environment, sampled once with
`ANALYZE`, and frozen before search. The search hot path never analyzes or
regenerates payloads.

### 3. Incrementality ends at query granularity

The incidence index maps every candidate to a conservative set of potentially
affected query IDs. A move replans those queries and reuses all other cached
estimates and objective contributions. False positives are allowed; false
negatives are correctness failures. There is no query-internal dependency oracle,
winner cache, residual-clause state, or contribution cache.

### 4. Affected queries are replanned

M0 calls the native PostgreSQL planner for every affected query and extracts the
target CE. It does not freeze a final plan or cache `PlannerInfo`, `RelOptInfo`, or
`RestrictInfo`. Batching or specialization requires later profiling evidence.

### 5. Search is a client

The evaluator exposes design and move evaluation independently of search. A later
deterministic greedy plus ADD/DROP/SWAP client may be used, but no search algorithm
owns CE semantics.

### 6. Recommendations are deployable

Every selected design maps to concrete `CREATE STATISTICS` SQL. Deployment runs a
fresh `ANALYZE`; validation keeps frozen-hypothetical behavior, fresh payload
realization, native CE, plans, and eventual runtime measurements separate.

## Component ownership

1. **Workload**: stable IDs, SQL, truth, relation/clause metadata, cached estimates,
   and objective contributions.
2. **Candidates**: deterministic MCV/FD definitions and explicit precedence.
3. **Payloads**: acquisition provenance plus authoritative native binary payloads;
   decoded forms are diagnostics only.
4. **Incidence**: conservative candidate-to-query mapping.
5. **PostgreSQL adapter**: backend-local design activation and native CE extraction.
6. **Evaluator/objective**: query-level reuse and fixed positive-truth q-error sum.
7. **Search**: replaceable client.
8. **Deploy/validate**: executable SQL and frozen-versus-fresh validation.

## Evaluator API draft

```python
evaluate_design(design: Design) -> EvaluationState

evaluate_move(
    current_design: Design,
    move: Move,
    current_state: EvaluationState,
) -> MoveEvaluation
```

`EvaluationState` contains per-query native estimates, truths, objective
contributions, aggregate loss, and payload/design lineage. `evaluate_move` derives
the endpoint incidence union, activates the counterfactual ordered design,
replans exactly the affected queries, and returns an updated immutable state.

## PostgreSQL injection boundary

The proposed PG16 patch is deliberately narrow:

- after `RelationGetStatExtList()`, filter/reorder the local OID list before
  `get_relation_statistics()` creates `StatisticExtInfo` nodes;
- provide backend-local serialized payloads to `statext_mcv_load()` and
  `statext_dependencies_load()` while retaining native deserializers;
- keep default/off behavior byte-for-byte on the original catalog paths;
- keep repository and ordered design state backend-local with explicit lifetime;
- perform no per-move catalog write, `ANALYZE`, rollback switch, or relcache
  invalidation.

The old prototype validates these locations but its string GUC API, linear list
lookups, lazy catalog registration, and benchmark instrumentation are not the
production interface.
