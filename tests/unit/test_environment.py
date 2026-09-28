import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_frozen_upstream_patch_and_build_provenance() -> None:
    build = json.loads((ROOT / "experiments/environment/postgresql-16.14-build.json").read_text())
    tarball = Path(build["upstream_tarball_path"])
    patch = Path(build["patch_path"])
    assert hashlib.sha256(tarball.read_bytes()).hexdigest() == build["upstream_sha256"]
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == build["patch_sha256"]
    assert build["postgres_version"] == "16.14"
    assert build["install_prefix"] == str(ROOT / ".build/postgresql-16.14-install")
    assert not build["assertions_enabled"] and not build["debug_enabled"]


def test_dataset_provenance_and_absolute_loader_tools() -> None:
    expected = {"census": (2458285, 69), "dmv": (11591877, 11)}
    for name, (rows, columns) in expected.items():
        value = json.loads((ROOT / f"experiments/environment/{name}-dataset.json").read_text())
        assert value["postgres_version"] == "16.14"
        assert value["row_count"] == rows
        assert len(value["ordered_column_schema"]) == columns
        assert value["postgres_build_recipe_digest"]
        loader = (ROOT / value["preprocessing_import_script"]).read_text()
        assert ".build/postgresql-16.14-install/bin" in loader
        assert "PG_BIN" in loader
