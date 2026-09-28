"""context.models：三分区/状态/作用域枚举与 ContextEntry 等契约。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tester_agent.context.models import (
    AssemblyReport,
    ContextEntry,
    ContextPartition,
    ContextView,
    EntryKind,
    EntryRefs,
    EntryStatus,
    Phase,
    ProfileName,
    ScopeLevel,
)


def test_partition_enum_values():
    assert ContextPartition.P0.value == "P0"
    assert ContextPartition.P1.value == "P1"
    assert ContextPartition.P2.value == "P2"


def test_status_kind_scope_phase_profile_enums():
    assert EntryStatus.ACTIVE.value == "active"
    assert EntryStatus.DEMOTED.value == "demoted"
    assert EntryStatus.EVICTED.value == "evicted"
    assert {k.value for k in EntryKind} >= {
        "methodology", "kb_block", "plan", "decision", "artifact",
        "tool_result", "chat_turn", "reflection", "goal", "outline",
    }
    assert {s.value for s in ScopeLevel} == {"task", "batch", "item"}
    assert {p.value for p in Phase} == {"shared", "design", "write"}
    assert {p.value for p in ProfileName} == {
        "chat", "plan", "execute", "reflect", "review", "case_item",
    }


def _entry(**over):
    base = dict(
        entry_id="plan:p1:v1",
        partition=ContextPartition.P2,
        entry_kind=EntryKind.PLAN,
        content="计划正文",
        digest="计划 v1",
        created_at="2026-09-28T00:00:00.000Z",
    )
    base.update(over)
    return ContextEntry(**base)


def test_entry_defaults():
    e = _entry()
    assert e.status is EntryStatus.ACTIVE
    assert e.pinned is False
    assert e.refs == EntryRefs()
    assert e.scope_level is ScopeLevel.TASK
    assert e.phase is Phase.SHARED
    assert e.step_id is None
    assert e.step_seq == 0
    assert e.batch_id is None
    assert e.item_key is None
    assert e.turn_seq == 0
    assert e.tokens_est == 0
    assert e.supersedes is None
    assert e.source == "runtime"


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        _entry(unexpected="x")  # type: ignore[arg-type]


def test_invalid_partition_rejected():
    with pytest.raises(ValidationError):
        _entry(partition="P3")  # type: ignore[arg-type]


def test_invalid_status_rejected():
    with pytest.raises(ValidationError):
        _entry(status="gone")  # type: ignore[arg-type]


def test_refs_optional_pointers():
    r = EntryRefs(payload_ref="art-1", trace_id="tr-1")
    assert r.snapshot_id is None
    assert r.message_id is None
    # refs 作为 JSON 可往返（含中文）
    e = _entry(refs=EntryRefs(payload_ref="art-中文"))
    assert ContextEntry.model_validate(e.model_dump()).refs.payload_ref == "art-中文"


def test_assembly_report_minimal():
    r = AssemblyReport(
        profile=ProfileName.CASE_ITEM,
        policy_version="cp-v1",
        p0_version="abc123abc123",
        per_partition_tokens={"P0": 100, "P1": 200, "P2": 300},
        included=["e1", "e2"],
        demoted_shown=[],
        evicted_in_assembly=[],
        budget_overrides=[],
    )
    assert r.frozen is False
    assert r.per_partition_tokens["P2"] == 300


def test_context_view_construct():
    v = ContextView(
        owner={"scope": "task", "owner_id": "t1"},
        goal="覆盖登录",
        entries=[_entry()],
        totals={"P2": {"active": 1, "tokens": 4}},
    )
    assert v.goal == "覆盖登录"
    assert v.entries[0].entry_id == "plan:p1:v1"
