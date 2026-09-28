"""context.p0：P0 冻结引导段（spec §5，I2 确定性/防漂移）。"""

from __future__ import annotations

import hashlib

import pytest
from langchain_core.messages import SystemMessage

from tester_agent.context.p0 import bootstrap_p0
from tester_agent.context.tokens import estimate_tokens
from tester_agent.prompts.loader import PromptTemplate


class FakeLoader:
    def __init__(self, tpls: dict[str, PromptTemplate]):
        self._tpls = tpls

    def load(self, name: str) -> PromptTemplate:
        return self._tpls[name]


def _tpl(name: str, version: str, content: str) -> PromptTemplate:
    return PromptTemplate(name=name, version=version, content=content, source=f"{name}.md")


def test_bootstrap_concats_in_given_order_as_system_messages():
    loader = FakeLoader({
        "methodology": _tpl("methodology", "2026-09-28.1", "方法论正文"),
        "system.shared": _tpl("system.shared", "2026-09-26.1", "通用规则"),
    })
    messages, version, tokens = bootstrap_p0(
        loader=loader, template_names=["methodology", "system.shared"]
    )
    assert len(messages) == 1
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == "方法论正文\n\n通用规则"
    assert tokens == estimate_tokens("方法论正文\n\n通用规则")


def test_bootstrap_version_is_order_independent_deterministic_12_hex():
    loader = FakeLoader({
        "a": _tpl("a", "v1", "甲"),
        "b": _tpl("b", "v2", "乙"),
    })
    _, v1, _ = bootstrap_p0(loader=loader, template_names=["a", "b"])
    _, v2, _ = bootstrap_p0(loader=loader, template_names=["b", "a"])
    assert v1 == v2
    assert len(v1) == 12
    assert all(c in "0123456789abcdef" for c in v1)


def test_bootstrap_version_changes_with_template_version_and_extra():
    loader = FakeLoader({"a": _tpl("a", "v1", "甲")})
    _, v_old, _ = bootstrap_p0(loader=loader, template_names=["a"])
    loader = FakeLoader({"a": _tpl("a", "v2", "甲改")})
    _, v_new, _ = bootstrap_p0(loader=loader, template_names=["a"])
    assert v_old != v_new

    loader = FakeLoader({"a": _tpl("a", "v1", "甲")})
    _, v_base, _ = bootstrap_p0(loader=loader, template_names=["a"])
    _, v_extra, _ = bootstrap_p0(
        loader=loader, template_names=["a"], extra_static="项目专属约束"
    )
    assert v_base != v_extra
    expect = hashlib.sha256(
        ("a:v1" + "项目专属约束").encode()
    ).hexdigest()[:12]
    assert v_extra == expect


def test_bootstrap_extra_static_appended():
    loader = FakeLoader({"a": _tpl("a", "v1", "甲")})
    messages, _, _ = bootstrap_p0(
        loader=loader, template_names=["a"], extra_static="项目约束X"
    )
    assert messages[0].content.endswith("项目约束X")
    assert "甲" in messages[0].content


def test_bootstrap_empty_names_returns_empty():
    loader = FakeLoader({})
    messages, version, tokens = bootstrap_p0(loader=loader, template_names=[])
    assert messages == []
    assert version == hashlib.sha256(b"").hexdigest()[:12]
    assert tokens == 0


def test_bootstrap_unknown_template_propagates_error():
    loader = FakeLoader({})
    with pytest.raises(KeyError):
        bootstrap_p0(loader=loader, template_names=["missing"])


async def test_bind_p0_version_flows_into_assembly_report():
    from tester_agent.context.store import ContextStore
    from tester_agent.context.models import ProfileName
    from tester_agent.context.assembler import assemble

    loader = FakeLoader({"a": _tpl("a", "v1", "甲")})
    messages, version, _ = bootstrap_p0(loader=loader, template_names=["a"])
    store = ContextStore(
        owner_type="task", owner_id="t1", workspace_id="ws1", journal=None
    )
    store.bind_p0(version)
    r = await assemble(
        store, ProfileName.CHAT, p0_messages=messages, model_window=128_000
    )
    assert r.report.p0_version == version
