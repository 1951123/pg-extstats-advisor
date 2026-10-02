import hashlib
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_cleanroom_patch_stack_is_tracked_and_ordered() -> None:
    hypothetical = ROOT / "pg/patches/postgresql-16.14-hypothetical-extstats.patch"
    sample = ROOT / "pg/patches/postgresql-16.14-analyze-sample-cache.patch"
    assert hashlib.sha256(hypothetical.read_bytes()).hexdigest() == (
        "22c7f48632585e81fd8a557dc8bffba873ac5da070aca31713e22c60261c3b4f"
    )
    assert hashlib.sha256(sample.read_bytes()).hexdigest() == (
        "0d8c3fb24d59c52e2548875b04403d81c3fc1dfe22c1cfd12c935fb7f1691bc5"
    )
    script = (ROOT / "scripts/build_postgres16_cleanroom.sh").read_text()
    assert script.index("PATCH_HYPOTHETICAL") < script.index("PATCH_SAMPLE_CACHE")
    assert "patch -d \"$SOURCE_DIR\" -p1 --batch --forward < \"$PATCH_HYPOTHETICAL\"" in script
    assert "patch -d \"$SOURCE_DIR\" -p1 --batch --forward < \"$PATCH_SAMPLE_CACHE\"" in script
    assert "rm -rf" not in script


def test_sample_cache_documentation_records_portable_identity() -> None:
    note = (ROOT / "notes/analyze-sample-cache.md").read_text()
    assert "canonical" in note
    assert "portable relation-identity" in note
    assert "221/221" in note
