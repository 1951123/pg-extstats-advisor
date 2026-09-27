"""Fixed-workload container and identity digest."""

import hashlib
import json
from dataclasses import dataclass

from pg_extstats_advisor.models import QueryId, WorkloadQuery


@dataclass(frozen=True, slots=True)
class Workload:
    workload_id: str
    queries: tuple[WorkloadQuery, ...]

    def __post_init__(self) -> None:
        ids = [query.query_id for query in self.queries]
        if not self.workload_id or len(ids) != len(set(ids)):
            raise ValueError("workload ID and unique query IDs are required")

    @property
    def by_id(self) -> dict[QueryId, WorkloadQuery]:
        return {query.query_id: query for query in self.queries}

    @property
    def digest(self) -> str:
        value = [
            {
                "id": query.query_id,
                "sql": query.sql,
                "truth": query.truth,
                "target_relation": query.target_relation,
                "relation_oids": sorted(query.relation_oids),
            }
            for query in sorted(self.queries, key=lambda item: item.query_id)
        ]
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()
