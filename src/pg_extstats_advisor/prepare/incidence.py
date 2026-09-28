"""Conservative structural candidate-to-query incidence derivation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.incidence.index import IncidenceIndex
from pg_extstats_advisor.models import CandidateId, QueryId
from pg_extstats_advisor.prepare.workload import IngestedWorkload
from pg_extstats_advisor.search.model import candidate_catalog_digest


@dataclass(frozen=True, slots=True)
class IncidenceArtifact:
    index: IncidenceIndex
    workload_digest: str
    candidate_catalog_digest: str
    edges: tuple[tuple[str, str, str], ...]
    fallback_query_count: int
    digest: str

    def write(self, path: Path) -> None:
        value = {
            "format_version": 1,
            "derivation_version": "pg16-base-extstats-structural-v1",
            "workload_digest": self.workload_digest,
            "candidate_catalog_digest": self.candidate_catalog_digest,
            "edges": [
                {"candidate_id": candidate, "query_id": query, "reason": reason}
                for candidate, query, reason in self.edges
            ],
            "total_edges": len(self.edges),
            "fallback_query_count": self.fallback_query_count,
            "digest": self.digest,
        }
        path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def derive_incidence(ingested: IngestedWorkload, catalog: CandidateCatalog) -> IncidenceArtifact:
    edges = []
    mapping: dict[CandidateId, set[QueryId]] = {
        item.candidate_id: set() for item in catalog.candidates
    }
    fallback_count = 0
    for inspection in ingested.inspections:
        fallback = inspection.derivation_mode == "conservative-fallback"
        fallback_count += int(fallback)
        for candidate in catalog.candidates:
            if candidate.relation_name != inspection.relation.qualified_name:
                continue
            overlap = len(set(candidate.attributes) & inspection.predicate_columns)
            if fallback or overlap >= 2:
                mapping[candidate.candidate_id].add(inspection.query_id)
                edges.append(
                    (
                        str(candidate.candidate_id),
                        str(inspection.query_id),
                        inspection.derivation_mode,
                    )
                )
    ordered_edges = tuple(sorted(edges))
    canonical = json.dumps(ordered_edges, separators=(",", ":")).encode()
    return IncidenceArtifact(
        IncidenceIndex(
            tuple((key, frozenset(value)) for key, value in mapping.items()),
            frozenset(ingested.workload.by_id),
        ),
        ingested.workload.digest,
        candidate_catalog_digest(catalog.candidates),
        ordered_edges,
        fallback_count,
        hashlib.sha256(canonical).hexdigest(),
    )
