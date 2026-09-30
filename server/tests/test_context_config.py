"""Wave 0.3：DEFAULT_RUNTIME_CONFIG 的 context.* 默认键（spec §8 / plan §10）。"""

from __future__ import annotations

from tester_agent.context.budget import PROFILES
from tester_agent.context.models import ProfileName
from tester_agent.store.db import DEFAULT_RUNTIME_CONFIG


def test_context_runtime_config_defaults():
    cfg = DEFAULT_RUNTIME_CONFIG
    assert cfg["context.enabled"] is True
    assert cfg["context.policy_version"] == "cp-v1"
    assert cfg["context.step_window"] == 1
    assert cfg["context.chat_recent_turns"] == 6
    assert cfg["context.goal_overlap_floor"] == 0.05
    assert cfg["context.goal_drift_window"] == 5
    assert cfg["context.case_index_digest"] is False
    assert cfg["context.intervention.enabled"] is True


def test_context_profiles_match_budget_module():
    profiles = DEFAULT_RUNTIME_CONFIG["context.profiles"]
    assert set(profiles) == {p.value for p in ProfileName}
    for name, budget in PROFILES.items():
        row = profiles[name.value]
        assert row == {"p0": budget.p0, "p1": budget.p1, "p2": budget.p2}
