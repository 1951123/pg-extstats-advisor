import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_authoritative_source_provenance_and_derived_patch() -> None:
    provenance = json.loads((ROOT / "pg/postgresql-source.json").read_text())
    patch = ROOT / provenance["derived_patch"]
    assert provenance["upstream_tag"] == "REL_16_14"
    assert provenance["upstream_commit"] == "0d1c00c624fa7367d4a895f44381887757289682"
    assert provenance["pgextadv_commit"] == "7e992ab6438fef2f8eb98c7a9ed30c9f1c816ce7"
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == provenance["derived_patch_sha256"]
    script = (ROOT / "scripts/export_postgres_patch.sh").read_text()
    assert "--binary --full-index --no-ext-diff --no-renames" in script
    assert "status --porcelain" in script
    assert "merge-base --is-ancestor" in script
    assert "rm -rf" not in script


def test_build_workflows_use_authority_or_aggregate_derivative() -> None:
    authoritative = (ROOT / "scripts/build_postgres16_authoritative.sh").read_text()
    aggregate = (ROOT / "scripts/build_postgres16_cleanroom.sh").read_text()
    assert "AUTHORITATIVE_COMMIT" in authoritative
    assert "PATCH_AGGREGATE" in aggregate
    assert "postgresql-16.14-hypothetical-extstats.patch" not in aggregate
    assert "postgresql-16.14-analyze-sample-cache.patch" not in aggregate


def test_sample_cache_documentation_records_authority() -> None:
    note = (ROOT / "notes/analyze-sample-cache.md").read_text()
    assert "postgresql-pgextadv" in note
    assert "derived advisor patch" in note
    assert "221/221" in note
