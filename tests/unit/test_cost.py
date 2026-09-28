from decimal import Decimal

import pytest

from pg_extstats_advisor.candidates.model import CandidateCatalog
from pg_extstats_advisor.cost.model import MaintenanceBudget
from pg_extstats_advisor.cost.preset import PresetMaintenanceCostModel
from pg_extstats_advisor.models import Candidate, CandidateId, Design, MechanismKind


def candidate(name: str, kind: MechanismKind, arity: int, rank: int) -> Candidate:
    return Candidate(
        CandidateId(name), 10, "r", kind, tuple(f"c{i}" for i in range(arity)), (), rank, 100 + rank
    )


def test_preset_candidate_and_design_cost() -> None:
    model = PresetMaintenanceCostModel(Decimal(1), Decimal(2), Decimal(3), Decimal(4))
    mcv = candidate("m", MechanismKind.MCV, 2, 0)
    fd = candidate("f", MechanismKind.FD, 3, 1)
    catalog = CandidateCatalog((mcv, fd))
    assert model.estimate_candidate(mcv) == 5
    assert model.estimate_candidate(fd) == 15
    assert model.estimate_design(Design(()), catalog) == 0
    assert model.estimate_design(Design((CandidateId("m"), CandidateId("f"))), catalog) == 20
    assert model.is_feasible(Design((CandidateId("m"),)), catalog, MaintenanceBudget(5, model.unit))


def test_cost_validation_digest_and_units() -> None:
    model1 = PresetMaintenanceCostModel(0, 1, 0, 2)
    model2 = PresetMaintenanceCostModel(0, 1, 0, 2)
    assert model1.digest == model2.digest
    assert len(model1.digest) == 64
    with pytest.raises(ValueError, match="non-negative"):
        PresetMaintenanceCostModel(-1, 1, 1, 1)
    with pytest.raises(ValueError, match="non-negative"):
        MaintenanceBudget(Decimal("NaN"), model1.unit)
    with pytest.raises(ValueError, match="units differ|does not match"):
        model1.is_feasible(Design(()), CandidateCatalog(()), MaintenanceBudget(0, "milliseconds"))
