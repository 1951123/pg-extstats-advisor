from __future__ import annotations

from pg_extstats_advisor.deploy.verification import _verify_object


def _selected() -> dict:
    return {
        "candidate_id": "cand-a",
        "statistics_name": "pgextadv_cand_a",
        "relation": "public.t",
        "attributes": ["a", "b"],
        "mechanism": "mcv",
    }


def _columns() -> list[dict]:
    return [
        {"attnum": 1, "name": "a", "attstattarget": -1},
        {"attnum": 2, "name": "b", "attstattarget": -1},
    ]


def _existing(*, kinds: list[str] | None = None, keys: list[int] | None = None) -> dict:
    return {
        "oid": 42,
        "relation_oid": 9,
        "name": "pgextadv_cand_a",
        "kinds": kinds or ["m"],
        "keys": keys or [1, 2],
        "target": 100,
    }


def test_verify_object_requires_definition_and_data_row() -> None:
    selected = _selected()
    present = _verify_object(
        selected,
        _existing(),
        [{"inherit": False, "row_present": True, "mcv_payload_present": True, "dependencies_payload_present": False}],
        9,
        _columns(),
        100,
    )
    assert present["status"] == "DEFINITION_PRESENT_DATA_PRESENT"
    assert present["data_materialized"] is True
    native_absent = _verify_object(
        selected,
        _existing(),
        [{"inherit": False, "row_present": True, "mcv_payload_present": False, "dependencies_payload_present": False}],
        9,
        _columns(),
        100,
    )
    assert native_absent["status"] == "DEFINITION_PRESENT_DATA_PRESENT"
    assert native_absent["payload_status"] == "ABSENT_NATIVE"
    absent = _verify_object(selected, _existing(), [], 9, _columns(), 100)
    assert absent["status"] == "DEFINITION_PRESENT_DATA_ABSENT"
    assert absent["data_materialized"] is False


def test_verify_object_rejects_wrong_kind_or_columns() -> None:
    selected = _selected()
    wrong_kind = _verify_object(selected, _existing(kinds=["f"]), [], 9, _columns(), 100)
    assert wrong_kind["status"] == "DEFINITION_MISMATCH"
    wrong_columns = _verify_object(selected, _existing(keys=[1, 3]), [], 9, _columns(), 100)
    assert wrong_columns["status"] == "DEFINITION_MISMATCH"
