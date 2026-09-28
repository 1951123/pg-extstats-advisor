"""Versioned frozen native-payload repository reader."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.models import Candidate, CandidateId, MechanismKind

FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class FrozenPayload:
    candidate: Candidate
    blob_path: Path
    payload: bytes
    payload_sha256: str
    acquisition: tuple[tuple[str, Any], ...]
    relation_fingerprint: str
    interpretation: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PayloadRepository:
    root: Path
    repository_id: str
    postgres_version: str
    upstream_sha256: str
    patch_commit: str
    payloads: tuple[FrozenPayload, ...]
    digest: str

    @property
    def catalog(self) -> CandidateCatalog:
        return CandidateCatalog(tuple(item.candidate for item in self.payloads))

    @property
    def by_candidate(self) -> dict[CandidateId, FrozenPayload]:
        return {item.candidate.candidate_id: item for item in self.payloads}

    @classmethod
    def load(cls, root: Path) -> PayloadRepository:
        manifest_path = root / "manifest.json"
        try:
            raw = manifest_path.read_bytes()
            manifest = json.loads(raw)
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read payload manifest: {error}") from error
        required = {
            "format_version",
            "repository_id",
            "postgres_version",
            "upstream_tarball_sha256",
            "patch_commit",
            "acquisition_provenance",
            "candidates",
        }
        missing = required - manifest.keys()
        if missing or manifest["format_version"] != FORMAT_VERSION:
            raise ValueError(f"invalid repository manifest; missing={sorted(missing)}")
        payloads: list[FrozenPayload] = []
        seen: set[str] = set()
        for record in manifest["candidates"]:
            item_required = {
                "candidate_id",
                "relation_oid",
                "relation_name",
                "mechanism",
                "attributes",
                "definition",
                "precedence_rank",
                "backend_oid",
                "payload_path",
                "payload_size",
                "payload_sha256",
                "relation_fingerprint",
                "interpretation",
            }
            item_missing = item_required - record.keys()
            if item_missing:
                raise ValueError(f"candidate manifest fields missing: {sorted(item_missing)}")
            candidate_id = str(record["candidate_id"])
            if candidate_id in seen:
                raise ValueError(f"duplicate candidate ID: {candidate_id}")
            seen.add(candidate_id)
            relative = Path(record["payload_path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("payload path must remain inside repository")
            blob_path = root / relative
            payload = blob_path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if len(payload) != record["payload_size"] or digest != record["payload_sha256"]:
                raise ValueError(f"payload integrity failure: {candidate_id}")
            candidate = Candidate(
                candidate_id=CandidateId(candidate_id),
                relation_oid=int(record["relation_oid"]),
                relation_name=str(record["relation_name"]),
                mechanism=MechanismKind(record["mechanism"]),
                attributes=tuple(record["attributes"]),
                definition=tuple(sorted(record["definition"].items())),
                precedence_rank=int(record["precedence_rank"]),
                backend_oid=int(record["backend_oid"]),
            )
            if candidate.backend_oid == 0:
                raise ValueError(f"acquired candidate has no backend OID: {candidate_id}")
            payloads.append(
                FrozenPayload(
                    candidate=candidate,
                    blob_path=blob_path,
                    payload=payload,
                    payload_sha256=digest,
                    acquisition=tuple(sorted(manifest["acquisition_provenance"].items())),
                    relation_fingerprint=str(record["relation_fingerprint"]),
                    interpretation=tuple(sorted(record["interpretation"].items())),
                )
            )
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        repository_digest = hashlib.sha256(canonical).hexdigest()
        return cls(
            root=root,
            repository_id=str(manifest["repository_id"]),
            postgres_version=str(manifest["postgres_version"]),
            upstream_sha256=str(manifest["upstream_tarball_sha256"]),
            patch_commit=str(manifest["patch_commit"]),
            payloads=tuple(payloads),
            digest=repository_digest,
        )
