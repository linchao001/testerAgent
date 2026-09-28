"""context.budget：profile 预算表、校验、跨区 10% 余量。"""

from __future__ import annotations

import pytest

from tester_agent.context.budget import PROFILES, SPILL_RATE, ProfileBudget, validate_budget
from tester_agent.context.models import ContextPartition, ProfileName
from tester_agent.errors import ValidationError


def test_default_profiles_match_spec():
    assert PROFILES[ProfileName.CHAT] == ProfileBudget(p0=1500, p1=3000, p2=8000)
    assert PROFILES[ProfileName.PLAN] == ProfileBudget(p0=3000, p1=4000, p2=6000)
    assert PROFILES[ProfileName.EXECUTE] == ProfileBudget(p0=3000, p1=8000, p2=6000)
    assert PROFILES[ProfileName.REFLECT] == ProfileBudget(p0=2000, p1=2000, p2=4000)
    assert PROFILES[ProfileName.REVIEW] == ProfileBudget(p0=2500, p1=8000, p2=8000)
    assert PROFILES[ProfileName.CASE_ITEM] == ProfileBudget(p0=2000, p1=6000, p2=4000)


def test_all_six_profiles_present():
    assert set(PROFILES) == set(ProfileName)


def test_total_and_partition_access():
    b = PROFILES[ProfileName.CHAT]
    assert b.total() == 12500
    assert b.of(ContextPartition.P0) == 1500
    assert b.of(ContextPartition.P1) == 3000
    assert b.of(ContextPartition.P2) == 8000


def test_spill_rate_is_10_percent():
    assert SPILL_RATE == 0.10


def test_capacity_with_spill():
    # P2 可侵占其余两区预算和的 10%：8000 + (1500+3000)*0.1 = 8450
    b = PROFILES[ProfileName.CHAT]
    assert b.capacity(ContextPartition.P2) == 8450
    # P0：1500 + (3000+8000)*0.1 = 2600
    assert b.capacity(ContextPartition.P0) == 2600
    # 容量恒不小于本区预算
    for p in ContextPartition:
        assert b.capacity(p) >= b.of(p)


def test_validate_rejects_non_positive():
    with pytest.raises(ValidationError):
        validate_budget(ProfileBudget(p0=0, p1=100, p2=100), model_window=10000)
    with pytest.raises(ValidationError):
        validate_budget(ProfileBudget(p0=100, p1=-1, p2=100), model_window=10000)


def test_validate_rejects_over_model_window():
    # 总预算 10000 > 8000*0.8=6400
    with pytest.raises(ValidationError):
        validate_budget(ProfileBudget(p0=2000, p1=3000, p2=5000), model_window=8000)


def test_validate_allows_exact_80_percent_boundary():
    # 恰好 0.8 边界允许
    validate_budget(ProfileBudget(p0=2000, p1=2000, p2=2400), model_window=8000)


def test_validate_rejects_non_positive_window():
    with pytest.raises(ValidationError):
        validate_budget(PROFILES[ProfileName.CHAT], model_window=0)
