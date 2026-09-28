from pathlib import Path

from pg_extstats_advisor.models import QueryId, WorkloadQuery
from pg_extstats_advisor.prepare.candidates import generate_candidates
from pg_extstats_advisor.prepare.config import PreparationConfig
from pg_extstats_advisor.prepare.incidence import derive_incidence
from pg_extstats_advisor.prepare.workload import IngestedWorkload, QueryInspection, RelationMetadata
from pg_extstats_advisor.workload.model import Workload


def config(*, arity: int = 3, cap: int | None = None, mechanisms=("mcv", "fd")):
    return PreparationConfig(
        1,
        "source",
        "acquisition",
        Path("workload.json"),
        Path("run"),
        mechanisms,
        arity,
        cap,
        (),
        100,
        (),
    )


def ingested(mode: str = "precise-structural") -> IngestedWorkload:
    relation = RelationMetadata(
        "public", "t", 10, ((1, "a", "integer"), (2, "b", "integer"), (3, "c", "integer"))
    )
    workload = Workload(
        "w",
        (
            WorkloadQuery(
                QueryId("q"), "SELECT * FROM t WHERE b=1 AND a=1 AND c=1", 1, "t", frozenset({10})
            ),
        ),
    )
    return IngestedWorkload(
        workload, (QueryInspection(QueryId("q"), relation, frozenset({"c", "a", "b"}), mode),)
    )


def test_candidate_generation_is_canonical_deterministic_and_capped() -> None:
    first = generate_candidates(config(), ingested())
    second = generate_candidates(config(), ingested())
    assert first == second
    assert len(first.candidates) == 8
    assert all(len(item.attributes) >= 2 for item in first.candidates)
    assert {item.attributes for item in first.candidates if len(item.attributes) == 3} == {
        ("a", "b", "c")
    }
    assert len(generate_candidates(config(arity=2), ingested()).candidates) == 6
    capped = generate_candidates(config(cap=2), ingested())
    assert capped.candidates == first.candidates[:2]
    assert {
        item.mechanism.value
        for item in generate_candidates(config(mechanisms=("mcv",)), ingested()).candidates
    } == {"mcv"}


def test_incidence_precise_and_fallback_are_conservative() -> None:
    precise = ingested()
    catalog = generate_candidates(config(arity=2, mechanisms=("mcv",)), precise)
    artifact = derive_incidence(precise, catalog)
    assert len(artifact.edges) == 3
    assert artifact.fallback_query_count == 0
    fallback = ingested("conservative-fallback")
    fallback_artifact = derive_incidence(fallback, catalog)
    assert len(fallback_artifact.edges) == len(catalog.candidates)
    assert fallback_artifact.fallback_query_count == 1
