from pathlib import Path

import pytest

from pg_extstats_advisor.capture.bundle_v2 import (
    BundleV2VerificationError,
    verify_bundle_v2,
    write_bundle_root,
)
from pg_extstats_advisor.capture.target_policy import TargetGridPolicy


def _bundle(tmp_path: Path) -> Path:
    policy = TargetGridPolicy(allowed_targets=(100,), realization_ids=("A", "B", "C"))
    snapshot = {
        "mode": "strong_single_snapshot",
        "snapshot_identity": "snapshot-1",
        "coordinator": {"isolation": "repeatable_read", "read_only": True, "committed_after_workers": True},
        "components": {"metadata": "snapshot-1", "truth": "snapshot-1", "reservoir_grid": "snapshot-1"},
    }
    cells = [
        {
            "statistics_target": 100,
            "realization_id": label,
            "snapshot_identity": "snapshot-1",
            "native_analyze_equivalent": False,
            "acquisition_method": "deterministic_reservoir_v1",
            "artifact_cache_relative": f"dmv-m2-27a-v2/100/{label}",
            "sample_row_count": 10,
            "source_population_rows": 100,
            "semantic_sha256": f"semantic-{label}",
            "binary_sha256": f"binary-{label}",
            "artifact_identity": f"100-{label}",
            "seed": policy.seed(100, label),
        }
        for label in ("A", "B", "C")
    ]
    write_bundle_root(
        tmp_path,
        policy,
        snapshot,
        {"cells": cells},
        environment={"postgres_version": "16.14"},
        schema={"relation": "public.dmv"},
        workload={"effective_query_count": 1},
        truth={"query_count": 1},
        permissions={"select_allowed": True},
    )
    return tmp_path


def test_bundle_v2_validates_target_identity(tmp_path: Path) -> None:
    assert verify_bundle_v2(_bundle(tmp_path))["target_cells"] == 3
    bundle = (tmp_path / "acquisition.json").read_text()
    (tmp_path / "acquisition.json").write_text(bundle.replace('"seed":  ', '"seed":  '))


def test_bundle_v2_rejects_snapshot_mismatch(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    path = root / "acquisition.json"
    text = path.read_text().replace('"snapshot_identity": "snapshot-1"', '"snapshot_identity": "wrong"', 1)
    path.write_text(text)
    with pytest.raises(BundleV2VerificationError, match="snapshot mismatch"):
        verify_bundle_v2(root)


def test_bundle_v2_rejects_wrong_target_identity(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    path = root / "acquisition.json"
    text = path.read_text().replace('"statistics_target": 100', '"statistics_target": 300', 1)
    path.write_text(text)
    with pytest.raises(BundleV2VerificationError, match="unexpected target cell"):
        verify_bundle_v2(root)
