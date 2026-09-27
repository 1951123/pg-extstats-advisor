# M0 implementation plan

## Vertical slice

Use a small fixed workload to connect to the patched PostgreSQL server, ingest
queries and positive truths, define MCV/FD candidates, acquire each payload once,
freeze the native repository, build conservative incidence, evaluate one design,
switch to a second design, replan only the incidence union, reuse unaffected
contributions, and aggregate q-error.

M0 succeeds only when hypothetical/native estimates agree with physical/native
references for the covered cases, while every move performs zero `ANALYZE` and
zero catalog mutation.

## Frozen payload repository format, version 1

The persisted repository is a versioned manifest plus opaque binary blobs:

```text
repository/
  manifest.json
  payloads/<candidate-id>.mcv.bin
  payloads/<candidate-id>.fd.bin
```

Each manifest candidate contains:

- stable candidate ID and backend-local registration handle;
- database/server version, database identity, relation OID and qualified name;
- mechanism kind (`mcv` or `fd`), ordered attribute numbers/names, and definition
  SQL/metadata;
- explicit effective-precedence rank;
- native payload blob path, length, and SHA256;
- interpretation metadata required to validate the blob against its definition;
- acquisition transaction/time, `ANALYZE` and statistics-target settings,
  relation fingerprint, tool version, and upstream/patch commit provenance;
- optional decoded diagnostic view, explicitly non-authoritative.

The backend receives native bytes plus validated metadata. It never executes a
decoded JSON form.

## External evaluator API

```python
def evaluate_design(design: Design) -> EvaluationState: ...

def evaluate_move(
    current_design: Design,
    move: Move,
    current_state: EvaluationState,
) -> MoveEvaluation: ...
```

`EvaluationState` is immutable and records ordered design identity, repository
lineage, per-query estimates/truths/q-errors, aggregate loss, and evaluation
provenance. `evaluate_move` computes ADD incidence, DROP incidence, or the union
of SWAP endpoint incidence; activates the complete resulting ordered design;
replans those query IDs through native PostgreSQL; and structurally shares or
copies unaffected contributions.

## Implementation order

1. Define typed IDs, candidates, designs, moves, workload records, and immutable
   evaluation states.
2. Implement manifest/schema validation, blob hashing, and acquisition provenance.
3. Implement the PostgreSQL backend-local repository, design activation, and
   regression tests in the tracked patch.
4. Prove the patch applies to pristine 16.14 and build/install from scratch.
5. Create one isolated acquisition fixture and freeze MCV/FD native payloads.
6. Implement workload parsing, positive-truth validation, and conservative
   candidate-query incidence.
7. Implement session control and native target-CE extraction.
8. Implement full-design and move evaluation with query-level reuse.
9. Run the correctness matrix below against physical/native states.

## Correctness matrix

- empty design;
- one MCV candidate;
- overlapping MCV candidates with precedence competition;
- FD only;
- mixed MCV and FD;
- ADD, DROP, and SWAP affected-query sets;
- full reevaluation versus query-level reuse equality;
- repeated switching with unchanged `pg_statistic_ext` and
  `pg_statistic_ext_data` fingerprints and no `ANALYZE`.

## Exit evidence

Record the pristine archive digest, patch digest and repository commit, server
build identity, payload-manifest digest, workload digest, each hypothetical and
physical estimate, affected-query sets, and catalog fingerprints. Complete search
remains outside M0.
