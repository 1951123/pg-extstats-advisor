"""Replay the frozen top-100 membership under canonical and lexical orders.

This is diagnostic only.  It never runs the advisor search, ANALYZE, or any
physical-statistics operation.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from census_advisor_top512_authoritative import ROOT, DB_NAME, load_inputs  # noqa: E402

from pgextstats_benchmarks.postgres.configuration_provider import (
    PLANNER_ORDER_CONTRACT,
    PostgreSQLStatisticsConfigurationProvider,
)
from pgextstats_benchmarks.postgres.connection import PostgresConnection
from pgextstats_benchmarks.postgres.estimate_provider import extract_plan_rows
from pgextstats_benchmarks.postgres.instance import PostgresInstance
from pgextstats_benchmarks.statistics_configuration import StatisticsConfiguration
from pg_extstats_advisor.objective.qerror import q_error


SEARCH_ID = "census-advisor-top100-search-v1"
REPOSITORY_ID = "census-statistics-repository-singleton-pgextadv-v1"
OUT_ID = "census-advisor-top100-replay-provenance-v1"
SEARCH_OBJECTIVE = 1373.7027573851321


def run(label: str, provider: PostgreSQLStatisticsConfigurationProvider, oids: list[int], workload: object, truth: dict[str, float], relation: str) -> dict[str, object]:
    active = provider.target.hypothetical_activate(oids)
    rows: list[dict[str, object]] = []
    for query in workload.queries:
        estimate = extract_plan_rows(provider.target.explain_json(query.sql), relation)
        rows.append({"query_id": query.query_id, "plan_rows": estimate, "q_error": q_error(estimate, truth[query.query_id])})
    objective = math.fsum(item["q_error"] for item in sorted(rows, key=lambda item: item["query_id"]))
    return {"label": label, "active_oids": active, "objective": objective, "rows": rows}


def main() -> int:
    repo, _catalog, workload, truth_artifact, extra, precedence = load_inputs()
    selected = json.loads((ROOT / "census/artifacts" / SEARCH_ID / "result.json").read_text())["selected_design"]
    canonical = sorted(selected, key=lambda item: precedence[item])
    lexical = sorted(selected)
    truth = {item.query_id: float(item.cardinality) for item in truth_artifact.query_results}
    provider = PostgreSQLStatisticsConfigurationProvider(PostgresConnection(host="localhost", port=55437, user="wqts", database="postgres"))
    instance = PostgresInstance(DB_NAME, DB_NAME, "READY", {"benchmark_id": "census"})
    try:
        provider.register_repository(instance, repo, root=ROOT)
        config = StatisticsConfiguration("top100-replay-diagnostic", repo.artifact_id, repo.repository_digest, tuple(lexical), repo.relation_identity, metadata={"planner_order_contract": PLANNER_ORDER_CONTRACT})
        oid_by_id = provider._oids
        p_oids = [oid_by_id[item] for item in canonical]
        l_oids = [oid_by_id[item] for item in lexical]
        p1 = run("P1", provider, p_oids, workload, truth, repo.relation_identity)
        lexical_result = run("L", provider, l_oids, workload, truth, repo.relation_identity)
        p2 = run("P2", provider, p_oids, workload, truth, repo.relation_identity)
    finally:
        provider.close()
    differences = [
        {"query_id": left["query_id"], "P": left["plan_rows"], "L": right["plan_rows"], "P_q_error": left["q_error"], "L_q_error": right["q_error"]}
        for left, right in zip(p1["rows"], lexical_result["rows"])
        if left != right
    ]
    registration_position = {state.candidate_id: index for index, state in enumerate(repo.candidate_states)}
    selected_order = [{"candidate_id": item, "precedence_rank": precedence[item], "search_design_position": index, "registration_position": registration_position[item], "synthetic_oid": oid_by_id[item]} for index, item in enumerate(canonical)]
    result = {
        "format": OUT_ID,
        "benchmark_id": "census",
        "search_objective": SEARCH_OBJECTIVE,
        "selected_order": selected_order,
        "registration_contract": {"order": "repository candidate_states order (lexical artifact order)", "synthetic_oid": "hash(relid,kind,candidate_id) with collision probing; independent of registration order"},
        "planner_order_contract": PLANNER_ORDER_CONTRACT,
        "configuration_serialization_order": lexical,
        "P1": p1,
        "L": lexical_result,
        "P2": p2,
        "comparison": {"P1_equals_P2": p1["rows"] == p2["rows"] and p1["objective"] == p2["objective"], "P_equals_search": p1["objective"] == SEARCH_OBJECTIVE, "P_objective": p1["objective"], "L_objective": lexical_result["objective"], "objective_delta_L_minus_P": lexical_result["objective"] - p1["objective"], "differing_query_count": len(differences), "first_20_differences": differences[:20]},
    }
    path = ROOT / "census/artifacts" / OUT_ID
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["comparison"], sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
