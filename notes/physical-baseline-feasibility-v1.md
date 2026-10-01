# Physical-per-design Baseline Feasibility Study v1

## Scope and evidence inspected

This is a read-only feasibility assessment.  No PostgreSQL, Docker, search,
or benchmark run was started for this note, and no frozen experiment or
manuscript was changed.

System repository baseline:

- repository: `pg-extstats-advisor`
- HEAD / `origin/main`: `88797e4b82ff1d5c8bbba28dc27987af78ce78ad`
- branch: `main`, clean at inspection time

Relevant existing evidence inspected:

- `experiments/dmv-m2-18-frozen-hyp-vs-physical/`: a 31-statistics DMV
  same-realization hypothetical/physical validation.  It creates physical
  definitions, runs relation `ANALYZE`, then runs native `EXPLAIN`; it reports
  exact equality for 1,963 estimates, q-errors, and 31 payloads.  The artifact
  does not record per-stage or total wall-clock timings.
- `experiments/census-m2-21-frozen-authoritative/`: the reproducible Census
  search sequence (4,506 candidates, 112 accepted ADD moves, 19,210 conceptual
  moves, 16,769 native evaluations, 88,654 planner calls, about 341 seconds
  per recorded search run) and physical validation of the final design only.
  It is not a physical evaluation of each intermediate design.
- `experiments/m2-33-docker-cleanroom/`: clean-room lifecycle/provenance
  evidence.  It is a synthetic fixture and contains no per-design baseline.
- `src/pg_extstats_advisor/prepare/acquisition.py`: physical candidate
  acquisition (`CREATE STATISTICS`, `ALTER STATISTICS`, relation-grouped
  `ANALYZE`, native send-function extraction, and cleanup).
- `src/pg_extstats_advisor/deploy/physical.py`: deployment already times the
  `ANALYZE` portion (`analyze_elapsed_seconds`) and supports exact cleanup.
- `src/pg_extstats_advisor/deploy/sql.py` and
  `src/pg_extstats_advisor/validate/deployment.py`: deterministic DDL-plan
  generation, fresh physical evaluation through native `EXPLAIN`, and payload
  validation.
- `src/pg_extstats_advisor/calibration/runner.py`: an existing timing pattern
  for repeated owned-statistics activation and `ANALYZE`, but it measures
  aggregate ANALYZE calibration rather than workload objective evaluation.

The paper repository was read for scope only.  Its current evaluation and
claims matrix explicitly report operation counts rather than a physical-
per-design wall-clock speedup; the paper was not modified.

## A. Implementation feasibility

**Classification: MODERATE.**

The physical primitive already exists, so this does not require a new CE
semantics or PostgreSQL patch.  A controlled baseline would need a small new
orchestration/timing layer that, for each selected design in a predeclared
sequence:

1. starts from an isolated disposable database (or drops only
   recommendation-owned objects);
2. emits the existing deterministic `CREATE STATISTICS` and `ALTER STATISTICS`
   statements at the fixed target;
3. runs relation-grouped `ANALYZE`;
4. runs the same workload through native `EXPLAIN` and the same objective;
5. records catalog/data-row/payload identity and all timing components; and
6. drops the owned definitions and verifies cleanup.

The catalog handling is already implemented by the acquisition/deployment
helpers.  The new work is primarily isolation, sequence replay, timing, and
artifact schema.  It must not reuse the frozen payload bytes as if they were
the output of the physical baseline: physical `ANALYZE` creates a fresh native
realization unless the experiment deliberately uses the M2.18 fixed-sample
controls.  A digest mismatch would be an operational-cost observation, not a
same-realization fidelity result.

There is no need to modify the PostgreSQL patch.  A new experiment script and
possibly a small timing/report helper would be required; the frozen release
can remain unchanged.

## B. Experimental feasibility

**Bounded pilot: MODERATE.  Full Census per-search evaluation: HARD and not
appropriate for the current frozen release.**

Available controlled populations are:

- DMV M2.18: a 31-statistics design, persisted sample, ordinary-statistics
  state, PG16.14 build, workload, and truth vector are already fixed and
  physically reproducible.  This is the lowest-risk feasibility pilot.
- Census M2.21: a fixed 468-query workload, 4,506-candidate catalog, and
  deterministic accepted sequence are available.  The accepted sequence has
  112 states, but only its final state has physical validation today.

The comparison must hold constant: PostgreSQL 16.14 build/patch, relation and
sample lineage, ordinary statistics, global target, candidate sequence,
workload and truth vector, objective implementation, planner settings, and
cleanup policy.  Cold and warm executions should be reported separately, with
predeclared repetitions and no concurrent workload.

The full 19,210 Census conceptual-move population would require a physical
`CREATE/DROP + ANALYZE + workload EXPLAIN` cycle for every evaluated state.  It
is therefore not a reasonable submission-scope baseline.  Replaying only a
short prefix (for example 5--10 accepted states) or a small DMV design ladder
would answer feasibility without turning the experiment into another search.
The existing repository contains no measured per-design runtime from which to
claim a numerical runtime estimate.  Planning expectation is hours-scale for
a small multi-design DMV pilot, many-hours-to-days for all 112 Census accepted
states, and impractical for all 19,210 conceptual moves; these are scheduling
estimates, not results.

## C. Measurement design

Use the same predeclared design sequence in both paths.  A suitable pilot is
the empty design plus a small deterministic prefix (or a DMV ladder such as
1, 2, 4, 8, and final selected objects).  For every design record:

| Quantity | Physical path | Native substrate path |
|---|---|---|
| setup/materialization | `CREATE STATISTICS` and `ALTER STATISTICS` elapsed time; number of definitions | backend-local registration/activation elapsed time; number of active candidates |
| realization | `ANALYZE` elapsed time; catalog data-row count; requested payload state/digests | frozen repository state/digests and activation order |
| objective evaluation | per-query native `EXPLAIN` elapsed time, planner-call count, objective computation time | same per-query native `EXPLAIN`/objective fields, including incidence reuse and native-call count |
| teardown | `DROP STATISTICS` elapsed time and cleanup verification | overlay reset/deactivation time and cleanup verification |
| total | end-to-end wall clock, with the above components | end-to-end wall clock, with the above components |

Also record query/estimate/objective digests, PostgreSQL/build identity,
statistics target, relation/sample provenance, cache policy, and repetition
statistics (median, range, and coefficient of variation where meaningful).
Report materialization overhead separately from planner/objective time.  Do
not call a ratio a general speedup unless the pilot has matched these controls
and repeated both paths.

## D. Scientific positioning impact

The narrow scientific benefit is to quantify the physical materialization work
that the current architecture moves outside the search loop.  Framed as a
bounded cost characterization, it need not introduce a new CE mechanism or a
new optimizer contribution.  It should not be presented as proof of universal
PostgreSQL speedup, a tuning-system comparison, or an end-to-end advisor
performance claim.

If added to the paper, it would most naturally be a small RQ2 supplement or a
separate measurement paragraph: “quantifying avoided physical
materialization cost under this controlled sequence.”  It would require
updating the current explicit non-claim about wall-clock speedup and the C5/E15
claims ledger.  That positioning change is larger than the code primitive
itself and should not be made implicitly.

## E. Risks

### Paper risk

- A wall-clock table can invite requests for a full physical tuning baseline,
  competing advisors, or broader end-to-end speedup claims.
- Fresh physical `ANALYZE` realizations can diverge from frozen replay bytes;
  conflating that drift with timing would weaken the existing same-realization
  correctness boundary.
- Adding the result late would consume page space and require synchronized
  changes to RQ2, the claims matrix, and the non-claims language.

### System/reproducibility risk

- Reusing a long-lived database without strict owned-object cleanup can leak
  statistics between designs and invalidate the sequence.
- Physical `ANALYZE` is data- and cache-sensitive; a single timing is not a
  stable per-candidate cost model.
- Running the experiment inside the frozen release artifacts would blur the
  separation between authoritative replay evidence and a new diagnostic
  performance study.  A disposable clone and a new artifact namespace are
  required.
- No Docker or PostgreSQL patch change is intrinsically required, but a new
  runner must preserve the M2.33 stock-production/patched-advisor boundary if
  containerized.

## F. Effort estimate

- **Implementation:** approximately 8--16 engineering hours for a bounded
  runner/report schema and isolation checks, assuming the existing deployment
  helpers are reused; longer if per-state capture, repeated cold starts, or a
  Census-scale sequence is required.
- **Experiment:** no reliable wall-clock number exists before a pilot.  A
  5--10-state DMV pilot is expected to be hours-scale; all 112 Census accepted
  states is expected to be many-hours-to-days; all 19,210 conceptual moves is
  not a practical submission experiment.
- **Paper:** a compact table and qualification would occupy roughly
  0.25--0.5 page, plus claims-matrix and reproducibility updates.  This is an
  estimate, not a manuscript change in this study.

## G. Recommendation

**B — Add only if easy and it does not disturb the freeze.**

The system can support a small, isolated DMV pilot with moderate engineering
effort, and the result could directly answer the reviewer's cost question.
However, the current evidence is already sufficient for the frozen paper's
structural claim: no per-design/per-move `CREATE/DROP/ANALYZE` occurs in the
search loop.  A full Census physical baseline is too expensive and would
create avoidable positioning and reproducibility risk.  Therefore do not add
the experiment before submission unless a bounded pilot can be run in a
disposable environment, with no release or manuscript disturbance, and the
result is explicitly labeled diagnostic/quantification rather than a general
speedup claim.

## Conclusion

The physical-per-design baseline is technically feasible as a bounded
follow-up, not as a drop-in rerun of the full Census search.  Existing M2.18
and deployment infrastructure substantially reduce implementation risk, but
the repository currently has no per-design timing artifact.  Any future pilot
must preserve the existing semantic-versus-operational distinction and keep
the frozen release authoritative for correctness, bounded-work, and lifecycle
claims.
