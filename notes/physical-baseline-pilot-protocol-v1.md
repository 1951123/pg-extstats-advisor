# Physical Baseline Pilot Protocol v1

## Scope and non-goals

This note specifies a minimal, bounded protocol for a possible DMV pilot. It
is a design artifact only: no experiment, PostgreSQL run, Docker build, source
change, manuscript change, or frozen-artifact rewrite is part of this task.

The pilot is intended to quantify **bounded avoided physical materialization
cost**. It is not an advisor benchmark or a claim about production behavior.

## 1. Scientific objective

### Primary question

For the same small, predeclared sequence of DMV designs, how much measured
work is attributable to physical statistics materialization
(`CREATE/ALTER STATISTICS` plus `ANALYZE` and cleanup) compared with activating
an already captured native state in the backend-local substrate, while both
paths run the same native `EXPLAIN` workload evaluation?

The answer should be reported as an operation/time decomposition for the
controlled sequence, not as a universal ratio.

### Prohibited interpretations

The pilot does **not** establish:

- general speedup or lower end-to-end runtime;
- advisor superiority or better design quality;
- production performance or production cost savings;
- optimizer quality, optimality, or convergence improvement;
- deployment or DBA workflow improvement;
- a universal per-candidate PostgreSQL `ANALYZE` cost model;
- equivalence for future samples, other PostgreSQL versions, joins, or
  unsupported statistics mechanisms.

## 2. Realization mismatch and the comparison boundary

The physical path normally has this sequence:

```text
CREATE STATISTICS -> ANALYZE -> newly materialized native payload
```

The substrate path has this sequence:

```text
frozen repository payload/absence -> backend-local activation -> native EXPLAIN
```

These paths can produce different native realizations. Therefore an objective
value from a fresh physical realization must not be presented as a direct
quality comparison with the frozen replay objective. In particular, a changed
q-error objective can reflect sampling or statistics-state drift rather than
the activation mechanism.

The recommended protocol has two explicitly separated lanes:

1. **Matched-realization control.** Reuse the M2.18 controls: PG16.14 build and
   patch, persisted DMV sample, ordinary-statistics state, fixed target,
   workload, truth, and candidate order. For every pilot design, verify the
   physical payload/data-row state and, where applicable, payload digests
   against the repository. This lane establishes that the two paths are
   evaluating the same semantic realization. It is a validity gate, not a
   performance claim.
2. **Cost observation.** Time the physical and substrate setup/teardown and
   common native planning separately. If a design fails the matched-realization
   gate, retain its timing only as a diagnostic materialization observation and
   exclude its objective from a same-realization quality comparison.

The headline result, if any, should therefore be “measured materialization
work for this controlled DMV sequence,” not “the substrate is faster.” Common
`EXPLAIN`/objective time may be shown side by side only when workload,
planner settings, realization identity, and repetition policy are identical.

## 3. DMV candidate/design selection protocol

Use a small deterministic design ladder with **five designs**:

1. empty design;
2. the first accepted ADD state from the persisted DMV sequence;
3. an intermediate accepted state near the middle of the sequence;
4. the penultimate accepted state; and
5. the final M2.17d/M2.18 recommendation (31 objects: 8 MCV and 23 FD).

The exact intermediate indices must be frozen before execution (for example,
the first, middle, and penultimate rows of the existing accepted sequence),
and the selected design IDs and precedence order must be recorded in the
protocol manifest. The final design is included because M2.18 already proves
that its physical path is representable and semantically checkable.

Do not add random feasible designs in the primary pilot. Random designs would
consume budget without answering the reviewer’s materialization question and
would complicate interpretation of payload and objective drift. If a sanity
check is desired, at most one random feasible design may be run as explicitly
diagnostic and must not be pooled with the five primary states.

Do not run the Census accepted sequence or its 19,210 conceptual moves. The
DMV ladder is sufficient to exercise empty, small, intermediate, and final
physical states while keeping ANALYZE and workload EXPLAIN cost bounded.

For every state, use a disposable database or a strict owned-statistics
namespace. Start from the same ordinary-statistics/sample state, reset the
overlay, verify no prior owned definitions remain, and verify cleanup before
the next state.

## 4. Metrics and timing protocol

Use `time.perf_counter` around each operation. Predeclare warm-up and measured
repetitions (recommended: one untimed warm-up and three measured repetitions
per path/state). Run cold and warm cache policies as separate labels; do not
mix their measurements. Record median, minimum, maximum, and standard
deviation for each measured component.

### Physical materialization metrics

Record separately:

- `create_elapsed_seconds`: all `CREATE STATISTICS` statements;
- `alter_elapsed_seconds`: target-setting statements, if emitted separately;
- `analyze_elapsed_seconds`: relation-grouped `ANALYZE`;
- `catalog_check_elapsed_seconds`: OID/kind/data-row/payload-state checks;
- `cleanup_elapsed_seconds`: owned `DROP STATISTICS` and cleanup verification;
- `physical_setup_total_seconds`: create + alter + analyze + checks;
- `physical_end_to_end_seconds`: setup + common EXPLAIN/objective + cleanup;
- number of definitions, data rows, and requested payload states; and
- payload and ordinary-statistics digests.

### Hypothetical substrate metrics

Record separately:

- `repository_lookup_elapsed_seconds`;
- `activation_elapsed_seconds` (registration and ordered activation);
- `overlay_reset_elapsed_seconds`;
- `substrate_setup_total_seconds`;
- active candidate count/order and PRESENT/ABSENT_NATIVE state counts; and
- `substrate_end_to_end_seconds`, including cleanup/reset.

### Common metrics

For both paths record:

- per-query native `EXPLAIN` elapsed time;
- planner-call count and query count;
- objective computation elapsed time and aggregate objective;
- estimate-vector and objective digests;
- PostgreSQL/build identity, target, workload/truth digests, and cache policy.

Only the common EXPLAIN/objective components are directly comparable between
paths. Materialization and activation are intentionally separate components;
adding all components into a ratio is allowed only as a descriptive total for
this exact protocol. A realization mismatch invalidates a semantic/objective
comparison but does not erase the separately labeled physical timing.

## 5. Paper integration plan

Do not create a new research question. If the pilot is accepted, add a compact
paragraph and table under existing **RQ2**, after the bounded-work table. The
table should have one row per design state and columns for physical setup,
ANALYZE, cleanup, substrate activation, common EXPLAIN, and total descriptive
time. Keep DMV-specific scope in the caption.

The manuscript wording should say that the pilot *quantifies materialization
work for a controlled DMV sequence*. It must retain the current non-claim that
the evidence is not a general speedup or production-performance result. The
claims matrix would need a narrowly scoped C5/RQ2 evidence entry and removal
or qualification of the current “no wall-clock baseline” non-claim; no new
algorithmic contribution should be implied.

Expected page cost is approximately one-quarter to one-half page including a
small table and qualification. If that space or the qualification cannot be
accommodated without disturbing the camera-ready freeze, keep the result in a
supplementary artifact instead of changing the manuscript.

## 6. Reviewer impact

### Potential benefits

- Directly addresses why a backend-local substrate matters beyond semantic
  fidelity: it exposes the physical materialization step as a separately
  measurable cost.
- Makes the HypoPG analogy more precise by showing that the comparison is
  about native-state realization and lifecycle work, not a replacement CE.
- Converts the current structural “no per-design ANALYZE” statement into a
  bounded operational characterization without claiming universal speedup.

### New risks

- Any wall-clock table may cause reviewers to request a full physical tuning
  baseline, more workloads, or competing advisors.
- Fresh `ANALYZE` payloads may differ from frozen payloads; failing to report
  the matched-realization gate would conflate semantic drift with cost.
- A five-state DMV sequence cannot support broad scalability claims.
- Repeated DDL/ANALYZE can leak state or become cache-sensitive unless each
  state is isolated and cleanup is verified.

## 7. Implementation feasibility

**Classification: moderate (bounded pilot), hard (full Census replay).**

Current helpers are sufficient for the core operations:

- `build_deployment_plan` emits deterministic create/target/analyze SQL;
- `PhysicalDeployer` executes and already records ANALYZE elapsed time;
- `evaluate_physical` runs the native workload objective;
- M2.18 supplies the fixed-sample and payload-digest validation pattern; and
- cleanup helpers drop only owned definitions.

The pilot still needs a new experiment-level runner, per-component timers,
manifest schema, deterministic state selection, repeated-run aggregation, and
failure/cleanup checks. These should live in a new diagnostic artifact
namespace and must not modify frozen release code or artifacts. Docker clean
room does not need extension for the first pilot; if containerized later, use
the existing M2.33 role separation and record the image/build identities.

## 8. Final recommendation

**B — Design is valuable but defer after submission.**

The pilot has real scientific value and is feasible with moderate effort, but
the current paper is already frozen around semantic fidelity, bounded work,
contextual design, lifecycle closure, and bounded DMV robustness. The matched-
realization requirement and the risk of triggering a larger performance
baseline make a pre-submission run disproportionate to its likely benefit.

Keep this protocol ready as a post-submission diagnostic. If the reviewer
requires it before submission, run only the five-state DMV ladder in a
disposable environment, treat realization matching as a hard validity gate,
and integrate only a tightly qualified RQ2 supplement. Do not run Census
per-design materialization and do not turn the pilot into a new optimizer or
speedup study.

## Current repository facts

At protocol-design time, the system repository was at local commit
`f305696aea13d3fe89709cbc9e773074c54c3d49`, one local note commit ahead of
`origin/main` (`88797e4b82ff1d5c8bbba28dc27987af78ce78ad`). The paper repository
was not modified. No experiment was run for this protocol.
