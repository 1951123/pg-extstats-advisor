# Claims-to-evidence matrix

This matrix freezes the claims that the current prototype is entitled to make.
Evidence paths are tracked repository artifacts or code paths; milestone names
are provenance labels, not public feature names.

| ID | Precise claim | Scope and evidence | Code path | Limitations / non-claims |
|---|---|---|---|---|
| C1 | Hypothetical extended-statistics replay evaluates designs without search-time `ANALYZE`. | M2.21 replay/search artifacts and M2.33 cold/warm lifecycle; native hypothetical evaluation is used by `src/pg_extstats_advisor/evaluator/`. | `src/pg_extstats_advisor/evaluator/`, `src/pg_extstats_advisor/search/` | PostgreSQL 16.14, fixed base-relation scope, MCV/FD and frozen payloads only. |
| C2 | A same frozen realization reproduces ordinary statistics, payload state, baseline CE, and search result exactly within scope. | `experiments/census-m2-21-frozen-authoritative/replay-determinism.json`, `physical-validation.json`, `search/repeat.json`. | `src/pg_extstats_advisor/payloads/`, `src/pg_extstats_advisor/evaluator/`, `src/pg_extstats_advisor/search/` | Exactness is same-realization exactness; it is not independent-sample invariance. |
| C3 | Same-sample hypothetical and physical extended statistics agree under the established PG16.14 MCV/FD scope. | `experiments/dmv-m2-18-frozen-hyp-vs-physical/` and Census `physical-validation.json` report exact payload/estimate/q-error gates. | `src/pg_extstats_advisor/deploy/`, `src/pg_extstats_advisor/evaluator/` | Fixed persisted samples only; no claim about fresh `ANALYZE` payload equality. |
| C4 | Candidate utility is contextual; singleton-negative candidates cannot be safely discarded as a general pruning rule. | M2.20 `summary.json` records accepted singleton-negative moves and contextual rescues across five samples. | `src/pg_extstats_advisor/search/`, `src/pg_extstats_advisor/analysis/singleton.py` | This does not disprove submodularity or establish a universal pruning theorem. |
| C5 | A fixed design showed positive CE improvement across the established fresh-native DMV realizations in the predefined experiment. | M2.19 `summary.json`/`report.md`: the immutable 31-stat design was evaluated on 10 independent native PG16.14 realizations; `all_positive=true`, relative improvement range 43.751352%--48.623259%. | Search/evaluator code plus experiment protocol. | Fixed design, canonical DMV data distribution, ten runs; not universal robustness or design-selection stability. |
| C6 | Native sample realizations can select different designs while producing near-equivalent cross-evaluated CE outcomes in the established DMV experiment. | M2.20 `summary.json`/`report.md`: five persisted native samples, design Jaccard 0.7429--0.9375, interpretation `multiple near-equivalent designs`, complete 5x5 cross-evaluation. | `src/pg_extstats_advisor/search/`, `src/pg_extstats_advisor/evaluator/` | DMV evidence only; exact identities are not invariant and the statement is not a sampling-equivalence claim. |
| C7 | Stock PostgreSQL production can remain unpatched while production-side capture exports a sealed bundle for offline advice. | M2.33 `lifecycle-summary.json`, `build-provenance.json`, and capture bundle contract. | `src/pg_extstats_advisor/capture/`, `docs/production-capture-bundle-v1.md` | The capture role needs configured read-only privileges; transport/storage protection is external. |
| C8 | Advisor execution can continue after production becomes unavailable. | M2.33 `offline_advisor_while_production_stopped=true` and `unavailable_capture=PASS`. | `src/pg_extstats_advisor/workflow.py`, CLI `advise` | The sealed bundle and private advisor PostgreSQL must already exist. |
| C9 | The fixed external statistics target is part of problem/configuration identity and is not optimized by the core workflow. | `docs/statistics-target-scope.md`, target-identity unit tests, and M2.33 fixed `T=100` evidence. | `src/pg_extstats_advisor/statistics.py`, `src/pg_extstats_advisor/cost/empirical.py`, `src/pg_extstats_advisor/payloads/cache.py` | Maintenance models are target-specific; another target requires a compatible calibrated model. |
| C10 | Recommendation DDL is deployable on stock PostgreSQL 16.14 within scope. | M2.32 `deployment-pass.json` and M2.33 deployment PASS use standard `CREATE STATISTICS`/`ALTER STATISTICS`/`ANALYZE` SQL. | `src/pg_extstats_advisor/deploy/sql.py` | Deployment is DBA-controlled and not transactionally atomic or automatic. |
| C11 | Read-only preflight detects established version, target, schema, permission, and object incompatibilities. | M2.31 pass/target/schema/collision/equivalent/permission reports; M2.33 wrong-target and preflight PASS. | `src/pg_extstats_advisor/deploy/preflight.py` | Point-in-time compatibility evidence, not authorization or a guarantee against later drift. |
| C12 | Post-deployment verification distinguishes definition-only state from `ANALYZE`-materialized native extended-statistics state. | M2.32 `definition-without-analyze.json`, `deployment-pass.json`, and source-grounded materialization rule in its README; M2.33 missing-ANALYZE gate. | `src/pg_extstats_advisor/deploy/verification.py` | A data row can still contain a legitimate native NULL payload; verification does not compare bytes or CE. |
| C13 | Rollback verification confirms removal of recommendation-owned definitions without claiming restoration of a prior sampling realization. | M2.32 `rollback-pass.json` and `partial-rollback-fail.json`; `verify-rollback` CLI. | `src/pg_extstats_advisor/deploy/verification.py`, `src/pg_extstats_advisor/deploy/sql.py` | It verifies object absence only; it is not automatic repair or statistics restoration. |
| C14 | The Docker clean-room path reconstructs the supported system without host `.venv`, `.build`, host PostgreSQL installs, or author-machine runtime paths. | M2.33 `build-provenance.json`, `lifecycle-summary.json`, Docker guide, and non-root/no-host-mount checks. | `docker/*.Dockerfile`, `scripts/docker_cleanroom_m233.sh` | Semantic reproducibility only; no byte-for-byte image, wheel, or binary guarantee. |

## Non-claims

The project does **not** claim that:

- search finds a global optimum;
- `T=100` is universally optimal or `T=300` is preferred;
- the advisor sample equals a future production `ANALYZE` sample;
- reservoir sampling is native `ANALYZE`-equivalent;
- designs are invariant across sampling realizations;
- CE improvement implies latency improvement;
- joins, arbitrary PostgreSQL versions, higher arity, or arbitrary mechanisms
  are supported;
- Docker builds are byte-for-byte reproducible;
- rollback restores a prior PostgreSQL statistics realization;
- the current maintenance model transfers automatically to arbitrary targets;
- the project provides encryption, secret management, centralized auth,
  network policy, or automatic retention/deletion.

## Terminology and evidence discipline

Use `candidate definition`, `frozen payload realization`, `PRESENT`,
`ABSENT_NATIVE`, `UNREGISTERED`, `fixed statistics target`, `contextual
evaluation`, `recommendation bundle`, and `production capture bundle` as the
canonical terms. Numerical claims must be read from the cited tracked artifact;
historical artifacts are not rewritten to make the current narrative cleaner.
