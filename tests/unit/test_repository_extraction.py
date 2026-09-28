import hashlib
import json
from pathlib import Path

import pytest

from pg_extstats_advisor.models import CandidateId
from pg_extstats_advisor.payloads.repository import NativePayloadState, PayloadRepository
from pg_extstats_advisor.postgres.extraction import extract_target_estimate


def write_repository(root: Path, payload: bytes = b"native") -> None:
    (root / "payloads").mkdir()
    (root / "payloads" / "s1.mcv.bin").write_bytes(payload)
    manifest = {
        "format_version": 1,
        "repository_id": "fixture",
        "postgres_version": "16.14",
        "upstream_tarball_sha256": "a" * 64,
        "patch_commit": "b" * 40,
        "acquisition_provenance": {"analyze": "once"},
        "candidates": [
            {
                "candidate_id": "s1",
                "relation_oid": 10,
                "relation_name": "fixture",
                "mechanism": "mcv",
                "attributes": ["a", "b"],
                "definition": {"sql": "CREATE STATISTICS"},
                "precedence_rank": 1,
                "backend_oid": 20,
                "payload_path": "payloads/s1.mcv.bin",
                "payload_size": len(payload),
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "relation_fingerprint": "rows:1",
                "interpretation": {"format": "pg_mcv_list_send"},
            }
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest))


def test_repository_validation(tmp_path: Path) -> None:
    write_repository(tmp_path)
    repository = PayloadRepository.load(tmp_path)
    assert repository.catalog.candidates[0].candidate_id == CandidateId("s1")
    assert len(repository.digest) == 64
    (tmp_path / "payloads" / "s1.mcv.bin").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        PayloadRepository.load(tmp_path)


def test_strict_target_extraction() -> None:
    plan = [{"Plan": {"Node Type": "Seq Scan", "Relation Name": "r", "Plan Rows": 17}}]
    assert extract_target_estimate(plan, "r") == 17
    with pytest.raises(ValueError, match="matched 0"):
        extract_target_estimate(plan, "missing")
    ambiguous = [{"Plan": {"Relation Name": "r", "Plan Rows": 1, "Plans": [plan[0]["Plan"]]}}]
    with pytest.raises(ValueError, match="matched 2"):
        extract_target_estimate(ambiguous, "r")


def test_absent_native_round_trip_and_invalid_combinations(tmp_path: Path) -> None:
    manifest = {
        "format_version": 2, "repository_id": "absent", "postgres_version": "16.14",
        "upstream_tarball_sha256": "a" * 64, "patch_commit": "b" * 40,
        "acquisition_provenance": {}, "candidates": [{
            "candidate_id": "s1", "relation_oid": 10, "relation_name": "fixture",
            "mechanism": "fd", "attributes": ["a", "b"], "definition": {},
            "precedence_rank": 1, "backend_oid": 20, "state": "ABSENT_NATIVE",
            "payload_path": None, "payload_size": None, "payload_sha256": None,
            "relation_fingerprint": "rows:1", "interpretation": {},
        }],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    repository = PayloadRepository.load(tmp_path)
    item = repository.payloads[0]
    assert item.state is NativePayloadState.ABSENT_NATIVE
    assert item.payload is None
    assert item.payload_sha256 is None
    manifest["candidates"][0]["payload_size"] = 1
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="absent-native payload contains bytes"):
        PayloadRepository.load(tmp_path)
