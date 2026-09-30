from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_product_cli_help_exposes_stable_commands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pg_extstats_advisor.cli", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "capture" in result.stdout
    assert "advise" in result.stdout
    assert "validate" in result.stdout
    assert "M2." not in result.stdout


def test_validate_malformed_recommendation_is_concise_and_nonzero(tmp_path: Path) -> None:
    path = tmp_path / "recommendation.json"
    path.write_text(json.dumps({"format_version": 1, "digest": "bad"}))
    result = subprocess.run(
        [sys.executable, "-m", "pg_extstats_advisor.cli", "validate", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 5
    assert "Traceback" not in result.stderr
