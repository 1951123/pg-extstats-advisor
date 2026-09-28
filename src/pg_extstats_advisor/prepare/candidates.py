"""Deterministic workload-driven and explicit candidate generation."""

from __future__ import annotations

import hashlib
import itertools

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.models import Candidate, CandidateId, MechanismKind
from pg_extstats_advisor.prepare.config import PreparationConfig
from pg_extstats_advisor.prepare.workload import IngestedWorkload, RelationMetadata


def _candidate_id(relation: str, mechanism: str, attributes: tuple[str, ...]) -> CandidateId:
    identity = f"{relation}|{mechanism}|{','.join(attributes)}"
    return CandidateId(f"cand_{hashlib.sha256(identity.encode()).hexdigest()[:20]}")


def generate_candidates(config: PreparationConfig, ingested: IngestedWorkload) -> CandidateCatalog:
    relations = {item.relation.qualified_name: item.relation for item in ingested.inspections}
    specs: set[tuple[str, str, tuple[str, ...]]] = set()
    if config.explicit_candidates:
        source = (
            (item.relation, item.mechanism, item.columns) for item in config.explicit_candidates
        )
        for relation_name, mechanism, columns in source:
            metadata = relations.get(relation_name)
            if metadata is None:
                raise ValueError(
                    f"explicit candidate relation absent from workload: {relation_name}"
                )
            specs.add((relation_name, mechanism, _canonical_columns(metadata, columns)))
    else:
        for inspection in ingested.inspections:
            if inspection.derivation_mode != "precise-structural":
                continue
            metadata = inspection.relation
            ordered = _canonical_columns(metadata, tuple(inspection.predicate_columns))
            for arity in range(2, min(config.max_candidate_arity, len(ordered)) + 1):
                for group in itertools.combinations(ordered, arity):
                    for mechanism in config.mechanisms:
                        specs.add((metadata.qualified_name, mechanism, group))
    mechanism_order = {"mcv": 0, "fd": 1}
    ordered_specs = sorted(
        specs,
        key=lambda item: (
            item[0],
            mechanism_order[item[1]],
            len(item[2]),
            tuple(_attnum(relations[item[0]], column) for column in item[2]),
            item[2],
        ),
    )
    if config.max_candidates_per_relation is not None:
        kept, counts = [], {}
        for item in ordered_specs:
            count = counts.get(item[0], 0)
            if count < config.max_candidates_per_relation:
                kept.append(item)
                counts[item[0]] = count + 1
        ordered_specs = kept
    candidates = []
    for rank, (relation_name, mechanism, attributes) in enumerate(ordered_specs):
        metadata = relations[relation_name]
        attnums = tuple(_attnum(metadata, item) for item in attributes)
        candidates.append(
            Candidate(
                _candidate_id(relation_name, mechanism, attributes),
                metadata.oid,
                relation_name,
                MechanismKind(mechanism),
                attributes,
                (("attnums", attnums), ("schema", metadata.schema)),
                rank,
                0,
            )
        )
    return CandidateCatalog(tuple(candidates))


def _attnum(metadata: RelationMetadata, column: str) -> int:
    by_name = {name: attnum for attnum, name, _, _ in metadata.columns}
    try:
        return by_name[column]
    except KeyError as error:
        raise ValueError(f"unknown column {column!r} on {metadata.qualified_name}") from error


def _canonical_columns(metadata: RelationMetadata, columns: tuple[str, ...]) -> tuple[str, ...]:
    if len(set(columns)) != len(columns):
        raise ValueError("candidate columns contain duplicates")
    ordered = tuple(sorted(columns, key=lambda item: _attnum(metadata, item)))
    if len(ordered) < 2:
        raise ValueError("extended-statistics candidate requires at least two columns")
    return ordered
