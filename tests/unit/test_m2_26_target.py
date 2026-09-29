import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from pg_extstats_advisor.payloads.cache import canonical_digest

_SPEC = importlib.util.spec_from_file_location(
    "dmv_m2_26_statistics_target_stability",
    Path(__file__).parents[2] / "tools/dmv_m2_26_statistics_target_stability.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
validate_target_lineage = _MODULE.validate_target_lineage


def test_m2_26_cache_identity_includes_global_target() -> None:
    assert canonical_digest({"statistics_target": 100}) != canonical_digest(
        {"statistics_target": 300}
    )


def test_m2_26_replay_rejects_target_mismatch(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        '{"acquisition_provenance": {"global_statistics_target": 100}}\n'
    )
    repository = SimpleNamespace(root=tmp_path)
    with pytest.raises(ValueError, match="repository target"):
        validate_target_lineage({"target": 300}, repository, 300)
