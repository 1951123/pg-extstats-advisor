from __future__ import annotations

from pathlib import Path

import pytest

from pg_extstats_advisor.payloads.cache import resolve_or_build_repository


class FakeRepository:
    def __init__(self, value: str):
        self.value = value
        self.payloads = ()


def fake_load(path: Path) -> FakeRepository:
    return FakeRepository((path / "value").read_text())


def fake_semantic(repository: FakeRepository) -> str:
    return repository.value


def _resolve(monkeypatch, cache, *, identity, expected, build):
    monkeypatch.setattr(
        "pg_extstats_advisor.payloads.cache.repository_semantic_digest", fake_semantic
    )
    return resolve_or_build_repository(
        cache,
        "fixture",
        identity=identity,
        expected_semantic_digest=expected,
        build_repository=build,
        load_repository=fake_load,
    )


def test_missing_cache_rebuilds_and_warm_hit_reuses(tmp_path, monkeypatch):
    calls = []

    def build(path):
        calls.append(path)
        path.mkdir(parents=True)
        (path / "value").write_text("semantic-1")
        return fake_load(path)

    first = _resolve(monkeypatch, tmp_path, identity={"sample": "s"}, expected="semantic-1", build=build)
    second = _resolve(monkeypatch, tmp_path, identity={"sample": "s"}, expected="semantic-1", build=build)
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert len(calls) == 1


@pytest.mark.parametrize("mutation", ["manifest", "payload", "partial"])
def test_corrupt_or_partial_cache_rebuilds(tmp_path, monkeypatch, mutation):
    calls = []

    def build(path):
        calls.append(1)
        path.mkdir(parents=True)
        (path / "value").write_text("semantic-2")
        return fake_load(path)

    _resolve(monkeypatch, tmp_path, identity={"sample": "s"}, expected="semantic-2", build=build)
    entry = tmp_path / "fixture"
    if mutation == "manifest":
        (entry / "cache-manifest.json").write_text("{}")
    elif mutation == "payload":
        (entry / "repository" / "value").write_text("wrong")
    else:
        (entry / "repository" / "value").unlink()
    result = _resolve(monkeypatch, tmp_path, identity={"sample": "s"}, expected="semantic-2", build=build)
    assert result.cache_hit is False
    assert len(calls) == 2


def test_wrong_identity_and_digest_fail_closed(tmp_path, monkeypatch):
    calls = []

    def build(path):
        calls.append(1)
        path.mkdir(parents=True)
        (path / "value").write_text("semantic-3")
        return fake_load(path)

    _resolve(monkeypatch, tmp_path, identity={"sample": "s"}, expected="semantic-3", build=build)
    wrong_identity = _resolve(monkeypatch, tmp_path, identity={"sample": "other"}, expected="semantic-3", build=build)
    assert wrong_identity.cache_hit is False
    with pytest.raises(ValueError, match="semantic digest"):
        _resolve(monkeypatch, tmp_path, identity={"sample": "s"}, expected="not-the-build", build=build)
    assert len(calls) == 3
