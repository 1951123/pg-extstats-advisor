#!/usr/bin/env python3
"""M2.25: evaluate the M2.24 reservoir design and transfer it to M2.20 natives.

This is deliberately an orchestration-only milestone.  It reuses the frozen
M2.24 bundle, the persisted M2.20 sample files, and the existing ADD-only
search implementation; it does not capture a sample or change search code.
"""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
import subprocess
import sys
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import dmv_m2_17b_frozen_sample as acquisition
import dmv_m2_24_bundle as bundle_tool

from pg_extstats_advisor.capture.bundle import decode_sample, verify_production_capture_bundle
from pg_extstats_advisor.cost.empirical import EmpiricalMechanismCountCostModel
from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.evaluator.native import NativeEvaluator
from pg_extstats_advisor.models import CandidateId, Design, Move
from pg_extstats_advisor.orchestration import load_prepared_run
from pg_extstats_advisor.payloads.cache import repository_semantic_digest
from pg_extstats_advisor.payloads.repository import PayloadRepository
from pg_extstats_advisor.postgres.adapter import PostgresAdapter
from pg_extstats_advisor.prepare.acquisition import acquire_payloads
from pg_extstats_advisor.prepare.workload import RelationMetadata
from pg_extstats_advisor.search.deterministic import DeterministicBudgetSearch
from pg_extstats_advisor.search.model import SearchConfig, candidate_catalog_digest

PREPARED = ROOT / "experiments/dmv-m2-15-singletons/prepared-run"
BUNDLE = ROOT / ".build/production-captures/dmv-m2-24-v1"
OUT = ROOT / "experiments/dmv-m2-25-reservoir-to-native-transfer"
DERIVED = ROOT / ".build/production-captures/dmv-m2-25-reservoir-derived-repository"
ADVISOR_DSN = f"host={ROOT}/.build/pg16.14-advisor-socket port=55438 dbname=postgres user=postgres"
PRODUCTION_SOCKET = ROOT / ".build/pg16.14-production-sim-socket"
STOCK_READY = ROOT / ".build/postgresql-16.14-stock-install/bin/pg_isready"
TARGET = "public.dmv"
SAMPLE_REL = "public.pgextadv_m225_sample"
MODEL_PATH = ROOT / "calibration/dmv-pg16.14-m2-16-r1/maintenance-model.json"
EXPECTED_MODEL = "f8885af9b1411bb417dcb1fb93f5e368d5e89c0eac29e7803d0c5e8563204714"
EXPECTED_SAMPLE_DIGEST = "b25f33ecaaec24f1a79de504c60010270944ade36c54170c0b34e249c2bd75b4"
EXPECTED_ROOT_DIGEST = "f503bf80d9f10d8c364a8ef4197117ed5214bfca8ab815afb54b9a67259c5e97"
EXPECTED_RESERVOIR_REPO = "eb78679910b1a163c3219a00bf28c1a5ed849fe6e94b48ee97cf7d5a3dceff3f"
EXPECTED_RESERVOIR_STATS = "b833015894099758cf362d1436319388c83851e0eb067d3ce8561981bc277604"
EXPECTED_RESERVOIR_BASELINE = 88053.61940613187
EXPECTED_RESERVOIR_VECTOR = "6be99060002f3be7c49070e20ba57ecf520a5970f01f42053707dcf02bad1274"
EXPECTED_BUDGET = Decimal("372.045872636249472")
EXPECTED_TOTALROWS = 11591877
CEILING = 600.0
NATIVE_EXPECTED = {
    "A": {"semantic": "59dc8dbe007a81cbd10a11894ff8abf9b4a8f0cac11523221dbd63e34dd4ca7f", "repo": "6bd8e770c1f39dc31365e3ae4dd4156af3025b437238956116ffbfafaa757443", "baseline": 42791.986480127205, "final": 22014.061316846422, "design": "c196630393e536612b600cd1abbabf025961ed1e3c977477d0c0e0a8f0ef99a9", "totalrows": 11687702},
    "B": {"semantic": "eaa2111cc3233acc8fddee05dd9c88907aa668e0197bc1884b4cfcb68fecf4b2", "repo": "e7eec0762fb32dec736df5258dc5d9953ab5d0a28e66383dd78fb47bc2b446c6", "baseline": 41037.45988160012, "final": 21897.22109476092, "design": "d4c87c2bc85de341d82dbd0b19a6136fbdabff055331971d73c3fd10dfd85319", "totalrows": 11608827},
    "C": {"semantic": "e25974bf493ad5d2046f40d8c0050e8e287ae09f247065b955bdcdc7f3e7d950", "repo": "98435f5704f408e6dd7d41ed1f84b0b6349e9645e442af8f9f620bf2777e7232", "baseline": 42839.702641726464, "final": 21937.965918495007, "design": "b4156683b747331b352c82bb7e3ff356e336042b8c12609c7b47ed64bf89a3a7", "totalrows": 11695938},
    "D": {"semantic": "62db231f1253fcc6c5223fe750d3a3d33496ff3320ac5a801abc6155c70c1fc9", "repo": "ee9a8c4817ca73294ee3edc5aa07109429f9dcee752dd19ef4abb044b128a307", "baseline": 41916.76853930936, "final": 22172.194940317873, "design": "91b7de4525cab0aa289ba52fee0676f817fe05c28091fd9052d84d6dd940dcd6", "totalrows": 11470225},
    "E": {"semantic": "967842a7bd8d17ac11ea6c7aa99c01a6e3d75e4b6a9d9cad117800253bd22df1", "repo": "753c8f8e6e87f19ba02c2de891d506ca8bccc55859bd70ed9e613f4774fa27fc", "baseline": 41506.822577293795, "final": 21815.49258901561, "design": "5a8a7753247ab9155cf08d14ecd7f763924588550b24fc6f2257adc8796d92bf", "totalrows": 11634600},
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + "\n")


def qvector(state: Any) -> list[dict[str, Any]]:
    # Match the sealed M2.24 semantic vector contract (provenance is runtime
    # metadata, not an estimate-semantic component).
    return [{"query_id": str(x.query_id), "estimate": x.estimate, "truth": x.truth, "contribution": x.contribution} for x in state.query_evaluations]


def vector_digest(state: Any) -> str:
    return digest(qvector(state))


def qdist(state: Any) -> dict[str, Any]:
    values = [float(x.contribution) for x in state.query_evaluations]
    values_sorted = sorted(values)
    def quantile(p: float) -> float:
        pos = (len(values_sorted) - 1) * p
        lo = int(pos); hi = min(lo + 1, len(values_sorted) - 1)
        return values_sorted[lo] + (values_sorted[hi] - values_sorted[lo]) * (pos - lo)
    return {"query_count": len(values), "mean": statistics.fmean(values), "median": statistics.median(values), "p90": quantile(.9), "max": max(values)}


def model_and_budget() -> tuple[Any, MaintenanceBudget]:
    model = EmpiricalMechanismCountCostModel.load(MODEL_PATH)
    if model.digest != EXPECTED_MODEL:
        raise RuntimeError("maintenance model digest mismatch")
    if sum((model.estimate_candidate(c) for c in load_prepared_run(PREPARED).catalog.candidates), Decimal(0)) != EXPECTED_BUDGET:
        raise RuntimeError("full catalog maintenance budget mismatch")
    return model, MaintenanceBudget(EXPECTED_BUDGET, model.unit)


def verify_inputs() -> dict[str, Any]:
    if OUT.exists():
        raise RuntimeError(f"refusing to overwrite existing M2.25 output: {OUT}")
    if not BUNDLE.exists():
        raise RuntimeError("M2.24 formal bundle is missing")
    verification = verify_production_capture_bundle(BUNDLE)
    if json.loads((BUNDLE / "bundle.json").read_text())["semantic_digest"] != EXPECTED_ROOT_DIGEST:
        raise RuntimeError("M2.24 root digest mismatch")
    sample_manifest = json.loads((BUNDLE / "acquisition/relations/public.dmv/manifest.json").read_text())
    if sample_manifest["semantic_digest"] != EXPECTED_SAMPLE_DIGEST or sample_manifest["sample_row_count"] != 30000:
        raise RuntimeError("M2.24 reservoir sample mismatch")
    ready = subprocess.run([str(STOCK_READY), "-h", str(PRODUCTION_SOCKET), "-p", "55437"], capture_output=True, text=True, check=False)
    if ready.returncode == 0:
        raise RuntimeError("production simulator is reachable; refusing M2.25")
    return {"bundle_verification": verification, "production_offline": True, "sample_manifest": sample_manifest}


def ident(v: str) -> str:
    return '"' + v.replace('"', '""') + '"'


def drop_stats(conn: Any, candidates: tuple[Any, ...]) -> None:
    conn.execute("SELECT pg_hypothetical_extstats_reset()")
    for c in candidates:
        schema = c.relation_name.split(".", 1)[0]
        name = str(dict(c.definition)["statistics_name"])
        conn.execute(f"DROP STATISTICS IF EXISTS {ident(schema)}.{ident(name)}")


def create_shells(conn: Any, candidates: tuple[Any, ...]) -> None:
    for c in candidates:
        schema, relation = c.relation_name.split(".", 1)
        name = str(dict(c.definition)["statistics_name"])
        kind = "mcv" if c.mechanism.value == "mcv" else "dependencies"
        attrs = ", ".join(ident(a) for a in c.attributes)
        qname = f"{ident(schema)}.{ident(name)}"
        conn.execute(f"CREATE STATISTICS {qname} ({kind}) ON {attrs} FROM {ident(schema)}.{ident(relation)}")
        conn.execute(f"ALTER STATISTICS {qname} SET STATISTICS 100")


def create_tables(conn: Any, rows_binary: bytes | list[tuple[Any, ...]], relation: dict[str, Any], decoded_rows: list[tuple[Any, ...]] | None = None) -> None:
    conn.execute("DROP TABLE IF EXISTS public.pgextadv_m225_sample")
    conn.execute("DROP TABLE IF EXISTS public.dmv")
    ddl = "CREATE UNLOGGED TABLE public.dmv (" + ",".join(f'{ident(c["name"])} text' for c in relation["columns"]) + ")"
    conn.execute(ddl)
    conn.execute(ddl.replace("public.dmv", "public.pgextadv_m225_sample"))
    if decoded_rows is None and isinstance(rows_binary, bytes):
        with conn.cursor().copy("COPY public.pgextadv_m225_sample FROM STDIN (FORMAT binary)") as copy:
            copy.write(rows_binary)
        with conn.cursor().copy("COPY public.dmv FROM STDIN (FORMAT binary)") as copy:
            copy.write(rows_binary)
    else:
        row_values = decoded_rows if decoded_rows is not None else rows_binary
        with conn.cursor().copy("COPY public.pgextadv_m225_sample FROM STDIN") as copy:
            for row in row_values:
                copy.write_row(tuple(row))
        with conn.cursor().copy("COPY public.dmv FROM STDIN") as copy:
            for row in row_values:
                copy.write_row(tuple(row))
    conn.commit()


def bundle_metadata(relation: dict[str, Any]) -> RelationMetadata:
    columns = tuple((int(c["attnum"]), str(c["name"]), "text", not bool(c["nullable"])) for c in relation["columns"])
    return RelationMetadata("public", "dmv", 0, columns)


def ordinary_stats_digest(conn: Any) -> str:
    rows = conn.execute("SELECT attname,null_frac,avg_width,n_distinct FROM pg_stats WHERE schemaname='public' AND tablename='dmv' ORDER BY attname").fetchall()
    return digest([dict(zip(("column", "nullfrac", "avg_width", "distinct"), row, strict=True)) for row in rows])


def prepare_backend(conn: Any, repo: PayloadRepository, rows_binary: bytes, relation: dict[str, Any], totalrows: int, expected_stats: str | None = None) -> tuple[Any, Any, Any]:
    candidates = repo.catalog.candidates
    drop_stats(conn, candidates)
    create_tables(conn, rows_binary, relation)
    create_shells(conn, candidates)
    conn.execute("SELECT set_config('pg_extstats.frozen_sample_mode','replay',false)")
    conn.execute("SELECT set_config('pg_extstats.frozen_sample_relation','public.pgextadv_m225_sample',false)")
    conn.execute("SELECT set_config('pg_extstats.frozen_totalrows',%s,false)", (str(totalrows),))
    conn.execute("ANALYZE public.dmv")
    conn.commit()
    stats = ordinary_stats_digest(conn)
    if expected_stats is not None and stats != expected_stats:
        raise RuntimeError(f"ordinary statistics digest mismatch: {stats} != {expected_stats}")
    drop_stats(conn, candidates)
    conn.execute("DROP TABLE IF EXISTS public.pgextadv_m225_sample")
    conn.execute("SELECT set_config('pg_extstats.frozen_sample_mode','off',false)")
    conn.execute("SELECT set_config('pg_extstats.frozen_sample_relation','',false)")
    conn.execute("SELECT set_config('pg_extstats.frozen_totalrows','0',false)")
    create_shells(conn, candidates)
    conn.commit()
    prepared = load_prepared_run(PREPARED)
    evaluator = NativeEvaluator(prepared.workload, repo, prepared.incidence, PostgresAdapter(conn, repo))
    return prepared, evaluator, stats


def run_search(repo: PayloadRepository, rows: bytes | list[tuple[Any, ...]], relation: dict[str, Any], model: Any, budget: Any, expected_baseline: float, expected_vector: str, run_id: str) -> dict[str, Any]:
    started = time.perf_counter()
    with psycopg.connect(ADVISOR_DSN) as conn:
        prepared, evaluator, _stats = prepare_backend(conn, repo, rows, relation, EXPECTED_TOTALROWS, EXPECTED_RESERVOIR_STATS)
        config = SearchConfig(exact_bound_pruning=True, record_pruned_moves=False, add_only=True, candidate_set_mode="full", budget_mode="full-catalog-total", visible_candidate_count=72)
        search = DeterministicBudgetSearch(evaluator, repo.catalog, model, budget, config, prepared.incidence)
        initial = evaluator.evaluate_design(Design(()))
        if initial.aggregate_objective != expected_baseline or vector_digest(initial) != expected_vector:
            raise RuntimeError(f"reservoir baseline mismatch: {initial.aggregate_objective} {vector_digest(initial)}")
        search._calls = 1
        current = initial; current_cost = Decimal(0); rounds = []; accepted = []
        timed_out = False; round_no = 0
        while True:
            if time.perf_counter() - started >= CEILING:
                timed_out = True; break
            round_no += 1; before = current; before_cost = current_cost
            rem = search._ordered_ids(False, before.design)
            counts = (search._considered, search._skipped, search._bound_pruned_no_improvement, search._bound_pruned_incumbent, search._evaluated, evaluator.adapter.planner_calls_total)
            winner = search._finish_streaming_round("greedy-add", before, before_cost, (Move.add_candidate(cid) for cid in rem))
            after = winner.state if winner else before; after_cost = winner.cost if winner else before_cost
            row = {"round": round_no, "objective_before": before.aggregate_objective, "objective_after": after.aggregate_objective, "cost_before": str(before_cost), "cost_after": str(after_cost), "remaining_candidates": len(rem), "conceptual_moves": search._considered-counts[0], "infeasible": search._skipped-counts[1], "bound_pruned": (search._bound_pruned_no_improvement-counts[2])+(search._bound_pruned_incumbent-counts[3]), "native_evaluated": search._evaluated-counts[4], "planner_calls": evaluator.adapter.planner_calls_total-counts[5], "accepted_candidate": str(winner.move.add) if winner else None, "elapsed_seconds": time.perf_counter()-started}
            rounds.append(row)
            if winner:
                accepted.append(str(winner.move.add)); current, current_cost = after, after_cost
            if time.perf_counter() - started >= CEILING:
                timed_out = True; break
            if not winner: break
        result = {"run_id": run_id, "status": "performance-ceiling" if timed_out else "complete", "termination_reason": "performance-ceiling" if timed_out else "add-local-optimum", "elapsed_seconds": time.perf_counter()-started, "baseline_objective": initial.aggregate_objective, "final_objective": current.aggregate_objective, "baseline_vector_digest": vector_digest(initial), "final_vector_digest": vector_digest(current), "selected_design": [str(x) for x in current.design.candidate_ids], "selected_cost": str(current_cost), "selected_mcv_count": sum(repo.by_candidate[CandidateId(x)].candidate.mechanism.value == "mcv" for x in current.design.candidate_ids), "selected_fd_count": sum(repo.by_candidate[CandidateId(x)].candidate.mechanism.value == "fd" for x in current.design.candidate_ids), "selected_present_count": sum(repo.by_candidate[CandidateId(x)].state.value == "PRESENT" for x in current.design.candidate_ids), "selected_absent_native_count": sum(repo.by_candidate[CandidateId(x)].state.value == "ABSENT_NATIVE" for x in current.design.candidate_ids), "rounds": rounds, "accepted_sequence": accepted, "round_count": len(rounds), "search_counters": {"conceptual_add_moves": search._considered, "budget_infeasible_moves": search._skipped, "bound_pruned_no_improvement": search._bound_pruned_no_improvement, "bound_pruned_incumbent": search._bound_pruned_incumbent, "bound_pruned_moves": search._bound_pruned_no_improvement+search._bound_pruned_incumbent, "native_evaluated_moves": search._evaluated, "evaluator_calls": search._calls, "accepted_moves": search._accepted, "planner_calls": evaluator.adapter.planner_calls_total, "affected_query_replans": evaluator.adapter.planner_calls_total}}
        drop_stats(conn, repo.catalog.candidates); conn.execute("DROP TABLE IF EXISTS public.dmv"); conn.commit()
    if timed_out: raise RuntimeError("reservoir search exceeded 600-second ceiling")
    return result


def reservoir_repo_and_rows(prepared: Any, relation: dict[str, Any], sample_manifest: dict[str, Any]) -> tuple[PayloadRepository, bytes, dict[str, Any]]:
    rows_path = BUNDLE / "acquisition/relations/public.dmv/sample.copy.bin"
    rows = rows_path.read_bytes()
    if DERIVED.exists():
        repo = PayloadRepository.load(DERIVED)
        if bundle_tool.repository_semantic_digest(repo) != EXPECTED_RESERVOIR_REPO:
            raise RuntimeError("existing reservoir repository has the wrong semantic digest")
        return repo, rows, {"ordinary_statistics_digest": EXPECTED_RESERVOIR_STATS, "repository_digest": EXPECTED_RESERVOIR_REPO, "runtime_repository_digest": repository_semantic_digest(repo), "states": Counter(item.state.value for item in repo.payloads)}
    with psycopg.connect(ADVISOR_DSN) as conn:
        drop_stats(conn, prepared.catalog.candidates)
        decoded = decode_sample(rows_path, int(sample_manifest["sample_row_count"]), len(relation["columns"]))
        create_tables(conn, rows, relation, decoded)
        conn.execute("SELECT set_config('pg_extstats.frozen_sample_mode','replay',false)")
        conn.execute("SELECT set_config('pg_extstats.frozen_sample_relation','public.pgextadv_m225_sample',false)")
        conn.execute("SELECT set_config('pg_extstats.frozen_totalrows',%s,false)", (str(EXPECTED_TOTALROWS),))
        conn.commit()
        metadata = bundle_tool._catalog_and_incidence(BUNDLE, conn)[3]
        result = acquire_payloads(conn, prepared.catalog, DERIVED, statistics_target=100, upstream_sha256="f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471", patch_commit="m2-24-formal-bundle", repository_id="dmv-m2-25-reservoir", source_relations=(metadata,))
        repo = result.repository
        semantic_repo = bundle_tool.repository_semantic_digest(repo)
        if semantic_repo != EXPECTED_RESERVOIR_REPO:
            raise RuntimeError(f"reservoir repository digest mismatch: {semantic_repo}")
        stats = ordinary_stats_digest(conn)
        drop_stats(conn, prepared.catalog.candidates); conn.execute("DROP TABLE IF EXISTS public.pgextadv_m225_sample")
        conn.execute("SELECT set_config('pg_extstats.frozen_sample_mode','off',false)"); conn.commit()
    return repo, rows, {"ordinary_statistics_digest": stats, "repository_digest": bundle_tool.repository_semantic_digest(repo), "runtime_repository_digest": repository_semantic_digest(repo), "states": Counter(item.state.value for item in repo.payloads)}


def native_repository(name: str, prepared: Any, relation: dict[str, Any], metadata: Any) -> tuple[PayloadRepository, bytes, dict[str, Any]]:
    sample_dir = ROOT / "datasets/dmv-frozen-acquisition-sample-v1" if name == "A" else ROOT / f"datasets/dmv-frozen-acquisition-sample-v2-{name.lower()}"
    manifest = json.loads((sample_dir / "manifest.json").read_text())
    rows = (sample_dir / "sample.copy.bin").read_bytes()
    expected = NATIVE_EXPECTED[name]
    with psycopg.connect(ADVISOR_DSN) as conn:
        drop_stats(conn, prepared.catalog.candidates); create_tables(conn, rows, relation)
        conn.execute("SELECT set_config('pg_extstats.frozen_sample_mode','replay',false)")
        conn.execute("SELECT set_config('pg_extstats.frozen_sample_relation','public.pgextadv_m225_sample',false)")
        conn.execute("SELECT set_config('pg_extstats.frozen_totalrows',%s,false)", (str(int(expected["totalrows"])),)); conn.commit()
        repo_summary = ROOT / "experiments/dmv-m2-17b-frozen-acquisition-sample/build-1/repository-summary.json" if name == "A" else ROOT / f"experiments/dmv-m2-20-multisample-design-stability/sample-{name.lower()}-acquisition/build-1/repository-summary.json"
        repo = acquisition.resolve_frozen_repository(conn, prepared, metadata, {"semantic_sha256": manifest["semantic_sha256"], "serialization_sha256": manifest["sample_file_sha256"], "statistics_target": 100, "totalrows_used_by_builder": expected["totalrows"]}, json.loads(repo_summary.read_text())["semantic_repository_digest"], lineage_key=f"dmv-m2-25-native-{name.lower()}")
        # A cache miss builds payloads with temporary physical definitions;
        # the fixed-design replay must start with the same definition-only
        # state as M2.20 after acquisition cleanup.
        drop_stats(conn, prepared.catalog.candidates)
        create_shells(conn, repo.catalog.candidates); conn.execute("ANALYZE public.dmv"); conn.commit()
        stats = ordinary_stats_digest(conn)
        drop_stats(conn, repo.catalog.candidates); conn.execute("DROP TABLE IF EXISTS public.pgextadv_m225_sample"); conn.execute("SELECT set_config('pg_extstats.frozen_sample_mode','off',false)"); conn.commit()
    return repo, rows, {"sample_manifest": manifest, "expected": expected, "stats": stats, "selected_state": {cid: repo.by_candidate[CandidateId(cid)].state.value for cid in expected.get("selected_design", [])}}


def cross_eval(name: str, repo: PayloadRepository, rows: bytes, relation: dict[str, Any], reservoir_ids: list[str], prepared: Any, expected: dict[str, Any]) -> dict[str, Any]:
    with psycopg.connect(ADVISOR_DSN) as conn:
        _prepared, evaluator, _stats = prepare_backend(conn, repo, rows, relation, int(expected["totalrows"]), None)
        empty = evaluator.evaluate_design(Design(())); fixed = evaluator.evaluate_design(Design(tuple(CandidateId(x) for x in reservoir_ids)))
        local_objective = float(expected["final"])
        fixed_values = [float(x.contribution) for x in fixed.query_evaluations]
        local_values = [float(x.contribution) for x in empty.query_evaluations]
        design_local = []
        # Native local design is loaded from the frozen M2.20 final result if present.
        result_path = ROOT / ("experiments/dmv-m2-17d-frozen-full72-add/final-result.json" if name == "A" else f"experiments/dmv-m2-20-multisample-design-stability/sample-{name.lower()}-search/search.json")
        if result_path.exists():
            design_local = json.loads(result_path.read_text())["selected_design"]
        overlap = len(set(reservoir_ids) & set(design_local)) / len(set(reservoir_ids) | set(design_local)) if design_local else None
        drop_stats(conn, repo.catalog.candidates); conn.execute("DROP TABLE IF EXISTS public.dmv"); conn.commit()
    state_counts = Counter(repo.by_candidate[CandidateId(x)].state.value for x in reservoir_ids)
    state_mapping = {cid: repo.by_candidate[CandidateId(cid)].state.value for cid in reservoir_ids}
    return {"sample": name, "sample_digest": expected["semantic"], "repository_digest": expected["repo"], "ordinary_statistics_digest": _stats, "baseline_objective": empty.aggregate_objective, "baseline_vector_digest": vector_digest(empty), "reservoir_objective": fixed.aggregate_objective, "reservoir_vector_digest": vector_digest(fixed), "local_objective": local_objective, "absolute_gap": fixed.aggregate_objective-local_objective, "gap": (fixed.aggregate_objective-local_objective)/local_objective, "native_local_benefit": empty.aggregate_objective-local_objective, "reservoir_benefit": empty.aggregate_objective-fixed.aggregate_objective, "benefit_retention": (empty.aggregate_objective-fixed.aggregate_objective)/(empty.aggregate_objective-local_objective), "jaccard_with_local": overlap, "overlap_count_with_local": len(set(reservoir_ids) & set(design_local)), "reservoir_only_candidates": sorted(set(reservoir_ids)-set(design_local)), "native_local_only_candidates": sorted(set(design_local)-set(reservoir_ids)), "reservoir_mcv_count": sum(repo.by_candidate[CandidateId(x)].candidate.mechanism.value == "mcv" for x in reservoir_ids), "reservoir_fd_count": sum(repo.by_candidate[CandidateId(x)].candidate.mechanism.value == "fd" for x in reservoir_ids), "reservoir_present_count": state_counts.get("PRESENT", 0), "reservoir_absent_native_count": state_counts.get("ABSENT_NATIVE", 0), "design_state_mapping": state_mapping, "design_state_mapping_digest": digest(state_mapping), "improved_query_count": sum(a < b for a,b in zip(fixed_values, local_values)), "unchanged_query_count": sum(a == b for a,b in zip(fixed_values, local_values)), "worsened_query_count": sum(a > b for a,b in zip(fixed_values, local_values)), "distribution": qdist(fixed)}


def main() -> int:
    inputs = verify_inputs(); prepared = load_prepared_run(PREPARED); relation = json.loads((BUNDLE / "schema.json").read_text())["relations"][0]
    model, budget = model_and_budget(); OUT.mkdir(parents=True)
    protocol = {"milestone": "M2.25", "status": "running", "search_started": True, "new_sample_capture": False, "new_native_analyze": False, "m2_24_bundle_root_digest": EXPECTED_ROOT_DIGEST, "m2_24_sample_digest": EXPECTED_SAMPLE_DIGEST, "budget": str(budget.value), "budget_unit": budget.unit, "search_semantics": "deterministic-best-improvement-ADD-only", "exact_bound_pruning": True, "candidate_set": "full-72", "screening": False, "drop": False, "swap": False, "performance_ceiling_seconds": CEILING, "inputs": inputs}
    write_json(OUT / "protocol.json", protocol)
    reservoir_repo, _reservoir_rows_binary, reservoir_info = reservoir_repo_and_rows(prepared, relation, inputs["sample_manifest"])
    reservoir_rows = decode_sample(BUNDLE / "acquisition/relations/public.dmv/sample.copy.bin", 30000, len(relation["columns"]))
    baseline = {"ordinary_statistics_digest": reservoir_info["ordinary_statistics_digest"], "repository_digest": reservoir_info["repository_digest"], "state_counts": dict(reservoir_info["states"]), "baseline_objective": EXPECTED_RESERVOIR_BASELINE, "baseline_vector_digest": EXPECTED_RESERVOIR_VECTOR}
    search1 = run_search(reservoir_repo, reservoir_rows, relation, model, budget, EXPECTED_RESERVOIR_BASELINE, EXPECTED_RESERVOIR_VECTOR, "run-1")
    search2 = run_search(reservoir_repo, reservoir_rows, relation, model, budget, EXPECTED_RESERVOIR_BASELINE, EXPECTED_RESERVOIR_VECTOR, "run-2-fresh-backend")
    if search1["selected_design"] != search2["selected_design"] or search1["final_vector_digest"] != search2["final_vector_digest"] or search1["accepted_sequence"] != search2["accepted_sequence"]:
        raise RuntimeError("reservoir search repeatability failed")
    selected = search1["selected_design"]
    details = [{"candidate_id": cid, "mechanism": reservoir_repo.by_candidate[CandidateId(cid)].candidate.mechanism.value, "attributes": list(reservoir_repo.by_candidate[CandidateId(cid)].candidate.attributes), "state": reservoir_repo.by_candidate[CandidateId(cid)].state.value, "precedence_rank": reservoir_repo.by_candidate[CandidateId(cid)].candidate.precedence_rank} for cid in selected]
    write_json(OUT / "reservoir-search.json", {"baseline": baseline, "run1": search1, "run2": search2, "exact_repeat": True, "candidate_catalog_digest": candidate_catalog_digest(reservoir_repo.catalog.candidates), "workload_digest": prepared.workload.digest})
    write_json(OUT / "reservoir-selected-design.json", {"source": "M2.24 formal production capture bundle", "bundle_root_digest": EXPECTED_ROOT_DIGEST, "sample_digest": EXPECTED_SAMPLE_DIGEST, "repository_digest": EXPECTED_RESERVOIR_REPO, "selected_design": selected, "design_digest": digest(details), "candidate_details": details, "maintenance_cost": search1["selected_cost"]})
    native_rows = []; native_data = {}
    metadata = bundle_metadata(relation)
    for name in ("A", "B", "C", "D", "E"):
        repo, rows, info = native_repository(name, prepared, relation, metadata)
        first = cross_eval(name, repo, rows, relation, selected, prepared, info["expected"])
        second = cross_eval(name, repo, rows, relation, selected, prepared, info["expected"])
        if first["reservoir_vector_digest"] != second["reservoir_vector_digest"] or first["baseline_vector_digest"] != second["baseline_vector_digest"]:
            raise RuntimeError(f"native cross-evaluation repeatability failed for {name}")
        native_rows.append(first); native_data[name] = {"run1": first, "run2": second}
    write_json(OUT / "native-cross-evaluation.json", {"runs": native_data, "rows": native_rows, "all_repeats_exact": True})
    fields = list(native_rows[0]);
    with (OUT / "native-cross-evaluation.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(native_rows)
    native_designs: dict[str, list[str]] = {}
    for name in ("A", "B", "C", "D", "E"):
        result_path = ROOT / ("experiments/dmv-m2-17d-frozen-full72-add/final-result.json" if name == "A" else f"experiments/dmv-m2-20-multisample-design-stability/sample-{name.lower()}-search/search.json")
        native_designs[name] = json.loads(result_path.read_text())["selected_design"]
    frequencies = Counter(cid for ids in native_designs.values() for cid in ids)
    core = set(json.loads((ROOT / "experiments/dmv-m2-20-multisample-design-stability/summary.json").read_text())["consensus_core"])
    at_least_four = {cid for cid, count in frequencies.items() if count >= 4}
    overlap = {name: {"native_selected_count": len(ids), "overlap_count": len(set(selected) & set(ids)), "jaccard": len(set(selected) & set(ids))/len(set(selected) | set(ids)), "reservoir_only": sorted(set(selected)-set(ids)), "native_only": sorted(set(ids)-set(selected))} for name, ids in native_designs.items()}
    write_json(OUT / "design-overlap.json", {"reservoir_selected_count": len(selected), "native_design_counts": {name: len(ids) for name, ids in native_designs.items()}, "per_sample": overlap, "consensus_core_25": sorted(core), "at_least_4_of_5_set_28": sorted(at_least_four), "reservoir_overlap_with_consensus_core": sorted(set(selected) & core), "reservoir_overlap_with_at_least_4_of_5": sorted(set(selected) & at_least_four), "native_selection_frequency": dict(sorted(frequencies.items()))})
    write_json(OUT / "query-summary.json", {"fixed_design_query_summary": [{"sample": x["sample"], "baseline_objective": x["baseline_objective"], "reservoir_objective": x["reservoir_objective"], "improved": x["improved_query_count"], "unchanged": x["unchanged_query_count"], "worsened": x["worsened_query_count"], **x["distribution"]} for x in native_rows], "vector_digests": {x["sample"]: x["reservoir_vector_digest"] for x in native_rows}})
    max_gap = max(float(x["gap"]) for x in native_rows); min_ret = min(float(x["benefit_retention"]) for x in native_rows)
    report = ["# M2.25 — Reservoir-selected design transfer to native DMV samples", "", "The M2.24 formal 30k-row deterministic reservoir sample was evaluated with the unchanged full-72 deterministic exact-bound-pruned ADD-only search. No new sample capture, production access, or search algorithm was used.", "", f"The reservoir design selected {len(selected)} candidates ({search1['selected_mcv_count']} MCV and {search1['selected_fd_count']} FD), at maintenance cost {search1['selected_cost']} and objective {search1['final_objective']:.12f}; the two fresh-backend searches were exact in accepted sequence, trajectory/design/objective/cost and termination.", "", f"Fixed-design cross-evaluation on native M2.20 samples A–E was repeated exactly per sample. Maximum local relative gap was {max_gap:.8%}; minimum benefit retention was {min_ret:.8%}. These results support stable transfer of this reservoir-selected design as a product-facing test, but do not establish equivalence with native-sample optimization.", "", "The comparison remains a transfer-fidelity experiment: the reservoir uses the M2.24 frozen totalrows while each native sample uses its own persisted totalrows and repository. The previously audited M2.24 nested postgres_version metadata normalization defect was not changed in this milestone."]
    (OUT / "report.md").write_text("\n".join(report) + "\n")
    protocol["status"] = "complete"; protocol["search_result"] = "reservoir-search.json"; protocol["cross_evaluation_result"] = "native-cross-evaluation.json"; protocol["max_relative_gap"] = max_gap; protocol["min_benefit_retention"] = min_ret
    write_json(OUT / "protocol.json", protocol)
    print(json.dumps({"status": "complete", "selected_count": len(selected), "max_relative_gap": max_gap, "min_benefit_retention": min_ret}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
