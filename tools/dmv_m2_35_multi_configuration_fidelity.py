"""M2.35: same-realization hypothetical/physical fidelity over many designs.

This harness deliberately reuses the M2.18 frozen-sample protocol.  It does
not search, resample, or change the frozen payload repository.  Every physical
configuration is rebuilt from the persisted sample and its native payloads are
compared with the frozen repository before its estimates are interpreted.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import random
import time
from pathlib import Path
from typing import Any

import psycopg

from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import CandidateId, Design, EvaluationState, QueryEvaluation
from pg_extstats_advisor.objective.qerror import aggregate_objective, q_error
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.payloads.repository import NativePayloadState, PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.postgres.extraction import extract_target_estimate
from pg_extstats_advisor.search.model import candidate_catalog_digest

ROOT = Path(__file__).resolve().parents[1]
PREPARED_ROOT = ROOT / "experiments/dmv-m2-15-singletons/prepared-run"
REPOSITORY_ROOT = ROOT / ".build/artifact-cache/dmv-m2-17b-frozen-sample-v1/repository"
SEARCH_ROOT = ROOT / "experiments/dmv-m2-17d-frozen-full72-add"
FROZEN_ROOT = ROOT / "datasets/dmv-frozen-acquisition-sample-v1"
OUT = ROOT / "experiments/dmv-m2-35-multi-configuration-fidelity"
DSN = f"host={ROOT}/.build/pg16.14-experiment-socket port=55436 dbname=pgextadv_exp16_dmv user=postgres"
TARGET = "public.dmv"
SAMPLE = "public.pgextadv_frozen_sample"
EXPECTED_BASE_STATS = "bf6db08fd40e3873e1fc51675b0ff77de011b20fa20bcdf4f2e5817c4f7fc4fc"
EXPECTED_BASELINE = 42791.986480127205
EXPECTED_BASELINE_VECTOR = "f3254350d068cbd625fffd5537cd25573961da71779977e73811c1d6acb4a93f"
EXPECTED_SAMPLE = "59dc8dbe007a81cbd10a11894ff8abf9b4a8f0cac11523221dbd63e34dd4ca7f"
EXPECTED_SAMPLE_BINARY = "c3b30ddfebf118cb9137bae122e2913e19e88d0cbee14b2c93e980c7693cf463"


def load_m218() -> Any:
    path = ROOT / "tools/dmv_m2_18_frozen_hyp_vs_physical.py"
    spec = importlib.util.spec_from_file_location("m2_18_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load M2.18 helper module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def vector_digest(state: EvaluationState) -> str:
    return digest([
        {
            "query_id": str(item.query_id),
            "estimate": item.estimate,
            "truth": item.truth,
            "contribution": item.contribution,
            "provenance": item.provenance,
        }
        for item in state.query_evaluations
    ])


def state_rows(state: EvaluationState) -> list[dict[str, Any]]:
    return [
        {
            "query_id": str(item.query_id),
            "estimate": item.estimate,
            "truth": item.truth,
            "contribution": item.contribution,
            "provenance": item.provenance,
        }
        for item in state.query_evaluations
    ]


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def drop_definitions(conn: psycopg.Connection[Any], repository: PayloadRepository) -> None:
    for frozen in repository.payloads:
        candidate = frozen.candidate
        schema = candidate.relation_name.split(".", 1)[0]
        name = str(dict(candidate.definition)["statistics_name"])
        conn.execute(f"DROP STATISTICS IF EXISTS {quote_ident(schema)}.{quote_ident(name)}")


def create_definitions(conn: psycopg.Connection[Any], candidates: tuple[Any, ...]) -> None:
    for candidate in candidates:
        schema, relation = candidate.relation_name.split(".", 1)
        mechanism = "mcv" if candidate.mechanism.value == "mcv" else "dependencies"
        name = str(dict(candidate.definition)["statistics_name"])
        attrs = ", ".join(quote_ident(item) for item in candidate.attributes)
        qname = f"{quote_ident(schema)}.{quote_ident(name)}"
        conn.execute(
            f"CREATE STATISTICS {qname} ({mechanism}) ON {attrs} "
            f"FROM {quote_ident(schema)}.{quote_ident(relation)}"
        )
        conn.execute(f"ALTER STATISTICS {qname} SET STATISTICS 100")


def set_replay(conn: psycopg.Connection[Any], mode: str) -> None:
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_mode", mode))
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_sample_relation", SAMPLE))
    conn.execute("SELECT set_config(%s,%s,false)", ("pg_extstats.frozen_totalrows", "11687702"))


def generic_physical_state(
    conn: psycopg.Connection[Any], workload: Any, design: Design, repository_digest: str
) -> EvaluationState:
    active = conn.execute("SELECT pg_hypothetical_extstats_active()").fetchone()[0]
    if active not in (None, [], ()):
        raise RuntimeError(f"physical-only state has active hypothetical overlay: {active}")
    version = str(conn.execute("SHOW server_version").fetchone()[0])
    evaluations: list[QueryEvaluation] = []
    for query in sorted(workload.queries, key=lambda item: item.query_id):
        row = conn.execute(f"EXPLAIN (FORMAT JSON) {query.sql}").fetchone()
        estimate = extract_target_estimate(row[0], query.target_relation)
        evaluations.append(QueryEvaluation(
            query.query_id, estimate, query.truth, q_error(estimate, query.truth),
            f"native-explain:{version}",
        ))
    return EvaluationState(
        design,
        tuple(evaluations),
        aggregate_objective(evaluations),
        repository_digest,
        workload.digest,
        version,
        "physical-native-explain",
        tuple(item.query_id for item in evaluations),
        (),
    )


def payload_match(repository: PayloadRepository, actual: dict[str, dict[str, Any]], ids: tuple[CandidateId, ...]) -> bool:
    for candidate_id in ids:
        frozen = repository.by_candidate[candidate_id]
        observed = actual[str(candidate_id)]
        expected_digest = frozen.payload_sha256
        if observed["physical_realization_state"] != frozen.state.value:
            return False
        if observed["physical_payload_digest"] != expected_digest:
            return False
    return True


def normalized(catalog: Any, candidates: list[Any] | tuple[Any, ...]) -> tuple[Any, ...]:
    ids = {candidate.candidate_id for candidate in candidates}
    return tuple(catalog.by_id[item] for item in catalog.normalize_design(ids).candidate_ids)


def config_overlap(candidates: tuple[Any, ...]) -> bool:
    return any(set(left.attributes) & set(right.attributes) for i, left in enumerate(candidates) for right in candidates[i + 1:])


def make_configurations(repository: PayloadRepository) -> list[dict[str, Any]]:
    catalog = repository.catalog
    ordered = list(catalog.candidates)
    mcv = [item for item in ordered if item.mechanism.value == "mcv"]
    fd = [item for item in ordered if item.mechanism.value == "fd"]
    absent = [item for item in ordered if repository.by_candidate[item.candidate_id].state is NativePayloadState.ABSENT_NATIVE]
    by_id = catalog.by_id
    configs: list[dict[str, Any]] = []

    def add(name: str, source: str, items: list[Any] | tuple[Any, ...]) -> None:
        selected = normalized(catalog, items)
        ids = [str(item.candidate_id) for item in selected]
        if any(row["selected_design"] == ids for row in configs):
            return
        configs.append({
            "config_id": f"cfg-{len(configs) + 1:02d}",
            "name": name,
            "source": source,
            "selected_design": ids,
            "selected_count": len(selected),
            "mcv_count": sum(item.mechanism.value == "mcv" for item in selected),
            "fd_count": sum(item.mechanism.value == "fd" for item in selected),
            "includes_absent_native": any(repository.by_candidate[item.candidate_id].state is NativePayloadState.ABSENT_NATIVE for item in selected),
            "overlap": config_overlap(selected),
        })

    add("empty", "empty design", [])
    add("mcv-singleton", "MCV singleton", mcv[:1])
    add("fd-singleton", "FD singleton", fd[:1])
    if absent:
        add("absent-native-singleton", "ABSENT_NATIVE singleton", absent[:1])
    add("mcv-small", "first three MCV candidates", mcv[:3])
    add("fd-small", "first three FD candidates", fd[:3])
    add("mixed-small", "two MCV plus two FD", mcv[:2] + fd[:2])
    add("same-columns-mcv-fd", "same-column MCV/FD pair", [mcv[0], fd[0]])
    add("mcv-medium", "first eight MCV candidates", mcv[:8])
    add("fd-medium", "first eight FD candidates", fd[:8])
    add("mixed-medium", "five MCV plus five FD", mcv[:5] + fd[:5])
    add("mcv-large", "all MCV candidates", mcv)
    add("fd-large", "all FD candidates", fd)
    add("full-universe", "all 72 candidates", ordered)

    search = json.loads((SEARCH_ROOT / "final-result.json").read_text())
    accepted = [by_id[CandidateId(str(item["candidate_id"]))] for item in search["accepted_sequence"]]
    for size in (1, 5, 10, 20, 30):
        add(f"trajectory-prefix-{size}", f"M2.17d accepted trajectory prefix {size}", accepted[:size])
    final = [by_id[CandidateId(str(item))] for item in search["selected_design"]]
    add("final-design-31", "M2.17d final 31-object design", final)

    rng = random.Random(20261001)
    for size in (7, 17, 29, 43):
        add(f"random-{size}", f"deterministic random subset seed 20261001 size {size}", rng.sample(ordered, size))
    if absent:
        add("absent-mixed", "ABSENT_NATIVE plus overlapping MCV/FD", mcv[:2] + fd[:2] + absent)
    add("precedence-overlap", "precedence-ordered overlapping pairs", mcv[:6] + fd[:6])
    return configs


def evaluate_configuration(
    helpers: Any, prepared: Any, repository: PayloadRepository, config: dict[str, Any]
) -> dict[str, Any]:
    by_id = repository.catalog.by_id
    selected_ids = tuple(CandidateId(item) for item in config["selected_design"])
    selected = tuple(by_id[item] for item in selected_ids)
    all_candidates = repository.catalog.candidates
    design = Design(selected_ids)
    started = time.perf_counter()
    with psycopg.connect(DSN) as conn:
        helpers.drop_definitions(conn, repository)
        helpers.set_replay(conn, "off")
        helpers.load_sample(conn)
        conn.commit()
        h_rebuild_start = time.perf_counter()
        h_base, h_metadata, h_payloads = helpers.build_physical_sample(conn, repository, all_candidates)
        h_rebuild_seconds = time.perf_counter() - h_rebuild_start
        if not payload_match(repository, h_payloads, tuple(item.candidate_id for item in all_candidates)):
            raise RuntimeError(f"native payload mismatch while rebuilding frozen repository for {config['name']}")
        helpers.drop_definitions(conn, repository)
        helpers.create_definitions(conn, all_candidates)
        conn.commit()
        adapter = PostgresAdapter(conn, repository)
        evaluator = NativeEvaluator(prepared.workload, repository, prepared.incidence, adapter)
        h_eval_start = time.perf_counter()
        h_baseline = evaluator.evaluate_design(Design(()))
        h_state = evaluator.evaluate_design(design)
        h_eval_seconds = time.perf_counter() - h_eval_start
        active = conn.execute("SELECT pg_hypothetical_extstats_active()").fetchone()[0]
        if len(active or []) != len(selected):
            raise RuntimeError(f"hypothetical active count mismatch for {config['name']}: {active}")
        conn.execute("SELECT pg_hypothetical_extstats_reset()")
        p_rebuild_start = time.perf_counter()
        p_base, p_metadata, p_payloads = helpers.build_physical_sample(conn, repository, selected)
        p_rebuild_seconds = time.perf_counter() - p_rebuild_start
        if not payload_match(repository, p_payloads, selected_ids):
            raise RuntimeError(f"native payload mismatch for selected physical design {config['name']}")
        p_state = generic_physical_state(conn, prepared.workload, design, repository.digest)
        p_eval_seconds = time.perf_counter() - p_rebuild_start - p_rebuild_seconds
        if h_base != EXPECTED_BASE_STATS or p_base != EXPECTED_BASE_STATS or h_base != p_base:
            raise RuntimeError(f"ordinary statistics mismatch for {config['name']}: {h_base} / {p_base}")
        if h_metadata != p_metadata:
            raise RuntimeError(f"relation metadata mismatch for {config['name']}")
        if h_baseline.aggregate_objective != EXPECTED_BASELINE:
            raise RuntimeError(f"baseline objective mismatch for {config['name']}: {h_baseline.aggregate_objective}")
        if len(p_state.query_evaluations) != len(prepared.workload.queries):
            raise RuntimeError(f"physical query count mismatch for {config['name']}")
        if vector_digest(h_baseline) != EXPECTED_BASELINE_VECTOR:
            raise RuntimeError(f"baseline estimate vector mismatch for {config['name']}")
        h_digest = vector_digest(h_state)
        p_digest = vector_digest(p_state)
        exact = h_digest == p_digest and h_state.aggregate_objective == p_state.aggregate_objective and state_rows(h_state) == state_rows(p_state)
        if not exact:
            raise RuntimeError(f"hypothetical/physical mismatch for {config['name']}: {h_digest} / {p_digest}")
        helpers.cleanup_db(conn, repository)
    return {
        **config,
        "status": "PASS",
        "same_realization": True,
        "objective": h_state.aggregate_objective,
        "hypothetical_objective": h_state.aggregate_objective,
        "physical_objective": p_state.aggregate_objective,
        "estimate_vector_digest": h_digest,
        "hypothetical_estimate_vector_digest": h_digest,
        "physical_estimate_vector_digest": p_digest,
        "query_count": len(h_state.query_evaluations),
        "physical_payload_match": True,
        "estimate_exact_count": len(h_state.query_evaluations),
        "q_error_exact_count": len(h_state.query_evaluations),
        "selected_payload_exact_count": len(selected),
        "full_repository_payload_exact_count": len(all_candidates),
        "ordinary_statistics_digest": h_base,
        "relation_metadata_digest": h_metadata["digest"],
        "h_rebuild_seconds": h_rebuild_seconds,
        "h_evaluation_seconds": h_eval_seconds,
        "p_rebuild_seconds": p_rebuild_seconds,
        "p_evaluation_seconds": p_eval_seconds,
        "wall_seconds": time.perf_counter() - started,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as handle:
        fields = list(rows[0])
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="run only the first N configurations")
    parser.add_argument("--resume", action="store_true", help="reuse completed per-configuration JSON files")
    parser.add_argument("--rerun", action="store_true", help="re-evaluate configurations even when per-run JSON exists")
    args = parser.parse_args()
    if OUT.exists() and not args.resume:
        raise RuntimeError(f"refusing to overwrite existing output directory: {OUT}")
    helpers = load_m218()
    sample = helpers.verify_sample()
    if sample["semantic_sha256"] != EXPECTED_SAMPLE or sample["sample_file_sha256"] != EXPECTED_SAMPLE_BINARY:
        raise RuntimeError("frozen sample digest mismatch")
    prepared = load_prepared_run(PREPARED_ROOT)
    repository = PayloadRepository.load(REPOSITORY_ROOT)
    # The retained M2.17b cache has the same logical candidate definitions but
    # OIDs from its historical acquisition session.  PostgresAdapter resolves
    # current shell OIDs by statistics name, so compare logical identity while
    # deliberately excluding those session-local OIDs.
    prepared_by_id = {item.candidate_id: item for item in prepared.catalog.candidates}
    for candidate in repository.catalog.candidates:
        reference = prepared_by_id.get(candidate.candidate_id)
        if reference is None or (
            candidate.mechanism != reference.mechanism
            or candidate.attributes != reference.attributes
            or candidate.definition != reference.definition
            or candidate.precedence_rank != reference.precedence_rank
            or candidate.relation_name != reference.relation_name
        ):
            raise RuntimeError(f"repository logical candidate mismatch: {candidate.candidate_id}")
    configs = make_configurations(repository)
    if args.limit is not None:
        configs = configs[:args.limit]
    OUT.mkdir(parents=True, exist_ok=True)
    protocol = {
        "milestone": "M2.35",
        "status": "running",
        "purpose": "same-realization hypothetical-vs-physical fidelity across multiple designs",
        "system_head": __import__("subprocess").run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip(),
        "postgres_version": "16.14",
        "sample_semantic_sha256": sample["semantic_sha256"],
        "sample_file_sha256": sample["sample_file_sha256"],
        "sample_rows": sample["row_count"],
        "source_relation_rows": sample["source_relation_row_count"],
        "payload_repository_path": str(REPOSITORY_ROOT.relative_to(ROOT)),
        "payload_repository_digest": repository.digest,
        "payload_repository_realization_states": {state.value: sum(item.state is state for item in repository.payloads) for state in NativePayloadState},
        "payload_repository_catalog_digest": candidate_catalog_digest(repository.catalog.candidates),
        "workload_digest": prepared.workload.digest,
        "candidate_count": len(repository.catalog.candidates),
        "configuration_count": len(configs),
        "configuration_suite": configs,
        "new_search_runs": 0,
        "new_sampling_runs": 0,
        "new_maintenance_calibration_runs": 0,
        "same_persisted_sample_for_all_physical_states": True,
        "payload_match_required_before_interpretation": True,
    }
    write_json(OUT / "protocol.json", protocol)
    rows: list[dict[str, Any]] = []
    for config in configs:
        result_path = OUT / "runs" / f"{config['config_id']}.json"
        if args.resume and not args.rerun and result_path.exists():
            result = json.loads(result_path.read_text())
        else:
            print(f"[{config['config_id']}/{len(configs)}] {config['name']} ({config['selected_count']} objects)", flush=True)
            result = evaluate_configuration(helpers, prepared, repository, config)
            write_json(result_path, result)
        rows.append(result)
    write_csv(OUT / "configuration-results.csv", rows)
    summary = {
        "milestone": "M2.35",
        "status": "complete",
        "configuration_count": len(rows),
        "pass_count": sum(item.get("status") == "PASS" for item in rows),
        "all_same_realization": all(item.get("same_realization") for item in rows),
        "all_payloads_exact": all(item.get("physical_payload_match") for item in rows),
        "all_estimate_vectors_exact": all(item.get("hypothetical_estimate_vector_digest") == item.get("physical_estimate_vector_digest") for item in rows),
        "all_objectives_exact": all(item.get("hypothetical_objective") == item.get("physical_objective") for item in rows),
        "total_estimate_comparisons": sum(int(item.get("estimate_exact_count", 0)) for item in rows),
        "total_q_error_comparisons": sum(int(item.get("q_error_exact_count", 0)) for item in rows),
        "total_selected_payload_checks": sum(int(item.get("selected_payload_exact_count", 0)) for item in rows),
        "total_full_repository_payload_checks": sum(int(item.get("full_repository_payload_exact_count", 0)) for item in rows),
        "selected_absent_native_configurations": [item["config_id"] for item in rows if item.get("includes_absent_native")],
        "total_wall_seconds": sum(float(item.get("wall_seconds", 0.0)) for item in rows),
    }
    write_json(OUT / "summary.json", summary)
    protocol["status"] = "complete"
    protocol["summary"] = summary
    write_json(OUT / "protocol.json", protocol)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
